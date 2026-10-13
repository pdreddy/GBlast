#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  command -v python3 >/dev/null || { echo "Python 3 is not installed. Get it from https://www.python.org/downloads/macos/"; read -p "Press Enter to close"; exit 1; }
  echo "First run: setting up Python environment (1-2 minutes)..."
  python3 -m venv .venv && .venv/bin/python -m pip install --upgrade pip -q && .venv/bin/python -m pip install -q -r requirements.txt || { read -p "Setup failed. Press Enter to close"; exit 1; }
fi
if ls data/*.csv >/dev/null 2>&1; then
  .venv/bin/python expiry_backtest.py
else
  echo "No data/*.csv found - running the synthetic DEMO instead (put your 1-minute CSVs in data/ for the real test)."
  .venv/bin/python expiry_backtest.py --demo null
fi
open expiry_report.html expiry_report_DEMO_null.html 2>/dev/null
read -p "Done. Press Enter to close"
