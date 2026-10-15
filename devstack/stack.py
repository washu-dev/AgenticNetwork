"""Native dev stack: start, check and stop the whole system with one command.

    python3 -m devstack up        # or scripts/stack up
    python3 -m devstack status --json
    python3 -m devstack smoke
    python3 -m devstack down

Everything runs as local processes from the repository's own venvs; no Docker,
no database server, no SSO, no keys. The services, in start order:

    directory     a file-backed fake ``dirctl`` (devstack/bin/dirctl) shared by all
    mock-llm      deterministic OpenAI-compatible LLM (skipped with --llm openai)
    registration  registration_service, in-memory DB, real supervisor and spawner
    planner       planner_agent, real dirctl backend, no DB
    proxy         dev identity proxy: the browser's front door, signed in as the dev user

then the seed agents (devstack/seeds/*.json) and any agents added with
``add-agent`` are registered and the command waits until the planner lists them.

The tooling treats the system as a black box: it talks to the services only
through their HTTP APIs and the ``dirctl`` CLI, so the same commands can judge a
rebuilt component. Service start commands live in ``_service_specs``.

Exit codes: 0 ok; 1 not ready (status) or something left running (down, reset);
2 usage; 3 preflight failed; 5 a service failed to become ready; 6 smoke failed.

Standard library only (Python >= 3.11).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import fake_dirctl

REPO = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO / "devstack"
SEEDS_DIR = PACKAGE_DIR / "seeds"
DIRCTL_SHIM = PACKAGE_DIR / "bin" / "dirctl"
DEFAULT_STATE_DIR = REPO / ".devstack"
ENV_FILE = REPO / ".devstack.env"
STATE_MARKER = ".devstack-state"
# What `up` and the services create in a state dir; `reset` deletes only these.
STATE_DIRS = ("logs", "pids", "agents", "uploads", "uploaded_handlers")
STATE_FILES = ("directory.json", "directory.json.lock", "stack.json", ".gitignore")

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_USAGE = 2
EXIT_PREFLIGHT = 3
EXIT_SERVICE = 5
EXIT_SMOKE = 6

BASE_PORTS = {"proxy": 8080, "planner": 8090, "registration": 8091, "mock-llm": 8099, "directory": 8888}
AGENT_PORT_START = 19200
AGENT_PORT_END = 19299
PROCESS_SERVICES = ("mock-llm", "registration", "planner", "proxy")
LLM_MODES = ("mock", "openai")
DEFAULT_DEV_USER = "dev@example.test"
DEFAULT_TIMEOUT = 120.0
LOG_TAIL_LINES = 40
# Spawned agents run one of these (registration_service/backend.py); `down` uses
# them to recognise our agent processes in the agent port range.
AGENT_PROCESS_MARKERS = ("registration_service/runner.py", "agent_skeleton.container_runner")
SMOKE_PROMPT = "Echo this back and summarize it: the devstack smoke test reached a specialist agent."
_MOCK_MODEL_PREFIX = "mock-"
# Variables a service may inherit from the caller's shell. Everything else (a stray
# VIRTUAL_ENV, PYTHONHOME, REGISTRATION_DB_DSN, real keys) stays out, so the stack
# has no hidden dependencies. Add more with DEVSTACK_PASSTHROUGH_ENV=A,B.
_PASSTHROUGH_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TZ", "TERM",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "FAKE_DIRCTL_ROUTE_DELAY_SECONDS", "AGENT_SPAWN_HEALTH_TIMEOUT_SECONDS",
)


class StackError(Exception):
    """A failure with an exit code; ``service`` and ``log`` name what broke."""

    def __init__(self, message: str, code: int, *, service: str | None = None, log: Path | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.service = service
        self.log = log


# ── Configuration ─────────────────────────────────────────────────────────────


@dataclass
class StackConfig:
    state_dir: Path
    llm: str = "mock"
    port_offset: int = 0
    dev_user: str = DEFAULT_DEV_USER
    seed: bool = True
    # --llm openai settings. The API key is never written to disk by the stack.
    openai_base_url: str = ""
    planner_model: str = "mock-planner"
    specialist_model: str = "mock-specialist"
    openai_api_key: str = field(default="", repr=False)

    def port(self, service: str) -> int:
        return BASE_PORTS[service] + self.port_offset

    @property
    def agent_ports(self) -> tuple[int, int]:
        return AGENT_PORT_START + self.port_offset, AGENT_PORT_END + self.port_offset

    def url(self, service: str) -> str:
        return f"http://127.0.0.1:{self.port(service)}"

    @property
    def llm_base_url(self) -> str:
        return f"{self.url('mock-llm')}/v1" if self.llm == "mock" else self.openai_base_url

    @property
    def services(self) -> tuple[str, ...]:
        """Service names in start order (``directory`` is a state file, not a process)."""
        procs = PROCESS_SERVICES if self.llm == "mock" else tuple(s for s in PROCESS_SERVICES if s != "mock-llm")
        return ("directory", *procs)

    # Paths inside the state dir.
    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def agent_logs_dir(self) -> Path:
        return self.logs_dir / "agents"

    @property
    def pids_dir(self) -> Path:
        return self.state_dir / "pids"

    @property
    def added_agents_dir(self) -> Path:
        return self.state_dir / "agents"

    @property
    def directory_state(self) -> Path:
        return self.state_dir / "directory.json"

    @property
    def config_file(self) -> Path:
        return self.state_dir / "stack.json"

    def log_file(self, service: str) -> Path:
        return self.logs_dir / f"{service}.log"

    def pid_file(self, service: str) -> Path:
        return self.pids_dir / f"{service}.json"

    def saved(self) -> dict[str, Any]:
        return {
            "llm": self.llm,
            "port_offset": self.port_offset,
            "dev_user": self.dev_user,
            "seed": self.seed,
            "openai_base_url": self.openai_base_url,
            "planner_model": self.planner_model,
            "specialist_model": self.specialist_model,
        }


def state_dir_from(arg: str | None) -> Path:
    raw = arg or os.environ.get("DEVSTACK_STATE_DIR") or str(DEFAULT_STATE_DIR)
    return Path(raw).expanduser().resolve()


def load_saved_config(state_dir: Path) -> StackConfig | None:
    try:
        data = json.loads((state_dir / "stack.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    known = {k: v for k, v in data.items() if k in StackConfig.__dataclass_fields__ and k != "openai_api_key"}
    return StackConfig(state_dir=state_dir, **known)


def config_for_existing(state_dir: Path) -> StackConfig:
    """The config of the stack in ``state_dir`` (defaults if it was never started)."""
    return load_saved_config(state_dir) or StackConfig(state_dir=state_dir)


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; ``#`` comments, ``export`` prefixes and quotes allowed."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def setting(name: str, file_values: dict[str, str]) -> str:
    """The environment wins over .devstack.env."""
    return (os.environ.get(name) or file_values.get(name) or "").strip()


def build_up_config(args: argparse.Namespace) -> StackConfig:
    file_values = read_env_file(ENV_FILE)
    llm = args.llm or setting("DEVSTACK_LLM", file_values) or "mock"
    if llm not in LLM_MODES:
        raise StackError(f"unknown LLM mode {llm!r}; use one of {', '.join(LLM_MODES)}", EXIT_USAGE)
    if args.port_offset < 0 or AGENT_PORT_END + args.port_offset > 65535:
        raise StackError(f"--port-offset {args.port_offset} puts ports out of range", EXIT_USAGE)
    cfg = StackConfig(
        state_dir=state_dir_from(args.state_dir),
        llm=llm,
        port_offset=args.port_offset,
        dev_user=setting("DEV_USER_EMAIL", file_values) or DEFAULT_DEV_USER,
        seed=not args.no_seed,
    )
    if llm == "openai":
        cfg.openai_api_key = setting("OPENAI_API_KEY", file_values)
        cfg.openai_base_url = setting("OPENAI_BASE_URL", file_values)
        cfg.planner_model = setting("MAIN_AGENT_LLM_MODEL", file_values)
        cfg.specialist_model = setting("DEVSTACK_SPECIALIST_MODEL", file_values) or cfg.planner_model
        missing = [
            name
            for name, value in (
                ("OPENAI_BASE_URL", cfg.openai_base_url),
                ("OPENAI_API_KEY", cfg.openai_api_key),
                ("MAIN_AGENT_LLM_MODEL", cfg.planner_model),
            )
            if not value
        ]
        if missing:
            raise StackError(
                f"--llm openai needs {', '.join(missing)}. Set them in the environment or in {ENV_FILE} "
                "(KEY=VALUE lines), for example OPENAI_BASE_URL=https://api.openai.com/v1 "
                "or http://127.0.0.1:11434/v1 for a local Ollama.",
                EXIT_PREFLIGHT,
            )
    return cfg


# ── Small utilities ───────────────────────────────────────────────────────────

# Local calls must never go through an HTTP proxy from the caller's environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_request(
    method: str,
    url: str,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes]:
    """(status, body). Status 0 means no HTTP response (connection refused, timeout)."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    all_headers = dict(headers or {})
    if data is not None:
        all_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, str(getattr(exc, "reason", exc)).encode("utf-8")


