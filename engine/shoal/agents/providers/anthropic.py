"""Anthropic (Claude) provider using the official ``anthropic`` SDK."""

from __future__ import annotations

import os
from typing import Any

import anthropic

from shoal.agents.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderUnavailable

# USD per million tokens (input, output). Used for budgets and cost reporting.
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def estimate_cost(model: str, usage: dict[str, float]) -> float:
    price_in, price_out = PRICING.get(model, (4.0, 20.0))
    tokens_in = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) * 1.25
    tokens_in += usage.get("cache_read_input_tokens", 0) * 0.1
    return round((tokens_in * price_in + usage.get("output_tokens", 0) * price_out) / 1_000_000, 6)


class AnthropicProvider(ModelProvider):
    """Claude via the Messages API.

    ``provider_config`` keys: ``api_key_secret`` (resolved by the agent step into
    ``api_key``), ``base_url`` (any Anthropic-compatible gateway), ``headers``,
    ``effort`` (low..max), ``fallbacks`` ("default" enables server-side refusal
    fallbacks; set to null to disable, e.g. for gateways that reject it).
    """

    name = "anthropic"

    def __init__(self) -> None:
        self._clients: dict[tuple[Any, ...], anthropic.AsyncAnthropic] = {}

    def _client(self, api_key: str | None, base_url: str | None, headers: dict[str, str]) -> anthropic.AsyncAnthropic:
        key = (api_key, base_url, tuple(sorted(headers.items())))
        if key not in self._clients:
            self._clients[key] = anthropic.AsyncAnthropic(
                api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
                base_url=base_url,
                default_headers=headers or None,
                max_retries=4,
            )
        return self._clients[key]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        cfg = request.config
        client = self._client(cfg.get("api_key"), cfg.get("base_url"), cfg.get("headers") or {})
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": request.messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": cfg.get("effort", "high")},
        }
        if request.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in request.tools
            ]
        extra_headers: dict[str, str] = {}
        extra_body: dict[str, Any] = {}
        fallbacks = cfg.get("fallbacks", "default")
        if fallbacks:
            extra_headers["anthropic-beta"] = SERVER_FALLBACK_BETA
            extra_body["fallbacks"] = fallbacks
        try:
            async with client.messages.stream(**params, extra_headers=extra_headers, extra_body=extra_body) as stream:
                message = await stream.get_final_message()
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            raise ProviderUnavailable(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code in (408, 409, 529) or exc.status_code >= 500:
                raise ProviderUnavailable(str(exc)) from exc
            raise

        content = [block.model_dump(mode="json", exclude_none=True) for block in message.content]
        usage_obj = message.usage
        usage: dict[str, float] = {
            "input_tokens": usage_obj.input_tokens or 0,
            "output_tokens": usage_obj.output_tokens or 0,
            "cache_read_input_tokens": getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
        }
        usage["cost_usd"] = estimate_cost(message.model or request.model, usage)
        return ModelResponse(
            content=content, stop_reason=message.stop_reason or "end_turn", usage=usage, model=message.model
        )
