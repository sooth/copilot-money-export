# copilot-money-export

Read-only export of your own [Copilot Money](https://copilot.money) data on macOS. It reads the app's local databases and writes:

- `copilot_transactions.csv`: every transaction with category, parent category, tags and account names
- `fast_financial_report.json`: account balances, assets, debt and net worth

It also ships a small local dashboard for the balances report.

Unofficial. Not affiliated with Copilot Money. The local storage format is undocumented and can change with any app update.

## Safety

Copilot keeps transactions in a SQLite database and balances in a Firestore LevelDB. **Opening the live LevelDB can corrupt it**, and then the app has to be reset. These scripts always copy the database files to a temp directory and read only the copy. Do not change that.

## Requirements

- macOS with the Copilot Money Mac app installed and synced
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- `leveldb` Python bindings (`brew install leveldb` may be needed to build them)

## Usage

```bash
uv sync
uv run python setup.py build_ext --inplace   # optional Cython speedups
uv run python refresh_exports.py
```

Output goes to `~/Library/Application Support/copilot2/` by default, with a dated copy under `archive/YYYY-MM-DD/`. Set `COPILOT2_EXPORT_DIR` or pass `--out-dir` to change it.

To run the two exporters on their own:

```bash
uv run python extract_transactions.py
uv run python fast_financial_extractor.py
```

### Dashboard

```bash
uv run python web/app.py   # http://127.0.0.1:5051
```

To get an "Exclude primary residence" toggle, set account-name substrings for the home and its mortgage:

```bash
COPILOT2_RESIDENCE_ASSET="Single Family" COPILOT2_RESIDENCE_MORTGAGE="Mortgage" uv run python web/app.py
```

### Daily export (launchd)

Edit the paths in `launchd/com.example.copilot-export.plist`, then:

```bash
cp launchd/com.example.copilot-export.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.copilot-export.plist
```

## Tests

```bash
uv run pytest
```

## License

MIT
