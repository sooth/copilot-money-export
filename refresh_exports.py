#!/usr/bin/env python3
"""
Daily Copilot export: transactions CSV + balances JSON.

Writes current files into ~/Library/Application Support/copilot2/ and a
dated copy under archive/YYYY-MM-DD/. Never opens live Copilot databases
(delegates to extract_transactions / fast_financial_extractor, which copy first).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from utils import default_export_dir


TRANSACTIONS_NAME = "copilot_transactions.csv"
BALANCES_NAME = "fast_financial_report.json"


def publish(src: Path, export_dir: Path, day: str) -> Path:
    """Copy src to export_dir/<name> and export_dir/archive/<day>/<name>."""
    if not src.is_file():
        raise FileNotFoundError(f"export source missing: {src}")
    export_dir.mkdir(parents=True, exist_ok=True)
    latest = export_dir / src.name
    if src.resolve() != latest.resolve():
        shutil.copy2(src, latest)
    archive = export_dir / "archive" / day / src.name
    archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(latest, archive)
    return latest


def refresh(export_dir: Path | None = None) -> dict:
    from extract_transactions import extract_transactions
    from fast_financial_extractor import build_report, extract_accounts

    dest = Path(export_dir or default_export_dir())
    dest.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")

    csv_path, tx_stats = extract_transactions(out_path=dest / TRANSACTIONS_NAME)
    publish(csv_path, dest, day)

    accounts = extract_accounts()
    report = build_report(accounts)
    json_path = dest / BALANCES_NAME
    tmp = json_path.with_name(json_path.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
    tmp.replace(json_path)
    publish(json_path, dest, day)

    return {
        "export_dir": str(dest),
        "day": day,
        "transactions": str(dest / TRANSACTIONS_NAME),
        "balances": str(json_path),
        "archive": str(dest / "archive" / day),
        "tx_rows": tx_stats["rows"],
        "tx_elapsed_s": tx_stats["elapsed_s"],
        "net_worth": report["summary"]["net_worth"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh Copilot CSV/JSON exports")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Export directory (default: ~/Library/Application Support/copilot2)",
    )
    args = parser.parse_args()
    print("Copilot Money — daily export")
    print("=" * 70)
    try:
        stats = refresh(args.out_dir)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Export dir:  {stats['export_dir']}")
    print(f"Archive:     {stats['archive']}")
    print(f"Transactions:{stats['tx_rows']:,} rows ({stats['tx_elapsed_s']:.1f}s)")
    print(f"Net worth:   ${stats['net_worth']:,.2f}")
    print("Done.")


if __name__ == "__main__":
    main()
