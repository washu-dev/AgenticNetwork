#!/bin/bash
set -euo pipefail

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
PGPORT="${PGPORT:-5432}"
PGDATABASE="${PGDATABASE:-registration}"
PGBIN=/usr/lib/postgresql/15/bin

PGSOCKET=/tmp/postgresql
mkdir -p "$PGSOCKET"

pg_start() {
    "$PGBIN/pg_ctl" start -D "$PGDATA" -l "$PGDATA/postgres.log" -w -t 30 \
        -o "-c unix_socket_directories=$PGSOCKET" || {
        echo "[registration] pg_ctl failed to start. Postgres log:"
        cat "$PGDATA/postgres.log" || true
        exit 1
    }
}

# ── PostgreSQL init ───────────────────────────────────────────────────────────
if [ -z "$(ls -A "$PGDATA" 2>/dev/null)" ]; then
    echo "[registration] Initialising PostgreSQL data directory at ${PGDATA}..."
    mkdir -p "$PGDATA"
    chmod 0700 "$PGDATA"
    "$PGBIN/initdb" -D "$PGDATA" --encoding=UTF8 --locale=C

    # SysV shared memory works in HPC containers where /dev/shm (posix) is restricted.
    # Use mv instead of sed -i to avoid NFS permission-preservation errors.
    sed "s|#dynamic_shared_memory_type = posix|dynamic_shared_memory_type = sysv|" \
        "$PGDATA/postgresql.conf" > "$PGDATA/postgresql.conf.tmp" \
        && mv "$PGDATA/postgresql.conf.tmp" "$PGDATA/postgresql.conf"

    echo "[registration] Cluster initialised."
fi

echo "[registration] Starting PostgreSQL..."
rm -f "$PGDATA/postmaster.pid"
pg_start

echo "[registration] Waiting for PostgreSQL to accept connections..."
for i in $(seq 1 30); do
    if "$PGBIN/pg_isready" -h "$PGSOCKET" -p "$PGPORT" -q 2>/dev/null; then
        echo "[registration] PostgreSQL ready (${i}s)."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "[registration] ERROR: PostgreSQL not ready after 30s. Postgres log:"
        cat "$PGDATA/postgres.log" || true
        exit 1
    fi
    sleep 1
done

# Ensure postgres superuser role and registration database exist (idempotent).
"$PGBIN/createuser" -h "$PGSOCKET" -p "$PGPORT" --superuser postgres 2>/dev/null || true
"$PGBIN/createdb"   -h "$PGSOCKET" -p "$PGPORT" -U postgres "$PGDATABASE" 2>/dev/null || true
echo "[registration] Database '${PGDATABASE}' ready."

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
