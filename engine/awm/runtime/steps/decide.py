"""The ``decide`` and ``grade`` steps: typed decisions from a decision model."""

from __future__ import annotations

from awm.core.errors import StepError
from awm.core.models import DecideStep, DecisionQuestion, GradeStep
from awm.runtime.context import Completed, StepContext, StepResult, Suspended


class DecideHandler:
    """Output: one entry per question (``value``, ``probabilities``, ``confidence``,
    ``score``) plus ``low_confidence`` (question ids below ``min_confidence``)."""

    async def run(self, ctx: StepContext) -> StepResult:
        step = ctx.step
        assert isinstance(step, DecideStep)
        if "result" not in ctx.state:
            result = await ctx.engine.decisions.decide(
                ctx.tenant_id,
                ctx.render(step.state),
                step.questions,
                provider=step.provider,
                config=ctx.render(step.provider_config),
                redactor=ctx.redactor,
            )
            ctx.state["result"] = result.to_dict()
            ctx.state["usage"] = result.usage
            await ctx.emit("decision.made", ctx.state["result"])
        result = ctx.state["result"]
        answers = result["answers"]
        low = [k for k, a in answers.items() if step.min_confidence is not None and a["confidence"] < step.min_confidence]
        output = {**answers, "low_confidence": low, "provider": result["provider"]}
        usage = ctx.state.get("usage") or {}

        if low and step.on_low_confidence == "fail":
            raise StepError(f"low-confidence decision on: {', '.join(low)}", details=output)
        if low and step.on_low_confidence == "approval":
            review = ctx.state.get("decision")
            if review:
                return Completed({**output, "reviewed": review}, usage=usage)
            if not ctx.state.get("requested"):
                ctx.state["requested"] = True
                ctx.state["message"] = f"Low-confidence decision on {', '.join(low)}; please review."
                await ctx.emit("approval.requested", {"message": ctx.state["message"], "answers": answers})
            return Suspended(wait_keys=[f"approval:{ctx.run.id}:{step.id}"], reason="decision needs review", usage=usage)
        return Completed(output, usage=usage)


class GradeHandler:
    """Output: ``level``, ``score`` (0..1), ``confidence``, ``passed``."""

    async def run(self, ctx: StepContext) -> StepResult:
        step = ctx.step
        assert isinstance(step, GradeStep)
        question = DecisionQuestion(type="score", instructions=step.instructions, levels=step.levels)
        result = await ctx.engine.decisions.decide(
            ctx.tenant_id,
            ctx.render(step.subject),
            {"grade": question},
            provider=step.provider,
            config=ctx.render(step.provider_config),
            redactor=ctx.redactor,
        )
        answer = result.answers["grade"]
        output = {
            "level": answer.value,
            "score": answer.score,
            "confidence": answer.confidence,
            "probabilities": answer.probabilities,
            "passed": answer.score >= step.threshold,
            "threshold": step.threshold,
        }
        await ctx.emit("grade.scored", output)
        if not output["passed"] and step.on_fail == "fail":
            raise StepError(f"grade {answer.score:.2f} is below the threshold {step.threshold}", details=output)
        return Completed(output, usage=result.usage)
