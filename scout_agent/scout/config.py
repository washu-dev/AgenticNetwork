import os
from dataclasses import dataclass, field

DEFAULT_SHARED_SECRET = "scout-dev-shared-secret-must-be-32+chars-long-PLACEHOLDER"


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    # Model + agent loop
    model: str = "openai/gpt-oss-120b"
    max_iterations: int = 8
    max_tool_calls: int = 12
    brave_timeout: float = 15.0
    response_max_tokens: int = 4096

    # Network (Phase 2)
    agent_name: str = field(
        default_factory=lambda: os.environ.get("SCOUT_AGENT_NAME", "Scout")
    )
    # HTTP/JSONRPC endpoint: where Scout's A2A server listens and what we
    # advertise in our card. Must be reachable from peer agents — set
    # SCOUT_HTTP_HOST to your LAN IP if peers run on other machines.
    http_host: str = field(
        default_factory=lambda: os.environ.get("SCOUT_HTTP_HOST", "0.0.0.0")
    )
    http_port: int = field(
        default_factory=lambda: int(os.environ.get("SCOUT_HTTP_PORT", "9104"))
    )
    # Externally-reachable URL we put in the card. Defaults to 127.0.0.1 which
    # only works for same-host callers; override with SCOUT_PUBLIC_HOST for
    # LAN/public access.
    public_host: str = field(
        default_factory=lambda: os.environ.get("SCOUT_PUBLIC_HOST", "127.0.0.1")
    )
    directory_endpoint: str = field(
        default_factory=lambda: os.environ.get(
            "DIRECTORY_ENDPOINT", "127.0.0.1:8889"
        )
    )
    peer_skill_filters: list[str] = field(
        default_factory=lambda: _csv_env("SCOUT_PEER_SKILLS")
    )

    @property
    def public_endpoint(self) -> str:
        return f"http://{self.public_host}:{self.http_port}/"
