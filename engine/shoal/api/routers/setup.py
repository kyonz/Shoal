"""What still needs setting up (``GET /v1/setup``), so clients can guide people to it."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from shoal.api.deps import CtxDep
from shoal.api.routers.assistant import assistant_info
from shoal.api.schemas import SetupProblem
from shoal.core.errors import MissingConfiguration, ShoalError
from shoal.core.scope import use_namespace

router = APIRouter(prefix="/v1/setup", tags=["setup"])


class SetupCheck(BaseModel):
    id: str = Field(description="models, assistant or sandbox.")
    title: str
    ready: bool
    problem: SetupProblem | None = None


class GatewayStatus(BaseModel):
    name: str
    managed: bool = Field(description="A Gateway resource; false for a provider plugin used directly.")
    ready: bool
    problem: SetupProblem | None = None


class SetupStatus(BaseModel):
    ready: bool = Field(description="Every check is ready.")
    checks: list[SetupCheck]
    gateways: list[GatewayStatus] = Field(description="Each gateway and built-in provider an agent can name, and whether it works.")


def _problem(exc: ShoalError) -> SetupProblem:
    return SetupProblem.of(exc) if isinstance(exc, MissingConfiguration) else SetupProblem(code=exc.code, detail=exc.message)


@router.get("", response_model=SetupStatus, summary="What still needs setting up before agents and the assistant work")
async def setup_status(ctx: CtxDep, namespace: str = "default") -> SetupStatus:
    await ctx.namespace(namespace, "workflows:read")
    engine, tenant = ctx.engine, ctx.tenant_id
    settings = engine.settings
    with use_namespace(namespace):  # fix links for namespaced things point here
        managed = sorted(r.name for r in await engine.store.list_resources(tenant, "Gateway"))
        builtins = [p for p in engine.registry.names("providers") if p not in managed and p != "scripted"]
        gateways: list[GatewayStatus] = []
        for name in [*managed, *builtins]:
            status = GatewayStatus(name=name, managed=name in managed, ready=True)
            try:
                await engine.models.gateway_target(tenant, name, settings.assistant_model)
            except ShoalError as exc:
                status.ready, status.problem = False, _problem(exc)
            gateways.append(status)

        checks: list[SetupCheck] = []
        if any(g.ready for g in gateways):
            checks.append(SetupCheck(id="models", title="Model access", ready=True))
        else:
            broken = next((g.problem for g in gateways if g.managed and g.problem), None)
            problem = broken or SetupProblem.of(MissingConfiguration(
                "no LLM gateway is ready, so agents and the assistant cannot call a model", kind="Gateway"
            ))
            checks.append(SetupCheck(id="models", title="Model access", ready=False, problem=problem))

        if settings.assistant_enabled:
            info = await assistant_info(ctx)  # the same check as the assistant panel's
            checks.append(SetupCheck(id="assistant", title="Assistant", ready=info.ready, problem=info.problem))

        backend = settings.default_sandbox_backend
        if backend in engine.registry.names("sandboxes"):
            checks.append(SetupCheck(id="sandbox", title="Sandboxes", ready=True))
        else:
            checks.append(SetupCheck(id="sandbox", title="Sandboxes", ready=False, problem=SetupProblem.of(MissingConfiguration(
                f"the default sandbox backend '{backend}' (SHOAL_DEFAULT_SANDBOX_BACKEND) is not installed, so script "
                "and agent steps cannot run", kind="Setting", name="SHOAL_DEFAULT_SANDBOX_BACKEND",
            ))))
    return SetupStatus(ready=all(c.ready for c in checks), checks=checks, gateways=gateways)
