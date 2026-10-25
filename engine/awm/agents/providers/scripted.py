"""Deterministic provider for tests, demos and offline development.

The script lives in the agent's ``provider_config.script``: a list of turns,
each with optional ``text`` and ``tool_calls`` (``{name, input}``). Inputs may
use ``{{ prompt }}`` style placeholders via the ``{{ }}`` syntax with
``prompt`` and ``last_result`` in scope. When the script runs out, the
provider calls ``finish`` with ``provider_config.final_outputs``.

Tests can also register a Python callable with :func:`set_script_function`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from awm.agents.providers.base import ModelProvider, ModelRequest, ModelResponse
from awm.core.expressions import render

ScriptFn = Callable[[ModelRequest], ModelResponse | Awaitable[ModelResponse]]
_functions: dict[str, ScriptFn] = {}


def set_script_function(name: str, fn: ScriptFn) -> None:
    _functions[name] = fn


def _last_tool_result(messages: list[dict[str, Any]]) -> Any:
    for message in reversed(messages):
        if message["role"] != "user" or not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = "".join(c.get("text", "") for c in content)
                try:
                    return json.loads(content)
                except (TypeError, ValueError):
                    return content
    return None


class ScriptedProvider(ModelProvider):
    name = "scripted"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        cfg = request.config
        if cfg.get("function"):
            result = _functions[cfg["function"]](request)
            if isinstance(result, Awaitable):
                result = await result
            return result

        turn_index = sum(1 for m in request.messages if m["role"] == "assistant")
        script: list[dict[str, Any]] = cfg.get("script", [])
        first_user = request.messages[0]["content"]
        prompt = first_user if isinstance(first_user, str) else ""
        scope = {"prompt": prompt, "last_result": _last_tool_result(request.messages), "turn": turn_index}

        content: list[dict[str, Any]] = []
        if turn_index < len(script):
            turn = script[turn_index]
            if turn.get("text"):
                content.append({"type": "text", "text": render(turn["text"], scope)})
            for i, call in enumerate(turn.get("tool_calls", [])):
                content.append(
                    {
                        "type": "tool_use",
                        "id": f"toolu_{turn_index}_{i}",
                        "name": call["name"],
                        "input": render(call.get("input", {}), scope),
                    }
                )
        else:
            outputs = render(cfg.get("final_outputs", {}), scope)
            content.append({"type": "text", "text": "Done."})
            content.append({"type": "tool_use", "id": f"toolu_{turn_index}_finish", "name": "finish", "input": outputs})
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return ModelResponse(
            content=content,
            stop_reason=stop,
            usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.0},
            model="scripted",
        )
