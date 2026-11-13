#!/usr/bin/env python3
"""
Fast financial extractor for Copilot Money.

Decodes the Firestore account documents stored in Copilot's local LevelDB and
reads the real, named fields (current_balance, type, subtype, ...) instead of
guessing at IEEE-754 doubles in the raw bytes. This is accurate where the old
heuristic scan was not: the heuristic grabbed a credit card's `limit` instead of
its `current_balance`, mis-summed orphaned duplicate accounts, and misclassified
IRAs as debt. Validated against the live app to the cent.

Output schema (consumed by web/app.py + dashboard.html) is unchanged:
  { extraction_date, summary{...}, accounts{investments,bank_accounts,
    real_estate,other_assets,debts}, all_accounts[...] }
"""
import json
import glob
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils import (
    open_db_copy,
    cleanup_temp_dir,
    default_export_dir,
    parse_key,
    is_copilot_running,
)
from cython_utils import filter_keys  # build: uv run python setup.py build_ext --inplace


# =============================================================================
# Self-healing database discovery
# =============================================================================

def find_firestore_db() -> str:
    """Locate Copilot's active Firestore LevelDB, surviving directory renames.

    Copilot changes the firestore subdirectory name over time (project id,
    a `.copilot-production-firestore` suffix, etc.), so we never hardcode it:
    glob for any `*/main/` holding a CURRENT file and pick the freshest.
    """
    base = Path(
        "~/Library/Containers/com.copilot.production/Data/Library/"
        "Application Support/firestore"
    ).expanduser()

    candidates = [Path(p).parent for p in glob.glob(str(base / "**/main/CURRENT"), recursive=True)]
    if not candidates:
        raise FileNotFoundError(
            f"No Copilot Firestore database found under {base}. "
            "Is Copilot Money installed and has it synced at least once?"
        )
    # Freshest CURRENT = the live database.
    best = max(candidates, key=lambda d: (d / "CURRENT").stat().st_mtime)
    return str(best) + "/"


# Resolved lazily so importing this module never crashes when the cache is
# absent (e.g. just after clearing it — the app rebuilds it on next launch).
try:
    FIRESTORE_PATH: Optional[str] = find_firestore_db()
except FileNotFoundError:
    FIRESTORE_PATH = None


# =============================================================================
# Minimal protobuf wire decoder (Firestore Document / Value)
# =============================================================================
# Firestore Value field numbers: bool=1, int64=2 (varint), double=3 (fixed64),
# map=6, array=9, timestamp=10, string=17, bytes=18, reference=5.
# Document: name=1, fields(map<string,Value>)=2 (repeated {key=1, value=2}),
# create_time=3, update_time=4.

def _varint(d: bytes, p: int) -> tuple[int, int]:
    r = s = 0
    while p < len(d):
        b = d[p]
        r |= (b & 0x7F) << s
        p += 1
        if not (b & 0x80):
            return r, p
        s += 7
    return r, p


def _fields(d: bytes) -> list[tuple[int, int, object]]:
    """Split one protobuf message into [(field_number, wire_type, raw)]."""
    out: list[tuple[int, int, object]] = []
    p = 0
    n = len(d)
    while p < n:
        tag, p = _varint(d, p)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, p = _varint(d, p)
            out.append((fn, 0, v))
        elif wt == 1:
            out.append((fn, 1, d[p:p + 8]))
            p += 8
        elif wt == 2:
            ln, p = _varint(d, p)
            out.append((fn, 2, d[p:p + ln]))
            p += ln
        elif wt == 5:
            out.append((fn, 5, d[p:p + 4]))
            p += 4
        else:  # unknown / group — bail on this message
            break
    return out


def _value(raw: bytes):
    """Decode a Firestore Value message into a Python scalar (or None)."""
    for fn, wt, v in _fields(raw):
        if fn == 3 and wt == 1:                 # double_value
            return struct.unpack('<d', v)[0]
        if fn == 2 and wt == 0:                 # integer_value
            return v
        if fn == 1 and wt == 0:                 # boolean_value
            return bool(v)
        if fn == 17 and wt == 2:                # string_value
            return v.decode('utf-8', 'replace')
        if fn == 10 and wt == 2:                # timestamp_value -> date string
            secs = next((tv for tfn, twt, tv in _fields(v) if tfn == 1 and twt == 0), None)
            if secs:
                return datetime.fromtimestamp(secs, tz=timezone.utc).strftime('%Y-%m-%d')
            return None
    return None


