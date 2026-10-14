#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  command -v python3 >/dev/null || { echo "Python 3 is not installed. Get it from https://www.python.org/downloads/macos/"; read -p "Press Enter to close"; exit 1; }
  echo "First run: setting up Python environment (1-2 minutes)..."
  python3 -m venv .venv && .venv/bin/python -m pip install --upgrade pip -q && .venv/bin/python -m pip install -q -r requirements.txt || { read -p "Setup failed. Press Enter to close"; exit 1; }
fi
read -p "How many days back to test? [365]: " DAYS
.venv/bin/python run_all.py --days "${DAYS:-365}"    # ONE login, downloads data/, runs every lab + backtest, builds master_report.html
open master_report.html 2>/dev/null
read -p "Done. Press Enter to close"
