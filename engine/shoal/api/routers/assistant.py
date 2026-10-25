"""The chat assistant (``POST /v1/assistant/chat``): see :mod:`shoal.assistant.chat`."""

from __future__ import annotations

from typing import Any, Literal

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from shoal.api.deps import CtxDep
from shoal.api.schemas import SetupProblem
from shoal.assistant.chat import Assistant
from shoal.core.errors import InvalidRequest, MissingConfiguration, ShoalError
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
    ready: bool = Field(True, description="Whether the gateway and model are set up, so the assistant can answer.")
    problem: SetupProblem | None = Field(None, description="Why it is not ready, and where to fix it.")


async def _target(ctx: CtxDep) -> Any:
    """The assistant's model target, or MissingConfiguration naming what to set up."""
    settings = ctx.engine.settings
    tenant = ctx.tenant_id
    if not ModelRouter.permitted(settings.assistant_model, await ctx.engine.models.policy(tenant, DEFAULT_POLICY)):
        raise MissingConfiguration(
            f"the assistant's model '{settings.assistant_model}' is not permitted by the tenant's model policy",
            kind="ModelPolicy", name=DEFAULT_POLICY, exists=True, field="allow_models",
        )
    try:
        return await ctx.engine.models.gateway_target(
            tenant, settings.assistant_gateway, settings.assistant_model, effort=settings.assistant_effort
        )
    except MissingConfiguration:
        raise
    except ShoalError as exc:
        raise AssistantUnavailable(f"assistant gateway: {exc.message}") from exc


@router.get("", response_model=AssistantInfo, summary="Whether the assistant is enabled and set up, and its model")
async def assistant_info(ctx: CtxDep) -> AssistantInfo:
    s = ctx.engine.settings
    info = AssistantInfo(enabled=s.assistant_enabled, gateway=s.assistant_gateway, model=s.assistant_model)
    if not s.assistant_enabled:
        return info
    try:
        await _target(ctx)
    except MissingConfiguration as exc:
        info.ready = False
        info.problem = SetupProblem.of(exc)
    except ShoalError as exc:
        info.ready = False
        info.problem = SetupProblem(code=exc.code, detail=exc.message)
    return info


@router.post("/chat", response_model=ChatOut, summary="Send a message or approval decisions to the assistant")
async def chat(body: ChatIn, request: Request, ctx: CtxDep) -> ChatOut:
    settings = ctx.engine.settings
    if not settings.assistant_enabled:
        raise AssistantUnavailable("the assistant is disabled on this server (SHOAL_ASSISTANT_ENABLED)")
    namespace = await ctx.namespace(body.namespace, "workflows:read")
    if not (body.message or body.decisions or body.messages):
        raise InvalidRequest("send a message")
    target = await _target(ctx)

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
            actor=ctx.principal.display_name or ctx.actor, page=body.page, max_turns=settings.assistant_max_turns,
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
