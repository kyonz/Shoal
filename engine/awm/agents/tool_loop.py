"""AWM's own agent loop: model <-> tools until the model calls ``finish``."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from awm.agents.providers.base import ModelProvider, ModelRequest
from awm.agents.runtime import AgentInvocation, AgentResult, AgentRuntime, install_skills, outputs_description
from awm.agents.tools import Toolbox
from awm.core.errors import StepError
from awm.core.models import ParamValidationError, validate_params
from awm.runtime.executor import add_usage

MAX_DELEGATION_DEPTH = 3
MAX_NUDGES = 2

SYSTEM_TEMPLATE = """{instructions}

# Environment
You are an autonomous agent running inside AI Workflow Manager as step `{step_id}` of workflow `{workflow}`.
Your workspace is a sandbox at `{workdir}`. Work there; nothing outside the sandbox is visible to you
except through the tools provided. No human is watching this session: do not ask questions, make
reasonable decisions, and record assumptions in your summary.
{skills}
# Finishing
When the task is complete, call the `finish` tool exactly once with these fields:
{outputs}
If you cannot complete the task, still call `finish`, fill in what you can, and explain why in `summary`.
"""


class ToolLoopRuntime(AgentRuntime):
    name = "tool-loop"

    async def run(self, inv: AgentInvocation) -> AgentResult:
        ctx = inv.ctx
        provider: ModelProvider = ctx.engine.plugin("providers", inv.agent.provider)
        skills = await install_skills(inv)
        skills_text = ""
        if skills:
            lines = "\n".join(f"- **{n}**: {d} (read `{p}` before using it)" for n, d, p in skills)
            skills_text = f"\n# Skills\nSkills are packaged instructions for specific tasks:\n{lines}\n"
        system = SYSTEM_TEMPLATE.format(
            instructions=inv.agent.instructions or "You are a careful, capable software agent.",
            step_id=ctx.step.id,
            workflow=ctx.run.workflow_name,
            workdir=inv.sandbox.workdir,
            skills=skills_text,
            outputs=outputs_description(inv.outputs),
        )

        toolbox = await Toolbox(inv).build()
        finished: dict[str, Any] = {}

        async def finish(args: dict[str, Any]) -> str:
            values = {k: v for k, v in args.items() if k != "summary"}
            try:
                finished["outputs"] = validate_params(inv.outputs, values)
            except ParamValidationError as exc:
                raise ValueError(f"invalid outputs: {exc}. Call finish again with corrected values.") from exc
            finished["summary"] = str(args.get("summary", ""))
            return "Outputs recorded. You are done."

        toolbox.add_finish(inv.finish_schema(), finish)
        if inv.agent.subagents and inv.depth < MAX_DELEGATION_DEPTH:
            toolbox.add_delegate(inv.agent.subagents, self._delegate_handler(inv))

        messages: list[dict[str, Any]] = [{"role": "user", "content": inv.prompt}]
        usage: dict[str, float] = {}
        limits = inv.agent.limits
        nudges = 0
        for turn in range(1, limits.max_turns + 1):
            response = await provider.complete(
                ModelRequest(
                    model=inv.agent.model,
                    system=system,
                    messages=messages,
                    tools=toolbox.definitions,
                    max_tokens=limits.max_tokens,
                    config=inv.provider_config,
                )
            )
            usage = add_usage(usage, response.usage)
            if limits.budget_usd is not None and usage.get("cost_usd", 0) > limits.budget_usd:
                raise StepError(f"agent exceeded its budget of ${limits.budget_usd}", details={"usage": usage})
            if response.stop_reason == "refusal":
                raise StepError("the model declined this request", details={"usage": usage})
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = response.tool_uses
            await ctx.emit(
                "agent.turn",
                {
                    "agent": inv.agent_name,
                    "depth": inv.depth,
                    "turn": turn,
                    "text": response.text[:4000],
                    "tool_calls": [{"name": t["name"], "input": _preview(t.get("input"))} for t in tool_uses],
                    "usage": response.usage,
                },
            )
            if not tool_uses:
                if response.stop_reason == "max_tokens":
                    messages.append({"role": "user", "content": "Your response was cut off. Continue."})
                    continue
                nudges += 1
                if nudges > MAX_NUDGES:
                    raise StepError("agent stopped without calling finish", details={"text": response.text[:2000]})
                messages.append(
                    {"role": "user", "content": "Continue the task. When it is complete, call the `finish` tool."}
                )
                continue

            async def call(block: dict[str, Any]) -> dict[str, Any]:
                content, is_error = await toolbox.execute(block["name"], block.get("input") or {})
                await ctx.emit(
                    "agent.tool_result",
                    {"agent": inv.agent_name, "tool": block["name"], "is_error": is_error, "output": content[:2000]},
                )
                result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block["id"], "content": content}
                if is_error:
                    result["is_error"] = True
                return result

            # finish runs last so earlier tool calls in the same turn take effect first
            ordered = sorted(tool_uses, key=lambda b: b["name"] == "finish")
            others = [b for b in ordered if b["name"] != "finish"]
            results = list(await asyncio.gather(*(call(b) for b in others)))
            results += [await call(b) for b in ordered if b["name"] == "finish"]
            messages.append({"role": "user", "content": results})
            if "outputs" in finished:
                return AgentResult(
                    outputs=finished["outputs"], summary=finished["summary"], usage=usage, turns=turn
                )
        raise StepError(f"agent did not finish within {limits.max_turns} turns", details={"usage": usage})

    def _delegate_handler(self, inv: AgentInvocation):  # type: ignore[no-untyped-def]
        async def delegate(args: dict[str, Any]) -> str:
            from awm.agents.invoke import run_agent

            result = await run_agent(
                inv.ctx,
                args["agent"],
                prompt=args["task"],
                outputs={},
                sandbox=inv.sandbox,
                depth=inv.depth + 1,
            )
            inv.ctx.state.setdefault("delegated_usage", {})
            inv.ctx.state["delegated_usage"] = add_usage(inv.ctx.state["delegated_usage"], result.usage)
            return json.dumps({"summary": result.summary, "outputs": result.outputs})

        return delegate


def _preview(value: Any, limit: int = 1500) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= limit:
        return value
    return text[:limit] + "..."
