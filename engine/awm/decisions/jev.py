"""TypeSafe Jev (System One) decision provider.

Uses the official ``typesafe_sdk`` package (``pip install typesafe-sdk``);
AWM does not call an undocumented REST API. ``config``:

* ``api_key`` / ``api_key_secret`` - otherwise the SDK reads ``TYPESAFE_API_KEY``
* ``model`` - optional model id

Jev is hosted-only and closed-weight: route data-resident tenants to the
``llm`` provider instead. Pricing: $0.042 per million input tokens, output free.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from awm.core.errors import StepError
from awm.core.models import DecisionQuestion
from awm.decisions.base import (
    Answer,
    DecisionProvider,
    DecisionRequest,
    DecisionResult,
    answer_from_probabilities,
)

PRICE_PER_MTOK_INPUT = 0.042


def wire_questions(questions: dict[str, DecisionQuestion]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, q in questions.items():
        if q.type == "choice":
            out[key] = {"type": "choice", "instructions": q.instructions, "criteria": dict(q.options)}
        elif q.type == "score":
            out[key] = {"type": "score", "instructions": q.instructions, "criteria": list(q.levels)}
        else:
            out[key] = {"type": "noul", "instructions": q.instructions}
    return out


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__") and not isinstance(value, dict):
        return {k: _plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def parse_answer(question: DecisionQuestion, raw: dict[str, Any]) -> Answer:
    if question.type == "boolean":
        p = float(raw.get("noul", raw.get("probability", 0.0)))
        answer = answer_from_probabilities(question, {"true": p})
        if raw.get("confidence") is not None:
            answer.confidence = float(raw["confidence"])
        return answer
    probabilities = raw.get("probabilities")
    keys = list(question.options) if question.type == "choice" else list(question.levels)
    if isinstance(probabilities, list) and len(probabilities) == len(keys):
        probabilities = dict(zip(keys, probabilities, strict=True))
    if not isinstance(probabilities, dict) or not probabilities:
        if question.type == "choice":
            probabilities = {raw["choice"]: 1.0}
        else:
            score = raw["score"]
            if isinstance(score, int) and 0 <= score < len(keys):
                index = score
            elif isinstance(score, str) and score in keys:
                index = keys.index(score)
            else:
                index = round(min(max(float(score), 0.0), 1.0) * (len(keys) - 1))
            probabilities = {keys[index]: 1.0}
    answer = answer_from_probabilities(question, probabilities)
    if question.type == "choice" and raw.get("choice") in keys:
        answer.value = raw["choice"]
    if raw.get("confidence") is not None:
        answer.confidence = float(raw["confidence"])
    return answer


class JevProvider(DecisionProvider):
    name = "jev"

    def __init__(self, client_factory: Callable[[dict[str, Any]], Any] | None = None):
        self.client_factory = client_factory

    def _client(self, config: dict[str, Any]) -> Any:
        if self.client_factory is not None:
            return self.client_factory(config)
        try:
            import typesafe_sdk
        except ImportError as exc:
            raise StepError("the 'jev' decision provider needs the typesafe-sdk package installed") from exc
        kwargs = {"api_key": config["api_key"]} if config.get("api_key") else {}
        return typesafe_sdk.TypeSafeClient(**kwargs)

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        client = self._client(request.config)
        kwargs: dict[str, Any] = {"state": request.state, "questions": wire_questions(request.questions)}
        if request.config.get("model"):
            kwargs["model"] = request.config["model"]
        method = client.system_one
        if inspect.iscoroutinefunction(method):
            response = await method(**kwargs)
        else:
            response = await asyncio.to_thread(method, **kwargs)
            if inspect.isawaitable(response):
                response = await response
        data = _plain(response)
        answers_raw = data.get("answers", {})
        answers = {key: parse_answer(q, _plain(answers_raw[key])) for key, q in request.questions.items() if key in answers_raw}
        raw_usage = data.get("usage") or {}
        usage = {"decision_input_tokens": float(raw_usage.get("input_tokens", 0))}
        usage["cost_usd"] = round(usage["decision_input_tokens"] * PRICE_PER_MTOK_INPUT / 1_000_000, 8)
        return DecisionResult(answers=answers, provider=self.name, usage=usage)
