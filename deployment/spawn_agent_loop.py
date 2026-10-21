#!/usr/bin/env python3
"""Host-side spawn loop for custom cluster agents (plan Note #8).

Runs in submit.sbatch's native Slurm environment — where `srun` works — because
`srun` is NOT available inside the registration container. Each cycle it:

  1. GET  /internal/spawn-queue  → agents marked image_ready (with srun argv + env)
  2. launches each via `srun --overlap` (in the background) and
     POST /internal/spawned {agent_id, pid}
  3. GET  /internal/stop-queue   → PIDs to kill (agents deleted/stopped); only
     PIDs this loop launched are signalled

Stdlib only (urllib + subprocess) so it needs no pip deps or jq on the host.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request

REG = os.environ.get("REGISTRATION_INTERNAL_URL", "http://localhost:8091")
POLL = float(os.environ.get("SPAWN_LOOP_POLL_SECONDS", "15"))
LOGS = os.environ.get("LOGS", "/tmp")


def _get(path: str):
    try:
        with urllib.request.urlopen(REG + path, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 — loop must survive transient errors
        print(f"[spawn-loop] GET {path} failed: {exc}", flush=True)
        return None


def _post(path: str, payload: dict) -> bool:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        REG + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"[spawn-loop] POST {path} failed: {exc}", flush=True)
        return False


def _reap(launched: set[int]) -> None:
    """Reap any exited child srun processes so killed agents don't linger as zombies."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        launched.discard(pid)


def main() -> None:
    print(f"[spawn-loop] starting reg={REG} poll={POLL}s logs={LOGS}", flush=True)
    launched: set[int] = set()  # srun PIDs this loop started and has not reaped
    while True:
        _reap(launched)
        for item in _get("/internal/spawn-queue") or []:
            aid = item["agent_id"]
            argv = item["argv"]
            env_extra = item.get("env", {})
            log_path = os.path.join(LOGS, f"custom-{aid}.log")
            print(f"[spawn-loop] launching {aid}: {' '.join(argv)}", flush=True)
            try:
                log_f = open(log_path, "a")
                proc = subprocess.Popen(
                    argv, env={**os.environ, **env_extra},
                    stdout=log_f, stderr=subprocess.STDOUT,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[spawn-loop] launch FAILED {aid}: {exc}", flush=True)
                continue
            launched.add(proc.pid)
            if _post("/internal/spawned", {"agent_id": aid, "pid": proc.pid}):
                print(f"[spawn-loop] reported pid {proc.pid} for {aid}", flush=True)

        stops = _get("/internal/stop-queue") or {"pids": []}
        for pid in stops.get("pids", []):
            if pid not in launched:
                print(f"[spawn-loop] skipped stop of pid {pid}: not launched by this loop", flush=True)
                continue
            try:
                os.kill(int(pid), signal.SIGTERM)
                print(f"[spawn-loop] killed pid {pid}", flush=True)
            except ProcessLookupError:
                pass
            except Exception as exc:  # noqa: BLE001
                print(f"[spawn-loop] kill {pid} failed: {exc}", flush=True)

        time.sleep(POLL)


if __name__ == "__main__":
    main()
