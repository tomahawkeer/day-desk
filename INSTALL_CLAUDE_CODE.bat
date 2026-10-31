@echo off
REM Installs Claude Code (the "claude" command) using Anthropics official installer,
REM then opens it once so you can sign in with your normal Claude account.
REM Sign in with your Claude.ai subscription account - NOT an API key - so there is no separate bill.
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
echo.
echo Now signing in. Choose your Claude account (subscription) when asked, then type /exit.
if exist "%USERPROFILE%\.local\bin\claude.exe" ("%USERPROFILE%\.local\bin\claude.exe") else (claude)
pause
