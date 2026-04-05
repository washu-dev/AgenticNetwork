@echo off
REM One-click start: Infrastructure + Command Center + Robot Agents
REM
REM Startup order:
REM   1. ADS (Agent Directory Service)
REM   2. NATS (Message Broker)
REM   3. Robot Agents (background)
REM   4. Command Center (foreground, Ctrl+C to stop)

setlocal
set "SCRIPT_DIR=%~dp0"

echo ============================================
echo  Multi-Spider Agent - Windows Launcher
echo ============================================
echo.

REM Step 1: Start ADS
echo [1/4] Starting ADS...
call "%SCRIPT_DIR%start_ads.bat"
echo.

REM Step 2: Start NATS
echo [2/4] Starting NATS...
call "%SCRIPT_DIR%start_nats.bat"
echo.

REM Step 3: Start Robot Agents (background)
echo [3/4] Starting Robot Agents...
call "%SCRIPT_DIR%start_robots.bat"
echo.

REM Step 4: Start Command Center (foreground)
echo [4/4] Starting Command Center (Ctrl+C to stop)...
echo ============================================
call "%SCRIPT_DIR%start.bat"

endlocal
