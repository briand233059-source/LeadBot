@echo off
cd /d "%~dp0"
echo.
echo  Starting Lead Extractor Bot...
echo  Press Ctrl+C to stop.
echo.
pip install python-telegram-bot --quiet
python lead_bot_3.py
pause
