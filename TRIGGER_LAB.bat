@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run SETUP.bat first. & pause & exit /b 1)
".venv\Scripts\python.exe" trigger_lab.py %*
pause
