@echo off
REM Stop Multi-Spider Agent Command Center
setlocal enabledelayedexpansion

set "STOPPED=0"

REM Kill any Command_Center.main processes
for /f "tokens=2" %%p in ('tasklist /fi "imagename eq python.exe" /fo list 2^>nul ^| findstr /i "PID"') do (
    wmic process where "ProcessId=%%p" get CommandLine 2>nul | findstr /i "Command_Center.main" >nul 2>&1
    if !errorlevel! == 0 (
        taskkill /pid %%p /f >nul 2>&1
        echo Stopped Command Center process (PID: %%p)
        set "STOPPED=1"
    )
)

if %STOPPED% == 0 (
    echo Command Center is not running.
)
endlocal
