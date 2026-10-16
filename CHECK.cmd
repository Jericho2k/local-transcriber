@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app.py" --check
set "TRANSCRIBER_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %TRANSCRIBER_EXIT%
