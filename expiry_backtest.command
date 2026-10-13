#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  command -v python3 >/dev/null || { echo "Python 3 is not installed. Get it from https://www.python.org/downloads/macos/"; read -p "Press Enter to close"; exit 1; }
  echo "First run: setting up Python environment (1-2 minutes)..."
  python3 -m venv .venv && .venv/bin/python -m pip install --upgrade pip -q && .venv/bin/python -m pip install -q -r requirements.txt || { read -p "Setup failed. Press Enter to close"; exit 1; }
fi
.venv/bin/python expiry_backtest.py    # logs in to FYERS (one auth_code paste), downloads data/, backtests
open expiry_report.html 2>/dev/null
read -p "Done. Press Enter to close"