def json_or_none(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def short(raw: bytes | str, limit: int = 300) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def tail_lines(path: Path, n: int) -> list[str]:
    """The last ``n`` lines of a file, reading backwards in blocks."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            data = b""
            while end > 0 and data.count(b"\n") <= n:
                step = min(65536, end)
                end -= step
                fh.seek(end)
                data = fh.read(step) + data
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-n:]


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        # In the ephemeral range a connect can land on itself (TCP self-connect).
        if sock.connect_ex(("127.0.0.1", port)) == 0 and sock.getsockname()[1] != port:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def listening_pids(first: int, last: int) -> dict[int, set[int]] | None:
    """{pid: {ports}} for TCP listeners in [first, last]; None if lsof is missing."""
    if not shutil.which("lsof"):
        return None
    span = f"{first}-{last}" if last != first else str(first)
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{span}", "-sTCP:LISTEN", "-Fpn"],
        capture_output=True, text=True, check=False,
    )
    found: dict[int, set[int]] = {}
    pid = None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            port = line.rsplit(":", 1)[-1]
            if port.isdigit():
                found.setdefault(pid, set()).add(int(port))
    return found


def process_command(pid: int) -> str:
    result = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def process_stdout(pid: int) -> Path | None:
    """The file ``pid`` writes its stdout to (via lsof), or None."""
    if not shutil.which("lsof"):
        return None
    result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "1", "-Fn"], capture_output=True, text=True, check=False)
    names = [line[1:] for line in result.stdout.splitlines() if line.startswith("n/")]
    return Path(names[0]) if names else None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def port_holder(port: int) -> str:
    holders = listening_pids(port, port) or {}
    if not holders:
        return "an unknown process"
    pid = sorted(holders)[0]
    return f"pid {pid} ({short(process_command(pid), 120) or '?'})"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ── Services ──────────────────────────────────────────────────────────────────


@dataclass
class ServiceSpec:
    name: str
    command: list[str]
    env: dict[str, str]
    marker: str  # substring of the process command line that proves it is ours
    url: str
    probe: Callable[[], tuple[bool, str]]
    stop_grace: float = 5.0


def planner_python() -> str:
    return os.environ.get("PLANNER_PYTHON") or str(REPO / "planner_agent" / ".venv" / "bin" / "python")


def registration_python() -> str:
    return os.environ.get("REGISTRATION_PYTHON") or str(REPO / "registration_service" / ".venv" / "bin" / "python")


def base_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _PASSTHROUGH_ENV or k.startswith("LC_")}
    for name in os.environ.get("DEVSTACK_PASSTHROUGH_ENV", "").split(","):
        name = name.strip()
        if name and name in os.environ:
            env[name] = os.environ[name]
    # The fake dirctl first on PATH, so scripts that hard-code `dirctl` use it too.
    env["PATH"] = os.pathsep.join([str(DIRCTL_SHIM.parent), env.get("PATH") or os.defpath])
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = ",".join(filter(None, [env.get(key, ""), "127.0.0.1,localhost"]))
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(REPO)
    return env


def directory_env(cfg: StackConfig) -> dict[str, str]:
    return {
        "DIRCTL_BIN": str(DIRCTL_SHIM),
        "FAKE_DIRCTL_STATE": str(cfg.directory_state),
        "FAKE_DIRCTL_LOG": str(cfg.log_file("directory")),
        "ADS_SERVER_ADDR": f"127.0.0.1:{cfg.port('directory')}",
    }


def llm_env(cfg: StackConfig) -> dict[str, str]:
    if cfg.llm == "mock":
        return {"OPENAI_BASE_URL": cfg.llm_base_url, "OPENAI_API_KEY": "mock-key"}
    return {"OPENAI_BASE_URL": cfg.openai_base_url, "OPENAI_API_KEY": cfg.openai_api_key}


def _service_specs(cfg: StackConfig) -> dict[str, ServiceSpec]:
    first_agent_port, last_agent_port = cfg.agent_ports
    specs = {
        "mock-llm": ServiceSpec(
            name="mock-llm",
            command=[sys.executable, "-m", "devstack.mock_llm", "--host", "127.0.0.1", "--port", str(cfg.port("mock-llm"))],
            env={**base_env(), "MOCK_LLM_LOG": str(cfg.logs_dir / "mock-llm.requests.jsonl")},
            marker="devstack.mock_llm",
            url=cfg.llm_base_url,
            probe=lambda: probe_http(f"{cfg.url('mock-llm')}/v1/models"),
        ),
        "registration": ServiceSpec(
            name="registration",
            command=[registration_python(), "-m", "registration_service.server"],
            env={
                **base_env(),
                **directory_env(cfg),
                **llm_env(cfg),
                "FAKE_DB": "1",
                "FAKE_SUPERVISOR": "0",
                "REGISTRATION_SERVICE_HOST": "127.0.0.1",
                "REGISTRATION_SERVICE_PORT": str(cfg.port("registration")),
                "AGENT_BIND_HOST": "127.0.0.1",  # spawned agents inherit this env
                "CUSTOM_AGENT_PORT_START": str(first_agent_port),
                "CUSTOM_AGENT_PORT_END": str(last_agent_port),
                "AGENT_LOG_DIR": str(cfg.agent_logs_dir),
                "HANDLER_FILES_DIR": str(cfg.state_dir / "uploaded_handlers"),
                "ADMIN_EMAILS": cfg.dev_user,
            },
            marker="registration_service.server",
            url=cfg.url("registration"),
            probe=lambda: probe_ready(cfg.url("registration")),
            # Registration terminates its spawned agents on shutdown; give it time.
            stop_grace=15.0,
        ),
        "planner": ServiceSpec(
            name="planner",
            command=[
                planner_python(), "-m", "planner_agent.scripts.run_server",
                "--host", "127.0.0.1", "--port", str(cfg.port("planner")),
            ],
            env={
                **base_env(),
                **directory_env(cfg),
                **llm_env(cfg),
                "AGENT_NETWORK_FAKE_ADS": "false",
                "MAIN_AGENT_LLM_MODEL": cfg.planner_model,
                "REGISTRATION_SERVICE_URL": cfg.url("registration"),
                "MAIN_AGENT_UPLOAD_DIR": str(cfg.state_dir / "uploads"),
                "ADMIN_EMAILS": cfg.dev_user,
            },
            marker="planner_agent.scripts.run_server",
            url=cfg.url("planner"),
            probe=lambda: probe_ready(cfg.url("planner")),
        ),
        "proxy": ServiceSpec(
            name="proxy",
            command=[
                sys.executable, "-m", "devstack.dev_identity_proxy", "--host", "127.0.0.1",
                "--port", str(cfg.port("proxy")), "--upstream", cfg.url("planner"), "--user", cfg.dev_user,
            ],
            env=base_env(),
            marker="devstack.dev_identity_proxy",
            url=cfg.url("proxy"),
            probe=lambda: probe_identity(cfg),
        ),
    }
    return {name: specs[name] for name in cfg.services if name in specs}


def probe_http(url: str) -> tuple[bool, str]:
    status, body = http_request("GET", url, timeout=3.0)
    return status == 200, f"GET {url} -> {status or short(body)}"


def probe_ready(base: str) -> tuple[bool, str]:
    """GET /ready (200 ready, 503 not ready); fall back to /health if /ready is absent."""
    status, body = http_request("GET", f"{base}/ready", timeout=8.0)
    if status == 404:
        status, body = http_request("GET", f"{base}/health", timeout=3.0)
        return status == 200, f"GET /health -> {status or short(body)} (no /ready endpoint)"
    if status == 200:
        return True, "GET /ready -> 200"
    payload = json_or_none(body)
    checks = payload.get("checks") if isinstance(payload, dict) else None
    detail = json.dumps(checks, sort_keys=True) if checks else short(body)
    return False, f"GET /ready -> {status or 'no response'}: {detail}"


def probe_identity(cfg: StackConfig) -> tuple[bool, str]:
    status, body = http_request("GET", f"{cfg.url('proxy')}/me", timeout=5.0)
    payload = json_or_none(body)
    email = payload.get("email") if isinstance(payload, dict) else None
    if status == 200 and isinstance(email, str) and email.lower() == cfg.dev_user.lower():
        return True, f"GET /me via proxy -> {email}"
    return False, f"GET /me via proxy -> {status or 'no response'}: {short(body)}"


def probe_directory(cfg: StackConfig) -> tuple[bool, str]:
    if not cfg.directory_state.exists():
        return False, "directory state file missing"
    env = {**base_env(), **directory_env(cfg)}
    command = [str(DIRCTL_SHIM), "--server-addr", env["ADS_SERVER_ADDR"], "routing", "list", "--output", "json"]
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"dirctl routing list failed: {exc}"
    if result.returncode != 0:
        return False, f"dirctl routing list exited {result.returncode}: {short(result.stderr)}"
    items = json_or_none(result.stdout.encode())
    count = len(items) if isinstance(items, list) else "?"
    return True, f"{count} routed record(s)"


# ── Process management ────────────────────────────────────────────────────────


def read_pid_file(cfg: StackConfig, name: str) -> dict[str, Any] | None:
    try:
        return json.loads(cfg.pid_file(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def service_pid(cfg: StackConfig, name: str, marker: str) -> int | None:
    """The pid of our running ``name`` service, or None (stale pid files are removed)."""
    info = read_pid_file(cfg, name)
    if not info:
        return None
    pid = int(info.get("pid") or 0)
    if pid > 0 and process_alive(pid) and marker in process_command(pid):
        return pid
    with contextlib.suppress(OSError):
        cfg.pid_file(name).unlink()
    return None


def start_service(cfg: StackConfig, spec: ServiceSpec) -> subprocess.Popen:
    log_path = cfg.log_file(spec.name)
    with log_path.open("ab") as log:
        log.write(f"\n===== devstack: starting {spec.name} at {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode())
        log.flush()
        try:
            proc = subprocess.Popen(
                spec.command,
                cwd=REPO,
                env=spec.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise StackError(f"could not start {spec.name}: {exc}", EXIT_SERVICE, service=spec.name, log=log_path)
    cfg.pid_file(spec.name).write_text(
        json.dumps(
            {"pid": proc.pid, "pgid": proc.pid, "command": spec.command, "started_at": time.time(), "url": spec.url}
        ),
        encoding="utf-8",
    )
    return proc


def stop_pid(pid: int, *, group: bool, grace: float) -> bool:
    """SIGTERM (the group), wait up to ``grace``, then SIGKILL. True if it is gone."""
    def send(sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if group:
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)

    send(signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not process_alive(pid) or _reap(pid):
            return True
        time.sleep(0.1)
    send(signal.SIGKILL)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not process_alive(pid) or _reap(pid):
            return True
        time.sleep(0.1)
    return False


def _reap(pid: int) -> bool:
    """Reap ``pid`` if it is our own exited child (a zombie still answers kill 0)."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    return done == pid


def stop_service(cfg: StackConfig, spec: ServiceSpec) -> str | None:
    """Stop one service; returns a problem description or None.

    A service that does not exit keeps its pid file, so a later `down` can retry."""
    pid = service_pid(cfg, spec.name, spec.marker)
    if pid is not None and not stop_pid(pid, group=True, grace=spec.stop_grace):
        return f"{spec.name} (pid {pid}) did not exit after SIGKILL; kept {cfg.pid_file(spec.name)}"
    with contextlib.suppress(OSError):
        cfg.pid_file(spec.name).unlink()
    return None


def stray_services(cfg: StackConfig, name: str, marker: str) -> tuple[list[int], list[dict[str, Any]]]:
    """Devstack ``name`` services on our port that no pid file here names: (ours, foreign).

    Ours log into this state dir (their pid file was lost); ``down`` stops them. Foreign
    ones were started from another state dir (or by hand) and are never stopped here:
    [{service, pid, port, state_dir, stop_command}], ``state_dir`` None when unknown."""
    port = cfg.port(name)
    named = (read_pid_file(cfg, name) or {}).get("pid")
    ours: list[int] = []
    foreign: list[dict[str, Any]] = []
    for pid in sorted(listening_pids(port, port) or {}):
        if pid == named or marker not in process_command(pid):
            continue
        log = process_stdout(pid)
        if log == cfg.log_file(name):
            ours.append(pid)
            continue
        other = log.parent.parent if log is not None and log.name == f"{name}.log" else None
        if other is not None and not (other / STATE_MARKER).exists():
            other = None  # not a state dir: `down --state-dir` there would stop nothing
        foreign.append({
            "service": name,
            "pid": pid,
            "port": port,
            "state_dir": str(other) if other else None,
            "stop_command": f"scripts/stack down --state-dir {shlex.quote(str(other))}" if other else f"kill {pid}",
        })
    return ours, foreign


def sweep_agents(cfg: StackConfig) -> list[str]:
    """Stop this stack's agents that still listen in its agent port range.

    An agent is ours when it runs an agent entry point and writes to this state dir's
    agent logs; anything else there (another stack's agents) is left alone.
    Returns descriptions of our agents still listening afterwards."""
    first, last = cfg.agent_ports
    holders = listening_pids(first, last)
    if holders is None:
        return ["lsof not found: install lsof; down and reset need it to check for leftover agents"]
    ours = {
        pid for pid in holders
        if any(marker in process_command(pid) for marker in AGENT_PROCESS_MARKERS)
        and getattr(process_stdout(pid), "parent", None) == cfg.agent_logs_dir
    }
    for pid in sorted(ours):
        stop_pid(pid, group=False, grace=3.0)
    problems = []
    for pid, ports in sorted((listening_pids(first, last) or {}).items()):
        line = f"pid {pid} listening on {sorted(ports)}: {short(process_command(pid), 160)}"
        if pid in ours:
            problems.append(line)
        else:
            print(f"devstack: not this stack's, left alone: {line}", file=sys.stderr)
    return problems


# ── Agents (seeds and add-agent) ──────────────────────────────────────────────


def seed_files() -> list[Path]:
    return sorted(SEEDS_DIR.glob("*.json"))


def expected_agent_files(cfg: StackConfig) -> list[Path]:
    added = sorted(cfg.added_agents_dir.glob("*.json")) if cfg.added_agents_dir.exists() else []
    return (seed_files() if cfg.seed else []) + added


def load_agent_body(path: Path, cfg: StackConfig) -> dict[str, Any]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StackError(f"cannot read agent definition {path}: {exc}", EXIT_USAGE)
    if not isinstance(body, dict) or not isinstance(body.get("name"), str) or not body["name"].strip():
        raise StackError(f"{path}: expected a registration body (a JSON object with a name)", EXIT_USAGE)
    config = body.get("config")
    if cfg.llm != "mock" and isinstance(config, dict) and str(config.get("model", "")).startswith(_MOCK_MODEL_PREFIX):
        body = {**body, "config": {**config, "model": cfg.specialist_model}}
    return body


def registered_agents(cfg: StackConfig) -> list[dict[str, Any]]:
    status, body = http_request("GET", f"{cfg.url('registration')}/agents", timeout=10.0)
    payload = json_or_none(body)
    return payload if status == 200 and isinstance(payload, list) else []


def planner_agents(cfg: StackConfig) -> list[dict[str, Any]] | None:
    status, body = http_request("GET", f"{cfg.url('planner')}/agents", timeout=15.0)
    payload = json_or_none(body)
    if status != 200 or not isinstance(payload, dict):
        return None
    return [a for a in payload.get("agents") or [] if isinstance(a, dict)]


def register_agent(cfg: StackConfig, body: dict[str, Any]) -> dict[str, Any]:
    """POST /agents; returns the AgentResponse (or the existing agent on 409)."""
    name = body["name"]
    status, raw = http_request("POST", f"{cfg.url('registration')}/agents", body=body, timeout=120.0)
    payload = json_or_none(raw)
    if status == 409:
        for agent in registered_agents(cfg):
            if str(agent.get("name", "")).lower() == name.lower():
                return agent
        raise StackError(f"registering {name!r}: 409 {short(raw)}", EXIT_SERVICE, service="agents")
    if status != 201 or not isinstance(payload, dict):
        raise StackError(
            f"registering {name!r}: POST /agents -> {status or 'no response'}: {short(raw, 600)}",
            EXIT_SERVICE, service="agents", log=cfg.log_file("registration"),
        )
    if not payload.get("ads_cid"):
        raise StackError(
            f"registered {name!r} (status {payload.get('status')!r}) but it was not published to the "
            "directory (ads_cid is null), so the planner cannot discover it",
            EXIT_SERVICE, service="agents", log=cfg.log_file("registration"),
        )
    return payload


def agent_answers(endpoint: str) -> bool:
    """True if the agent at ``endpoint`` serves its card (reached on 127.0.0.1)."""
    parts = urllib.parse.urlsplit(endpoint)
    if not parts.port:
        return False
    base = parts._replace(netloc=f"127.0.0.1:{parts.port}").geturl().rstrip("/")
    return http_request("GET", f"{base}/.well-known/agent-card.json", timeout=2.0)[0] == 200


def unreachable_agents(listed: list[dict[str, Any]], names: list[str]) -> list[str]:
    """Those of ``names`` the planner lists whose endpoint does not answer."""
    wanted = {n.lower() for n in names}
    return [
        str(a.get("name")) for a in listed
        if str(a.get("name", "")).lower() in wanted and not agent_answers(str(a.get("endpoint") or ""))
    ]


def wait_until_listed(cfg: StackConfig, names: list[str], deadline: float) -> list[dict[str, Any]]:
    """Wait until the planner's GET /agents lists every name and each answers; return its list."""
    wanted = {n.lower() for n in names}
    last: list[str] = []
    silent: list[str] = []
    while True:
        agents = planner_agents(cfg)
        if agents is not None:
            last = [str(a.get("name", "")) for a in agents]
            if wanted <= {n.lower() for n in last}:
                silent = unreachable_agents(agents, names)
                if not silent:
                    return agents
        if time.monotonic() >= deadline:
            missing = sorted(wanted - {n.lower() for n in last})
            raise StackError(
                f"the planner does not list {missing} (it lists {last or 'nothing'})" if missing
                else f"the planner lists {silent} but they do not answer at their endpoints",
                EXIT_SERVICE, service="agents", log=cfg.log_file("planner" if missing else "registration"),
            )
        time.sleep(0.5)


def restart_agent(cfg: StackConfig, agent: dict[str, Any]) -> None:
    status, raw = http_request("POST", f"{cfg.url('registration')}/agents/{agent.get('id')}/restart", timeout=120.0)
    if status != 200:
        raise StackError(
            f"restarting {agent.get('name')!r}: {status or 'no response'}: {short(raw, 600)}",
            EXIT_SERVICE, service="agents", log=cfg.log_file("registration"),
        )


def ensure_agents(cfg: StackConfig, deadline: float) -> list[str]:
    files = expected_agent_files(cfg)
    bodies = [load_agent_body(path, cfg) for path in files]
    existing = {str(a.get("name", "")).lower(): a for a in registered_agents(cfg)}
    for body in bodies:
        agent = existing.get(body["name"].lower())
        if agent is None:
            print(f"devstack: registering agent {body['name']!r}", file=sys.stderr)
            register_agent(cfg, body)
        elif agent.get("status") != "live":
            print(f"devstack: restarting agent {body['name']!r} (status {agent.get('status')!r})", file=sys.stderr)
            restart_agent(cfg, agent)
    names = [b["name"] for b in bodies]
    if names:
        wait_until_listed(cfg, names, deadline)
    return names


# ── Status ────────────────────────────────────────────────────────────────────


def collect_status(cfg: StackConfig) -> dict[str, Any]:
    specs = _service_specs(cfg)
    services: dict[str, Any] = {}
    all_ready = True
    for name in cfg.services:
        if name == "directory":
            running = cfg.directory_state.exists()
            pid = None
            url = f"file://{cfg.directory_state}"
            probe = lambda: probe_directory(cfg)  # noqa: E731
        else:
            spec = specs[name]
            pid = service_pid(cfg, name, spec.marker)
            running = pid is not None
            url = spec.url
            probe = spec.probe
        ready, detail = probe() if running else (False, "not running")
        all_ready = all_ready and ready
        services[name] = {
            "pid": pid,
            "running": running,
            "ready": ready,
            "url": url,
            "log": str(cfg.log_file(name)),
            "detail": detail,
        }
    listed = planner_agents(cfg) if services.get("planner", {}).get("ready") else None
    agents = [str(a.get("name", "")) for a in listed or []]
    expected = []
    for path in expected_agent_files(cfg):
        with contextlib.suppress(StackError):
            expected.append(load_agent_body(path, cfg)["name"])
    listed_lower = {a.lower() for a in agents}
    missing = [n for n in expected if n.lower() not in listed_lower]
    unreachable = unreachable_agents(listed or [], expected)
    return {
        "ready": all_ready and not missing and not unreachable,
        "mode": "native",
        "llm": cfg.llm,
        "llm_base_url": cfg.llm_base_url,
        "state_dir": str(cfg.state_dir),
        "dev_user": cfg.dev_user,
        "chat_url": f"http://localhost:{cfg.port('proxy')}/app",
        "agent_ports": list(cfg.agent_ports),
        "services": services,
        "agents": agents,
        "missing_agents": missing,
        "unreachable_agents": unreachable,
    }


def print_status(status: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(status, indent=2))
        return
    verdict = "READY" if status["ready"] else "NOT READY"
    print(f"devstack ({status['mode']}, llm={status['llm']}): {verdict}")
    print(f"  state dir: {status['state_dir']}")
    print(f"  {'SERVICE':<13} {'PID':>7}  {'RUNNING':<8} {'READY':<6} URL")
    for name, svc in status["services"].items():
        pid = svc["pid"] if svc["pid"] is not None else "-"
        print(
            f"  {name:<13} {pid:>7}  {'yes' if svc['running'] else 'no':<8} "
            f"{'yes' if svc['ready'] else 'no':<6} {svc['url']}"
        )
        if not svc["ready"]:
            print(f"  {'':<13} {'':>7}  -> {svc['detail']}")
    print(f"  agents: {', '.join(status['agents']) or '(none listed)'}")
    if status["missing_agents"]:
        print(f"  missing agents: {', '.join(status['missing_agents'])}")
    if status["unreachable_agents"]:
        print(f"  agents not answering: {', '.join(status['unreachable_agents'])}")
    if status["ready"]:
        print(f"  chat UI: {status['chat_url']}  (signed in as {status['dev_user']})")


# ── Commands ──────────────────────────────────────────────────────────────────


def ensure_state_dir(cfg: StackConfig) -> None:
    for path in (cfg.state_dir, cfg.logs_dir, cfg.agent_logs_dir, cfg.pids_dir, cfg.state_dir / "uploads"):
        path.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / STATE_MARKER).touch()
    # Keep the state dir out of git without touching the repository's .gitignore.
    ignore = cfg.state_dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# devstack runtime state\n*\n", encoding="utf-8")


