#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# entrypoint-postgres.sh — dedicated PostgreSQL service (Option B + B1).
#
# Serves two databases on localhost:5432 over trust auth:
#   - registration : PERSISTENT (on the /engrfs postgres_data volume) — the
#                     registered/approved-agent state used by the registration svc.
#   - dir          : EPHEMERAL — dropped + recreated every run (B1) so ADS's
#                     metadata is cold each deploy, matching its OCI store +
#                     routing datastore (which are in-container ephemeral).
#
# Debian postgresql-15 is used to stay binary/locale-compatible with the existing
# data dir the old registration entrypoint created (initdb UTF8 / locale=C,
# dynamic_shared_memory_type=sysv). Do NOT swap in postgres:*-alpine (musl libc).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
PGPORT="${PGPORT:-5432}"
PGBIN=/usr/lib/postgresql/15/bin
PGSOCKET=/tmp/postgresql
READY_FILE="${POSTGRES_READY_FILE:-/workspace/logs/postgres_ready}"
mkdir -p "$PGSOCKET"
rm -f "$READY_FILE" 2>/dev/null || true

# ── Init cluster if the volume is empty (first ever run) ──
if [ -z "$(ls -A "$PGDATA" 2>/dev/null)" ]; then
    echo "[postgres] Initialising cluster at ${PGDATA}..."
    mkdir -p "$PGDATA"; chmod 0700 "$PGDATA"
    "$PGBIN/initdb" -D "$PGDATA" --encoding=UTF8 --locale=C
    # SysV shared memory works in HPC containers where /dev/shm (posix) is restricted.
    sed "s|#dynamic_shared_memory_type = posix|dynamic_shared_memory_type = sysv|" \
        "$PGDATA/postgresql.conf" > "$PGDATA/postgresql.conf.tmp" \
        && mv "$PGDATA/postgresql.conf.tmp" "$PGDATA/postgresql.conf"
fi

# ── Ensure TCP on localhost + trust for 127.0.0.1 (idempotent) ──
grep -q "^listen_addresses" "$PGDATA/postgresql.conf" || echo "listen_addresses = 'localhost'" >> "$PGDATA/postgresql.conf"
grep -q "127.0.0.1/32" "$PGDATA/pg_hba.conf" || echo "host  all  all  127.0.0.1/32  trust" >> "$PGDATA/pg_hba.conf"

# Clear ONLY a stale postmaster.pid (leftover from a previous unclean shutdown).
# If the recorded PID is still alive, another Postgres already owns this data dir
# (e.g. an accidental double-submit) — refuse rather than start a second postmaster,
# which would run crash-recovery concurrently and CORRUPT pg_control/WAL.
if [ -f "$PGDATA/postmaster.pid" ]; then
    OLDPID="$(head -n1 "$PGDATA/postmaster.pid" 2>/dev/null || true)"
    if [ -n "${OLDPID:-}" ] && kill -0 "$OLDPID" 2>/dev/null; then
        echo "[postgres] ERROR: postmaster.pid holds live PID $OLDPID — another Postgres is"
        echo "[postgres]        already using $PGDATA (double-submit?). Refusing to start a second."
        exit 1
    fi
    echo "[postgres] Removing stale postmaster.pid (recorded PID ${OLDPID:-?} not running)."
    rm -f "$PGDATA/postmaster.pid"
fi
if ! "$PGBIN/pg_ctl" start -D "$PGDATA" -l "$PGDATA/postgres.log" -w -t 30 \
    -o "-c unix_socket_directories=$PGSOCKET -c listen_addresses='localhost' -c port=$PGPORT"; then
    echo "[postgres] ERROR: pg_ctl could not start the server. PG_VERSION=$(cat "$PGDATA/PG_VERSION" 2>/dev/null || echo '?'). Server log:"
    cat "$PGDATA/postgres.log" 2>/dev/null || echo "[postgres] (no $PGDATA/postgres.log written)"
    exit 1
fi

echo "[postgres] Waiting for PostgreSQL to accept connections..."
for i in $(seq 1 30); do
    "$PGBIN/pg_isready" -h "$PGSOCKET" -p "$PGPORT" -q && break
    if [ "$i" -eq 30 ]; then
        echo "[postgres] ERROR: PostgreSQL not ready after 30s. Postgres log:"
        cat "$PGDATA/postgres.log" || true
        exit 1
    fi
    sleep 1
done

# superuser + databases
"$PGBIN/createuser" -h "$PGSOCKET" -p "$PGPORT" --superuser postgres 2>/dev/null || true
"$PGBIN/createdb"   -h "$PGSOCKET" -p "$PGPORT" -U postgres registration 2>/dev/null || true   # persistent
# B1: reset the ADS directory DB fresh each run (no active ADS connections yet)
"$PGBIN/psql" -h "$PGSOCKET" -p "$PGPORT" -U postgres -c "DROP DATABASE IF EXISTS dir;"
"$PGBIN/psql" -h "$PGSOCKET" -p "$PGPORT" -U postgres -c "CREATE DATABASE dir;"

mkdir -p "$(dirname "$READY_FILE")"; touch "$READY_FILE"
echo "[postgres] ready — registration (persistent) + dir (fresh) available on :$PGPORT"

exec tail -f "$PGDATA/postgres.log"    # keep container alive; surface logs
