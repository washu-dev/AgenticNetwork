# Custom Agent Handler Support — Implementation Plan

**Status:** Ready to implement  
**Target repos:** `AgenticNetwork` (this repo) + `agent-directory` (sibling repo at `github.com/washu-dev/agent-directory`)  
**Contact:** amaan@wustl.edu

---

## What This Builds and Why

The registration service currently supports two handler types:

- **`llm`** — system prompt → single LLM call → answer. No tools, no loop.
- **`api_proxy`** — forwards A2A tasks to an existing upstream HTTP service.

Neither supports researchers who have an existing Python library or multi-step agent logic they want to expose through the system. The goal of this work is a **third handler type, `custom`**, that lets a researcher upload a Python file extending a base class. The framework handles all A2A protocol, lifecycle, ADS publishing, and Docker image concerns — the researcher implements one method.

The motivating use case is [`refbot`](https://github.com/...) — a Python library that reviews academic papers from PDFs or URLs using a multi-step LLM loop. Integrating it should require uploading a ~15-line Python file, nothing else.

---

## Design Decisions (already agreed — do not relitigate)

- **Two-track approach**: The existing `llm`/`api_proxy` tracks are unchanged. `custom` is purely additive.
- **Base class lives in `agent_skeleton/`**, not in `registration_service/`. `agent_skeleton` becomes a pip-installable library. The registration service depends on it.
- **`handle_structured()` is the one required method.** It must return a `dict` containing an `"answer"` key. This is enforced at runtime before the response is sent — missing it raises a clear `ValueError`. `handle()` has a default implementation that calls `handle_structured()` and extracts `"answer"`.
- **`__init__(self, config: dict)` for handler configuration.** The `extra` dict from `CustomConfig` is passed here. Python-native, structured, consistent with how `AGENT_CONFIG_JSON` already works in the runner.
- **`FileInput` wrapper** for binary inputs (PDFs etc.). Exposes `.bytes`, `.mime_type`, `.name`, and `as_tempfile()` context manager that writes to a temp file, yields a `Path`, and cleans up on exit.
- **Two-step upload API**: `POST /agents` creates the record, `POST /agents/{id}/handler` uploads the file. Enables re-upload without re-registration. Re-upload always resets approval to `False`.
- **Approval gate**: Custom agents require explicit admin approval before spawning. `llm`/`api_proxy` agents are auto-approved.
- **Approval UI lives in the planner's existing admin panel** — a new "Pending Approvals" nav item alongside Dashboard, Agent Directory, etc. Auth uses the existing `X-Admin-Token` / `window._adminToken` mechanism already in place. No new auth system needed.
- **OASF skills**: Custom handlers must select from the existing ~90-skill taxonomy. No free-text skills.
- **Per-agent Docker images** (not a shared base image). Each custom agent gets its own image with its own Python version and dependencies baked in. Images are built automatically via GitHub Actions.
- **Build pipeline (cluster path)**: On approval, the registration service commits the handler file + generated Dockerfile + card to `agent-directory/registered_agents/{agent_name}/`. A GitHub Actions workflow in `agent-directory` triggers on that push and builds/pushes the Docker image. The registration service polls Docker Hub until the image appears, then spawns via `srun --overlap` inside the existing Slurm allocation.
- **Local dev path**: `SubprocessBackend` uses per-agent virtualenvs (no Docker, no GitHub). Selected when `SPAWN_BACKEND` is not `"cluster"`.
- **Agent status gains a `"building"` state** between `"approved"` and `"live"`.
- **`GITHUB_TOKEN`** (write access to `agent-directory`) is added to `deployment/.env`.
- **Requirements security**: open for now. The AST import blocklist and admin approval gate are the primary safeguards. Container isolation handles the rest in production.

---

## System Context You Need

### How the existing deployment works

The entire system runs as one Slurm job on one CPU node (`deployment/submit.sbatch`). All services are `srun --ntasks=1 --overlap` steps launched in parallel after ADS starts. They communicate over localhost with fixed ports. The shared filesystem at `/engrfs/project/di2/AgenticNetwork` is mounted into every container as `/workspace`.

The registration service container (`Dockerfile.registration`, `entrypoint-registration.sh`) starts PostgreSQL internally before starting the FastAPI service. It runs on port 8091.

The planner (`Dockerfile.planner`, `entrypoint-planner.sh`) proxies the registration service at `/register/*` via `@app.api_route("/register/{path:path}", ...)`. The registration service JS detects the proxy path and adjusts API prefixes accordingly.

The planner's admin panel is accessed via the "Admin" button in the chat UI. Auth uses `MAIN_AGENT_ADMIN_TOKEN` env var — if set, requires a password; if unset, allows open access. The token is stored in-memory as `window._adminToken` and sent as `X-Admin-Token` on all admin API calls.

### How A2A agents work

Every agent publishes a card to ADS (the agent directory service) on startup via `dirctl push` + `dirctl routing publish`. The planner discovers agents by querying ADS and dispatches tasks over HTTP/JSON-RPC. Agents return a dual-channel response: a `DataPart` artifact (machine-readable structured output) and a text message with `structured_output` in its metadata. The planner reads `structured_output` preferentially.

### The agent_skeleton

`agent_skeleton/` is the canonical template for building new agents. It has `executor.py` (A2A executor), `llm_loop.py` (generic Chat Completions tool loop), `serve.py` (uvicorn + ADS publish), and `a2a_runtime.py` (SDK wrappers). These files are not changing — new files are added alongside them.

---

## Implementation Phases

---

### Phase 1 — `agent_skeleton` becomes an installable library

**Files to create:**

#### `agent_skeleton/pyproject.toml`
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "agent-skeleton"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "a2a-sdk>=0.3.26",
    "openai>=1.40",
    "uvicorn>=0.30",
]