def preflight(cfg: StackConfig, running: dict[str, int | None]) -> list[str]:
    problems = []
    if sys.version_info < (3, 11):
        problems.append(f"devstack needs Python >= 3.11; this is {sys.version.split()[0]} ({sys.executable})")
    for label, path in (("planner", planner_python()), ("registration", registration_python())):
        if not os.access(path, os.X_OK):
            problems.append(
                f"{label} interpreter not found: {path}. Create the venv (see docs/DEVSTACK.md) "
                f"or set {label.upper()}_PYTHON."
            )
    if not os.access(DIRCTL_SHIM, os.X_OK):
        problems.append(f"fake dirctl is not executable: {DIRCTL_SHIM} (chmod +x it)")
    for name in cfg.services:
        if name == "directory" or running.get(name):
            continue
        port = cfg.port(name)
        if port_in_use(port):
            problems.append(
                f"port {port} ({name}) is in use by {port_holder(port)}. Stop it, or pick other ports "
                "with --port-offset N."
            )
    return problems


def cmd_up(args: argparse.Namespace) -> int:
    cfg = build_up_config(args)
    if cfg.state_dir.exists() and not (cfg.state_dir / STATE_MARKER).exists() and (
        not cfg.state_dir.is_dir() or any(cfg.state_dir.iterdir())
    ):
        raise StackError(
            f"refusing to use {cfg.state_dir} as a state dir: it exists, is not empty and has no "
            f"{STATE_MARKER} marker. Pick a new or empty dir.",
            EXIT_USAGE,
        )
    saved = load_saved_config(cfg.state_dir)
    specs = _service_specs(cfg)
    probe_cfg = saved or cfg
    running = {
        name: service_pid(probe_cfg, name, spec.marker) for name, spec in _service_specs(probe_cfg).items()
    }
    if any(running.values()) and saved is not None:
        changed = [k for k in ("llm", "port_offset", "dev_user") if getattr(saved, k) != getattr(cfg, k)]
        if changed:
            raise StackError(
                f"the stack in {cfg.state_dir} is running with "
                + ", ".join(f"{k}={getattr(saved, k)!r}" for k in changed)
                + "; run `down` first to change that",
                EXIT_PREFLIGHT,
            )
    problems = preflight(cfg, running)
    if problems:
        raise StackError("preflight failed:\n  - " + "\n  - ".join(problems), EXIT_PREFLIGHT)

    ensure_state_dir(cfg)
    cfg.config_file.write_text(json.dumps(cfg.saved(), indent=2) + "\n", encoding="utf-8")
    deadline = time.monotonic() + args.timeout
    started: list[tuple[ServiceSpec, subprocess.Popen]] = []
    try:
        if not running.get("registration"):
            # A fresh registration service has an empty in-memory registry, so any
            # agents and directory records from an earlier run are stale.
            sweep_agents(cfg)
            fake_dirctl.init_state(str(cfg.directory_state))
        elif not cfg.directory_state.exists():
            fake_dirctl.init_state(str(cfg.directory_state))
        wait_ready(cfg, "directory", lambda: probe_directory(cfg), None, deadline)
        for name, spec in specs.items():
            proc = None
            if not running.get(name):
                print(f"devstack: starting {name}", file=sys.stderr)
                proc = start_service(cfg, spec)
                started.append((spec, proc))
            wait_ready(cfg, name, spec.probe, proc, deadline, marker=spec.marker)
        ensure_agents(cfg, deadline)
    except (StackError, KeyboardInterrupt):
        if started and not args.keep_on_failure:
            for spec, _ in reversed(started):
                stop_service(cfg, spec)
            if any(spec.name == "registration" for spec, _ in started):
                sweep_agents(cfg)
        raise
    status = collect_status(cfg)
    print_status(status, as_json=args.json)
    return EXIT_OK if status["ready"] else EXIT_SERVICE


