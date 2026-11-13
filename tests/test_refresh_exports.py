"""Tests for export publishing. Does not touch live Copilot databases."""
from __future__ import annotations

from pathlib import Path

from refresh_exports import publish
from utils import default_export_dir


def test_default_export_dir_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("COPILOT2_EXPORT_DIR", str(tmp_path / "out"))
    assert default_export_dir() == str(tmp_path / "out")


def test_publish_writes_latest_and_dated_archive(tmp_path: Path):
    src = tmp_path / "src" / "copilot_transactions.csv"
    src.parent.mkdir()
    src.write_text("date,name\n2026-09-06,Coffee Shop\n", encoding="utf-8")
    dest = tmp_path / "exports"
    latest = publish(src, dest, "2026-09-06")
    assert latest == dest / "copilot_transactions.csv"
    assert latest.read_text(encoding="utf-8").startswith("date,name")
    archived = dest / "archive" / "2026-09-06" / "copilot_transactions.csv"
    assert archived.is_file()
    assert archived.read_text(encoding="utf-8") == latest.read_text(encoding="utf-8")


def test_publish_when_src_is_already_latest(tmp_path: Path):
    dest = tmp_path / "exports"
    dest.mkdir()
    latest = dest / "fast_financial_report.json"
    latest.write_text('{"ok": true}', encoding="utf-8")
    published = publish(latest, dest, "2026-09-06")
    assert published == latest
    assert (dest / "archive" / "2026-09-06" / "fast_financial_report.json").read_text(
        encoding="utf-8"
    ) == '{"ok": true}'
