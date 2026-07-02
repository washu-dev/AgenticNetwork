#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# entrypoint-registration.sh — registration service container entrypoint.
#
# As of the ADS→Postgres migration (Option B), Postgres runs in its own
# dedicated container (see entrypoint-postgres.sh). This container is now a pure
# DB *client*: it no longer initialises or hosts Postgres. It waits for the
# external Postgres and ADS to be reachable over host-localhost, then starts the
# FastAPI registration service. Its DSN (REGISTRATION_DB_DSN) is unchanged.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PGPORT="${PGPORT:-5432}"

# ── Wait for the external PostgreSQL service ──────────────────────────────────
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

# ── Wait for ADS ──────────────────────────────────────────────────────────────
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

# ── Start registration service ────────────────────────────────────────────────
PORT="${REGISTRATION_SERVICE_PORT:-8091}"
echo "[registration] Starting registration service on port ${PORT}..."
exec /usr/local/bin/python3 -m registration_service.server
