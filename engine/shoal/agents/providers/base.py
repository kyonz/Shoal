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

    def missing_setup(self, config: dict[str, Any]) -> str | None:
        """Why calls with this (gateway-resolved) config cannot work, when that is clear without calling.

        For example "no API key". The router reports it as a missing
        configuration that points at the gateway, instead of a failed call.
        """
        return None


class ProviderRefusal(Exception):
    pass


class ProviderUnavailable(Exception):
    """Transient provider failure (rate limit, overload, outage). Routers fall back on it."""


class ProviderAuthFailed(Exception):
    """The provider rejected the gateway's credentials (HTTP 401/403). Routers fall back on it, then report the gateway."""
