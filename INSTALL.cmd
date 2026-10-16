@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Local Transcriber - setup
set "PYEXE="
py -3.11 -c "import sys;raise SystemExit(0 if sys.version_info[:2]==(3,11) and sys.maxsize>2**32 else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3.11"
if not defined PYEXE (
    python -c "import sys;raise SystemExit(0 if sys.version_info[:2]==(3,11) and sys.maxsize>2**32 else 1)" >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)
if defined PYEXE goto run
echo Python 3.11 x64 is required. Install it from the official Python website:
echo https://www.python.org/downloads/windows/
echo Then open a new window and run INSTALL.cmd again.
pause
exit /b 1
:run
%PYEXE% -I "%~dp0install.py" %*
set "TRANSCRIBER_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %TRANSCRIBER_EXIT%
