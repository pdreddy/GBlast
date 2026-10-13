@echo off
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
)
echo ===== TEST 1: offline logic =====
.venv\Scripts\python test_offline.py
echo.
echo ===== TEST 2: FYERS connection (no orders) =====
.venv\Scripts\python fyers_login.py
.venv\Scripts\python test_connection.py
pause
