import re
from uuid import uuid4

from a2a.types import AgentCard, Message, Part, Role, TextPart
from agntcy_app_sdk.semantic.a2a.client.factory import A2AClientFactory

from scout.capabilities.base import Capability, CapabilityResult


def _safe_tool_name(name: str) -> str:
    """OpenAI tool names allow [a-zA-Z0-9_-]+ up to 64 chars."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return s[:50] or "peer"


def _summarize_skills(card: AgentCard) -> str:
    parts: list[str] = []
    for s in card.skills or []:
        desc = (s.description or s.name or s.id or "").strip()
        if desc:
            parts.append(desc)
    return "; ".join(parts) if parts else "(no skills declared)"


class A2APeerCapability(Capability):
    """Wrap a discovered remote agent as a Capability.

    From the ReAct loop's perspective this is identical to a Brave search tool —
    it has a tool name, description, JSON-schema input, and an async invoke().
    The LLM decides when to call it based on the description (which is built
    from the peer's declared skills).
    """

    input_schema = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": (
                    "The natural-language request to send to the peer agent. "
                    "Be specific about what you need back."
                ),
            }
        },
        "required": ["message"],
        "additionalProperties": False,
    }

    def __init__(self, peer_card: AgentCard, client_factory: A2AClientFactory) -> None:
        safe = _safe_tool_name(peer_card.name)
        self.name = f"peer_{safe}"
        self.description = (
            f"Call remote agent '{peer_card.name}'. "
            f"{(peer_card.description or '').strip()} "
            f"Capabilities: {_summarize_skills(peer_card)}. "
            "Use when the user's request matches this peer's specialty AND public "
            "web search alone is insufficient."
        ).strip()
        self._card = peer_card
        self._factory = client_factory
        self._client = None  # lazy

    async def invoke(self, args: dict) -> CapabilityResult:
        message_text = (args or {}).get("message", "").strip()
        if not message_text:
            return CapabilityResult(ok=False, error="empty message")

        try:
            if self._client is None:
                self._client = await self._factory.create(self._card)
        except Exception as e:
            return CapabilityResult(
                ok=False, error=f"connect failed for {self._card.name}: {e}"
            )

        request = Message(
            role=Role.user,
            message_id=str(uuid4()),
            parts=[Part(root=TextPart(text=message_text))],
        )

        try:
            chunks: list[str] = []
            async for event in self._client.send_message(request=request):
                if isinstance(event, Message):
                    for part in event.parts:
                        root = getattr(part, "root", part)
                        text = getattr(root, "text", None)
                        if text:
                            chunks.append(text)
        except Exception as e:
            return CapabilityResult(
                ok=False, error=f"peer call failed for {self._card.name}: {e}"
            )

        if not chunks:
            return CapabilityResult(
                ok=False, error=f"peer {self._card.name} returned no text"
            )
        return CapabilityResult(ok=True, content={"reply": "".join(chunks)})
