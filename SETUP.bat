@echo off
cd /d "%~dp0"
echo Setting up Day Desk (one time)...
where py >/dev/null 2>/dev/null && (py -3 -m venv .venv) || (python -m venv .venv)
if not exist ".venv\Scripts\python.exe" (echo Could not create the Python environment. Is Python installed? & pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.
where claude >/dev/null 2>/dev/null && (echo Claude Code found. Good.) || (if exist "%USERPROFILE%\.local\bin\claude.exe" (echo Claude Code found. Good.) else (echo Claude Code is NOT installed yet. Run INSTALL_CLAUDE_CODE.bat next.))
echo.
echo Done. Then double-click RUN.bat
pause
