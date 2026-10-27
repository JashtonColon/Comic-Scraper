@echo off
REM Opens Comic Scraper from source (needs Python + requirements.txt installed)
cd /d "%~dp0..\app"
start "" pythonw gui.py
