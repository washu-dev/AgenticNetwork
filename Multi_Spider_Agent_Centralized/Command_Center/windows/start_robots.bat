@echo off
REM Start all Robot Agent processes from robots.json
setlocal

set "SCRIPT_DIR=%~dp0"
set "CC_DIR=%SCRIPT_DIR%.."

REM Resolve python: prefer venv, fallback to system
set "PYTHON=%CC_DIR%\venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    set "PYTHON=python"
)

"%PYTHON%" "%SCRIPT_DIR%launch_robots.py"

endlocal
