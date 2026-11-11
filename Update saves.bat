@echo off
rem Double-click to fetch new Instagram saves and process them.
cd /d "%~dp0"
python update.py
pause
