"""Helper script: read robots.json and launch each Robot Agent as a subprocess."""
import json
import subprocess
import sys
import os
import tempfile
from pathlib import Path

CC_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = CC_DIR.parent
CONFIG = CC_DIR / "robots.json"

if not CONFIG.exists():
    print(f"ERROR: {CONFIG} not found")
    sys.exit(1)

robots = json.loads(CONFIG.read_text())
print(f"Starting {len(robots)} Robot Agent(s)...\n")

python = sys.executable
procs = []

for r in robots:
    robot_id = r["robot_id"]
    pi_host = r["pi_host"]
    zone = r.get("zone", "")
    robot_type = r.get("robot_type", "spider")

    log_path = os.path.join(tempfile.gettempdir(), f"robot_agent_{robot_id}.log")
    log_file = open(log_path, "w")

    cmd = [
        python, "-B", "-m", "Command_Center.robot_agent",
        "--robot-id", robot_id,
        "--pi-host", pi_host,
        "--zone", zone,
        "--robot-type", robot_type,
    ]

    proc = subprocess.Popen(
        cmd, cwd=str(PROJECT_DIR),
        stdout=log_file, stderr=subprocess.STDOUT,
    )
    procs.append((robot_id, proc, log_path))
    print(f"  [{robot_id}] PID={proc.pid} zone={zone} pi={pi_host} log={log_path}")

print(f"\nAll Robot Agents started. PIDs: {[p.pid for _, p, _ in procs]}")
print("To stop: run stop_robots.bat")

# Keep this script alive so child processes stay running
try:
    for _, proc, _ in procs:
        proc.wait()
except KeyboardInterrupt:
    print("\nStopping all Robot Agents...")
    for rid, proc, _ in procs:
        proc.terminate()
        print(f"  [{rid}] stopped")
