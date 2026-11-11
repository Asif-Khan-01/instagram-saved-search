@echo off
rem Double-click to open the search page in your browser. Close this window to stop it.
cd /d "%~dp0"
python -m streamlit run app.py
