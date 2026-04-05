@echo off
REM Stop Agent Directory Service (ADS)

set "SCRIPT_DIR=%~dp0"
set "COMPOSE_FILE=%SCRIPT_DIR%..\docker-compose-ads.yaml"

echo Stopping ADS services...
docker compose -f "%COMPOSE_FILE%" down
echo ADS stopped.
