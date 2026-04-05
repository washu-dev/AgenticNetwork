@echo off
REM Stop all Robot Agent processes
setlocal enabledelayedexpansion

set "STOPPED=0"

for /f "tokens=2" %%p in ('tasklist /fi "imagename eq python.exe" /fo list 2^>nul ^| findstr /i "PID"') do (
    wmic process where "ProcessId=%%p" get CommandLine 2>nul | findstr /i "Command_Center.robot_agent" >nul 2>&1
    if !errorlevel! == 0 (
        taskkill /pid %%p /f >nul 2>&1
        echo Stopped Robot Agent process (PID: %%p)
        set "STOPPED=1"
    )
)

if %STOPPED% == 0 (
    echo No Robot Agent processes found.
)
endlocal
