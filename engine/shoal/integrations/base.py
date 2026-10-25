"""Integration framework.

An integration (``github``, ``jira``, ...) declares three capabilities:

* **actions** - typed operations (pydantic input/output). They are workflow
  ``action`` steps, agent tools and API operations at the same time;
* **resources** - metadata queries (list repositories, search issues) used by
  campaign target selection and UI pickers;
* **events** - webhook / poll events that fire triggers and wake waits.

A tenant configures a *connection* (credentials + settings) per integration.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, get_args, get_origin

import httpx
from pydantic import BaseModel, Field, SecretStr

if TYPE_CHECKING:
    from shoal.engine import Engine


class Interaction(BaseModel):
    """A person acting on something Shoal sent them (a button, a form).

    The engine resolves the event's ``actor`` to a Shoal user, checks their
    permission on the run, then records the approval decision or sends the signal.
    """

    kind: Literal["approval", "signal"]
    run_id: str
    step_id: str | None = Field(None, description="approval: the approval step.")
    approved: bool | None = Field(None, description="approval: the decision.")
    comment: str | None = None
    signal: str | None = Field(None, description="signal: the signal name.")
    payload: Any = Field(None, description="signal: the signal payload.")


class IntegrationEvent(BaseModel):
    """A normalised event parsed from a webhook or poll."""

    type: str
    id: str | None = Field(None, description="Provider delivery/event id, used for dedupe.")
    data: dict[str, Any] = Field(default_factory=dict)
    wake_keys: list[str] = Field(default_factory=list, description="Wait-subscription keys this event satisfies.")
    actor: dict[str, Any] = Field(
        default_factory=dict,
        description="Who caused the event: identity hints such as github, jira_account_id, slack_id, email, display_name.",
    )
    interaction: Interaction | None = Field(
        None, description="Set when the event is a person answering an approval or sending a run a signal."
    )


class ApprovalRequest(BaseModel):
    """An approval step asking people for a decision through an integration (``notify`` on the step)."""

    run_id: str
    step_id: str
    workflow: str
    namespace: str
    message: str
    to: str | None = Field(None, description="Where to ask: a channel or a person, in the integration's terms.")
    options: dict[str, Any] = Field(default_factory=dict, description="Integration-specific options (`with`).")
    approvers: list[str] = Field(default_factory=list)
    deadline: str | None = None
    run_url: str
    initiator: str | None = Field(None, description="Display name of the person the run acts for.")


class WebhookReply(BaseModel):
    """The HTTP answer to a webhook delivery, for services that read it (Slack challenges, modal errors)."""

    status: int = 200
    body: Any = None


class Page(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


@dataclass
class ActionDef:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None
    method: str
    mutating: bool = True

    def tool_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return schema


@dataclass
class ResourceDef:
    name: str
    description: str
    filter_model: type[BaseModel]
    method: str
    key_field: str = "id"


def describe_actions(actions: dict[str, ActionDef]) -> list[dict[str, Any]]:
    """Catalogue form of a set of actions (also served per connection for dynamic integrations)."""
    return [
        {
            "name": a.name,
            "description": a.description,
            "mutating": a.mutating,
            "input_schema": a.tool_schema(),
            "output_schema": a.output_model.model_json_schema() if a.output_model else None,
        }
        for a in actions.values()
    ]


def describe_resources(resources: dict[str, ResourceDef]) -> list[dict[str, Any]]:
    return [{"name": r.name, "description": r.description, "filter_schema": r.filter_model.model_json_schema()} for r in resources.values()]


@dataclass
class EventDef:
    name: str
    description: str
    example: dict[str, Any] = field(default_factory=dict)


def action(
    name: str | None = None,
    *,
    description: str,
    input: type[BaseModel],
    output: type[BaseModel] | None = None,
    mutating: bool = True,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    def wrap(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        fn.__shoal_action__ = ActionDef(  # type: ignore[attr-defined]
            name=name or fn.__name__,
            description=description,
            input_model=input,
            output_model=output,
            method=fn.__name__,
            mutating=mutating,
        )
        return fn

    return wrap


def resource(
    name: str, *, description: str, filter: type[BaseModel], key_field: str = "id"
) -> Callable[[Callable[..., Awaitable[Page]]], Callable[..., Awaitable[Page]]]:
    def wrap(fn: Callable[..., Awaitable[Page]]) -> Callable[..., Awaitable[Page]]:
        fn.__shoal_resource__ = ResourceDef(  # type: ignore[attr-defined]
            name=name, description=description, filter_model=filter, method=fn.__name__, key_field=key_field
        )
        return fn

    return wrap


@dataclass
class ConnectionContext:
    """Everything an integration needs to talk to one configured connection."""

    engine: Engine
    tenant_id: str
    name: str
    config: Any  # instance of Integration.connection_model
    integration: Integration
    initiator: dict[str, Any] | None = None
    run_id: str | None = None
    acting_as: str | None = None  # the user whose own credential this context holds (None: the connection's)

    def http(self, **kwargs: Any) -> httpx.AsyncClient:
        transport = self.engine.http_transport
        kwargs.setdefault("timeout", httpx.Timeout(30.0, connect=10.0))
        if transport is not None:
            kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)


class IntegrationError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class Integration:
    key: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str] = ""
    connection_model: ClassVar[type[BaseModel]]
    events: ClassVar[list[EventDef]] = []

    actions: ClassVar[dict[str, ActionDef]]
    resources: ClassVar[dict[str, ResourceDef]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.actions = {}
        cls.resources = {}
        for _, member in inspect.getmembers(cls):
            if hasattr(member, "__shoal_action__"):
                cls.actions[member.__shoal_action__.name] = member.__shoal_action__
            if hasattr(member, "__shoal_resource__"):
                cls.resources[member.__shoal_resource__.name] = member.__shoal_resource__

    # --- secrets -------------------------------------------------------------

    @classmethod
    def secret_fields(cls) -> list[str]:
        out = []
        for name, info in cls.connection_model.model_fields.items():
            ann = info.annotation
            if ann is SecretStr or (get_origin(ann) is not None and SecretStr in get_args(ann)):
                out.append(name)
        return out

    # --- overridable hooks ---------------------------------------------------

    async def test_connection(self, ctx: ConnectionContext) -> dict[str, Any]:
        return {"ok": True}

    async def verify_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> bool:
        return True

    async def parse_webhook(
        self, ctx: ConnectionContext, headers: dict[str, str], body: bytes
    ) -> list[IntegrationEvent]:
        return []

    async def webhook_reply(
        self, ctx: ConnectionContext, headers: dict[str, str], body: bytes, result: dict[str, Any]
    ) -> WebhookReply | None:
        """Answer the delivery yourself; ``result`` holds the events and each interaction's outcome.

        ``None`` (the default) answers ``202`` with the processing summary.
        """
        return None

    def webhook_setup_instructions(self, url: str) -> str:
        return f"Send webhooks to {url}"

    def setup_manifest(self, webhook_url: str, install_redirect_url: str, connection: str) -> dict[str, Any] | None:
        """An app manifest for the service with Shoal's URLs filled in (Slack app manifests), if it has one."""
        return None

    # --- trigger filters --------------------------------------------------------

    trigger_filter_model: ClassVar[type[BaseModel] | None] = None  # the shape of `Trigger.spec.match`

    async def trigger_matches(self, ctx: ConnectionContext, event: IntegrationEvent, options: BaseModel | None) -> str | None:
        """Apply a trigger's structured ``match`` filters (parsed with ``trigger_filter_model``; None when the
        integration has none). Return None to fire, or why the event does not match.

        Called for every trigger on the connection, also those without ``match``, so defaults apply
        (Slack ignores other bots unless asked).
        """
        return None

    # --- approvals -----------------------------------------------------------

    supports_approvals: ClassVar[bool] = False

    async def request_approval(self, ctx: ConnectionContext, request: ApprovalRequest) -> dict[str, Any]:
        """Ask for a decision (e.g. a message with Approve / Reject buttons); returns a reference to it.

        Answers come back as webhook events carrying an ``Interaction``.
        """
        raise IntegrationError(f"{self.key} cannot ask for approvals")

    async def approval_resolved(
        self, ctx: ConnectionContext, request: ApprovalRequest, ref: dict[str, Any], outcome: dict[str, Any]
    ) -> None:
        """The step was decided (here, in the UI or API) or timed out: show the outcome where it was asked.

        ``outcome`` is the step output: ``approved``, ``by``, ``comment``, ``timed_out``.
        """

    # --- workspace install (OAuth) ---------------------------------------------

    install_fields: ClassVar[list[str]] = []  # connection fields an install fills in (e.g. bot_token)

    def install_url(self, config: Any, redirect_uri: str, state: str) -> str:
        """Where to send an admin to install the app; ``config`` is the connection's (possibly partial) settings."""
        raise IntegrationError(f"{self.key} has no app install")

    async def complete_install(self, ctx: ConnectionContext, code: str, redirect_uri: str) -> dict[str, str]:
        """Exchange the install code; returns values for ``install_fields`` (stored as the connection's secrets)."""
        raise IntegrationError(f"{self.key} has no app install")

    async def actions_for(self, ctx: ConnectionContext) -> dict[str, ActionDef]:
        """Actions available on one connection; dynamic integrations (remote) override this."""
        return self.actions

    async def resources_for(self, ctx: ConnectionContext) -> dict[str, ResourceDef]:
        return self.resources

    # --- dispatch ------------------------------------------------------------

    async def call_action(self, ctx: ConnectionContext, name: str, params: dict[str, Any]) -> dict[str, Any]:
        definition = self.actions.get(name)
        if definition is None:
            raise IntegrationError(f"{self.key} has no action '{name}'")
        payload = definition.input_model.model_validate(params)
        result = await getattr(self, definition.method)(ctx, payload)
        if isinstance(result, BaseModel):
            return result.model_dump(mode="json")
        return result if isinstance(result, dict) else {"result": result}

    async def query(
        self, ctx: ConnectionContext, name: str, filter: dict[str, Any], *, cursor: str | None = None, limit: int = 100
    ) -> Page:
        definition = self.resources.get(name)
        if definition is None:
            raise IntegrationError(f"{self.key} has no resource '{name}'")
        parsed = definition.filter_model.model_validate(filter)
        return await getattr(self, definition.method)(ctx, parsed, cursor, limit)

    @classmethod
    def catalog(cls) -> dict[str, Any]:
        connection_schema = cls.connection_model.model_json_schema()
        return {
            "key": cls.key,
            "title": cls.title,
            "description": cls.description,
            "connection_schema": connection_schema,
            "secret_fields": cls.secret_fields(),
            "actions": describe_actions(cls.actions),
            "resources": describe_resources(cls.resources),
            "events": [{"name": e.name, "description": e.description, "example": e.example} for e in cls.events],
            "approvals": cls.supports_approvals,
            "trigger_filter_schema": cls.trigger_filter_model.model_json_schema() if cls.trigger_filter_model else None,
            "install": bool(cls.install_fields),
        }


def raise_for_status(response: httpx.Response, service: str) -> None:
    if response.is_success:
        return
    retryable = response.status_code in (408, 409, 425, 429) or response.status_code >= 500
    detail = response.text[:500]
    raise IntegrationError(
        f"{service} API {response.request.method} {response.request.url.path} failed: "
        f"{response.status_code} {detail}",
        status=response.status_code,
        retryable=retryable,
    )
