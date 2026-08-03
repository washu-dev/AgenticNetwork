"""Pydantic models for the registration service API."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from registration_service.credential_catalog import is_known_type


class OASFSkill(BaseModel):
    name: str
    id: int | None = None
    key: str | None = None   # canonical path-style id used for the card skill.id + ADS routing


class CredentialRequirement(BaseModel):
    """A user-owned credential a custom agent consumes (P4a).

    ``type`` is a canonical catalog key (e.g. ``openai_api_key``) or a
    ``custom:<slug>`` escape — see ``registration_service/credential_catalog.py``.
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


class LLMConfig(BaseModel):
    system_prompt: str
    model: str = "openai/gpt-oss-120b"
    # Preset tools chosen by name from agent_skeleton.system_tools.preset_tools
    # (e.g. ["calculator", "current_time"]). Empty => a prompt-only LLM turn.
    preset_tools: list[str] = Field(default_factory=list)
    # Reserved for future user-supplied raw tool schemas (not yet wired; see
    # docs/WRAPPER_PRESETS_AND_ENDPOINT_MANAGER_PLAN.md, decision D2).
    tools: list[dict[str, Any]] = Field(default_factory=list)


class APIProxyConfig(BaseModel):
    upstream_url: str
    auth_header: str | None = None


class OperationSpec(BaseModel):
    """One API operation exposed to the model as a typed tool (Feature B).

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


class EndpointOpsConfig(BaseModel):
    """A typed per-operation endpoint-wrapper agent (Feature B): an LLM loop over an
    external API where each operation is its own typed tool. ``base_url`` + ``auth_env``
    are the shared connection concerns; ``operations`` are the typed tools the model
    calls. The token is read at call time from the env var NAMED by ``auth_env`` —
    the value is never stored here."""

    base_url: str
    operations: list[OperationSpec] = Field(..., min_length=1)
    auth_env: str | None = None      # NAME of the env var holding the token (never the token)
    auth_scheme: str = "Bearer"      # 'Bearer' | 'raw'
    system_prompt: str | None = None  # optional override; otherwise a routing prompt is generated
    model: str = "openai/gpt-oss-120b"

    @field_validator("base_url")
    @classmethod
    def _http_base(cls, v: str) -> str:
        from urllib.parse import urlsplit

        scheme = urlsplit(v).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"base_url must be http(s); got {v!r}")
        return v


HARD_MAX_RUNTIME_SECONDS = 3600   # ceiling for a single agent request (D11)

# apt package tokens permitted in system_packages. Debian names are lowercase
# letters/digits plus +.-, but we also allow version pins (pkg=1.2) and arch
# qualifiers (pkg:amd64). This is NOT a judgment about which packages are
# "safe" (D10 permits arbitrary apt, flagged for the approver) — it purely
# blocks shell/Dockerfile injection by rejecting whitespace and metacharacters
# before the token reaches the generated `apt-get install` RUN line.
_APT_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+.:=~-]*$")


class CustomConfig(BaseModel):
    class_name: str
    python_version: str = "3.11"
    requirements: list[str] = Field(default_factory=list)
    system_packages: list[str] = Field(default_factory=list)   # apt, cluster build only (D10)
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
        # Reject tokens that could break out of the `apt-get install` RUN line
        # (injection guard, not a package allowlist — see _APT_TOKEN_RE).
        cleaned: list[str] = []
        for pkg in v:
            pkg = pkg.strip()
            if not pkg:
                continue
            if not _APT_TOKEN_RE.match(pkg):
                raise ValueError(f"Invalid apt package name: {pkg!r}")
            cleaned.append(pkg)
        return cleaned


class ToolLoopConfig(BaseModel):
    """An uploaded LLM tool-loop agent: the author supplies the tool bodies +
    schemas + a system prompt, and the service builds the loop / handler / A2A
    server around them via the shared ``agent_skeleton`` engine.

    Like ``custom`` it uploads Python and is admin-approved; unlike ``custom`` there
    is NO ``AgentHandler`` — the uploaded ``entry_file`` module exposes the tool
    contract instead: module-level ``TOOL_SCHEMAS`` + ``TOOL_REGISTRY`` (Chat
    Completions schemas + a ``{name: fn}`` map), OR a ``build_tools(config)``
    factory returning that pair (the closure-safe way to bake in secrets without
    exposing them as tool parameters). Everything else — the tool loop, the
    handler, the A2A wrapper — is generated (``agent_skeleton.spec.tool_loop_spec``).
    """
    system_prompt: str
    model: str = "openai/gpt-oss-120b"
    entry_file: str = "tools.py"            # module at the upload root exposing the tools
    python_version: str = "3.11"
    requirements: list[str] = Field(default_factory=list)
    system_packages: list[str] = Field(default_factory=list)   # apt, cluster build only (D10)
    install_project: bool = True            # pip install . if pyproject.toml/setup.py present at root
    extra: dict[str, Any] = Field(default_factory=dict)         # passed to build_tools(config)

    @field_validator("system_packages")
    @classmethod
    def _sanitize_apt(cls, v: list[str]) -> list[str]:
        # Same injection guard as CustomConfig (not a package allowlist — _APT_TOKEN_RE).
        cleaned: list[str] = []
        for pkg in v:
            pkg = pkg.strip()
            if not pkg:
                continue
            if not _APT_TOKEN_RE.match(pkg):
                raise ValueError(f"Invalid apt package name: {pkg!r}")
            cleaned.append(pkg)
        return cleaned


AnyConfig = LLMConfig | APIProxyConfig | CustomConfig | EndpointOpsConfig | ToolLoopConfig


def parse_config(handler_type: str, raw: dict) -> AnyConfig:
    """Build the typed config model for a handler_type from a raw dict."""
    if handler_type == "llm":
        return LLMConfig(**raw)
    if handler_type == "api_proxy":
        return APIProxyConfig(**raw)
    if handler_type == "custom":
        return CustomConfig(**raw)
    if handler_type == "endpoint_ops":
        return EndpointOpsConfig(**raw)
    if handler_type == "tool_loop":
        return ToolLoopConfig(**raw)
    raise ValueError(f"Unknown handler_type: {handler_type}")


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1)
    handler_type: Literal["llm", "api_proxy", "custom", "endpoint_ops", "tool_loop"]
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
        # This is robust when configs share optional fields (e.g. an endpoint_ops
        # config with a system_prompt could otherwise be mis-coerced to LLMConfig).
        if isinstance(v, (LLMConfig, APIProxyConfig, CustomConfig, EndpointOpsConfig, ToolLoopConfig)):
            return v
        handler_type = info.data.get("handler_type")
        if handler_type and isinstance(v, dict):
            return parse_config(handler_type, v)
        return v

    def typed_config(self) -> AnyConfig:
        raw = self.config if isinstance(self.config, dict) else self.config.model_dump()
        return parse_config(self.handler_type, raw)


class RejectRequest(BaseModel):
    reason: str = ""


class SpawnedReport(BaseModel):
    agent_id: uuid.UUID
    pid: int


class AgentResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    handler_type: str
    oasf_skills: list[OASFSkill]
    config: dict[str, Any]
    port: int | None
    status: str
    approved: bool = True
    has_handler: bool = False
    is_archive: bool = False
    endpoint_url: str | None
    ads_cid: str | None
    created_at: datetime
    updated_at: datetime
