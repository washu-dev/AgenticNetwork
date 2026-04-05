@echo off
REM Start Agent Directory Service (ADS) via Docker Compose
REM Run this ONCE before starting Command Center and Pi agents.
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "COMPOSE_FILE=%SCRIPT_DIR%..\docker-compose-ads.yaml"

echo Starting ADS services...
docker compose -f "%COMPOSE_FILE%" up -d

echo.
echo Waiting for ADS to be ready...
set "READY=0"
for /L %%i in (1,1,30) do (
    if !READY! == 0 (
        curl -sf http://127.0.0.1:8889/healthz/ready >nul 2>&1
        if !errorlevel! == 0 (
            echo ADS is ready! (gRPC: localhost:8888)
            set "READY=1"
        ) else (
            timeout /t 2 /nobreak >nul
        )
    )
)

if %READY% == 0 (
    echo WARNING: ADS health check timed out. Check: docker logs multi-spider-ads
)
