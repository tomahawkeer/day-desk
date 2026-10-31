@echo off
cd /d "%~dp0"
title Day Desk (paper trading)
if not exist ".venv\Scripts\python.exe" (echo Run SETUP.bat first. & pause & exit /b 1)
".venv\Scripts\python.exe" day_desk.py
pause
