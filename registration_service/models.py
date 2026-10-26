"""Pydantic models for the registration service API.

## The handler-type taxonomy (consolidated 2026-07)

There are **three** canonical handler types, split on the one question that
actually changes the runtime — *who runs the loop*:

| type | who runs the loop | capabilities come from |
|---|---|---|
| ``llm``    | our frozen engine (``agent_skeleton.llm_loop``) | ``LLMConfig.tools`` — none / presets / raw schemas / uploaded module / HTTP operations |
| ``proxy``  | nobody — each A2A task is relayed to a URL you host | — |
| ``custom`` | your uploaded ``AgentHandler`` subclass | your code |

This replaces the older five-way split (``llm``, ``api_proxy``, ``custom``,
``endpoint_ops``, ``tool_loop``). ``tool_loop`` and ``endpoint_ops`` were never
different *runtimes* from ``llm`` — the upload handler (now
``handlers/llm_upload_handler.py``) imports ``llm_handler``'s executor and card
builder verbatim and says so; only the ``AgentSpec`` differed. So "where do the
tools come from" is now a **field** (``LLMConfig.tools``) rather than a type.
``proxy`` stays separate precisely because "no LLM at all" is its defining
property.

**Legacy names keep working.** ``api_proxy`` / ``tool_loop`` / ``endpoint_ops``
are still accepted on the wire and still parse from their old config shapes (see
``LEGACY_HANDLER_ALIASES`` and ``parse_config``). This is load-bearing, not
politeness: rows already in Postgres carry the old ``handler_type`` *and* the old
config JSON, and the read path re-parses them on every spawn. Old rows are left
as-is and normalized on read; only newly written rows are canonical.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from agentnet.credential_catalog import is_known_type
from registration_service.archive import RESERVED_IMPORT_NAMES


class OASFSkill(BaseModel):
    name: str
    id: int | None = None
    key: str | None = None   # canonical path-style id used for the card skill.id + ADS routing


class CredentialRequirement(BaseModel):
    """A user-owned credential a custom agent consumes (P4a).

    ``type`` is a canonical catalog key (e.g. ``openai_api_key``) or a
    ``custom:<slug>`` escape — see ``agentnet/credential_catalog.py``.
    ``required`` drives P4c enforcement (required→block if missing; optional→proceed);
    ``env_var`` names the variable the agent reads the value under in its context.
    """
    type: str
    label: str
    required: bool = True
    env_var: str | None = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        v = v.strip()
        if not is_known_type(v):
            raise ValueError(
                f"Unknown credential type: {v!r} "
                "(use a catalog type or 'custom:<slug>')"
            )
        return v


_CPU_RE = re.compile(r"^\d+(\.\d+)?m?$")          # Cloud Run --cpu: "1", "2", "0.5", "1000m"
_MEMORY_RE = re.compile(r"^\d+(\.\d+)?(Mi|Gi|M|G)$")  # --memory: "512Mi", "2Gi"


class ResourceRequirements(BaseModel):
    """Deploy sizing an agent declares for its on-demand backend (load-bearing
    change #3). Maps to Cloud Run --cpu/--memory/--concurrency/--max-instances;
    ``gpu`` (or a future strict-isolation flag) signals the agent belongs on a
    GPU/isolation backend (a future GKEBackend) rather than Cloud Run.

    All fields have defaults equal to the CloudRunBackend's current hardcoded
    behavior, so an agent that declares no resources deploys exactly as before.
    """
    cpu: str = "1"
    memory: str = "512Mi"
    concurrency: int = Field(default=1, ge=1)      # requests per instance (1 = per-request isolation)
    max_instances: int = Field(default=1, ge=1)
    gpu: bool = False

    @field_validator("cpu")
    @classmethod
    def _valid_cpu(cls, v: str) -> str:
        if not _CPU_RE.match(v.strip()):
            raise ValueError(f"Invalid cpu {v!r} (e.g. '1', '2', '500m')")
        return v.strip()

    @field_validator("memory")
    @classmethod
    def _valid_memory(cls, v: str) -> str:
        if not _MEMORY_RE.match(v.strip()):
            raise ValueError(f"Invalid memory {v!r} (e.g. '512Mi', '2Gi')")
        return v.strip()


HARD_MAX_RUNTIME_SECONDS = 3600   # ceiling for a single agent request

# apt package tokens permitted in system_packages. Debian names are lowercase
# letters/digits plus +.-, but we also allow version pins (pkg=1.2) and arch
# qualifiers (pkg:amd64). This is NOT a judgment about which packages are
# "safe" (arbitrary apt packages are allowed but flagged for the approver) — it purely
# blocks shell/Dockerfile injection by rejecting whitespace and metacharacters
# before the token reaches the generated `apt-get install` RUN line.
_APT_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+.:=~-]*$")


def _sanitize_apt_list(v: list[str]) -> list[str]:
    """Reject tokens that could break out of the `apt-get install` RUN line
    (injection guard, not a package allowlist — see _APT_TOKEN_RE)."""
    cleaned: list[str] = []
    for pkg in v:
        pkg = pkg.strip()
        if not pkg:
            continue
        if not _APT_TOKEN_RE.match(pkg):
            raise ValueError(f"Invalid apt package name: {pkg!r}")
        cleaned.append(pkg)
    return cleaned


# The entry module sits at the upload root. It is joined onto the agent's handler
# dir (server.upload_handler writes it) and into the Dockerfile, so it must be a
# bare file name: no path separators, no "..", no whitespace or control characters.
_ENTRY_FILE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.py")


def _check_entry_file(v: str) -> str:
    if not _ENTRY_FILE_RE.fullmatch(v) or ".." in v:
        raise ValueError(f"entry_file must be a bare .py file name at the upload root; got {v!r}")
    # A single-file upload is stored under this name without going through the
    # archive's reserved-name check (archive.RESERVED_IMPORT_NAMES).
    if v.split(".", 1)[0] in RESERVED_IMPORT_NAMES:
        raise ValueError(
            f"entry_file {v!r} uses a reserved platform package name "
            f"({', '.join(sorted(RESERVED_IMPORT_NAMES))}); rename it"
        )
    return v


# ── Tool sources for an `llm` agent ─────────────────────────────────────────────
# A discriminated union on ``source``. Each member carries exactly the fields that
# source needs and nothing else, so an impossible combination (e.g. an upload's
# `requirements` on a preset-tools agent) cannot be expressed.

class NoTools(BaseModel):
    """A single LLM turn: a system prompt and no tools."""
    source: Literal["none"] = "none"


class PresetTools(BaseModel):
    """Tools chosen by name from the built-in catalog
    (``agent_skeleton.system_tools.preset_tools``), e.g. ``["calculator"]``."""
    source: Literal["presets"] = "presets"
    names: list[str] = Field(default_factory=list)


class RawTools(BaseModel):
    """Caller-supplied Chat Completions tool schemas with no bodies.

    The model can *emit* these calls but nothing dispatches them, so this is only
    useful for prompt/router experiments. Previously the unwired ``LLMConfig.tools``
    list; it now has an explicit home rather than being silently ignored.
    """
    source: Literal["raw"] = "raw"
    schemas: list[dict[str, Any]] = Field(default_factory=list)


class UploadedTools(BaseModel):
    """Author-uploaded tool bodies + schemas — the old ``tool_loop`` type.

    The uploaded ``entry_file`` module (at the archive root) must expose
    module-level ``TOOL_SCHEMAS`` + ``TOOL_REGISTRY``, or a ``build_tools(config)``
    factory returning that pair. The factory is the closure-safe path: it lets the
    author bake ``extra`` (an API key, a contact email, a timeout, a result cap)
    into each tool WITHOUT the value becoming a tool parameter the model can see or
    set. Everything else — the loop, the handler, the A2A server — is generated
    (``agent_skeleton.spec.tool_loop_spec``).

    Uploads Python, so it is admin-approval-gated exactly like ``custom``
    (see ``upload_spec``).
    """
    source: Literal["upload"] = "upload"
    entry_file: str = "tools.py"            # module at the upload root exposing the tools
    python_version: str = "3.11"
    requirements: list[str] = Field(default_factory=list)
    system_packages: list[str] = Field(default_factory=list)   # apt, cluster build only
    install_project: bool = True            # pip install . if pyproject.toml/setup.py present at root
    extra: dict[str, Any] = Field(default_factory=dict)         # passed to build_tools(config)

    @field_validator("system_packages")
    @classmethod
    def _sanitize_apt(cls, v: list[str]) -> list[str]:
        return _sanitize_apt_list(v)

    @field_validator("entry_file")
    @classmethod
    def _bare_entry_file(cls, v: str) -> str:
        return _check_entry_file(v)


class OperationSpec(BaseModel):
    """One API operation exposed to the model as a typed tool.

    Maps 1:1 to ``agent_skeleton.system_tools.call_endpoint.OperationConfig``.
    ``params`` is a JSON-Schema ``properties`` dict; a param named ``{x}`` in
    ``path`` is a path parameter, otherwise ``param_in`` routes it to query/body/
    header (defaulting to query for GET/DELETE/HEAD, body otherwise)."""

    name: str = Field(..., min_length=1)
    method: str = "GET"
    path: str = "/"
    description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)     # JSON-Schema properties
    required: list[str] = Field(default_factory=list)
    param_in: dict[str, str] = Field(default_factory=dict)   # name -> query|body|header

    @field_validator("method")
    @classmethod
    def _known_method(cls, v: str) -> str:
        m = v.strip().upper()
        if m not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
            raise ValueError(f"Unsupported HTTP method: {v!r}")
        return m


class HttpTools(BaseModel):
    """Each operation of an external API as its own typed tool — the old
    ``endpoint_ops`` type.

    ``base_url`` + ``auth_env`` are the shared connection concerns; ``operations``
    are the typed tools the model calls. The token is read at call time from the env
    var NAMED by ``auth_env`` — the value is never stored here. With this source a
    ``system_prompt`` is optional: a routing prompt is generated from the operations.
    """
    source: Literal["http"] = "http"
    base_url: str
    operations: list[OperationSpec] = Field(..., min_length=1)
    auth_env: str | None = None      # NAME of the env var holding the token (never the token)
    auth_scheme: str = "Bearer"      # 'Bearer' | 'raw'

    @field_validator("base_url")
    @classmethod
    def _http_base(cls, v: str) -> str:
        from urllib.parse import urlsplit

        scheme = urlsplit(v).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"base_url must be http(s); got {v!r}")
        return v


ToolSource = Annotated[
    NoTools | PresetTools | RawTools | UploadedTools | HttpTools,
    Field(discriminator="source"),
]


# ── The three canonical configs ────────────────────────────────────────────────

class LLMConfig(BaseModel):
    """An LLM tool loop run by the shared engine. ``tools`` selects where the
    capabilities come from (see ``ToolSource``)."""
    system_prompt: str | None = None
    model: str = "openai/gpt-oss-120b"
    tools: ToolSource = Field(default_factory=NoTools)

    @model_validator(mode="after")
    def _prompt_required_unless_generated(self) -> "LLMConfig":
        # Every source except `http` needs an author-written prompt; `http` can
        # generate a routing prompt from the operations (prompt.build_operations_prompt).
        if self.tools.source != "http" and not (self.system_prompt or "").strip():
            raise ValueError("system_prompt is required unless tools.source is 'http'")
        return self


class ProxyConfig(BaseModel):
    """No LLM: relay each A2A task to an upstream URL the author hosts.
    Formerly ``api_proxy`` (still accepted as a legacy name)."""
    upstream_url: str
    auth_header: str | None = None


class CustomConfig(BaseModel):
    """An uploaded ``AgentHandler`` subclass that owns the whole flow."""
    class_name: str
    python_version: str = "3.11"
    requirements: list[str] = Field(default_factory=list)
    system_packages: list[str] = Field(default_factory=list)   # apt, cluster build only
    entry_file: str = "handler.py"          # adapter module at the upload root that defines class_name
    install_project: bool = True            # pip install . if pyproject.toml/setup.py present at root
    max_runtime_seconds: int = 1800         # per-request cap; clamped to HARD_MAX_RUNTIME_SECONDS
    credentials: list[CredentialRequirement] = Field(default_factory=list)   # P4a: user-owned creds this agent consumes
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("max_runtime_seconds")
    @classmethod
    def _clamp_runtime(cls, v: int) -> int:
        # Clamp to a sane window: non-positive falls back to the default; the
        # hard ceiling protects the node from a runaway request.
        if v <= 0:
            return 1800
        return min(v, HARD_MAX_RUNTIME_SECONDS)

    @field_validator("system_packages")
    @classmethod
    def _sanitize_apt(cls, v: list[str]) -> list[str]:
        return _sanitize_apt_list(v)

    @field_validator("entry_file")
    @classmethod
    def _bare_entry_file(cls, v: str) -> str:
        return _check_entry_file(v)


AnyConfig = LLMConfig | ProxyConfig | CustomConfig


# ── Handler-type names ──────────────────────────────────────────────────────────

# Legacy wire/DB names → the canonical type they became. Retained because stored
# rows and older API clients still use them; ``parse_config`` also knows how to read
# each one's *old config shape*.
LEGACY_HANDLER_ALIASES = {
    "api_proxy": "proxy",
    "tool_loop": "llm",       # → LLMConfig(tools=UploadedTools(...))
    "endpoint_ops": "llm",    # → LLMConfig(tools=HttpTools(...))
}

HandlerType = Literal["llm", "proxy", "custom", "api_proxy", "tool_loop", "endpoint_ops"]


def normalize_handler_type(handler_type: str) -> str:
    """Map a wire/stored handler_type to its canonical name. Unknown values are
    returned unchanged so the caller's own error path reports them."""
    return LEGACY_HANDLER_ALIASES.get(handler_type, handler_type)


# ── Uploaded-code accessor ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class UploadSpec:
    """The build/run parameters of a config that ships uploaded Python.

    One shape for both uploaded-code paths (``custom`` and ``llm`` +
    ``UploadedTools``) so the builder, the local subprocess backend, and the
    approval gate stop branching on handler_type. ``carries_upload`` used to be
    ``handler_type in ("custom", "tool_loop")``; asking the *config* is the more
    honest question and it survives the taxonomy change.
    """
    entry_file: str
    python_version: str
    requirements: list[str]
    system_packages: list[str]
    install_project: bool
    extra: dict[str, Any]


def upload_spec(config: AnyConfig) -> UploadSpec | None:
    """The uploaded-code parameters, or ``None`` if this config uploads no code.

    A truthy result means: admin approval is required, an entry file must be
    uploaded before the agent can start, and a per-agent image/venv is built.
    """
    if isinstance(config, CustomConfig):
        return UploadSpec(
            entry_file=config.entry_file,
            python_version=config.python_version,
            requirements=list(config.requirements),
            system_packages=list(config.system_packages),
            install_project=config.install_project,
            extra=dict(config.extra),
        )
    if isinstance(config, LLMConfig) and isinstance(config.tools, UploadedTools):
        t = config.tools
        return UploadSpec(
            entry_file=t.entry_file,
            python_version=t.python_version,
            requirements=list(t.requirements),
            system_packages=list(t.system_packages),
            install_project=t.install_project,
            extra=dict(t.extra),
        )
    return None


def carries_upload(config: AnyConfig) -> bool:
    """True if this config ships uploaded Python (→ approval + entry file required)."""
    return upload_spec(config) is not None


# ── Parsing (including the legacy shapes) ───────────────────────────────────────

def _llm_config_from_raw(raw: dict) -> LLMConfig:
    """Build an ``LLMConfig``, accepting the pre-consolidation ``llm`` shape.

    Old shape had ``preset_tools: list[str]`` and an unwired ``tools: list[dict]``
    of raw schemas. Both are folded into the ``tools`` union. Stored configs were
    written with ``model_dump()``, so an old row reliably has BOTH keys present
    (usually ``[]``) — hence the emptiness checks rather than key presence.
    """
    raw = dict(raw)
    if not isinstance(raw.get("tools"), dict):
        legacy_presets = raw.pop("preset_tools", None) or []
        legacy_raw_schemas = raw.pop("tools", None) or []
        if legacy_presets:
            raw["tools"] = {"source": "presets", "names": list(legacy_presets)}
        elif legacy_raw_schemas:
            raw["tools"] = {"source": "raw", "schemas": list(legacy_raw_schemas)}
        else:
            raw.pop("tools", None)          # fall through to the NoTools default
    else:
        raw.pop("preset_tools", None)       # canonical `tools` wins over a stale sibling
    return LLMConfig(**raw)


def _llm_config_from_tool_loop(raw: dict) -> LLMConfig:
    """Legacy ``tool_loop`` config → ``LLMConfig(tools=UploadedTools(...))``."""
    raw = dict(raw)
    return LLMConfig(
        system_prompt=raw.get("system_prompt"),
        model=raw.get("model", "openai/gpt-oss-120b"),
        tools=UploadedTools(
            entry_file=raw.get("entry_file", "tools.py"),
            python_version=raw.get("python_version", "3.11"),
            requirements=raw.get("requirements") or [],
            system_packages=raw.get("system_packages") or [],
            install_project=raw.get("install_project", True),
            extra=raw.get("extra") or {},
        ),
    )


def _llm_config_from_endpoint_ops(raw: dict) -> LLMConfig:
    """Legacy ``endpoint_ops`` config → ``LLMConfig(tools=HttpTools(...))``."""
    raw = dict(raw)
    return LLMConfig(
        system_prompt=raw.get("system_prompt"),
        model=raw.get("model", "openai/gpt-oss-120b"),
        tools=HttpTools(
            # .get, not [...]: a missing key must surface as a pydantic
            # ValidationError (→ HTTP 422), not a KeyError (→ 500).
            base_url=raw.get("base_url"),
            operations=raw.get("operations") or [],
            auth_env=raw.get("auth_env"),
            auth_scheme=raw.get("auth_scheme", "Bearer"),
        ),
    )


def parse_config(handler_type: str, raw: dict) -> AnyConfig:
    """Build the typed config for a handler_type, canonical or legacy.

    Dispatches on the handler_type *as given* (not normalized) because the legacy
    name is what identifies the legacy config shape: ``tool_loop`` and
    ``endpoint_ops`` both normalize to ``llm`` but carry different old payloads.
    """
    if handler_type == "llm":
        return _llm_config_from_raw(raw)
    if handler_type == "tool_loop":
        return _llm_config_from_tool_loop(raw)
    if handler_type == "endpoint_ops":
        return _llm_config_from_endpoint_ops(raw)
    if handler_type in ("proxy", "api_proxy"):
        return ProxyConfig(**raw)
    if handler_type == "custom":
        return CustomConfig(**raw)
    raise ValueError(f"Unknown handler_type: {handler_type}")


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1)
    handler_type: HandlerType
    oasf_skills: list[OASFSkill] = Field(..., min_length=1)
    config: AnyConfig
    # Optional deploy sizing (cloud backends). None → the backend's defaults, so
    # agents that don't declare resources deploy exactly as before.
    resources: ResourceRequirements | None = None

    @field_validator("config", mode="before")
    @classmethod
    def validate_config(cls, v: Any, info: Any) -> Any:
        # Discriminate the config by handler_type (declared before this field, so
        # available in info.data) rather than by pydantic's structural union match.
        # This is robust when configs share optional fields (e.g. an LLMConfig with
        # HttpTools could otherwise be mis-coerced) and it is the only way to know
        # which *legacy* shape a `tool_loop`/`endpoint_ops` payload is in.
        if isinstance(v, (LLMConfig, ProxyConfig, CustomConfig)):
            return v
        handler_type = info.data.get("handler_type")
        if handler_type and isinstance(v, dict):
            return parse_config(handler_type, v)
        return v

    @property
    def canonical_handler_type(self) -> str:
        """The type to persist. ``handler_type`` keeps whatever the caller sent so
        ``typed_config()`` can still recognise a legacy config shape."""
        return normalize_handler_type(self.handler_type)

    def typed_config(self) -> AnyConfig:
        if isinstance(self.config, (LLMConfig, ProxyConfig, CustomConfig)):
            return self.config
        return parse_config(self.handler_type, self.config)


class RejectRequest(BaseModel):
    reason: str = ""


class ApproveRequest(BaseModel):
    # The revision of the code the approver reviewed (GET /agents/{id}/handler).
    # When given, the approval is refused (409) unless the stored code still has it.
    revision: str | None = None


class SpawnedReport(BaseModel):
    agent_id: uuid.UUID
    # A report the service does not take has its PID queued for the host loop
    # to stop, so only the PID of a single process is accepted.
    pid: int = Field(gt=1)


class AgentResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    handler_type: str
    oasf_skills: list[OASFSkill]
    config: dict[str, Any]   # secret values redacted (server._public_config)
    port: int | None
    status: str
    approved: bool = True
    has_handler: bool = False
    is_archive: bool = False
    endpoint_url: str | None
    ads_cid: str | None
    created_at: datetime
    updated_at: datetime
