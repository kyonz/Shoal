"""The chat assistant (``POST /v1/assistant/chat``): see :mod:`shoal.assistant.chat`."""

from __future__ import annotations

from typing import Any, Literal

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from shoal.agents.claude_code import sign_in
from shoal.api.deps import CtxDep
from shoal.assistant.chat import Assistant
from shoal.core.errors import CredentialRequired, Forbidden, InvalidRequest, ShoalError
from shoal.core.requestctx import current_request
from shoal.llm.router import DEFAULT_POLICY, ModelClient, ModelRouter

router = APIRouter(prefix="/v1/assistant", tags=["assistant"])

FORWARDED_HEADERS = ("authorization", "cookie", "x-shoal-tenant", "x-request-id")


class AssistantUnavailable(ShoalError):
    status_code = 503
    code = "assistant_unavailable"


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]


class Decision(BaseModel):
    approve: bool
    note: str | None = Field(None, max_length=2000)


class ChatIn(BaseModel):
    namespace: str = "default"
    messages: list[ChatMessage] = Field(default_factory=list, max_length=400,
                                        description="The history exactly as the previous response returned it.")
    message: str | None = Field(None, max_length=20000, description="The user's new message.")
    decisions: dict[str, Decision] = Field(default_factory=dict, description="Pending action id -> decision.")
    auto_approve: bool = Field(False, description="Make changes without asking (the caller's permissions still apply).")
    page: str | None = Field(None, max_length=500, description="The UI route the user is on, for context.")
    context: str | None = Field(
        None, max_length=60_000,
        description="What the user sees that the API can't show, e.g. a workflow open in the builder with unsaved edits.",
    )


class PendingAction(BaseModel):
    id: str
    tool: str
    summary: str
    method: str
    path: str
    namespace: str | None = None
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    yaml: str | None = None
    plan: Any = Field(None, description="apply_resources: the dry-run outcome per document.")
    kinds: list[dict[str, str]] = Field(default_factory=list)


class ChatOut(BaseModel):
    messages: list[dict[str, Any]]
    pending: list[PendingAction] = Field(default_factory=list)
    usage: dict[str, float] = Field(default_factory=dict)
    model: str | None = None
    stopped: str | None = Field(None, description="'max_turns' when the assistant ran out of turns for this message.")


class AssistantInfo(BaseModel):
    enabled: bool
    gateway: str
    model: str
    effort: str
    defaults: dict[str, str] = Field(default_factory=dict, description="The server's values (SHOAL_ASSISTANT_*), used where the tenant sets none.")
    overrides: dict[str, str] = Field(default_factory=dict, description="What the tenant's admins set in the admin console.")


class AssistantSettingsIn(BaseModel):
    gateway: str | None = Field(None, max_length=200, description="Gateway resource or provider plugin; null uses the server default.")
    model: str | None = Field(None, max_length=200, description="Model name; null uses the server default.")
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


SETTING_FIELDS = ("gateway", "model", "effort")


async def assistant_config(ctx: CtxDep) -> AssistantInfo:
    """The assistant's settings for the calling tenant: admin-console overrides on top of SHOAL_ASSISTANT_*."""
    s = ctx.engine.settings
    defaults = {"gateway": s.assistant_gateway, "model": s.assistant_model, "effort": s.assistant_effort}
    tenant = await ctx.engine.store.get_tenant(ctx.tenant_id)
    own = {k: v for k, v in ((tenant.settings or {}).get("assistant") or {}).items() if k in SETTING_FIELDS and v}
    return AssistantInfo(enabled=s.assistant_enabled, **{**defaults, **own}, defaults=defaults, overrides=own)


@router.get("", response_model=AssistantInfo, summary="Whether the assistant is enabled, and its gateway and model")
async def assistant_info(ctx: CtxDep) -> AssistantInfo:
    return await assistant_config(ctx)


@router.put("/settings", response_model=AssistantInfo, summary="Choose the assistant's gateway, model and effort for this tenant")
async def put_assistant_settings(body: AssistantSettingsIn, ctx: CtxDep) -> AssistantInfo:
    await ctx.require("tenant:manage")
    own = body.model_dump(exclude_none=True)
    if own.get("gateway") and await ctx.engine.store.find_resource(ctx.tenant_id, "Gateway", own["gateway"]) is None \
            and own["gateway"] not in ctx.engine.registry.names("providers"):
        raise InvalidRequest(f"no gateway or provider named '{own['gateway']}'")
    await ctx.engine.store.update_tenant_settings(ctx.tenant_id, assistant=own or None)
    await ctx.audit("assistant.settings", "assistant", **own)
    return await assistant_config(ctx)


@router.post("/chat", response_model=ChatOut, summary="Send a message or approval decisions to the assistant")
async def chat(body: ChatIn, request: Request, ctx: CtxDep) -> ChatOut:
    settings = ctx.engine.settings
    config = await assistant_config(ctx)
    if not settings.assistant_enabled:
        raise AssistantUnavailable("the assistant is disabled on this server (SHOAL_ASSISTANT_ENABLED)")
    namespace = await ctx.namespace(body.namespace, "workflows:read")
    if not (body.message or body.decisions or body.messages):
        raise InvalidRequest("send a message")
    tenant = ctx.tenant_id
    if not ModelRouter.permitted(config.model, await ctx.engine.models.policy(tenant, DEFAULT_POLICY)):
        raise Forbidden(f"model '{config.model}' is not permitted by the tenant's model policy")
    try:
        target = await ctx.engine.models.gateway_target(
            tenant, config.gateway, config.model, effort=config.effort
        )
        # A claude-code gateway on people's own plans runs the chat on the plan of the person chatting.
        owner = ctx.actor if ctx.actor.startswith("user:") else None
        await sign_in(ctx.engine, tenant, target, owner, who=ctx.principal.display_name)
    except CredentialRequired as exc:
        raise AssistantUnavailable(
            f"assistant gateway: {exc.message}" + (". Link your Claude subscription under Settings, Connected accounts." if exc.owner else ""),
            details={**exc.details, "connect_url": "/settings/credentials?connection=" + f"{exc.namespace}/{exc.connection}"},
        ) from exc
    except ShoalError as exc:
        raise AssistantUnavailable(f"assistant gateway: {exc.message}") from exc

    headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED_HEADERS}
    headers["x-shoal-client"] = "assistant"
    info = current_request()
    if info is not None:
        headers["x-request-id"] = info.request_id  # inner calls' audit entries share the chat's request id
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url=str(request.base_url).rstrip("/"), headers=headers,
                                 timeout=120) as api:
        assistant = Assistant(
            ModelClient([target]), api, request.app.openapi(), namespace=namespace,
            actor=ctx.principal.display_name or ctx.actor, page=body.page, context=body.context,
            max_turns=settings.assistant_max_turns,
            auto_approve=body.auto_approve,
        )
        try:
            result = await assistant.chat(
                [m.model_dump() for m in body.messages], message=body.message,
                decisions={k: v.model_dump() for k, v in body.decisions.items()},
            )
        except ShoalError as exc:
            if exc.status_code < 500 and not exc.code.startswith("step"):
                raise
            raise AssistantUnavailable(f"the model did not answer: {exc.message}", details=exc.details) from exc
    return ChatOut(messages=result.messages, pending=[PendingAction(**p) for p in result.pending], usage=result.usage,
                   model=result.model, stopped=result.stopped)
