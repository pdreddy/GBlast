#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  command -v python3 >/dev/null || { echo "Python 3 is not installed. Get it from https://www.python.org/downloads/macos/"; read -p "Press Enter to close"; exit 1; }
  echo "First run: setting up Python environment (1-2 minutes)..."
  python3 -m venv .venv && .venv/bin/python -m pip install --upgrade pip -q && .venv/bin/python -m pip install -q -r requirements.txt || { read -p "Setup failed. Press Enter to close"; exit 1; }
fi
.venv/bin/python fyers_login.py || { read -p "Press Enter to close"; exit 1; }
echo
read -p "How many days back to test? [180]: " DAYS
.venv/bin/python backtest.py --days "${DAYS:-180}" 2>&1 | grep -v " INFO "
open backtest_report.html 2>/dev/null
read -p "Done. Press Enter to close"
