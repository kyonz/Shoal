"""OpenAI-compatible chat completions provider (OpenRouter, vLLM, Ollama, LiteLLM, Azure OpenAI...).

Translates Shoal's neutral (Anthropic-shaped) messages to the ``/chat/completions``
wire format over plain HTTP, so no extra SDK is needed. ``provider_config``:

* ``base_url`` (required), ``api_key`` (or ``api_key_secret``), ``headers``
* ``pricing`` - ``[input, output]`` USD per million tokens, for cost reporting
* ``extra_body`` - merged into every request (e.g. ``{"provider": {...}}`` on OpenRouter)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from shoal.agents.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderUnavailable

if TYPE_CHECKING:
    from shoal.engine import Engine

STOP_REASONS = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
            continue
        if message["role"] == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})},
                }
                for b in content
                if b.get("type") == "tool_use"
            ]
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
            continue
        texts = []
        for block in content:
            if block.get("type") == "tool_result":
                result = block.get("content")
                if isinstance(result, list):
                    result = "".join(c.get("text", "") for c in result)
                out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": str(result)})
            elif block.get("type") == "text":
                texts.append(block["text"])
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def from_openai_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except ValueError:
            args = {"_raw": call["function"].get("arguments")}
        content.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": args})
    return content


class OpenAICompatibleProvider(ModelProvider):
    name = "openai-compatible"
    description = "Any OpenAI-compatible Chat Completions API: LiteLLM, Portkey, OpenRouter, vLLM, Ollama, ..."

    def __init__(self) -> None:
        self.transport: httpx.AsyncBaseTransport | None = None

    def setup(self, engine: Engine) -> None:
        self.transport = engine.http_transport

    async def complete(self, request: ModelRequest) -> ModelResponse:
        cfg = request.config
        if not cfg.get("base_url"):
            raise ValueError("openai-compatible provider needs 'base_url'")
        headers = {**(cfg.get("headers") or {})}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        body: dict[str, Any] = {
            "model": request.model,
            "messages": to_openai_messages(request.system, request.messages),
            "max_tokens": request.max_tokens,
            **(cfg.get("extra_body") or {}),
        }
        if request.tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in request.tools
            ]
        try:
            async with httpx.AsyncClient(timeout=600, transport=self.transport) as client:
                response = await client.post(f"{cfg['base_url'].rstrip('/')}/chat/completions", json=body, headers=headers)
        except httpx.TransportError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderUnavailable(f"{response.status_code}: {response.text[:500]}")
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        raw_usage = data.get("usage") or {}
        usage: dict[str, float] = {
            "input_tokens": raw_usage.get("prompt_tokens", 0),
            "output_tokens": raw_usage.get("completion_tokens", 0),
        }
        price_in, price_out = cfg.get("pricing") or (0.0, 0.0)
        usage["cost_usd"] = round((usage["input_tokens"] * price_in + usage["output_tokens"] * price_out) / 1_000_000, 6)
        return ModelResponse(
            content=from_openai_message(choice["message"]),
            stop_reason=STOP_REASONS.get(choice.get("finish_reason") or "stop", "end_turn"),
            usage=usage,
            model=data.get("model", request.model),
        )