def account_fields(value: bytes) -> dict:
    """Return {field_name: python_value} for a Firestore account document."""
    doc = next((v for fn, wt, v in _fields(value) if fn == 2 and wt == 2), None)
    if doc is None:
        return {}
    out: dict = {}
    for fn, wt, raw in _fields(doc):
        if fn != 2 or wt != 2:                  # only the repeated fields-map entries
            continue
        key = val = None
        for efn, ewt, eraw in _fields(raw):
            if efn == 1 and ewt == 2:
                key = eraw.decode('utf-8', 'replace')
            elif efn == 2 and ewt == 2:
                val = _value(eraw)
        if key is not None:
            out[key] = val
    return out


# =============================================================================
# Extraction
# =============================================================================

# An account is excluded if any of these flags are set: it's an orphaned record
# from a re-link, a closed card, or hidden by the user. dashboard_active is NOT
# used — most live accounts legitimately have dashboard_active=False.
_DEAD_FLAGS = ('provider_deleted', 'user_closed', 'user_hidden', 'user_deleted')

# Firestore `type` -> dashboard bucket.
_DEBT_TYPES = {'loan', 'credit'}


def _bucket(acct_type: str, subtype: str) -> str:
    if acct_type in ('real-estate', 'real_estate') or subtype in ('real estate', 'real-estate', 'real'):
        return 'real_estate'
    if acct_type == 'investment':
        return 'investments'
    if acct_type == 'depository':
        return 'bank_accounts'
    if acct_type in _DEBT_TYPES:
        return 'debts'
    return 'other_assets'


def extract_accounts() -> list[dict]:
    """Decode every live Copilot account from the local DB.

    The live database is NEVER opened. Opening Copilot's LevelDB with our
    (foreign) leveldb build is read-WRITE — on open it replays the WAL, rewrites
    MANIFEST/CURRENT, and deletes log files, leaving on-disk state Copilot's own
    reader rejects, so the app stops launching. We copy the DB to temp and open
    only the throwaway copy; the live files are touched only by a filesystem read.
    """
    global FIRESTORE_PATH
    if FIRESTORE_PATH is None:                     # cache was absent at import; retry now
        try:
            FIRESTORE_PATH = find_firestore_db()
        except FileNotFoundError:
            raise SystemExit(
                "No Copilot database found. Launch Copilot Money, let it finish "
                "syncing, then run this again."
            )

    if is_copilot_running():
        print("⚠️  Copilot is running — the copy may be momentarily inconsistent "
              "(the live DB is still never opened).")
    else:
        print("📁 Copying DB to temp and reading the copy (live DB untouched).")

    t0 = time.time()
    db, temp_dir = open_db_copy(FIRESTORE_PATH)   # copies, then opens ONLY the copy
    print(f"   Ready in {time.time() - t0:.1f}s")
    try:
        accts: list[dict] = []
        seen_ids: set[str] = set()
        total = 0
        t0 = time.time()
        print("   Scanning keys (compiled filter)...", flush=True)
        # Keys-only scan in C; values are read only for the few matching keys.
        # Pulling every value (77M entries) was most of the old cost.
        keys = filter_keys(db.RangeIter(include_value=False), b'accounts')
        print(f"   {len(keys):,} candidate keys in {time.time() - t0:.1f}s", flush=True)
        # Pure-Python path (slow, kept for reference):
        # for key, value in db.RangeIter():
        #     total += 1
        #     if b'accounts' not in key:
        #         continue
        for key in keys:
            total += 1
            parts = parse_key(key)
            if 'accounts' not in parts:
                continue
            ai = parts.index('accounts')
            if ai + 2 != len(parts):            # only the account doc itself (no sub-collection)
                continue

            f = account_fields(bytes(db.Get(key)))
            if not f:
                continue
            if any(f.get(flag) for flag in _DEAD_FLAGS):
                continue

            acct_id = parts[-1]
            if acct_id in seen_ids:             # same account doc seen twice — keep first
                continue

            bal = f.get('current_balance')
            if bal is None:
                bal = f.get('available_balance')
            if bal is None:
                continue
            seen_ids.add(acct_id)

            acct_type = f.get('type') or 'unknown'
            subtype = f.get('subtype') or 'unknown'
            balance = float(bal)
            accts.append({
                'id': acct_id,
                'name': f.get('official_name') or f.get('name') or f.get('nickname') or 'Unknown',
                'mask': f.get('mask'),
                'institution': f.get('institution_name'),
                'type': acct_type,
                'subtype': subtype,
                'balance': balance,
                'as_of': f.get('latest_balance_update'),
                'is_debt': acct_type in _DEBT_TYPES,
                'bucket': _bucket(acct_type, subtype),
            })

        print(f"   Checked {total:,} candidate keys, found {len(accts)} live accounts "
              f"in {time.time() - t0:.1f}s")
        return accts
    finally:
        if temp_dir:
            cleanup_temp_dir(temp_dir)


