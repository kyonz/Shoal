"""Resolve an agent (spec, skills, credentials, env) and run it in a sandbox."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from awm.agents.runtime import AgentInvocation, AgentResult
from awm.core.errors import StepError
from awm.core.expressions import referenced_secrets
from awm.core.models import AgentSpec, ParamSpec, SkillSpec

if TYPE_CHECKING:
    from awm.runtime.context import StepContext
    from awm.sandbox.base import Sandbox


async def load_agent(ctx: StepContext, name: str | None) -> tuple[str, AgentSpec]:
    if name is None:
        return "default", AgentSpec()
    resource = await ctx.engine.store.find_resource(ctx.tenant_id, "Agent", name)
    if resource is None:
        raise StepError(f"agent '{name}' not found")
    return name, resource.spec  # type: ignore[attr-defined]


async def load_skills(ctx: StepContext, names: list[str]) -> dict[str, SkillSpec]:
    skills: dict[str, SkillSpec] = {}
    for name in dict.fromkeys(names):
        resource = await ctx.engine.store.find_resource(ctx.tenant_id, "Skill", name)
        if resource is None:
            raise StepError(f"skill '{name}' not found")
        skills[name] = resource.spec  # type: ignore[attr-defined]
    return skills


async def resolve_env(ctx: StepContext, agent: AgentSpec) -> dict[str, str]:
    """Render agent env (may reference secrets) and register the values for redaction."""
    names = referenced_secrets(agent.env)
    secrets = await ctx.engine.secrets.resolve(ctx.tenant_id, names)
    missing = names - set(secrets)
    if missing:
        raise StepError(f"secrets not set: {', '.join(sorted(missing))}")
    env = {k: str(v) for k, v in ctx.render(agent.env, secrets={**ctx.expr["secrets"], **secrets}).items()}
    ctx.redactor.add(*secrets.values())
    return env


async def resolve_provider_config(ctx: StepContext, agent: AgentSpec) -> dict[str, Any]:
    config = dict(agent.provider_config)
    secret_name = config.pop("api_key_secret", None)
    if secret_name:
        value = await ctx.engine.secrets.get(ctx.tenant_id, secret_name)
        if value is None:
            raise StepError(f"secret '{secret_name}' (provider api key) is not set")
        config["api_key"] = value
        ctx.redactor.add(value)
    return config


async def run_agent(
    ctx: StepContext,
    agent_name: str | None,
    *,
    prompt: str,
    outputs: dict[str, ParamSpec],
    sandbox: Sandbox,
    extra_skills: list[str] | None = None,
    extra_tools: list[str] | None = None,
    depth: int = 0,
) -> AgentResult:
    name, agent = await load_agent(ctx, agent_name)
    invocation = AgentInvocation(
        ctx=ctx,
        agent_name=name,
        agent=agent,
        prompt=prompt,
        outputs=outputs,
        sandbox=sandbox,
        skills=await load_skills(ctx, [*agent.skills, *(extra_skills or [])]),
        tools=[*agent.tools, *(extra_tools or [])],
        provider_config=await resolve_provider_config(ctx, agent),
        env=await resolve_env(ctx, agent),
        depth=depth,
    )
    runtime = ctx.engine.plugin("agent_runtimes", agent.runtime)
    await ctx.emit("agent.started", {"agent": name, "model": agent.model, "runtime": agent.runtime, "depth": depth})
    result = await runtime.run(invocation)
    await ctx.emit("agent.finished", {"agent": name, "summary": result.summary[:4000], "turns": result.turns, "usage": result.usage})
    return result
