#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# entrypoint-registration.sh — registration service container entrypoint.
#
# As of the ADS→Postgres migration (Option B), Postgres runs in its own
# dedicated container (see entrypoint-postgres.sh). This container is now a pure
# DB *client*: it no longer initialises or hosts Postgres. It waits for the
# external Postgres and ADS to be reachable over host-localhost, then starts the
# FastAPI registration service, which reads its DSN from DATABASE_URL (the old name
# REGISTRATION_DB_DSN still works).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Port: Cloud Run injects $PORT (8080) and requires the app to listen on it; the
# cluster uses REGISTRATION_SERVICE_PORT (8091). Prefer $PORT when present.
export REGISTRATION_SERVICE_PORT="${PORT:-${REGISTRATION_SERVICE_PORT:-8091}}"

# The localhost TCP readiness waits below are a cluster concern: there Postgres and
# ADS are co-located and reached over host-localhost. On Cloud Run the DB is Cloud
# SQL (unix socket / private IP) and ADS is over the VPC connector — neither is on
# localhost — so set SKIP_STARTUP_WAITS=1 to skip them. init_db (DB) and the
# best-effort ADS publish tolerate a briefly-unready dependency there.
if [ -z "${SKIP_STARTUP_WAITS:-}" ]; then
    PGPORT="${PGPORT:-5432}"

    # ── Wait for the external PostgreSQL service ──────────────────────────────
    echo "[registration] Waiting for PostgreSQL on :${PGPORT} ..."
    for i in $(seq 1 60); do
        if /usr/local/bin/python3 -c "import socket; socket.create_connection(('localhost', ${PGPORT}), timeout=1).close()" 2>/dev/null; then
            echo "[registration] PostgreSQL ready (${i}s)."
            break
        fi
        if [ "$i" -eq 60 ]; then
            echo "[registration] ERROR: PostgreSQL not reachable on :${PGPORT} after 60s."
            exit 1
        fi
        sleep 1
    done

    # ── Wait for ADS ──────────────────────────────────────────────────────────
    ADS_ADDR="${ADS_SERVER_ADDR:-localhost:8888}"
    ADS_HOST="${ADS_ADDR%%:*}"
    ADS_PORT="${ADS_ADDR##*:}"

    echo "[registration] Waiting for ADS at ${ADS_ADDR}..."
    for i in $(seq 1 60); do
        if /usr/local/bin/python3 -c "import socket; socket.create_connection(('${ADS_HOST}', ${ADS_PORT}), timeout=1).close()" 2>/dev/null; then
            echo "[registration] ADS ready (${i}s)."
            break
        fi
        if [ "$i" -eq 60 ]; then
            echo "[registration] ERROR: ADS not ready after 60s."
            exit 1
        fi
        sleep 1
    done
fi

# ── Start registration service ────────────────────────────────────────────────
echo "[registration] Starting registration service on port ${REGISTRATION_SERVICE_PORT}..."
exec /usr/local/bin/python3 -m registration_service.server
