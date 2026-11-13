"""Fixture tests for the transaction CSV extractor. No live Copilot files."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from extract_transactions import (
    CSV_COLUMNS,
    build_csv_row,
    extract_transactions,
    parse_sst_document,
    resolve_names_from_sst,
)


def test_parse_sst_tag_name_after_path_not_before():
    # Name belonging to the previous doc must not win.
    blob = b"\x00".join(
        [
            b"name",
            b"WrongPreviousTag",
            b"/tags/TAGID1",
            b"_migration_backfill",
            b"hex_color",
            b"#FF00FF",
            b"name",
            b"Vacation2025",
            b"other",
        ]
    )
    parsed = parse_sst_document(blob, "tags", "TAGID1")
    assert parsed["name"] == "Vacation2025"


def test_parse_sst_account_name_after_logo_blob():
    logo = b"iVBORw0KGgo" + b"A" * 2000
    blob = (
        b"/accounts/AID1\x00logo\x00" + logo + b"\x00name\x00Checking\x00mask\x001234"
    )
    parsed = parse_sst_document(blob, "accounts", "AID1")
    assert parsed["name"] == "Checking"


def test_parse_sst_skips_account_subcollection():
    blob = (
        b"/accounts/AID1/transactions/TX1\x00name\x00WRONG\x00"
        b"/accounts/AID1\x00name\x00Checking"
    )
    parsed = parse_sst_document(blob, "accounts", "AID1")
    assert parsed["name"] == "Checking"


def test_parse_sst_category_parent():
    blob = b"\x00".join(
        [
            b"/categories/CAT1",
            b"name",
            b"Groceries",
            b"order",
            b"parent_category_id",
            b"PARENT1",
        ]
    )
    parsed = parse_sst_document(blob, "categories", "CAT1")
    assert parsed["name"] == "Groceries"
    assert parsed["parent_category_id"] == "PARENT1"


def test_build_csv_row_name_override_pending_tags_date():
    row = {
        "id": "tx1",
        "date": "2026-09-03 04:00:00.000",
        "name": "CORNER COFFEE",
        "name_override": "Corner Coffee",
        "original_name": "CORNER COFFEE 123",
        "amount": 6.96,
        "pending": 1,
        "type": "regular",
        "category_id": "CAT1",
        "account_id": "ACC1",
        "user_note": "latte",
        "recurring": 0,
        "recurring_id": None,
        "tag_ids": json.dumps(["TAG1", "TAG2"]),
        "user_reviewed": 0,
    }
    out = build_csv_row(
        row,
        category_names={"CAT1": "Groceries", "PARENT1": "Food"},
        parent_by_category={"CAT1": "PARENT1"},
        accounts={"ACC1": {"name": "Example Credit Card", "mask": "0000"}},
        tag_names={"TAG1": "Vacation2025", "TAG2": "Roadtrip2026"},
        recurring_names={},
    )
    assert out["date"] == "2026-09-03"
    assert out["name"] == "Corner Coffee"
    assert out["amount"] == "6.96"
    assert out["status"] == "Pending"
    assert out["category"] == "Groceries"
    assert out["parent_category"] == "Food"
    assert out["account"] == "Example Credit Card"
    assert out["account_mask"] == "0000"
    assert out["tags"] == "Vacation2025;Roadtrip2026"
    assert out["notes"] == "latte"
    assert out["user_reviewed"] == "false"
    assert list(out.keys()) == CSV_COLUMNS


def _make_ledger(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE Transactions (
            id TEXT PRIMARY KEY,
            date TEXT,
            name TEXT,
            name_override TEXT,
            original_name TEXT,
            amount DOUBLE,
            pending BOOLEAN,
            type TEXT,
            category_id TEXT,
            account_id TEXT,
            user_note TEXT,
            recurring BOOLEAN,
            recurring_id TEXT,
            tag_ids TEXT,
            user_reviewed BOOLEAN
        )
        """
    )
    conn.execute(
        """
        INSERT INTO Transactions VALUES (
            'tx1', '2026-09-02 04:00:00.000', 'ACME GROCERY SPRINGFIELD', NULL,
            'ACME GROCERY', 110.82, 0, 'regular', 'CAT1', 'ACC1', NULL,
            0, NULL, ?, 1
        )
        """,
        (json.dumps(["TAG1"]),),
    )
    conn.commit()
    conn.close()


def test_extract_transactions_writes_csv(tmp_path: Path):
    sqlite_path = tmp_path / "CopilotDB.sqlite"
    _make_ledger(sqlite_path)

    widget_dir = tmp_path / "widget-data"
    widget_dir.mkdir()
    (widget_dir / "widgets-category-categories.json").write_text(
        json.dumps([{"id": "CAT1", "name": "Groceries", "excluded": False}])
    )
    (widget_dir / "widgets-account-account_ACC1.json").write_text(
        json.dumps({"id": "ACC1", "name": "Example Credit Card", "mask": "0000"})
    )

    sst_dir = tmp_path / "sst"
    sst_dir.mkdir()
    blob = b"\x00".join(
        [
            b"/tags/TAG1",
            b"name",
            b"Vacation2025",
            b"/categories/CAT1",
            b"name",
            b"Groceries",
            b"parent_category_id",
            b"PARENT1",
            b"/categories/PARENT1",
            b"name",
            b"Food & Drink",
        ]
    )
    (sst_dir / "000001.ldb").write_bytes(blob)

    out_path = tmp_path / "out.csv"
    written, stats = extract_transactions(
        sqlite_path=sqlite_path,
        widget_dir=widget_dir,
        sst_dir=sst_dir,
        out_path=out_path,
    )
    assert written == out_path
    assert stats["rows"] == 1

    with out_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["name"] == "ACME GROCERY SPRINGFIELD"
    assert rows[0]["category"] == "Groceries"
    assert rows[0]["parent_category"] == "Food & Drink"
    assert rows[0]["tags"] == "Vacation2025"
    assert rows[0]["status"] == "Posted"
    assert rows[0]["account"] == "Example Credit Card"
    assert rows[0]["date"] == "2026-09-02"


def test_resolve_names_stops_after_all_found(tmp_path: Path):
    sst_dir = tmp_path / "sst"
    sst_dir.mkdir()
    (sst_dir / "000001.ldb").write_bytes(
        b"/tags/TAG1\x00name\x00Alpha"
    )
    (sst_dir / "000002.ldb").write_bytes(
        b"/tags/TAG2\x00name\x00Beta"
    )
    found = resolve_names_from_sst(sst_dir, {"TAG1": "tags"})
    assert found["TAG1"]["name"] == "Alpha"
    assert "TAG2" not in found
