@echo off
setlocal
cd /d "%~dp0"
title Dummy engine

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" --version >nul 2>nul
if errorlevel 1 goto nopython

"%PY%" launcher.py
if errorlevel 1 pause
exit /b 0

:nopython
echo.
echo   Python was not found on this computer.
echo   Install Python 3.11+ from https://www.python.org/downloads/
echo   (tick "Add Python to PATH"), then run:
echo       pip install -r requirements.txt
echo       playwright install chromium
echo.
pause
exit /b 1
