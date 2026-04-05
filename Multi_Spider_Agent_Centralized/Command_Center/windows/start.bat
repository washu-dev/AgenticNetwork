@echo off
REM Start Multi-Spider Agent Command Center
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "CC_DIR=%SCRIPT_DIR%.."
set "PROJECT_DIR=%CC_DIR%\.."
set "PID_FILE=%CC_DIR%\.pid"

REM Resolve python: prefer venv, fallback to system
set "PYTHON=%CC_DIR%\venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    set "PYTHON=python"
)

echo Starting Multi-Spider Agent Command Center...
echo Press Ctrl+C to stop
echo ---

REM Set environment variables
set "DISCOVERY_MODE=ads"
set "ADS_SERVER_ADDRESS=localhost:8888"

cd /d "%PROJECT_DIR%"
"%PYTHON%" -B -m Command_Center.main
