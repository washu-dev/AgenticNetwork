@echo off
REM Start NATS server for A2A transport
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "COMPOSE_FILE=%SCRIPT_DIR%..\docker-compose-nats.yaml"

echo Starting NATS server...
docker compose -f "%COMPOSE_FILE%" up -d

echo Waiting for NATS to be ready...
set "READY=0"
for /L %%i in (1,1,20) do (
    if !READY! == 0 (
        curl -sf http://localhost:8222/varz >nul 2>&1
        if !errorlevel! == 0 (
            echo NATS is ready on port 4222
            echo Monitor: http://localhost:8222
            set "READY=1"
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)

if %READY% == 0 (
    echo ERROR: NATS did not become ready in time
    exit /b 1
)
endlocal