[tool.setuptools.packages.find]
where = ["."]
include = ["agent_skeleton*"]
```

#### `agent_skeleton/base.py`
```python
from __future__ import annotations
import mimetypes
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


class FileInput:
    """Wraps a binary file arriving from an A2A FilePart."""

    def __init__(self, data: bytes, name: str | None = None, mime_type: str | None = None):
        self.bytes = data
        self.name = name
        self.mime_type = mime_type or _detect_mime(data, name)

    @contextmanager
    def as_tempfile(self, suffix: str | None = None) -> Generator[Path, None, None]:
        """Write bytes to a temp file, yield its Path, clean up on exit."""
        if suffix is None and self.name:
            suffix = Path(self.name).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(self.bytes)
            tmp = Path(f.name)
        try:
            yield tmp
        finally:
            tmp.unlink(missing_ok=True)


def _detect_mime(data: bytes, name: str | None) -> str:
    if name:
        guessed, _ = mimetypes.guess_type(name)
        if guessed:
            return guessed
    # Sniff common signatures
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:2] in (b"\xff\xd8", b"\x89P"):
        return "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"
    return "application/octet-stream"


class AgentHandler(ABC):
    """
    Base class for custom uploaded agents.

    Subclass this, implement handle_structured(), upload the file via the
    registration service UI. The framework handles A2A, ADS, and lifecycle.

    Minimum implementation:

        from agent_skeleton import AgentHandler, FileInput

        class MyHandler(AgentHandler):
            async def handle_structured(
                self, user_input: str, files: list[FileInput] = []
            ) -> dict:
                return {"answer": "your response"}
    """

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    async def handle_structured(
        self,
        user_input: str,
        files: list[FileInput] = [],
    ) -> dict:
        """
        Core logic. Must return a dict containing an "answer" key.

        The "answer" value is the human-readable text response shown to the user.
        All other keys are passed to the planner as structured output.

        Raises ValueError at runtime if "answer" is missing.
        """
        ...

    async def handle(self, user_input: str, files: list[FileInput] = []) -> str:
        """Returns the "answer" from handle_structured. Override if needed."""
        result = await self.handle_structured(user_input, files)
        return result.get("answer", str(result))
```

#### `agent_skeleton/handler_executor.py`
```python
from __future__ import annotations
import base64

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue

from agent_skeleton.a2a_runtime import data_part, task_updater, text_part
from agent_skeleton.base import AgentHandler, FileInput