# =============================================================================
# Reporting
# =============================================================================

def build_report(accts: list[dict]) -> dict:
    buckets: dict[str, list[dict]] = {
        'investments': [], 'bank_accounts': [], 'real_estate': [],
        'other_assets': [], 'debts': [],
    }
    for a in accts:
        buckets[a['bucket']].append(a)
    for v in buckets.values():
        v.sort(key=lambda x: abs(x['balance']), reverse=True)

    total_inv = sum(a['balance'] for a in buckets['investments'])
    total_bank = sum(a['balance'] for a in buckets['bank_accounts'])
    total_re = sum(a['balance'] for a in buckets['real_estate'])
    total_other = sum(a['balance'] for a in buckets['other_assets'])
    total_debt = sum(abs(a['balance']) for a in buckets['debts'])
    total_assets = total_inv + total_bank + total_re + total_other
    net_worth = total_assets - total_debt

    return {
        'extraction_date': datetime.now().isoformat(),
        'database': FIRESTORE_PATH,
        'summary': {
            'total_investments': round(total_inv, 2),
            'total_bank_accounts': round(total_bank, 2),
            'total_real_estate': round(total_re, 2),
            'total_other_assets': round(total_other, 2),
            'total_assets': round(total_assets, 2),
            'total_debt': round(total_debt, 2),
            'net_worth': round(net_worth, 2),
        },
        'accounts': buckets,
        'all_accounts': sorted(accts, key=lambda x: abs(x['balance']), reverse=True),
    }


def print_report(report: dict) -> None:
    s = report['summary']
    labels = [
        ('investments', '📈 INVESTMENTS'),
        ('bank_accounts', '🏦 BANK ACCOUNTS'),
        ('real_estate', '🏠 REAL ESTATE'),
        ('other_assets', '🪙 OTHER ASSETS'),
        ('debts', '💳 DEBTS / LIABILITIES'),
    ]
    for bucket, title in labels:
        rows = report['accounts'][bucket]
        if not rows:
            continue
        print(f"\n{title}\n" + "-" * 60)
        for a in rows:
            inst = f" ({a['institution']})" if a.get('institution') else ""
            mask = f" …{a['mask']}" if a.get('mask') else ""
            print(f"  {(a['name'] + mask)[:34]:34} ${a['balance']:>13,.2f}{inst}")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Total Investments:   ${s['total_investments']:>14,.2f}")
    print(f"  Total Bank Accounts: ${s['total_bank_accounts']:>14,.2f}")
    print(f"  Total Real Estate:   ${s['total_real_estate']:>14,.2f}")
    print(f"  Total Other Assets:  ${s['total_other_assets']:>14,.2f}")
    print(f"  {'─' * 38}")
    print(f"  TOTAL ASSETS:        ${s['total_assets']:>14,.2f}")
    print(f"  Total Debt:          ${s['total_debt']:>14,.2f}")
    print(f"  {'─' * 38}")
    print(f"  NET WORTH:           ${s['net_worth']:>14,.2f}")


def main() -> None:
    print("🚀 Copilot Money — Fast Financial Extractor")
    print("=" * 70)
    accts = extract_accounts()
    report = build_report(accts)
    print_report(report)

    out_path = Path(default_export_dir()) / "fast_financial_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=2))
    tmp.replace(out_path)
    print(f"\n💾 Saved to {out_path}")


if __name__ == '__main__':
    main()