def wait_ready(
    cfg: StackConfig,
    name: str,
    probe: Callable[[], tuple[bool, str]],
    proc: subprocess.Popen | None,
    deadline: float,
    *,
    marker: str | None = None,
) -> None:
    detail = ""
    while True:
        if proc is not None and proc.poll() is not None:
            raise StackError(
                f"{name} exited with code {proc.returncode} before it was ready",
                EXIT_SERVICE, service=name, log=cfg.log_file(name),
            )
        if proc is None and marker is not None and service_pid(cfg, name, marker) is None:
            raise StackError(f"{name} stopped while waiting for it", EXIT_SERVICE, service=name, log=cfg.log_file(name))
        ready, detail = probe()
        if ready:
            return
        if time.monotonic() >= deadline:
            raise StackError(
                f"{name} was not ready before the timeout: {detail}",
                EXIT_SERVICE, service=name, log=cfg.log_file(name),
            )
        time.sleep(0.3)


def cmd_down(args: argparse.Namespace) -> int:
    state_dir = state_dir_from(args.state_dir)
    saved = load_saved_config(state_dir)
    cfg = saved or StackConfig(state_dir=state_dir)
    specs = _service_specs(cfg)
    problems = []
    for name, spec in reversed(list(specs.items())):
        if service_pid(cfg, name, spec.marker) is not None:
            print(f"devstack: stopping {name}", file=sys.stderr)
        problem = stop_service(cfg, spec)
        if problem:
            problems.append(problem)
    foreign: list[dict[str, Any]] = []
    if saved is None:
        # Without stack.json the ports are unknown, so touch nothing a pid file did not name.
        print(f"devstack: no stack recorded in {state_dir} (no stack.json); stopped only what its pid files named",
              file=sys.stderr)
        return finish_down(state_dir, problems, foreign, as_json=getattr(args, "json", False))
    for name, spec in reversed(list(specs.items())):
        ours, others = stray_services(cfg, name, spec.marker)
        foreign.extend(others)
        for pid in ours:
            print(f"devstack: stopping {name} (pid {pid}), found by its log in {state_dir}", file=sys.stderr)
            with contextlib.suppress(ProcessLookupError):
                if not stop_pid(pid, group=os.getpgid(pid) == pid, grace=spec.stop_grace):
                    problems.append(f"{name} (pid {pid}) did not exit after SIGKILL")
    problems.extend(sweep_agents(cfg))
    # The registry was in memory; its directory records point at stopped agents.
    with contextlib.suppress(FileNotFoundError):
        cfg.directory_state.unlink()
    for holder in foreign:
        owner = f"of the stack in {holder['state_dir']}" if holder["state_dir"] else "(started outside any state dir)"
        problems.append(
            f"port {holder['port']} ({holder['service']}) is held by pid {holder['pid']} {owner}; "
            f"left alone. To stop it: {holder['stop_command']}"
        )
    held = {holder["port"] for holder in foreign}
    for name in cfg.services:
        if name != "directory" and cfg.port(name) not in held and port_in_use(cfg.port(name)):
            problems.append(f"port {cfg.port(name)} ({name}) is still in use by {port_holder(cfg.port(name))}")
    return finish_down(state_dir, problems, foreign, as_json=getattr(args, "json", False))


