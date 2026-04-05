@echo off
REM Stop NATS server

set "SCRIPT_DIR=%~dp0"
set "COMPOSE_FILE=%SCRIPT_DIR%..\docker-compose-nats.yaml"

echo Stopping NATS services...
docker compose -f "%COMPOSE_FILE%" down
echo NATS stopped.
