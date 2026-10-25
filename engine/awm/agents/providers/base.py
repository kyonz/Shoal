"""Model provider contract.

Messages and content blocks use the Anthropic Messages shape (``text``,
``tool_use``, ``tool_result``, ``thinking`` blocks as dicts) as the neutral
format; other providers translate to and from it.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ModelRequest:
    model: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[ToolDef]
    max_tokens: int = 32000
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    content: list[dict[str, Any]]
    stop_reason: str
    usage: dict[str, float] = field(default_factory=dict)
    model: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class ModelProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class ProviderRefusal(Exception):
    pass
