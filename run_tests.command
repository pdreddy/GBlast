#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  command -v python3 >/dev/null || { echo "Python 3 is not installed. Get it from https://www.python.org/downloads/macos/"; read -p "Press Enter to close"; exit 1; }
  echo "First run: setting up Python environment (1-2 minutes)..."
  python3 -m venv .venv && .venv/bin/python -m pip install --upgrade pip -q && .venv/bin/python -m pip install -q -r requirements.txt || { read -p "Setup failed. Press Enter to close"; exit 1; }
fi
echo "===== TEST 1: offline logic ====="
.venv/bin/python test_offline.py
echo
echo "===== TEST 2: FYERS connection (no orders) ====="
.venv/bin/python fyers_login.py && .venv/bin/python test_connection.py
read -p "Done. Press Enter to close"
