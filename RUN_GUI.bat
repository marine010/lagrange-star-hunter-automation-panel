@echo off
setlocal
cd /d "%~dp0"
python -m lagrange_bot.gui --config configs\star_hunter_1920.json
pause
