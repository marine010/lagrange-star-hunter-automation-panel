@echo off
setlocal
cd /d "%~dp0"
python --version >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Please install Python 3.12 or newer first.
  pause
  exit /b 1
)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m lagrange_bot.gui --config configs\star_hunter_1920.json
pause
