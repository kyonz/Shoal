"""Decision provider contract (System One style typed decisions, §7.5 of the architecture).

A provider answers named, typed questions about a ``state`` (text or JSON):

* ``choice``  - pick one option key; probabilities per option
* ``score``   - place the state on an ordered rubric (lowest level first)
* ``boolean`` - probability that a statement is true (Jev's *noul*)

Every answer is normalised to :class:`Answer` so workflows can branch on
``value``, ``score`` (0..1) and ``confidence`` regardless of provider.
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from typing import Any

from shoal.core.models import DecisionQuestion


@dataclass
class Answer:
    value: Any
    probabilities: dict[str, float]
    confidence: float
    score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionRequest:
    tenant_id: str
    state: Any
    questions: dict[str, DecisionQuestion]
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionResult:
    answers: dict[str, Answer]
    provider: str
    usage: dict[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "answers": {k: a.to_dict() for k, a in self.answers.items()},
            "provider": self.provider,
            "usage": self.usage,
            "latency_ms": round(self.latency_ms, 1),
        }


class DecisionProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def decide(self, request: DecisionRequest) -> DecisionResult: ...


def answer_from_probabilities(question: DecisionQuestion, probabilities: dict[str, float]) -> Answer:
    """Build a normalised answer from a probability per option/level (or ``true``/``false``)."""
    if question.type == "boolean":
        p = min(max(float(probabilities.get("true", 0.0)), 0.0), 1.0)
        return Answer(value=p >= 0.5, probabilities={"true": p, "false": 1 - p}, confidence=max(p, 1 - p), score=p)
    keys = list(question.options) if question.type == "choice" else list(question.levels)
    probs = {k: max(float(probabilities.get(k, 0.0)), 0.0) for k in keys}
    total = sum(probs.values())
    probs = {k: v / total for k, v in probs.items()} if total > 0 else {k: 1 / len(keys) for k in keys}
    best = max(keys, key=lambda k: probs[k])
    if question.type == "choice":
        return Answer(value=best, probabilities=probs, confidence=probs[best], score=probs[best])
    expected = sum(i * probs[k] for i, k in enumerate(keys)) / (len(keys) - 1)
    return Answer(value=best, probabilities=probs, confidence=probs[best], score=round(expected, 4))
