@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Please run INSTALL.cmd first.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I "%~dp0ui.py" %*
set "TRANSCRIBER_EXIT=%ERRORLEVEL%"
echo.
echo Press any key to close this window.
pause >nul
exit /b %TRANSCRIBER_EXIT%
