from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


RUN_DIR = Path("v2_agent_network/.runtime")
DEFAULT_PID_FILE = RUN_DIR / "ads_lan_proxy.pid"
DEFAULT_LOG_FILE = Path("v2_agent_network/ads_lan_proxy.log")
DEFAULT_DAEMON_LOG_FILE = Path("v2_agent_network/ads_daemon.log")
DEFAULT_DAEMON_CONFIG = Path("v2_agent_network/configs/ads_daemon_fast_routing.yaml")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage local ADS daemon LAN exposure.")
    parser.add_argument("--listen-host", default=os.getenv("ADS_LAN_HOST", _default_lan_ip()))
    parser.add_argument("--listen-port", type=int, default=int(os.getenv("ADS_LAN_PORT", "8888")))
    parser.add_argument("--target-host", default=os.getenv("ADS_TARGET_HOST", "127.0.0.1"))
    parser.add_argument("--target-port", type=int, default=int(os.getenv("ADS_TARGET_PORT", "8888")))
    parser.add_argument("--pid-file", type=Path, default=DEFAULT_PID_FILE)
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE)
    parser.add_argument(
        "--daemon-config",
        type=Path,
        default=Path(os.getenv("ADS_DAEMON_CONFIG", str(DEFAULT_DAEMON_CONFIG))),
        help="dirctl daemon config used when this launcher starts the daemon.",
    )
    parser.add_argument(
        "--daemon-log-file",
        type=Path,
        default=Path(os.getenv("ADS_DAEMON_LOG_FILE", str(DEFAULT_DAEMON_LOG_FILE))),
        help="Log file for a daemon started by this launcher.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start")
    stop = subparsers.add_parser("stop")
    stop.add_argument("--stop-daemon", action="store_true")
    subparsers.add_parser("status")
    subparsers.add_parser("proxy")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "start":
        return _start(args)
    if args.command == "stop":
        return _stop(args)
    if args.command == "status":
        return _status(args)
    if args.command == "proxy":
        asyncio.run(_run_proxy(args))
        return 0
    raise AssertionError(args.command)


def _start(args: argparse.Namespace) -> int:
    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    if not _ensure_daemon(args.daemon_config, args.daemon_log_file):
        print(f"dirctl daemon failed to start; see {args.daemon_log_file}", file=sys.stderr)
        return 1
    if _pid_running(_read_pid(args.pid_file)):
        print(f"ADS LAN proxy already running pid={_read_pid(args.pid_file)}")
        return 0
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "v2_agent_network.scripts.manage_ads_lan",
        "--listen-host",
        args.listen_host,
        "--listen-port",
        str(args.listen_port),
        "--target-host",
        args.target_host,
        "--target-port",
        str(args.target_port),
        "--pid-file",
        str(args.pid_file),
        "--log-file",
        str(args.log_file),
        "--daemon-config",
        str(args.daemon_config),
        "--daemon-log-file",
        str(args.daemon_log_file),
        "proxy",
    ]
    with args.log_file.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    args.pid_file.write_text(str(process.pid), encoding="utf-8")
    time.sleep(0.5)
    if not _pid_running(process.pid):
        print(f"ADS LAN proxy failed to start; see {args.log_file}", file=sys.stderr)
        return 1
    print(
        f"ADS LAN proxy listening on {args.listen_host}:{args.listen_port} "
        f"-> {args.target_host}:{args.target_port} pid={process.pid} log={args.log_file}"
    )
    return 0


def _stop(args: argparse.Namespace) -> int:
    pid = _read_pid(args.pid_file)
    if pid and _pid_running(pid):
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped ADS LAN proxy pid={pid}")
    args.pid_file.unlink(missing_ok=True)
    if args.stop_daemon:
        subprocess.run(["dirctl", "daemon", "stop"], check=False)
    return 0


def _status(args: argparse.Namespace) -> int:
    daemon = subprocess.run(["dirctl", "daemon", "status"], text=True, capture_output=True, check=False)
    pid = _read_pid(args.pid_file)
    print((daemon.stdout or daemon.stderr).strip())
    print(f"proxy_pid={pid or 'none'} running={_pid_running(pid)}")
    print(
        f"listen={args.listen_host}:{args.listen_port} "
        f"target={args.target_host}:{args.target_port} log={args.log_file}"
    )
    print(f"daemon_config={args.daemon_config} daemon_log={args.daemon_log_file}")
    return 0


async def _run_proxy(args: argparse.Namespace) -> None:
    server = await asyncio.start_server(
        lambda reader, writer: _handle_connection(reader, writer, args),
        host=args.listen_host,
        port=args.listen_port,
    )
    print(
        f"ADS proxy listening on {args.listen_host}:{args.listen_port} "
        f"-> {args.target_host}:{args.target_port}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


async def _handle_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    args: argparse.Namespace,
) -> None:
    peer = client_writer.get_extra_info("peername")
    print(f"proxy connection from {peer}", flush=True)
    try:
        target_reader, target_writer = await asyncio.open_connection(args.target_host, args.target_port)
        await asyncio.gather(
            _pipe(client_reader, target_writer),
            _pipe(target_reader, client_writer),
        )
    except Exception as exc:
        print(f"proxy connection error from {peer}: {type(exc).__name__}: {exc}", flush=True)
    finally:
        client_writer.close()
        await client_writer.wait_closed()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


def _ensure_daemon(daemon_config: Path, log_path: Path) -> bool:
    status = subprocess.run(["dirctl", "daemon", "status"], text=True, capture_output=True, check=False)
    if _daemon_status_is_running(status):
        return True
    if not daemon_config.exists():
        print(f"dirctl daemon config does not exist: {daemon_config}", file=sys.stderr)
        return False
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = ["dirctl", "daemon", "start", "--config", str(daemon_config)]
    with log_path.open("ab") as log_file:
        subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        time.sleep(0.5)
        status = subprocess.run(["dirctl", "daemon", "status"], text=True, capture_output=True, check=False)
        if _daemon_status_is_running(status):
            return True
    return False


def _daemon_status_is_running(status: subprocess.CompletedProcess[str]) -> bool:
    text = (status.stdout + status.stderr).lower()
    return status.returncode == 0 and "daemon is running" in text and "not running" not in text


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _default_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
