@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run SETUP.bat first. & pause & exit /b 1)
rem 11-01: the one-server row for claude-code#106413, plus B and E as same-day baselines. Skips row A (~429k tokens).
".venv\Scripts\python.exe" token_probe.py B E F G H
pause
