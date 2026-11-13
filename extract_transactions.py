#!/usr/bin/env python3
"""
Export Copilot Money transactions to CSV.

Reads the GRDB SQLite ledger (after copying it to temp) and resolves
category/tag/account/recurring names from widget JSON plus a read-only
scan of Firestore SST files. Never opens the live SQLite or the live
LevelDB — opening Copilot's LevelDB with our build corrupts it.
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Iterable, Optional

from utils import (
    cleanup_temp_dir,
    default_export_dir,
    is_copilot_running,
    open_sqlite_copy,
)


SQLITE_PATH = Path.home() / (
    "Library/Group Containers/group.com.copilot.production/"
    "database/CopilotDB.sqlite"
)
WIDGET_DIR = Path.home() / (
    "Library/Group Containers/group.com.copilot.production/widget-data"
)
FIRESTORE_BASE = Path.home() / (
    "Library/Containers/com.copilot.production/Data/Library/"
    "Application Support/firestore"
)

CSV_COLUMNS = [
    "date",
    "name",
    "amount",
    "status",
    "category",
    "parent_category",
    "type",
    "account",
    "account_mask",
    "notes",
    "recurring",
    "tags",
    "id",
    "original_name",
    "account_id",
    "category_id",
    "user_reviewed",
]

_TX_SQL = """
SELECT
    id, date, name, name_override, original_name, amount, pending, type,
    category_id, account_id, user_note, recurring, recurring_id, tag_ids,
    user_reviewed
FROM Transactions
ORDER BY date DESC, id DESC
"""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def find_firestore_sst_dir() -> Path:
    """Directory of live Firestore .ldb files (read as bytes, never opened)."""
    import glob
    matches = [
        Path(p).parent
        for p in glob.glob(str(FIRESTORE_BASE / "**/main/CURRENT"), recursive=True)
        if "corrupt-bak" not in p
    ]
    if not matches:
        raise FileNotFoundError(f"No Copilot Firestore database under {FIRESTORE_BASE}")
    return max(matches, key=lambda d: (d / "CURRENT").stat().st_mtime)


# ---------------------------------------------------------------------------
# Widget JSON lookups
# ---------------------------------------------------------------------------

def load_widget_categories(widget_dir: Path) -> dict[str, str]:
    path = widget_dir / "widgets-category-categories.json"
    if not path.is_file():
        return {}
    rows = json.loads(path.read_text())
    return {row["id"]: row["name"] for row in rows if row.get("id") and row.get("name")}


def load_widget_accounts(widget_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in widget_dir.glob("widgets-account-account_*.json"):
        try:
            row = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        acct_id = row.get("id")
        if acct_id:
            out[acct_id] = row
    return out


# ---------------------------------------------------------------------------
# SST (LevelDB table file) read-only name resolution
# ---------------------------------------------------------------------------

def _ascii_strings(buf: bytes, min_len: int = 2, max_len: int = 120) -> list[str]:
    """Extract printable ASCII runs. Skip huge blobs (account logos are base64)."""
    out: list[str] = []
    cur = bytearray()
    for b in buf:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if min_len <= len(cur) <= max_len:
                out.append(cur.decode("ascii"))
            cur = bytearray()
    if min_len <= len(cur) <= max_len:
        out.append(cur.decode("ascii"))
    return out


def _clean_name(value: str) -> str:
    return value.rstrip('"').strip()


def _field_after(strings: list[str], field: str) -> Optional[str]:
    for i, s in enumerate(strings):
        if s == field and i + 1 < len(strings):
            cleaned = _clean_name(strings[i + 1])
            if cleaned:
                return cleaned
    return None


def _window_after_marker(data: bytes, marker: bytes, window: int = 8192) -> Optional[list[str]]:
    """Return ASCII strings in the first non-subcollection hit of `marker`."""
    start = 0
    while True:
        idx = data.find(marker, start)
        if idx < 0:
            return None
        end = idx + len(marker)
        # `/accounts/{id}/transactions/...` is a subcollection — skip it.
        if end < len(data) and data[end] == ord("/"):
            start = end
            continue
        return _ascii_strings(data[idx:idx + window])


def parse_sst_document(data: bytes, collection: str, doc_id: str) -> dict[str, str]:
    """Pull `name` (and `parent_category_id` for categories) after `/{collection}/{id}`."""
    marker = f"/{collection}/{doc_id}".encode("ascii")
    strings = _window_after_marker(data, marker)
    if not strings:
        return {}
    out: dict[str, str] = {}
    name = _field_after(strings, "name") or _field_after(strings, "official_name")
    if name:
        out["name"] = name
    if collection == "categories":
        parent = _field_after(strings, "parent_category_id")
        if parent:
            out["parent_category_id"] = parent
    if collection == "accounts":
        mask = _field_after(strings, "mask")
        if mask:
            out["mask"] = mask
    return out


def resolve_names_from_sst(
    sst_dir: Path,
    wanted: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Read .ldb files as bytes. `wanted` maps id -> collection name.

    Collection is one of: tags, recurring, accounts, categories.
    Returns id -> {name, parent_category_id?}.
    """
    remaining = dict(wanted)
    found: dict[str, dict[str, str]] = {}
    if not remaining:
        return found

    ldb_files = sorted(sst_dir.glob("*.ldb"))
    for path in ldb_files:
        if not remaining:
            break
        try:
            data = path.read_bytes()
        except OSError:
            continue
        # Keep walking this file until it yields no more of the remaining ids.
        # Parent category ids are often in the same SST block as the child.
        made_progress = True
        while remaining and made_progress:
            made_progress = False
            for doc_id, collection in list(remaining.items()):
                parsed = parse_sst_document(data, collection, doc_id)
                if not parsed:
                    continue
                found[doc_id] = parsed
                remaining.pop(doc_id, None)
                made_progress = True
                parent = parsed.get("parent_category_id")
                if parent and parent not in found and parent not in remaining:
                    remaining[parent] = "categories"
    return found


