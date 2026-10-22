@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  python3 -m venv .venv || exit /b 1
)
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt || exit /b 1
.venv\Scripts\python.exe run.py