def finish_down(state_dir: Path, problems: list[str], foreign: list[dict[str, Any]], *, as_json: bool) -> int:
    """Report what `down` left running (as JSON too with --json); returns its exit code."""
    if as_json:
        print(json.dumps({"ok": not problems, "state_dir": str(state_dir), "left_running": problems,
                          "foreign": foreign}, indent=2))
    if problems:
        print("devstack: left running:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return EXIT_NOT_READY
    print("devstack: stopped", file=sys.stderr)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config_for_existing(state_dir_from(args.state_dir))
    status = collect_status(cfg)
    print_status(status, as_json=args.json)
    return EXIT_OK if status["ready"] else EXIT_NOT_READY


def resolve_log(cfg: StackConfig, target: str) -> Path:
    known = {*PROCESS_SERVICES, "directory"}
    if target in known:
        return cfg.log_file(target)
    if target == "mock-llm-requests":
        return cfg.logs_dir / "mock-llm.requests.jsonl"
    direct = cfg.agent_logs_dir / f"{target}.log"
    if direct.exists():
        return direct
    for agent in registered_agents(cfg):
        if target.lower() in {str(agent.get("name", "")).lower(), slug(str(agent.get("name", "")))}:
            return cfg.agent_logs_dir / f"{agent.get('id')}.log"
    agent_logs = sorted(p.stem for p in cfg.agent_logs_dir.glob("*.log")) if cfg.agent_logs_dir.exists() else []
    raise StackError(
        f"unknown log {target!r}. Services: {', '.join(sorted(known))}, mock-llm-requests; "
        f"or an agent name or id (agent logs: {', '.join(agent_logs) or 'none'})",
        EXIT_USAGE,
    )


def cmd_logs(args: argparse.Namespace) -> int:
    cfg = config_for_existing(state_dir_from(args.state_dir))
    path = resolve_log(cfg, args.service)
    if not path.exists():
        raise StackError(f"no log yet at {path}", EXIT_NOT_READY)
    for line in tail_lines(path, args.lines):
        print(line)
    if not args.follow:
        return EXIT_OK
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        try:
            while True:
                chunk = fh.read()
                if chunk:
                    sys.stdout.write(chunk.decode("utf-8", "replace"))
                    sys.stdout.flush()
                elif path.exists() and path.stat().st_size < fh.tell():
                    fh.seek(0)  # truncated
                else:
                    time.sleep(0.25)
        except KeyboardInterrupt:
            return EXIT_OK


def cmd_reset(args: argparse.Namespace) -> int:
    state_dir = state_dir_from(args.state_dir)
    # Check before stopping anything: a wrong --state-dir must change nothing.
    if state_dir.exists() and (
        state_dir in {REPO, Path.home().resolve(), Path("/")}
        or (not (state_dir / STATE_MARKER).exists() and any(state_dir.iterdir()))
    ):
        raise StackError(
            f"refusing to delete {state_dir}: it is not a devstack state dir (no {STATE_MARKER} marker)",
            EXIT_USAGE,
        )
    code = cmd_down(args)
    if code != EXIT_OK:
        # Keep the config, pid files and logs: they are how a later `down` finds what is left.
        print(f"devstack: deleted nothing in {state_dir} because down left something running; "
              "stop what is listed above, then run reset again", file=sys.stderr)
        return code
    if state_dir.exists():
        remove_state(state_dir)
    return code


def remove_state(state_dir: Path) -> None:
    """Delete what the stack created in ``state_dir``, then the dir if that empties it."""
    for name in STATE_DIRS:
        shutil.rmtree(state_dir / name, ignore_errors=True)
    for path in [*(state_dir / name for name in STATE_FILES), *state_dir.glob(".fake-dirctl-*")]:
        with contextlib.suppress(OSError):
            path.unlink()
    left = sorted(p.name for p in state_dir.iterdir() if p.name != STATE_MARKER)
    if left:  # keep the marker so the dir stays usable by `up` and `reset`
        print(f"devstack: cleared {state_dir}; kept what the stack did not create: {', '.join(left)}", file=sys.stderr)
    else:
        (state_dir / STATE_MARKER).unlink(missing_ok=True)
        state_dir.rmdir()
        print(f"devstack: removed {state_dir}", file=sys.stderr)


def cmd_smoke(args: argparse.Namespace) -> int:
    cfg = config_for_existing(state_dir_from(args.state_dir))
    proxy = cfg.url("proxy")
    result: dict[str, Any] = {"ok": False, "prompt": args.prompt}

    def fail(reason: str) -> int:
        result["error"] = reason
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"devstack smoke: FAILED: {reason}", file=sys.stderr)
        return EXIT_SMOKE

    status, raw = http_request("POST", f"{proxy}/messages", body={"text": args.prompt, "channel": "api"}, timeout=args.timeout)
    payload = json_or_none(raw)
    if status != 200 or not isinstance(payload, dict):
        return fail(f"POST /messages via proxy -> {status or 'no response'}: {short(raw)}")
    text = str(payload.get("text") or "")
    tools = [str(step.get("tool")) for step in payload.get("tool_trace") or [] if isinstance(step, dict)]
    result.update(
        {
            "state": payload.get("state"),
            "answer": text,
            "selected_agent": payload.get("selected_agent"),
            "context_id": payload.get("context_id"),
            "tools": tools,
        }
    )
    if payload.get("state") != "completed":
        return fail(f"planner state is {payload.get('state')!r}, not 'completed': {short(text)}")
    if "call_agent" not in tools or not payload.get("selected_agent"):
        return fail(f"the planner answered without calling an agent (tools: {tools})")
    if cfg.llm == "mock" and "[mock-llm]" not in text:
        return fail(f"the answer lacks the specialist's [mock-llm] marker: {short(text)}")
    status, raw = http_request("GET", f"{proxy}/conversations/{payload.get('context_id')}", timeout=10.0)
    history = json_or_none(raw)
    messages = history.get("messages") if isinstance(history, dict) else None
    if status != 200 or not messages:
        return fail(f"the conversation was not saved: GET /conversations -> {status}: {short(raw)}")
    result.update({"ok": True, "history_messages": len(messages)})
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"agent: {result['selected_agent']}")
        print(f"answer: {text}")
    return EXIT_OK