def self_check_categories(
    widget_names: dict[str, str],
    sst_docs: dict[str, dict[str, str]],
    sample_ids: Iterable[str],
) -> list[str]:
    """Return mismatch descriptions. Widget names win at runtime."""
    mismatches = []
    for cid in sample_ids:
        widget = widget_names.get(cid)
        sst_name = sst_docs.get(cid, {}).get("name")
        if widget and sst_name and widget != sst_name:
            mismatches.append(f"{cid}: widget={widget!r} sst={sst_name!r}")
    return mismatches


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------

def _parse_tag_ids(raw) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        val = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(val, list):
        return []
    return [str(x) for x in val if x]


def _fmt_date(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return text[:10]


def _truthy(value) -> bool:
    return bool(value) and value not in (0, "0", "false", "False")


def build_csv_row(
    row,
    category_names: dict[str, str],
    parent_by_category: dict[str, str],
    accounts: dict[str, dict],
    tag_names: dict[str, str],
    recurring_names: dict[str, str],
) -> dict[str, str]:
    category_id = row["category_id"] or ""
    account_id = row["account_id"] or ""
    acct = accounts.get(account_id) or {}
    display_name = (row["name_override"] or row["name"] or "") or ""
    tag_ids = _parse_tag_ids(row["tag_ids"])
    resolved_tags = [tag_names.get(tid, tid) for tid in tag_ids]
    recurring_id = row["recurring_id"] or ""
    recurring_label = ""
    if _truthy(row["recurring"]):
        recurring_label = recurring_names.get(recurring_id, recurring_id)
    parent_id = parent_by_category.get(category_id, "")
    parent_name = category_names.get(parent_id, "") if parent_id else ""
    amount = row["amount"]
    amount_str = "" if amount is None else f"{float(amount):.2f}"
    return {
        "date": _fmt_date(row["date"]),
        "name": display_name,
        "amount": amount_str,
        "status": "Pending" if _truthy(row["pending"]) else "Posted",
        "category": category_names.get(category_id, ""),
        "parent_category": parent_name,
        "type": row["type"] or "",
        "account": acct.get("name") or "",
        "account_mask": acct.get("mask") or "",
        "notes": row["user_note"] or "",
        "recurring": recurring_label,
        "tags": ";".join(resolved_tags),
        "id": row["id"] or "",
        "original_name": row["original_name"] or "",
        "account_id": account_id,
        "category_id": category_id,
        "user_reviewed": "true" if _truthy(row["user_reviewed"]) else "false",
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

def extract_transactions(
    sqlite_path: Path = SQLITE_PATH,
    widget_dir: Path = WIDGET_DIR,
    sst_dir: Optional[Path] = None,
    out_path: Optional[Path] = None,
) -> tuple[Path, dict]:
    """Copy sqlite, resolve names, write CSV. Returns (csv_path, stats)."""
    t0 = time.time()
    if out_path is None:
        out_path = Path(default_export_dir()) / "copilot_transactions.csv"

    category_names = load_widget_categories(widget_dir)
    accounts = load_widget_accounts(widget_dir)

    conn, temp_dir = open_sqlite_copy(str(sqlite_path))
    try:
        tx_rows = conn.execute(_TX_SQL).fetchall()
    finally:
        conn.close()
        cleanup_temp_dir(temp_dir)

    wanted: dict[str, str] = {}
    for row in tx_rows:
        for tid in _parse_tag_ids(row["tag_ids"]):
            wanted.setdefault(tid, "tags")
        if _truthy(row["recurring"]) and row["recurring_id"]:
            wanted.setdefault(row["recurring_id"], "recurring")
        acct_id = row["account_id"]
        if acct_id and acct_id not in accounts:
            wanted.setdefault(acct_id, "accounts")
        cid = row["category_id"]
        if cid:
            wanted.setdefault(cid, "categories")

    sst_docs: dict[str, dict[str, str]] = {}
    if wanted:
        if sst_dir is None:
            sst_dir = find_firestore_sst_dir()
        sst_docs = resolve_names_from_sst(sst_dir, wanted)

    sample_cids = [cid for cid, kind in wanted.items() if kind == "categories"][:8]
    mismatches = self_check_categories(category_names, sst_docs, sample_cids)

    # Widget names win for categories (setdefault). SST fills tags, recurrings,
    # missing accounts, parent category names, and any category gaps.
    tag_names: dict[str, str] = {}
    recurring_names: dict[str, str] = {}
    parent_by_category: dict[str, str] = {}
    for doc_id, parsed in sst_docs.items():
        name = parsed.get("name")
        kind = wanted.get(doc_id)
        if parsed.get("parent_category_id"):
            parent_by_category[doc_id] = parsed["parent_category_id"]
        if not name:
            continue
        if kind == "tags":
            tag_names[doc_id] = name
        elif kind == "recurring":
            recurring_names[doc_id] = name
        elif kind == "accounts":
            accounts.setdefault(doc_id, {"id": doc_id, "mask": parsed.get("mask") or ""})
            accounts[doc_id]["name"] = name
            if parsed.get("mask") and not accounts[doc_id].get("mask"):
                accounts[doc_id]["mask"] = parsed["mask"]
        else:
            category_names.setdefault(doc_id, name)

    csv_rows = [
        build_csv_row(
            row,
            category_names,
            parent_by_category,
            accounts,
            tag_names,
            recurring_names,
        )
        for row in tx_rows
    ]
    write_csv(out_path, csv_rows)

    tagged = sum(1 for r in csv_rows if r["tags"])
    named_accounts = sum(1 for r in csv_rows if r["account"])
    named_categories = sum(1 for r in csv_rows if r["category"])
    dates = [r["date"] for r in csv_rows if r["date"]]
    stats = {
        "rows": len(csv_rows),
        "tagged_rows": tagged,
        "named_accounts": named_accounts,
        "named_categories": named_categories,
        "tag_names": len(tag_names),
        "recurring_names": len(recurring_names),
        "category_mismatches": mismatches,
        "date_min": min(dates) if dates else "",
        "date_max": max(dates) if dates else "",
        "elapsed_s": time.time() - t0,
        "out_path": str(out_path),
    }
    return out_path, stats


def main() -> None:
    print("Copilot Money — Transaction CSV extractor")
    print("=" * 70)
    if is_copilot_running():
        print("Copilot is running — snapshot may be a split-second behind (live DB never opened).")
    else:
        print("Copying SQLite to temp and reading the copy (live DB untouched).")

    if not SQLITE_PATH.is_file():
        raise SystemExit(f"No Copilot SQLite ledger at {SQLITE_PATH}")

    out_path, stats = extract_transactions()
    print(f"Rows:              {stats['rows']:,}")
    print(f"Date range:        {stats['date_min']} → {stats['date_max']}")
    print(f"With category:     {stats['named_categories']:,}")
    print(f"With account name: {stats['named_accounts']:,}")
    print(f"With tags:         {stats['tagged_rows']:,}  ({stats['tag_names']} tag names)")
    print(f"Recurring names:   {stats['recurring_names']}")
    if stats["category_mismatches"]:
        print("WARNING: SST category names disagreed with widget JSON (widget wins):")
        for line in stats["category_mismatches"]:
            print(f"  {line}")
    print(f"Elapsed:           {stats['elapsed_s']:.1f}s")
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    main()
