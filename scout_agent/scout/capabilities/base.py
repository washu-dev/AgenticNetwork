from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CapabilityResult:
    ok: bool
    content: Any = None
    error: str | None = None
    meta: dict = field(default_factory=dict)


class Capability(ABC):
    name: str
    description: str
    input_schema: dict

    @abstractmethod
    async def invoke(self, args: dict) -> CapabilityResult: ...

    def tool_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class CapabilityRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, cap: Capability) -> None:
        if cap.name in self._caps:
            raise ValueError(f"capability already registered: {cap.name}")
        self._caps[cap.name] = cap

    def get(self, name: str) -> Capability | None:
        return self._caps.get(name)

    def all(self) -> list[Capability]:
        return list(self._caps.values())
