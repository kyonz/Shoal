"""LLM-backed decision provider: any model through the configured gateways.

The self-hostable fallback for typed decisions. The model is asked for a
probability per option/level (or that a statement is true) through a single
``answer`` tool, and answers are normalised like any other provider's.

``config``: ``gateway`` (Gateway resource or provider plugin, default
``anthropic``), ``model`` (default ``claude-haiku-5-5``), ``effort`` (default
``low``), plus any provider settings (e.g. ``function`` for ``scripted``). The tenant's
``default`` model policy allow/deny lists apply, as they do to agents.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from shoal.agents.providers.base import ModelRequest, ToolDef
from shoal.core.errors import StepError
from shoal.core.models import DecisionQuestion
from shoal.decisions.base import DecisionProvider, DecisionRequest, DecisionResult, answer_from_probabilities
from shoal.llm.router import DEFAULT_POLICY
from shoal.runtime.executor import add_usage

if TYPE_CHECKING:
    from shoal.engine import Engine

SYSTEM = """You are a decision model. You never write prose or code: you answer typed questions about a
state by calling the `answer` tool exactly once. Answer every question independently, and give calibrated
probabilities: when the state does not settle a question, spread probability rather than guessing."""


def question_schema(question: DecisionQuestion) -> dict[str, Any]:
    if question.type == "boolean":
        return {
            "type": "object",
            "properties": {"probability_true": {"type": "number", "minimum": 0, "maximum": 1}},
            "required": ["probability_true"],
        }
    keys = list(question.options) if question.type == "choice" else list(question.levels)
    return {
        "type": "object",
        "properties": {
            "probabilities": {
                "type": "object",
                "properties": {k: {"type": "number", "minimum": 0, "maximum": 1} for k in keys},
                "required": keys,
                "description": "Probability for each key; they should sum to 1.",
            }
        },
        "required": ["probabilities"],
    }


def describe(questions: dict[str, DecisionQuestion]) -> str:
    lines = []
    for key, q in questions.items():
        lines.append(f"## {key} ({q.type})\n{q.instructions}")
        if q.type == "choice":
            lines.extend(f"- `{k}`: {v}" for k, v in q.options.items())
        elif q.type == "score":
            lines.append("Levels, lowest first:")
            lines.extend(f"- `{level}`" for level in q.levels)
        else:
            lines.append("Give the probability that this statement is true.")
    return "\n\n".join(lines)


class LLMDecisionProvider(DecisionProvider):
    name = "llm"

    def setup(self, engine: Engine) -> None:
        self.engine = engine

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        cfg = dict(request.config)
        gateway = cfg.pop("gateway", "anthropic")
        model = cfg.pop("model", "claude-haiku-5-5")
        effort = cfg.pop("effort", "low")
        models = self.engine.models
        if not models.permitted(model, await models.policy(request.tenant_id, DEFAULT_POLICY)):
            raise StepError(f"decision model '{model}' is not permitted by the tenant's model policy")
        target = await models.gateway_target(
            request.tenant_id, gateway, model, effort=effort, overrides=cfg
        )
        tool = ToolDef(
            name="answer",
            description="Record the answers to all questions.",
            input_schema={
                "type": "object",
                "properties": {k: question_schema(q) for k, q in request.questions.items()},
                "required": list(request.questions),
            },
        )
        state = request.state if isinstance(request.state, str) else json.dumps(request.state, default=str, indent=2)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": f"<state>\n{state}\n</state>\n\n# Questions\n\n{describe(request.questions)}"}
        ]
        usage: dict[str, float] = {}
        for _ in range(2):
            response = await target.provider.complete(
                ModelRequest(model=model, system=SYSTEM, messages=messages, tools=[tool], max_tokens=4096, config=target.config)
            )
            usage = add_usage(usage, response.usage)
            call = next((b for b in response.tool_uses if b["name"] == "answer"), None)
            if call is not None:
                raw = call.get("input") or {}
                answers = {}
                for key, q in request.questions.items():
                    entry = raw.get(key) or {}
                    if q.type == "boolean":
                        answers[key] = answer_from_probabilities(q, {"true": float(entry.get("probability_true", 0.5))})
                    else:
                        answers[key] = answer_from_probabilities(q, entry.get("probabilities") or {})
                return DecisionResult(answers=answers, provider=f"{self.name}:{model}", usage=usage)
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": "Call the `answer` tool now."},
            ]
        raise StepError("decision model did not call the answer tool", retryable=True)
