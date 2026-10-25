"""The ``agent`` step: run an agent in a sandbox until it returns structured outputs."""

from __future__ import annotations

from awm.agents.invoke import load_agent, run_agent
from awm.core.models import AgentStep
from awm.runtime.context import Completed, StepContext, StepResult


class AgentHandler:
    async def run(self, ctx: StepContext) -> StepResult:
        step = ctx.step
        assert isinstance(step, AgentStep)
        agent_name = step.agent or ctx.workflow.defaults.agent
        _, agent = await load_agent(ctx, agent_name)
        prompt = ctx.render(step.prompt)
        profile = step.sandbox or agent.sandbox or ctx.workflow.defaults.sandbox
        async with ctx.engine.sandboxes.acquire(ctx.run, profile) as sandbox:
            result = await run_agent(
                ctx,
                agent_name,
                prompt=prompt,
                outputs=step.outputs,
                sandbox=sandbox,
                extra_skills=step.skills,
                extra_tools=step.tools,
            )
        usage = dict(result.usage)
        for key, value in (ctx.state.pop("delegated_usage", None) or {}).items():
            usage[key] = usage.get(key, 0) + value
        return Completed({**result.outputs, "summary": result.summary}, usage=usage)
