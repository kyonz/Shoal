"""Graders: score one eval case from 0 to 1.

Every grader gets the same scope: ``outputs`` (the run's outputs), ``expect``
and ``inputs`` (from the case), ``case`` (the whole case) and ``run``
(``id``, ``status``, ``error``, ``usage``, ``duration_ms``, ``workflow_version``).
A grader that raises is recorded with score 0 and its error, so one broken
grader never loses the rest of the case.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from shoal.agents.providers.base import ModelRequest, ToolDef
from shoal.core.expressions import evaluate_expression, render
from shoal.core.models import EvalSpec, Grader
from shoal.evals.schema import validate

if TYPE_CHECKING:
    from shoal.engine import Engine

JUDGE_SYSTEM = """You are an impartial evaluator. You grade the output of an AI workflow against a rubric.
Read the rubric, the case (inputs and expectations) and the output, reason briefly about how well the
output meets the rubric, then call the `verdict` tool exactly once with your reasoning and one level."""


@dataclass
class GraderScore:
    score: float
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _clamp(value: float) -> float:
    return round(min(max(float(value), 0.0), 1.0), 4)


def _expression(grader: Grader, scope: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    expr = grader.check or ""
    if expr.strip().startswith("{{"):
        value = render(expr, scope)
    else:
        value = evaluate_expression(expr, scope)
    if isinstance(value, bool) or value is None:
        return (1.0 if value else 0.0), {"value": value}
    if isinstance(value, int | float):
        return _clamp(value), {"value": value}
    if isinstance(value, str):
        return (0.0 if value.strip().lower() in ("", "false", "0", "no", "none", "null") else 1.0), {"value": value}
    return (1.0 if value else 0.0), {"value": str(value)[:200]}


def _json_schema(grader: Grader, scope: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    subject = render(grader.subject, scope)
    if isinstance(subject, str):
        try:
            subject = json.loads(subject)
        except ValueError:
            pass
    errors = validate(subject, grader.schema_ or {})
    return (0.0 if errors else 1.0), ({"errors": errors[:20]} if errors else {})


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str, indent=2)


async def _llm_judge(engine: Engine, tenant_id: str, grader: Grader, scope: dict[str, Any]) -> tuple[float, dict[str, Any], float]:
    config = render(dict(grader.config), scope)
    effort = config.pop("effort", None)
    target = await engine.models.gateway_target(tenant_id, grader.gateway, grader.model, effort=effort, overrides=config)
    tool = ToolDef(
        name="verdict",
        description="Record the grade.",
        input_schema={
            "type": "object",
            "properties": {
                "reasoning": {"type": "string", "description": "Two or three sentences."},
                "level": {"type": "string", "enum": list(grader.levels)},
            },
            "required": ["reasoning", "level"],
        },
    )
    levels = "\n".join(f"- `{level}`" for level in grader.levels)
    prompt = (
        f"# Rubric\n\n{grader.rubric}\n\nLevels, worst first:\n{levels}\n\n"
        f"# Case\n\n<inputs>\n{_as_text(scope.get('inputs'))}\n</inputs>\n"
        f"<expect>\n{_as_text(scope.get('expect'))}\n</expect>\n\n"
        f"# Output\n\n<output>\n{_as_text(render(grader.subject, scope))}\n</output>"
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    cost = 0.0
    for _ in range(2):
        response = await target.provider.complete(
            ModelRequest(model=grader.model, system=JUDGE_SYSTEM, messages=messages, tools=[tool], max_tokens=2048, config=target.config)
        )
        cost += float((response.usage or {}).get("cost_usd", 0.0))
        call = next((b for b in response.tool_uses if b["name"] == "verdict"), None)
        level = (call or {}).get("input", {}).get("level")
        if level in grader.levels:
            index = grader.levels.index(level)
            detail = {"level": level, "reasoning": call["input"].get("reasoning", ""), "judge": f"{grader.gateway}:{grader.model}"}  # type: ignore[index]
            return round(index / (len(grader.levels) - 1), 4), detail, cost
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": f"Call the `verdict` tool with one of: {', '.join(grader.levels)}."},
        ]
    raise ValueError("judge model did not return a valid verdict")


async def _decision(
    engine: Engine, tenant_id: str, spec: EvalSpec, grader: Grader, scope: dict[str, Any]
) -> tuple[float, dict[str, Any], float]:
    assert grader.question is not None
    result = await engine.decisions.decide(
        tenant_id,
        render(grader.state, scope),
        {"grade": grader.question},
        provider=grader.provider or spec.decision_provider,
        config=render(dict(grader.config), scope),
    )
    answer = result.answers["grade"]
    detail = {"value": answer.value, "confidence": answer.confidence, "probabilities": answer.probabilities, "provider": result.provider}
    return _clamp(answer.score), detail, float(result.usage.get("cost_usd", 0.0))


async def grade(engine: Engine, tenant_id: str, spec: EvalSpec, grader: Grader, scope: dict[str, Any]) -> GraderScore:
    cost = 0.0
    try:
        if grader.type == "expression":
            score, detail = _expression(grader, scope)
        elif grader.type == "json_schema":
            score, detail = _json_schema(grader, scope)
        elif grader.type == "llm_judge":
            score, detail, cost = await _llm_judge(engine, tenant_id, grader, scope)
        else:
            score, detail, cost = await _decision(engine, tenant_id, spec, grader, scope)
    except Exception as exc:  # noqa: BLE001 - a broken grader scores 0 with its error
        return GraderScore(score=0.0, passed=False, error=str(exc)[:500], cost_usd=cost)
    return GraderScore(score=score, passed=score >= spec.threshold, detail=detail, cost_usd=cost)


async def grade_case(engine: Engine, tenant_id: str, spec: EvalSpec, scope: dict[str, Any]) -> tuple[dict[str, Any], float, float]:
    """Run every grader. Returns (scores by grader, weighted score, grading cost)."""
    scores: dict[str, Any] = {}
    total = weight = cost = 0.0
    for grader in spec.graders:
        result = await grade(engine, tenant_id, spec, grader, scope)
        scores[grader.name] = result.to_dict()
        total += result.score * grader.weight
        weight += grader.weight
        cost += result.cost_usd
    return scores, round(total / weight, 4) if weight else 0.0, cost
