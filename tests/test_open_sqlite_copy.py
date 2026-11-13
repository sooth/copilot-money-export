"""Safety tests for sqlite snapshot helpers. Never touch the live Copilot DBs."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from utils import assert_safe_to_open, cleanup_temp_dir, open_sqlite_copy


def test_assert_safe_to_open_refuses_group_container_path():
    live = (
        "/Users/someone/Library/Group Containers/"
        "group.com.copilot.production/database/CopilotDB.sqlite"
    )
    with pytest.raises(RuntimeError, match="REFUSING"):
        assert_safe_to_open(live)


def test_assert_safe_to_open_refuses_app_container_path():
    live = (
        "/Users/someone/Library/Containers/com.copilot.production/"
        "Data/Library/Application Support/firestore/main"
    )
    with pytest.raises(RuntimeError, match="REFUSING"):
        assert_safe_to_open(live)


def test_assert_safe_to_open_allows_temp_copy(tmp_path: Path):
    copy = tmp_path / "CopilotDB.sqlite"
    copy.write_bytes(b"")
    assert_safe_to_open(str(copy))  # no live marker → allowed


def test_open_sqlite_copy_round_trips_a_row(tmp_path: Path):
    src = tmp_path / "CopilotDB.sqlite"
    src_conn = sqlite3.connect(src)
    src_conn.execute("CREATE TABLE t (id TEXT)")
    src_conn.execute("INSERT INTO t VALUES ('hello')")
    src_conn.commit()
    src_conn.close()

    conn, temp_dir = open_sqlite_copy(str(src))
    try:
        assert "copilot_sqlite_" in temp_dir
        row = conn.execute("SELECT id FROM t").fetchone()
        assert row["id"] == "hello"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES ('nope')")
    finally:
        conn.close()
        cleanup_temp_dir(temp_dir)
