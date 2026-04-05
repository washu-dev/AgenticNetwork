@echo off
REM One-click stop: Command Center + Robot Agents + Infrastructure

setlocal
set "SCRIPT_DIR=%~dp0"

echo ============================================
echo  Multi-Spider Agent - Windows Shutdown
echo ============================================
echo.

echo [1/4] Stopping Command Center...
call "%SCRIPT_DIR%stop.bat"
echo.

echo [2/4] Stopping Robot Agents...
call "%SCRIPT_DIR%stop_robots.bat"
echo.

echo [3/4] Stopping NATS...
call "%SCRIPT_DIR%stop_nats.bat"
echo.

echo [4/4] Stopping ADS...
call "%SCRIPT_DIR%stop_ads.bat"
echo.

echo ============================================
echo  All services stopped.
echo ============================================

endlocal