def cmd_add_agent(args: argparse.Namespace) -> int:
    cfg = config_for_existing(state_dir_from(args.state_dir))
    path = Path(args.definition).expanduser()
    body = load_agent_body(path, cfg)
    for name in ("registration", "planner"):
        ready, detail = _service_specs(cfg)[name].probe()
        if not ready:
            raise StackError(f"{name} is not ready ({detail}); run `up` first", EXIT_PREFLIGHT)
    agent = register_agent(cfg, body)
    listed = wait_until_listed(cfg, [body["name"]], time.monotonic() + args.timeout)
    planner_entry = next(a for a in listed if str(a.get("name", "")).lower() == body["name"].lower())
    cfg.added_agents_dir.mkdir(parents=True, exist_ok=True)
    saved = cfg.added_agents_dir / f"{slug(body['name'])}.json"
    saved.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    summary = {
        "name": body["name"],
        "id": agent.get("id"),
        "planner_agent_id": planner_entry.get("agent_id"),
        "status": agent.get("status"),
        "endpoint_url": agent.get("endpoint_url"),
        "ads_cid": agent.get("ads_cid"),
        "listed_by_planner": True,
        "saved_to": str(saved),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"devstack: {body['name']!r} is registered and listed by the planner "
              f"(id {summary['id']}, endpoint {summary['endpoint_url']}).")
        print(f"  It is re-registered on every `up` until you delete {saved} or run `reset`.")
    return EXIT_OK


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def venv_report(python: str) -> dict[str, Any]:
    code = (
        "import json, sys, importlib.metadata as m\n"
        "def v(p):\n"
        "    try: return m.version(p)\n"
        "    except m.PackageNotFoundError: return None\n"
        "print(json.dumps({'python': sys.version.split()[0], "
        "'packages': {p: v(p) for p in ('a2a-sdk', 'fastapi', 'uvicorn', 'httpx', 'openai', 'pytest')}}))"
    )
    try:
        result = subprocess.run([python, "-I", "-c", code], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    payload = json_or_none(result.stdout.encode())
    if result.returncode != 0 or not isinstance(payload, dict):
        return {"ok": False, "error": short(result.stderr or result.stdout)}
    return {"ok": True, **payload}


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = config_for_existing(state_dir_from(args.state_dir))
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str, *, warn: bool = False) -> None:
        checks.append({"check": name, "ok": ok, "level": "ok" if ok else ("warn" if warn else "fail"), "detail": detail})

    check("python3", sys.version_info >= (3, 11), f"{sys.version.split()[0]} at {sys.executable} (needs >= 3.11)")
    for label, python, requirements in (
        ("planner venv", planner_python(), REPO / "planner_agent" / "requirements.txt"),
        ("registration venv", registration_python(), REPO / "registration_service" / "requirements.txt"),
    ):
        if not os.access(python, os.X_OK):
            check(label, False, f"missing: {python}")
            continue
        report = venv_report(python)
        if not report.get("ok"):
            check(label, False, f"{python}: {report.get('error')}")
            continue
        packages = report["packages"]
        missing = [p for p in ("a2a-sdk", "fastapi", "uvicorn", "httpx", "openai") if not packages.get(p)]
        detail = f"Python {report['python']}, a2a-sdk {packages.get('a2a-sdk')}, " + ", ".join(
            f"{p} {packages.get(p)}" for p in ("fastapi", "uvicorn", "openai")
        )
        check(label, not missing, detail + (f"; missing {missing}" if missing else ""))
        pin = re.search(r"^a2a-sdk(?:\[[^\]]*\])?==(\S+)", _read_text(requirements), re.MULTILINE)
        if pin and packages.get("a2a-sdk") and packages["a2a-sdk"] != pin.group(1):
            check(f"{label} a2a-sdk pin", False, f"has {packages['a2a-sdk']}, {requirements.name} pins {pin.group(1)}", warn=True)
    try:
        result = subprocess.run(
            [str(DIRCTL_SHIM), "version"], env=base_env(), capture_output=True, text=True, timeout=10, check=False
        )
        check("fake dirctl", result.returncode == 0, short(result.stdout or result.stderr))
    except OSError as exc:
        check("fake dirctl", False, str(exc))
    check("lsof", shutil.which("lsof") is not None, shutil.which("lsof") or "not found (down and reset need it to find leftover processes)")
    for name in cfg.services:
        if name == "directory":
            continue
        spec = _service_specs(cfg)[name]
        port = cfg.port(name)
        if service_pid(cfg, name, spec.marker) is not None:
            check(f"port {port} ({name})", True, "in use by the running devstack service")
        elif port_in_use(port):
            check(f"port {port} ({name})", False, f"in use by {port_holder(port)}")
        else:
            check(f"port {port} ({name})", True, "free")
    first, last = cfg.agent_ports
    holders = listening_pids(first, last)
    if holders is not None:
        used = sorted(p for ports in holders.values() for p in ports)
        check(f"agent ports {first}-{last}", True, f"{len(used)} in use{': ' + str(used[:10]) if used else ''}", warn=True)
    file_values = read_env_file(ENV_FILE)
    llm = setting("DEVSTACK_LLM", file_values) or "mock"
    if llm == "openai":
        for key in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "MAIN_AGENT_LLM_MODEL"):
            check(f"llm setting {key}", bool(setting(key, file_values)), "set" if setting(key, file_values) else "missing")
    else:
        check("llm", True, f"default mode {llm!r} ({ENV_FILE.name} {'found' if ENV_FILE.exists() else 'absent'})")
    failed = [c for c in checks if c["level"] == "fail"]
    if args.json:
        print(json.dumps({"ok": not failed, "checks": checks}, indent=2))
    else:
        for c in checks:
            mark = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}[c["level"]]
            print(f"[{mark}] {c['check']}: {c['detail']}")
    return EXIT_PREFLIGHT if failed else EXIT_OK


