"""In-memory drop-in for the asyncpg pool — used when FAKE_DB=1.

Implements exactly the subset of asyncpg's Pool/Connection API that the
registration service actually calls.  No real DB, no network, no migration.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

# Kept in sync with port_pool.py — env-configurable so a test instance can offset
# its custom-agent port range. See docs/MULTI_CONTRIBUTOR.md.
_PORT_RANGE_START = int(os.getenv("CUSTOM_AGENT_PORT_START", "19200"))
_PORT_RANGE_END = int(os.getenv("CUSTOM_AGENT_PORT_END", "19299"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_uuid(v: Any) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


class FakeRecord(dict):
    """dict that mimics asyncpg.Record attribute/key access."""


class FakeTransaction:
    async def __aenter__(self) -> "FakeTransaction":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class FakeStore:
    """Shared in-memory state for a single FakePool instance."""

    def __init__(self) -> None:
        self.agents: dict[uuid.UUID, dict] = {}
        self.port_pool: dict[int, dict] = {}

    def seed_ports(self) -> None:
        if self.port_pool:
            return
        for p in range(_PORT_RANGE_START, _PORT_RANGE_END + 1):
            self.port_pool[p] = {"port": p, "in_use": False, "agent_id": None}


class FakeConnection:
    """Handles the specific SQL statements used by the registration service."""

    def __init__(self, store: FakeStore) -> None:
        self._s = store

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, sql: str, *args: Any) -> None:
        s = " ".join(sql.split()).lower()

        # ── agents table ──────────────────────────────────────────────────────

        if s.startswith("insert into agents"):
            uid, name, desc, handler_type, oasf_raw, config_raw = args
            now = _now()
            self._s.agents[_coerce_uuid(uid)] = {
                "id": _coerce_uuid(uid),
                "name": name,
                "description": desc,
                "handler_type": handler_type,
                "oasf_skills": json.loads(oasf_raw) if isinstance(oasf_raw, str) else oasf_raw,
                "config": json.loads(config_raw) if isinstance(config_raw, str) else config_raw,
                "port": None,
                "status": "registered",
                "handler_file_path": None,
                "approved": True,
                "is_archive": False,
                "endpoint_url": None,
                "ads_cid": None,
                "image_ref": None,
                "resources": None,
                "created_at": now,
                "updated_at": now,
            }

        elif s.startswith("update agents set port = $1, status = 'registered'"):
            port, uid = args
            a = self._s.agents[_coerce_uuid(uid)]
            a["port"] = port
            a["status"] = "registered"
            a["updated_at"] = _now()

        elif s.startswith("update agents set port = $1 where id = $2"):
            port, uid = args
            self._s.agents[_coerce_uuid(uid)]["port"] = port
            self._s.agents[_coerce_uuid(uid)]["updated_at"] = _now()

        elif "set status = $1" in s and "endpoint_url" in s:
            # _set_status helper: UPDATE agents SET status=$1, endpoint_url=COALESCE(...)
            status, endpoint_url, ads_cid, uid = args
            a = self._s.agents.get(_coerce_uuid(uid))
            if a:
                a["status"] = status
                if endpoint_url is not None:
                    a["endpoint_url"] = endpoint_url
                if ads_cid is not None:
                    a["ads_cid"] = ads_cid
                a["updated_at"] = _now()

        elif "set status = 'image_ready'" in s:
            # recover_live_agents (cluster): re-queue a ghost 'live' row for the
            # host spawn loop — UPDATE agents SET status='image_ready', ads_cid=NULL ...
            uid = args[0]
            a = self._s.agents.get(_coerce_uuid(uid))
            if a:
                a["status"] = "image_ready"
                a["ads_cid"] = None
                a["updated_at"] = _now()

        elif "set status = 'stopped'" in s:
            # supervisor stop_and_unpublish: UPDATE agents SET status='stopped', ads_cid=NULL ...
            uid = args[0]
            a = self._s.agents.get(_coerce_uuid(uid))
            if a:
                a["status"] = "stopped"
                a["ads_cid"] = None
                a["updated_at"] = _now()

        elif s.startswith("delete from agents where id"):
            self._s.agents.pop(_coerce_uuid(args[0]), None)

        elif s.startswith("delete from agents where status in"):
            stale = [k for k, v in self._s.agents.items() if v["status"] in ("stopped", "failed")]
            for k in stale:
                del self._s.agents[k]

        # ── port_pool table ───────────────────────────────────────────────────

        elif "set in_use = true" in s:
            agent_id_str, port = args
            self._s.port_pool[port]["in_use"] = True
            self._s.port_pool[port]["agent_id"] = agent_id_str

        elif "set in_use = false, agent_id = null where port = $1" in s:
            port = args[0]
            if port in self._s.port_pool:
                self._s.port_pool[port]["in_use"] = False
                self._s.port_pool[port]["agent_id"] = None

        elif "set in_use = false, agent_id = null where agent_id in" in s:
            stopped = {str(v["id"]) for v in self._s.agents.values() if v["status"] in ("stopped", "failed")}
            for p in self._s.port_pool.values():
                if str(p.get("agent_id")) in stopped:
                    p["in_use"] = False
                    p["agent_id"] = None

    async def executemany(self, sql: str, args_list: list) -> None:
        s = " ".join(sql.split()).lower()
        if "insert into port_pool" in s:
            for port, in_use, agent_id in args_list:
                self._s.port_pool[port] = {"port": port, "in_use": in_use, "agent_id": agent_id}

    async def fetchrow(self, sql: str, *args: Any) -> FakeRecord | None:
        s = " ".join(sql.split()).lower()

        if "select ads_cid, port from agents where id" in s:
            row = self._s.agents.get(_coerce_uuid(args[0]))
            if row is None:
                return None
            return FakeRecord({"ads_cid": row["ads_cid"], "port": row["port"]})

        if "select * from agents where id" in s:
            row = self._s.agents.get(_coerce_uuid(args[0]))
            return FakeRecord(row) if row else None

        if "select port from port_pool" in s:
            for p, info in sorted(self._s.port_pool.items()):
                if not info["in_use"]:
                    return FakeRecord({"port": p})
            return None

        return None

    async def fetch(self, sql: str, *args: Any) -> list[FakeRecord]:
        s = " ".join(sql.split()).lower()

        if "select * from agents order by created_at desc" in s:
            rows = sorted(self._s.agents.values(), key=lambda r: r["created_at"], reverse=True)
            return [FakeRecord(r) for r in rows]

        if "select id from agents where status" in s and "'live'" in s:
            return [FakeRecord({"id": v["id"]}) for v in self._s.agents.values() if v["status"] == "live"]

        # recover_live_agents (cluster): SELECT id, name, handler_type, port, updated_at ...
        if "from agents where status = 'live'" in s:
            return [
                FakeRecord({
                    "id": v["id"],
                    "name": v["name"],
                    "handler_type": v["handler_type"],
                    "port": v["port"],
                    "updated_at": v["updated_at"],
                })
                for v in self._s.agents.values()
                if v["status"] == "live"
            ]

        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        s = " ".join(sql.split()).lower()

        if "select 1 from agents where id" in s:
            return 1 if _coerce_uuid(args[0]) in self._s.agents else None

        if "select count(*) from port_pool" in s:
            return len(self._s.port_pool)

        return None


class _FakeConnCtx:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


class FakePool:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self._conn = FakeConnection(store)

    def acquire(self) -> _FakeConnCtx:
        return _FakeConnCtx(self._conn)

    async def close(self) -> None:
        pass


# ── Module-level singletons (reset between tests) ─────────────────────────────

_fake_store: FakeStore | None = None
_fake_pool: FakePool | None = None


def get_fake_pool() -> FakePool:
    global _fake_store, _fake_pool
    if _fake_pool is None:
        _fake_store = FakeStore()
        _fake_pool = FakePool(_fake_store)
    return _fake_pool


async def init_fake_db() -> None:
    pool = get_fake_pool()
    pool._store.seed_ports()
    conn = pool._conn
    # Mirror the cleanup that init_db() does on real postgres
    await conn.execute(
        "update port_pool set in_use = false, agent_id = null "
        "where agent_id in (select id from agents where status in ('stopped', 'failed'))"
    )
    await conn.execute("delete from agents where status in ('stopped', 'failed')")


async def close_fake_db() -> None:
    global _fake_store, _fake_pool
    _fake_store = None
    _fake_pool = None


def reset_fake_db() -> None:
    """Reset in-memory state — call between tests for isolation."""
    global _fake_store, _fake_pool
    _fake_store = None
    _fake_pool = None


# ── Helpers backing db.py's agent helper layer in FAKE_DB mode ──────────────────

_ALLOWED_UPDATE_COLS = {
    "status",
    "approved",
    "handler_file_path",
    "is_archive",
    "endpoint_url",
    "ads_cid",
    "port",
    "image_ref",
}


def _store() -> FakeStore:
    return get_fake_pool()._store


def fake_create_agent(
    *,
    name: str,
    description: str,
    handler_type: str,
    oasf_skills: list,
    config: dict,
    approved: bool = True,
    resources: dict | None = None,
) -> dict:
    s = _store()
    s.seed_ports()
    # Claim the lowest free port.
    port = None
    for p, info in sorted(s.port_pool.items()):
        if not info["in_use"]:
            port = p
            info["in_use"] = True
            break
    uid = uuid.uuid4()
    now = _now()
    if port is not None:
        s.port_pool[port]["agent_id"] = str(uid)
    row = {
        "id": uid,
        "name": name,
        "description": description,
        "handler_type": handler_type,
        "oasf_skills": oasf_skills,
        "config": config,
        "port": port,
        "status": "registered",
        "handler_file_path": None,
        "approved": approved,
        "is_archive": False,
        "endpoint_url": None,
        "ads_cid": None,
        "image_ref": None,
        "resources": resources,
        "created_at": now,
        "updated_at": now,
    }
    s.agents[uid] = row
    return dict(row)


def fake_get_agent(agent_id: Any) -> dict | None:
    row = _store().agents.get(_coerce_uuid(agent_id))
    return dict(row) if row else None


def fake_update_agent(agent_id: Any, **fields: Any) -> None:
    row = _store().agents.get(_coerce_uuid(agent_id))
    if row is None:
        return
    for col, val in fields.items():
        if col in _ALLOWED_UPDATE_COLS:
            row[col] = val
    row["updated_at"] = _now()


def fake_list_agents_by_status(status: str) -> list[dict]:
    rows = [v for v in _store().agents.values() if v["status"] == status]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return [dict(r) for r in rows]
