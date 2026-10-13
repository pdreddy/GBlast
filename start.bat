@echo off
cd /d "%~dp0"
if not exist .venv (
    echo First run: creating Python environment...
    python -m venv .venv || (echo Python 3.9+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python fyers_login.py || (pause & exit /b 1)
.venv\Scripts\python gamma_blast.py
pause