# ── Entry point ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", help="State directory (default: <repo>/.devstack or $DEVSTACK_STATE_DIR).")
    parser = argparse.ArgumentParser(prog="stack", description="Native one-command dev stack for AgenticNetwork.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    up = sub.add_parser("up", parents=[common], help="Start everything and wait until it is ready.")
    up.add_argument("--llm", choices=LLM_MODES, help="mock (default, offline) or openai (any OpenAI-compatible API).")
    up.add_argument("--no-seed", action="store_true", help="Do not register the seed agents.")
    up.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Seconds to wait for readiness (default 120).")
    up.add_argument(
        "--port-offset", type=int, default=0,
        help="Add N to every port. Stacks running at once need offsets at least 100 apart.",
    )
    up.add_argument("--json", action="store_true", help="Print the final status as JSON.")
    up.add_argument("--keep-on-failure", action="store_true", help="Leave started services running if up fails.")
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", parents=[common], help="Stop this stack's services and spawned agents.")
    down.add_argument("--json", action="store_true", help="Print what is left running (and whose it is) as JSON.")
    down.set_defaults(func=cmd_down)

    status = sub.add_parser("status", parents=[common], help="Show what is running (exit 0 only when ready).")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    logs = sub.add_parser("logs", parents=[common], help="Show a service's or an agent's log.")
    logs.add_argument("service", help="mock-llm, registration, planner, proxy, directory, mock-llm-requests, or an agent name/id")
    logs.add_argument("-n", "--lines", type=int, default=100)
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(func=cmd_logs)

    reset = sub.add_parser(
        "reset", parents=[common],
        help="down, then delete the state it created (logs, directory, added agents); "
        "deletes nothing if down leaves anything running.",
    )
    reset.set_defaults(func=cmd_reset)

    smoke = sub.add_parser("smoke", parents=[common], help="Send one prompt through the front door and check the answer.")
    smoke.add_argument("--prompt", default=SMOKE_PROMPT)
    smoke.add_argument("--timeout", type=float, default=120.0)
    smoke.add_argument("--json", action="store_true")
    smoke.set_defaults(func=cmd_smoke)

    doctor = sub.add_parser("doctor", parents=[common], help="Check venvs, versions and ports.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    add = sub.add_parser("add-agent", parents=[common], help="Register an agent from a JSON registration body.")
    add.add_argument("definition", metavar="SEED.json")
    add.add_argument("--timeout", type=float, default=60.0)
    add.add_argument("--json", action="store_true")
    add.set_defaults(func=cmd_add_agent)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except StackError as exc:
        report_error(exc, as_json=getattr(args, "json", False) and args.command == "up")
        return exc.code
    except KeyboardInterrupt:
        print("devstack: interrupted", file=sys.stderr)
        return 130


def report_error(exc: StackError, *, as_json: bool) -> None:
    lines = tail_lines(exc.log, LOG_TAIL_LINES) if exc.log else []
    if as_json:
        print(json.dumps({
            "ready": False,
            "error": {"message": str(exc), "service": exc.service, "log": str(exc.log) if exc.log else None,
                      "exit_code": exc.code, "log_tail": lines},
        }, indent=2))
        return
    where = f" [{exc.service}]" if exc.service else ""
    print(f"devstack{where}: {exc}", file=sys.stderr)
    if exc.log:
        print(f"--- last {len(lines)} lines of {exc.log} ---", file=sys.stderr)
        for line in lines:
            print(line, file=sys.stderr)
        print("--- end of log ---", file=sys.stderr)