class HandlerExecutor(AgentExecutor):
    """Wraps any AgentHandler in the A2A executor interface."""

    def __init__(self, handler: AgentHandler):
        self.handler = handler

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input() or ""
        files = _extract_files(context)

        result = await self.handler.handle_structured(user_input, files)

        if "answer" not in result:
            raise ValueError(
                f"handle_structured() must return a dict with an 'answer' key. "
                f"Got keys: {list(result.keys())}"
            )

        updater = task_updater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        await updater.add_artifact(
            [data_part(result)],
            name="result",
            metadata={"content_type": "application/json"},
        )
        await updater.complete(
            message=updater.new_agent_message(
                [text_part(result["answer"])],
                metadata={"structured_output": result},
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = task_updater(event_queue, context.task_id, context.context_id)
        await updater.reject()


def _extract_files(context: RequestContext) -> list[FileInput]:
    files = []
    message = getattr(context, "message", None)
    if not message:
        return files
    for part in getattr(message, "parts", None) or []:
        root = part.root
        # FilePart
        file_part = getattr(root, "file", None)
        if file_part is None:
            continue
        raw = getattr(file_part, "bytes", None) or getattr(file_part, "data", None)
        if raw is None:
            continue
        if isinstance(raw, str):
            raw = base64.b64decode(raw)
        name = getattr(file_part, "name", None)
        mime = getattr(file_part, "mimeType", None) or getattr(file_part, "mime_type", None)
        files.append(FileInput(data=raw, name=name, mime_type=mime))
    return files
```

#### `agent_skeleton/__init__.py`
```python
from agent_skeleton.base import AgentHandler, FileInput
from agent_skeleton.handler_executor import HandlerExecutor

__all__ = ["AgentHandler", "FileInput", "HandlerExecutor"]
```

**No changes** to existing files: `executor.py`, `llm_loop.py`, `serve.py`, `a2a_runtime.py`, `config.py`.

---

### Phase 2 — Registration service backend

#### Database (`registration_service/db.py`)

Add two columns in `init_db()` alongside the existing schema:

```sql
ALTER TABLE agents ADD COLUMN IF NOT EXISTS handler_file_path TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS approved BOOLEAN NOT NULL DEFAULT TRUE;
```

Status check constraint gains `'building'`:
```sql
CHECK (status IN ('registered', 'live', 'failed', 'stopped', 'building'))
```

Update `fake_db.py` (`FakeAgent` dataclass or dict): add `handler_file_path: str | None = None` and `approved: bool = True`.

#### Models (`registration_service/models.py`)

Add `CustomConfig`:
```python
class CustomConfig(BaseModel):
    class_name: str
    python_version: str = "3.11"
    requirements: list[str] = []
    extra: dict = {}
```

Add `"custom"` to the `handler_type` literal. Config routing:
```python
def parse_config(handler_type: str, raw: dict):
    if handler_type == "llm":
        return LLMConfig(**raw)
    if handler_type == "api_proxy":
        return APIProxyConfig(**raw)
    if handler_type == "custom":
        return CustomConfig(**raw)
    raise ValueError(f"Unknown handler_type: {handler_type}")
```

`AgentResponse` gains:
```python
approved: bool
```

#### Validator (`registration_service/validator.py` — new file)

Pure AST analysis. No code is executed.

```python
import ast
from dataclasses import dataclass

BLOCKED_IMPORTS = {"subprocess", "socket", "shutil"}
BLOCKED_CALLS = {"eval", "exec", "os.system", "os.popen"}


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]


def validate_handler(source: str, class_name: str) -> ValidationResult:
    errors = []

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return ValidationResult(ok=False, errors=[f"Syntax error at line {e.lineno}: {e.msg}"])

    # Check blocked imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BLOCKED_IMPORTS:
                    errors.append(f"Line {node.lineno}: disallowed import '{alias.name}'")
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BLOCKED_IMPORTS:
                errors.append(f"Line {node.lineno}: disallowed import from '{node.module}'")

    # Check class exists
    classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    if class_name not in classes:
        errors.append(f"Class '{class_name}' not found in file")
        return ValidationResult(ok=len(errors) == 0, errors=errors)

    cls = classes[class_name]

    # Check AgentHandler in bases (AST-level, no import needed)
    base_names = []
    for base in cls.bases:
        if isinstance(base, ast.Name):
            base_names.append(base.id)
        elif isinstance(base, ast.Attribute):
            base_names.append(base.attr)
    if "AgentHandler" not in base_names:
        errors.append(f"Class '{class_name}' must extend AgentHandler")

    # Check handle_structured is defined
    methods = {n.name for n in ast.walk(cls) if isinstance(n, ast.FunctionDef)}
    if "handle_structured" not in methods:
        errors.append(f"Class '{class_name}' must define handle_structured()")

    return ValidationResult(ok=len(errors) == 0, errors=errors)
```

#### File storage

Files are stored at `{HANDLER_FILES_DIR}/{agent_id}/handler.py`.

`HANDLER_FILES_DIR` defaults to `./uploaded_handlers` locally and `/workspace/registration_service/uploaded_handlers` on the cluster (set in `deployment/.env`).

Add to service startup (in `lifespan`):
```python
Path(os.getenv("HANDLER_FILES_DIR", "./uploaded_handlers")).mkdir(parents=True, exist_ok=True)
```

#### Backends (`registration_service/backend.py` — new file)

Extracts existing subprocess logic from `supervisor.py` into `SubprocessBackend` and adds `ClusterBackend`.

```python
import asyncio, json, os
from pathlib import Path


class SubprocessBackend:
    """Local dev: per-agent virtualenv, direct subprocess."""

    def __init__(self):
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    async def spawn(self, agent_id: str, port: int, handler_type: str, config: dict) -> None:
        handler_dir = Path(os.getenv("HANDLER_FILES_DIR", "./uploaded_handlers")) / agent_id

        if handler_type == "custom" and config.get("requirements"):
            venv = handler_dir / ".venv"
            await _run(["python3", "-m", "venv", str(venv)])
            await _run([str(venv / "bin/pip"), "install", *config["requirements"]])

        env = {**os.environ, "AGENT_PORT": str(port), "AGENT_CONFIG_JSON": json.dumps(config)}
        if handler_type == "custom":
            env["HANDLER_FILE_PATH"] = str(handler_dir / "handler.py")

        python = str(handler_dir / ".venv/bin/python") if (handler_dir / ".venv").exists() else "python3"
        proc = await asyncio.create_subprocess_exec(
            python, "-m", "registration_service.runner",
            env=env,
        )
        self._procs[agent_id] = proc

    async def stop(self, agent_id: str) -> None:
        proc = self._procs.pop(agent_id, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()

    def is_alive(self, agent_id: str) -> bool:
        proc = self._procs.get(agent_id)
        return proc is not None and proc.returncode is None


class ClusterBackend:
    """ENGR cluster: per-agent Docker image via srun --overlap within existing allocation."""

    def __init__(self):
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    async def spawn(self, agent_id: str, port: int, handler_type: str, config: dict) -> None:
        handler_files_dir = os.environ["HANDLER_FILES_DIR"]
        logs_dir = Path("/workspace/logs") / os.environ.get("USER", "agent")
        logs_dir.mkdir(parents=True, exist_ok=True)

        image = (
            f"amaankhan25/agenticnetwork-custom-{config.get('_agent_name', agent_id)}:latest"
            if handler_type == "custom"
            else f"amaankhan25/agenticnetwork-{handler_type}:latest"
        )

        env_exports = {
            "AGENT_PORT": str(port),
            "AGENT_CONFIG_JSON": json.dumps(config),
            "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", ""),
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "dummy"),
        }
        if handler_type == "custom":
            env_exports["HANDLER_FILE_PATH"] = f"{handler_files_dir}/{agent_id}/handler.py"

        export_str = ",".join(f"{k}={v}" for k, v in env_exports.items())
        log_path = logs_dir / f"{handler_type}-{agent_id}.log"

        cmd = [
            "srun", "--ntasks=1", "--overlap",
            f"--container-image={image}",
            f"--container-mounts=/engrfs/project/di2/AgenticNetwork:/workspace",
            "--container-writable",
            f"--export={export_str}",
            "/entrypoint.sh",
        ]
        with open(log_path, "a") as log_f:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=log_f, stderr=asyncio.subprocess.STDOUT,
            )
        self._procs[agent_id] = proc

    async def stop(self, agent_id: str) -> None:
        proc = self._procs.pop(agent_id, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()

    def is_alive(self, agent_id: str) -> bool:
        proc = self._procs.get(agent_id)
        return proc is not None and proc.returncode is None


async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()


def make_backend():
    if os.getenv("SPAWN_BACKEND") == "cluster":
        return ClusterBackend()
    return SubprocessBackend()
```

#### Runner (`registration_service/runner.py`)

Add `custom` branch:

```python
elif handler_type == "custom":
    import importlib.util
    config_obj = CustomConfig(**config_dict)
    file_path = os.environ["HANDLER_FILE_PATH"]

    spec = importlib.util.spec_from_file_location("custom_handler", file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    handler_class = getattr(module, config_obj.class_name)
    handler = handler_class(config_obj.extra)

    from agent_skeleton import HandlerExecutor
    executor = HandlerExecutor(handler)
    # continue to A2A server setup (same as other handler types)
```

#### Supervisor (`registration_service/supervisor.py`)

- Import `make_backend` from `backend.py`; replace inline `SubprocessBackend` instantiation with `make_backend()`
- `spawn_and_publish`: for `custom` handler_type, check `agent.approved == True` before proceeding; raise `RuntimeError("Agent not approved")` if not
- Add `_agent_name` to config dict passed to `ClusterBackend.spawn()` so it can construct the correct image tag

#### `fake_supervisor.py`

Add `"building"` to the set of valid statuses. `spawn_and_publish` for `custom` agents still just sets status to `"live"` (fake mode skips everything).

---

### Phase 3 — Registration service API

All changes in `registration_service/server.py`:

**`POST /agents`** — add `custom` handling:
```python
if body.handler_type == "custom":
    agent = await db.create_agent(..., approved=False)
    # Do NOT call supervisor.spawn_and_publish yet
    return AgentResponse(**agent, status="registered"), 201
else:
    # existing llm/api_proxy path unchanged
```

**New: `POST /agents/{id}/handler`** — multipart upload:
```python
@app.post("/agents/{agent_id}/handler", status_code=200)
async def upload_handler(agent_id: UUID, file: UploadFile = File(...)):
    agent = await db.get_agent(agent_id)
    if not agent or agent["handler_type"] != "custom":
        raise HTTPException(404)

    source = (await file.read()).decode("utf-8")
    config = CustomConfig(**agent["config"])
    result = validate_handler(source, config.class_name)

    if not result.ok:
        raise HTTPException(422, detail={"errors": result.errors})

    handler_dir = Path(os.getenv("HANDLER_FILES_DIR", "./uploaded_handlers")) / str(agent_id)
    handler_dir.mkdir(parents=True, exist_ok=True)
    (handler_dir / "handler.py").write_text(source)

    # Re-upload always resets approval
    await db.update_agent(agent_id, handler_file_path=str(handler_dir / "handler.py"), approved=False)
    return {"status": "uploaded", "errors": []}
```

**New: `POST /agents/{id}/approve`**:
```python
@app.post("/agents/{agent_id}/approve")
async def approve_agent(agent_id: UUID, request: Request):
    _check_admin_token(request)   # checks X-Admin-Token header
    await db.update_agent(agent_id, approved=True)
    await supervisor.spawn_and_publish(agent_id)
    return {"status": "ok"}
```

**New: `POST /agents/{id}/reject`**:
```python
@app.post("/agents/{agent_id}/reject")
async def reject_agent(agent_id: UUID, body: RejectRequest, request: Request):
    _check_admin_token(request)
    await db.update_agent(agent_id, approved=False, status="registered")
    return {"status": "ok"}

class RejectRequest(BaseModel):
    reason: str = ""
```

**`DELETE /agents/{id}`** — add cleanup:
```python
handler_dir = Path(os.getenv("HANDLER_FILES_DIR", "./uploaded_handlers")) / str(agent_id)
if handler_dir.exists():
    shutil.rmtree(handler_dir)
```

**Admin token check** (shared helper):
```python
def _check_admin_token(request: Request) -> None:
    token = request.headers.get("X-Admin-Token", "")
    expected = os.getenv("MAIN_AGENT_ADMIN_TOKEN", "")
    if expected and token != expected:
        raise HTTPException(403, "Forbidden")
```

---

### Phase 4 — Registration service UI (`registration_service/static/index.html`)

Add a third option to the handler type radio: **"Custom (Python)"**.

When selected, the config section (system prompt / upstream URL fields) is replaced by:

1. **Code editor** — a `<textarea class="code-editor">` pre-filled with this exact template:
```python
from agent_skeleton import AgentHandler, FileInput

class MyAgentHandler(AgentHandler):
    """Describe what your agent does here."""

    async def handle_structured(
        self,
        user_input: str,
        files: list[FileInput] = []
    ) -> dict:
        # Access uploaded files:
        #   with files[0].as_tempfile() as path:
        #       content = path.read_text()
        #
        # "answer" is required — it becomes the human-readable response.
        # All other keys are returned to the planner as structured data.

        return {
            "answer": "Your response here",
            # "key": value,
        }
```

2. **Class name field** — text input, default `"MyAgentHandler"`. Must match the class in the editor.

3. **Python version field** — dropdown: `3.11` (default), `3.12`, `3.13`.

4. **Requirements field** — text input, comma-separated (e.g. `refbot>=0.1, numpy`). Parsed into a list on submit.

5. **Extra config field** — JSON textarea (optional). Passed as `config.extra` to `__init__`.

6. **Upload alternative** — "Or upload a .py file" button that reads the file and populates the code editor.

7. **Submit flow** — JS orchestrates two calls transparently:
   - `POST /agents` (or `/register/agents` when proxied) with handler_type `"custom"` and config
   - On success: `POST /agents/{id}/handler` with the code as a `.py` file upload
   - Shows inline validation errors from the second call if any

8. **Active Agents table** — add status badge:
   - Amber `"Pending Approval"` for `approved=false` custom agents
   - Blue `"Building"` for `status="building"` agents
   - Existing badges unchanged for other statuses

---

### Phase 5 — Planner admin UI (`planner_agent/main_agent/web/index.html`)

Add **"Pending Approvals"** as a new nav item in the admin panel's left sidebar, between "Agent Directory" and "Registry Agent". Only visible in full admin mode (not guest).

**Content panel:**
- Table columns: Agent Name | Submitted | Skills | Python | Requirements | Actions
- Per row: expand chevron → reveals read-only code viewer (syntax-highlighted `<pre>`) of the handler file
- Actions: **Approve** button (green), **Reject** button (red, opens small textarea for reason)
- Approve calls `POST /register/agents/{id}/approve` with `X-Admin-Token` header
- Reject calls `POST /register/agents/{id}/reject`
- Auto-refreshes every 10s (same pattern as Active Agents in the registration UI)
- Empty state: "No agents pending approval."

The approve/reject calls go through the existing `/register/*` proxy so they reach the registration service without any CORS or auth changes.

Badge on the "Pending Approvals" nav item: shows count of pending agents (e.g. `Pending Approvals (2)`). Fetched alongside the auto-refresh.

---

### Phase 6 — GitHub Integration & ClusterBackend Build Pipeline

This phase makes the cluster path work end-to-end. It has three parts: Dockerfile generation, GitHub commit on approval, and Docker Hub polling.

#### Dockerfile generation (`registration_service/builder.py` — new file)

```python
from registration_service.models import CustomConfig


def generate_dockerfile(config: CustomConfig, runner_src: str) -> str:
    req_line = ""
    if config.requirements:
        pkgs = " ".join(config.requirements)
        req_line = f"RUN pip install --no-cache-dir {pkgs}"

    return f"""FROM python:${config.python_version}-slim-bookworm

# Install agent framework
RUN pip install --no-cache-dir agent-skeleton a2a-sdk openai uvicorn

# Install agent-specific requirements
{req_line}

WORKDIR /app
COPY handler.py handler.py
COPY runner.py runner.py

ENV PYTHONPATH=/app
ENTRYPOINT ["python", "runner.py"]
"""
```

#### GitHub commit on approval

In `supervisor.py::spawn_and_publish`, when `handler_type == "custom"` and `GITHUB_TOKEN` + `GITHUB_REPO` are set, after validation and before spawning:

```python
await _commit_to_github(agent)
await db.update_agent(agent.id, status="building")
# Do NOT call backend.spawn() yet — wait for image to be built
```

`_commit_to_github(agent)` does three GitHub API calls (using `httpx` or `requests`):
1. Get current SHA of `registered_agents/` tree
2. Create/update three files in one commit:
   - `registered_agents/{agent_name}/handler.py` — the uploaded source
   - `registered_agents/{agent_name}/Dockerfile` — generated by `builder.py`
   - `registered_agents/{agent_name}/{agent_name}.card.json` — generated from agent metadata
3. Commit message: `"Register custom agent: {agent_name}"`

#### Docker Hub polling

Add a background polling loop in `supervisor.py` (runs every 30s, alongside the existing health loop):

```python
async def _build_poll_loop(self):
    while True:
        await asyncio.sleep(30)
        building = await db.list_agents_by_status("building")
        for agent in building:
            config = CustomConfig(**agent["config"])
            image_name = f"amaankhan25/agenticnetwork-custom-{agent['name'].lower().replace(' ', '-')}"
            if await _image_exists_on_dockerhub(image_name):
                await self.spawn_and_publish(agent["id"])
```

`_image_exists_on_dockerhub(image_name)` queries `https://hub.docker.com/v2/repositories/{image_name}/tags/latest` — returns `True` if the tag exists and `last_updated` is after the agent's `updated_at` (the approval timestamp).

#### New environment variables (add to `deployment/.env` and `deployment/.env.example`)

```bash
GITHUB_TOKEN=ghp_...           # Personal access token with repo write access to washu-dev/agent-directory
GITHUB_REPO=washu-dev/agent-directory
SPAWN_BACKEND=cluster          # Activates ClusterBackend; omit for local dev
HANDLER_FILES_DIR=/workspace/registration_service/uploaded_handlers
```

Also add to `submit.sbatch` (before the agent container launches):
```bash
export SPAWN_BACKEND=cluster
export HANDLER_FILES_DIR=/workspace/registration_service/uploaded_handlers
```

---

### Phase 7 — GitHub Actions workflow in `agent-directory` repo

Create `.github/workflows/build-custom-agent.yml` in the `agent-directory` repo:

```yaml
name: Build custom agent image

on:
  push:
    paths:
      - 'registered_agents/**'

jobs:
  build:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 2

      - name: Detect changed agent directory
        id: agent
        run: |
          CHANGED=$(git diff HEAD~1 --name-only | grep '^registered_agents/' | head -1)
          AGENT_NAME=$(echo "$CHANGED" | cut -d/ -f2)
          echo "name=$AGENT_NAME" >> $GITHUB_OUTPUT
          echo "dir=registered_agents/$AGENT_NAME" >> $GITHUB_OUTPUT

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to Docker Hub
        uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: ${{ steps.agent.outputs.dir }}
          platforms: linux/amd64
          push: true
          tags: amaankhan25/agenticnetwork-custom-${{ steps.agent.outputs.name }}:latest
```

Add `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` to the `washu-dev/agent-directory` repo's GitHub Actions secrets.

---

### Phase 8 — End-to-end testing

**Local path first:**
1. Start registration service with `FAKE_DB=0` (real postgres) and `SPAWN_BACKEND` unset
2. Upload a minimal handler (no external requirements, just returns a static answer)
3. Approve via the planner admin UI
4. Verify the agent appears in ADS and the planner can call it
5. Re-upload a new version → verify re-approval is required

**Cluster path:**
1. Set `SPAWN_BACKEND=cluster`, `GITHUB_TOKEN`, `GITHUB_REPO`, `HANDLER_FILES_DIR`
2. Upload the same minimal handler
3. Approve → verify commit appears in `agent-directory/registered_agents/`
4. Verify GitHub Actions triggers and builds the image
5. Verify registration service detects the image and the agent goes live

---

### Phase 9 — Paper reviewer demo

Upload `paper_reviewer_handler.py` via the registration service UI:

```python
import re
from agent_skeleton import AgentHandler, FileInput
from refbot import review_paper


def _extract_url(text: str) -> str | None:
    match = re.search(r"https?://\S+\.pdf", text)
    return match.group(0) if match else None


class PaperReviewerHandler(AgentHandler):
    async def handle_structured(
        self, user_input: str, files: list[FileInput] = []
    ) -> dict:
        if files:
            with files[0].as_tempfile() as path:
                review = review_paper(str(path))
        else:
            url = _extract_url(user_input)
            if not url:
                return {"answer": "Please provide a PDF URL or attach a PDF file."}
            review = review_paper(url)

        return {
            "answer": review.markdown(),
            "summary": review.summary,
            "verdict": review.verdict,
            "strengths": review.strengths,
            "weaknesses": review.weaknesses,
        }
```

Registration UI fields:
- **Name**: `paper-reviewer`
- **Description**: `Reviews academic papers from PDFs or URLs. Extracts summary, contributions, methods, strengths, weaknesses, and verdict.`
- **Python version**: `3.12` (refbot requires 3.12+)
- **Requirements**: `refbot>=0.1`
- **Class name**: `PaperReviewerHandler`
- **OASF skills**: Select the closest match from the taxonomy (e.g. "Scientific Text Understanding" or "Document Analysis")

---

## File Change Summary

### `AgenticNetwork` repo

| File | Change |
|---|---|
| `agent_skeleton/pyproject.toml` | **New** — makes package pip-installable |
| `agent_skeleton/__init__.py` | **New** — public API: `AgentHandler`, `FileInput`, `HandlerExecutor` |
| `agent_skeleton/base.py` | **New** — `AgentHandler` ABC + `FileInput` wrapper |
| `agent_skeleton/handler_executor.py` | **New** — A2A executor wrapping any `AgentHandler` |
| `registration_service/validator.py` | **New** — AST-based handler file validation |
| `registration_service/backend.py` | **New** — `SubprocessBackend`, `ClusterBackend`, `make_backend()` |
| `registration_service/builder.py` | **New** — Dockerfile generation from `CustomConfig` |
| `registration_service/db.py` | **Modify** — add `handler_file_path`, `approved` columns; add `"building"` to status enum |
| `registration_service/models.py` | **Modify** — add `CustomConfig`; add `approved` to `AgentResponse` |
| `registration_service/server.py` | **Modify** — `custom` handler type; new upload, approve, reject endpoints; `_check_admin_token` helper |
| `registration_service/supervisor.py` | **Modify** — use `make_backend()`; add GitHub commit on approval; add Docker Hub polling loop |
| `registration_service/fake_db.py` | **Modify** — add `handler_file_path`, `approved` fields; add `"building"` status |
| `registration_service/fake_supervisor.py` | **Modify** — handle `custom` handler type (skip to live) |
| `registration_service/static/index.html` | **Modify** — new "Custom (Python)" handler type; code editor; Pending Approval/Building badges |
| `registration_service/requirements.txt` | **Modify** — add `-e ../agent_skeleton` |
| `planner_agent/main_agent/web/index.html` | **Modify** — new "Pending Approvals" admin nav item and panel |
| `deployment/submit.sbatch` | **Modify** — add `SPAWN_BACKEND=cluster` and `HANDLER_FILES_DIR` exports |
| `deployment/.env.example` | **Modify** — add `GITHUB_TOKEN`, `GITHUB_REPO`, `SPAWN_BACKEND`, `HANDLER_FILES_DIR` |
| `docs/CUSTOM_AGENT_HANDLER_PLAN.md` | This file |

### `agent-directory` repo

| File | Change |
|---|---|
| `.github/workflows/build-custom-agent.yml` | **New** — builds Docker image on push to `registered_agents/**` |
| `registered_agents/` | **New directory** — auto-populated by registration service on approval |

---

## Important Notes for the Implementing Session

1. **The `agent_skeleton` package must be installed in the registration service's venv** before the runner can import `HandlerExecutor`. Add `-e ../agent_skeleton` to `registration_service/requirements.txt` and rebuild `Dockerfile.registration` to `COPY agent_skeleton/` and `RUN pip install -e /app/agent_skeleton`.

2. **`Dockerfile.registration` needs `srun` available** for `ClusterBackend`. Verify whether the ENGR cluster's Pyxis containers have Slurm client tools available inside containers. If not, `srun` must be added to `Dockerfile.registration` (install from the cluster's Slurm packages or copy the binary). **If this is infeasible, see Note #8 for the fallback approach.**

3. **The `--overlap` flag on nested `srun` calls** requires the registration service to be running within an existing Slurm allocation (it is — it's an `srun` step in `submit.sbatch`). The `SLURM_JOB_ID` env var will be present and inherited by the nested call.

4. **Docker Hub polling assumes public repositories.** The image tag endpoint `https://hub.docker.com/v2/repositories/{image}/tags/latest` works for public repos without auth. If the repo is private, add `DOCKERHUB_TOKEN` to the polling request headers.

5. **The planner proxy forwards all headers** including `X-Admin-Token`. The registration service's `_check_admin_token()` helper reads this header directly — no session management needed.

6. **`refbot` requires Python 3.12+.** When testing the paper reviewer demo, `python_version` must be `"3.12"` in `CustomConfig`, and the generated Dockerfile will use `python:3.12-slim-bookworm`.

7. **Status lifecycle for custom agents:**
   ```
   registered            ← created by POST /agents
        ↓
   registered            ← file uploaded via POST /agents/{id}/handler (approved=False)
        ↓
   pending approval      ← visible in admin UI (approved=False, no status change in DB)
        ↓
   building              ← approved, committed to GitHub, waiting for image
        ↓
   live                  ← image detected on Docker Hub, spawned, ADS published
        ↓
   failed / stopped      ← health check failure or DELETE
   ```
   Note: "pending approval" is not a DB status value — it's a derived UI state (`approved=False AND status='registered'`). The actual status column only gains `'building'`.

8. **Fallback if `srun` is unavailable inside Pyxis containers (replaces Note #2 and #3 if the primary approach is infeasible).** If the ENGR cluster's security policy prevents calling `srun` from within a container, the spawn responsibility moves to a host-side bash script launched by `submit.sbatch` itself — which runs in the native Slurm environment where `srun` works unconditionally. The changes required are:

   **New `deployment/spawn_agent_loop.sh`** — a polling loop that runs on the host (not inside any container). It calls `GET /internal/spawn-queue` on the registration service every 15s, calls `srun --overlap` for each agent returned, then reports the srun PID back via `POST /internal/spawned`. It also calls `GET /internal/stop-queue` and kills PIDs for agents that have been stopped.

   **Add to `submit.sbatch`** (after ADS is ready, before the `wait`):
   ```bash
   bash /workspace/deployment/spawn_agent_loop.sh >> "$LOGS/spawn_agent_loop.log" 2>&1 &
   ```

   **Two new internal-only endpoints in `registration_service/server.py`** (no auth — localhost only):
   - `GET /internal/spawn-queue` — returns agents with `status="image_ready"` (a new status inserted between `"building"` and `"live"`), with full config needed to construct the `srun` command.
   - `POST /internal/spawned` — receives `{"agent_id": "...", "pid": 12345}` from the spawn loop. Registration service stores the PID, updates status to `"live"`, publishes to ADS.
   - `GET /internal/stop-queue` — returns PIDs of agents marked `"stopped"` whose srun process hasn't been killed yet. Clears the queue after returning.

   **`ClusterBackend` simplifies** — `spawn()` becomes a no-op (the host loop handles it). `is_alive()` polls `http://localhost:{port}/health` instead of checking a subprocess returncode. `stop()` appends the agent's stored PID to an in-memory stop list consumed by `/internal/stop-queue`.

   **Status lifecycle gains one additional state** under this approach:
   ```
   building → image_ready → live
   ```
   `"image_ready"` means the Docker Hub polling loop confirmed the image exists; the host spawn loop picks it up within one poll cycle (≤15s).

   **`Dockerfile.registration` requires no Slurm tools** under this approach — the registration service never calls `srun` directly.

   **File change summary additions** (on top of the main plan):
   - `deployment/spawn_agent_loop.sh` — **New**
   - `registration_service/server.py` — add three `/internal/*` endpoints
   - `registration_service/backend.py` — simplify `ClusterBackend` as described above
   - `registration_service/db.py` — add `"image_ready"` to status enum
   - `deployment/submit.sbatch` — add `spawn_agent_loop.sh` background launch

---

## Open Items / Follow-ups (surfaced during implementation — decide later)

These were noted while implementing Phases 1–6 and are **not yet resolved**. None
block the local (SubprocessBackend) path, which is fully working and tested.

### Cluster path (unverifiable locally — needs a real ENGR run)
1. **`srun` inside Pyxis containers — DECIDED: host-side loop (Note #8).** Probed on
   `condo-di2` (2026-06-30, job 92051): the registration image has **no `srun`
   binary**, so the nested-srun primary path is dead. Implemented the host-side
   spawn loop instead: build-poll marks agents `image_ready`; `deployment/
   spawn_agent_loop.py` (launched by `submit.sbatch`, runs in native Slurm) polls
   `/internal/spawn-queue`, runs `srun --overlap`, reports PIDs to
   `/internal/spawned`, and kills stopped PIDs from `/internal/stop-queue`.
   `ClusterBackend.spawn` is now a no-op; `is_alive` polls `/health`; `stop` queues
   the PID. **Known limitation:** the supervisor does not auto-restart a crashed
   cluster agent (recovery + health-restart are skipped for `ClusterBackend`) —
   re-approve to redeploy.
2. **`agent_skeleton` install in generated images.** Not on PyPI, so the generated
   Dockerfile installs it via `git+https://github.com/washu-dev/AgenticNetwork…#subdirectory=agent_skeleton`
   (overridable via `AGENT_SKELETON_PIP_SPEC`). **If AgenticNetwork is private, the
   GitHub Actions build must authenticate** (token in the URL or a checkout step).
3. **No `agenticnetwork-llm` / `agenticnetwork-api_proxy` images exist.**
   `ClusterBackend` references shared images for non-custom cluster spawns, but only
   the **custom** per-agent path is fully wired. Today llm/api_proxy agents spawn as
   subprocesses; containerizing them on the cluster is a separate, unbuilt piece.
4. **Docker Hub poll assumes public images** (`hub.docker.com/v2/...` unauthenticated).
   Private repos need a token on the poll request.

### Secrets / config (operator action, not code)
5. **`GITHUB_TOKEN`** (write access to `agent-directory`) must go in the real
   `deployment/.env` — never committed.
6. **`MAIN_AGENT_ADMIN_TOKEN` must match** between the planner and registration
   containers, or proxied approve/reject/source calls (`X-Admin-Token`) get 403.
   Local dev (token unset) = open mode, works as-is.
7. **GitHub Actions secrets** `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` must be set in
   the `agent-directory` repo (Phase 7).

### UX / smaller follow-ups
8. **Two-step upload orphan.** If `POST /agents` succeeds but the handler upload
   fails validation, an agent row is left in "Pending Approval" with no handler
   file (harmless: approve returns 409; admin can Delete). The registration UI has
   **no in-table "re-upload handler" action** — re-upload currently needs the API
   directly or re-registration. Consider adding it.
9. **`.claude/launch.json`** was added for the browser-preview verification
   (registration + planner dev configs). Keep or drop per preference; not required
   by the feature.
10. **Pre-existing `s["key"]` skill-shape assumption.** `supervisor._build_agent_card`
    / `_register_oasf_routing_skills` read `s["key"]`, but `OASFSkill` is `{name, id}`.
    Custom agents inherit whatever the UI sends (the registration UI sends the same
    skill shape as before). Not introduced by this work, but worth reconciling.
