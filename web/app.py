#!/usr/bin/env python3
"""
Simple Web UI for Copilot Money Financial Summary.
Displays assets, debt, and net worth in a clean interface.
"""

import json
import os
from datetime import datetime
from flask import Flask, render_template, jsonify

app = Flask(__name__)

def _report_path() -> str:
    """Prefer the daily-export directory; fall back to the repo copy."""
    export_dir = os.environ.get(
        "COPILOT2_EXPORT_DIR",
        os.path.expanduser("~/Library/Application Support/copilot2"),
    )
    exported = os.path.join(export_dir, "fast_financial_report.json")
    if os.path.isfile(exported):
        return exported
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "fast_financial_report.json")


def load_financial_data():
    """Load the latest financial data from JSON."""
    try:
        with open(_report_path(), 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return None


@app.route('/')
def index():
    """Main dashboard page."""
    data = load_financial_data()
    if not data:
        return render_template('error.html', message="No financial data found. Run refresh_exports.py first.")

    summary = data.get('summary', {})
    extraction_date = data.get('extraction_date', '')

    # Parse the extraction date
    if extraction_date:
        try:
            dt = datetime.fromisoformat(extraction_date)
            extraction_date = dt.strftime('%B %d, %Y at %I:%M %p')
        except ValueError:
            pass

    return render_template('dashboard.html',
                          summary=summary,
                          extraction_date=extraction_date,
                          accounts=data.get('accounts', {}),
                          residence={
                              'asset': os.environ.get('COPILOT2_RESIDENCE_ASSET', ''),
                              'mortgage': os.environ.get('COPILOT2_RESIDENCE_MORTGAGE', ''),
                          })


@app.route('/api/summary')
def api_summary():
    """API endpoint for financial summary."""
    data = load_financial_data()
    if not data:
        return jsonify({'error': 'No data found'}), 404
    return jsonify(data.get('summary', {}))


@app.route('/refresh')
def refresh():
    """Refresh data by re-running the extractor."""
    import subprocess
    try:
        result = subprocess.run(
            ['uv', 'run', 'python', 'refresh_exports.py'],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            capture_output=True,
            text=True,
            timeout=300
        )
        if result.returncode == 0:
            return jsonify({'status': 'success', 'message': 'Data refreshed successfully'})
        else:
            return jsonify({'status': 'error', 'message': result.stderr}), 500
    except subprocess.TimeoutExpired:
        return jsonify({'status': 'error', 'message': 'Extraction timed out'}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


if __name__ == '__main__':
    print("Starting Copilot Money Dashboard...")
    print(f"Data source: {_report_path()}")
    app.run(host='127.0.0.1', port=5051)
