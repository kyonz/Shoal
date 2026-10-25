"""Starter integration.

Replace the example ticket-tracker API below with the real service. The three
capabilities are independent; keep the ones you need:

* actions   - ``@action`` methods: workflow steps, agent tools and API calls;
* resources - ``@resource`` methods: metadata for campaign targets and UI pickers;
* events    - ``parse_webhook``: events that fire triggers and wake waiting runs.
"""

from __future__ import annotations

from typing import Any, Literal

from shoal.sdk import (
    BaseModel,
    ConnectionContext,
    EventDef,
    Field,
    Integration,
    IntegrationEvent,
    Page,
    SecretStr,
    action,
    actor,
    decode_cursor,
    encode_cursor,
    json_body,
    raise_for_status,
    resource,
    verify_hmac_sha256,
)

SIGNATURE_HEADER = "x-starter-signature"


class StarterConnection(BaseModel):
    """What a tenant fills in to connect. SecretStr fields are stored as secrets."""

    base_url: str = Field("https://api.starter.example", description="API base URL.")
    api_token: SecretStr = Field(description="API token with read/write access.")
    webhook_secret: SecretStr | None = Field(None, description="Secret used to sign webhooks.")


# --------------------------------------------------------------------------- action models


class CreateTicket(BaseModel):
    project: str = Field(description="Project key, e.g. OPS.")
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)


class Ticket(BaseModel):
    id: str
    url: str
    status: str


class AddComment(BaseModel):
    ticket_id: str
    body: str


class GetTicket(BaseModel):
    ticket_id: str


class TicketFilter(BaseModel):
    project: str | None = None
    status: Literal["open", "closed", "any"] = "open"


# --------------------------------------------------------------------------- integration


class StarterIntegration(Integration):
    key = "starter"
    title = "Starter"
    description = "Tickets, comments and ticket events from Starter."
    connection_model = StarterConnection
    events = [
        EventDef("ticket.created", "A ticket was created.", {"ticket": {"id": "OPS-1", "status": "open"}}),
        EventDef(
            "ticket.updated", "A ticket changed (status, labels, assignee).", {"ticket": {"id": "OPS-1"}}
        ),
    ]

    def _client(self, ctx: ConnectionContext):
        cfg: StarterConnection = ctx.config
        return ctx.http(
            base_url=cfg.base_url,
            headers={
                "Authorization": f"Bearer {cfg.api_token.get_secret_value()}",
                "Accept": "application/json",
            },
        )

    async def test_connection(self, ctx: ConnectionContext) -> dict[str, Any]:
        async with self._client(ctx) as client:
            response = await client.get("/me")
        raise_for_status(response, "Starter")
        return {"ok": True, "user": response.json().get("name")}

    # --- actions ------------------------------------------------------------

    @action(description="Create a ticket in a project.", input=CreateTicket, output=Ticket)
    async def create_ticket(self, ctx: ConnectionContext, params: CreateTicket) -> Ticket:
        async with self._client(ctx) as client:
            response = await client.post("/tickets", json=params.model_dump())
        raise_for_status(response, "Starter")
        return Ticket.model_validate(response.json())

    @action(description="Add a comment to a ticket.", input=AddComment)
    async def add_comment(self, ctx: ConnectionContext, params: AddComment) -> dict[str, Any]:
        async with self._client(ctx) as client:
            response = await client.post(f"/tickets/{params.ticket_id}/comments", json={"body": params.body})
        raise_for_status(response, "Starter")
        return {"id": response.json()["id"]}

    @action(description="Read a ticket.", input=GetTicket, output=Ticket, mutating=False)
    async def get_ticket(self, ctx: ConnectionContext, params: GetTicket) -> Ticket:
        async with self._client(ctx) as client:
            response = await client.get(f"/tickets/{params.ticket_id}")
        raise_for_status(response, "Starter")
        return Ticket.model_validate(response.json())

    # --- resources ----------------------------------------------------------

    @resource("ticket", description="Tickets, filtered by project and status.", filter=TicketFilter)
    async def list_tickets(
        self, ctx: ConnectionContext, filter: TicketFilter, cursor: str | None, limit: int
    ) -> Page:
        page = decode_cursor(cursor, {"page": 1})["page"]
        params: dict[str, Any] = {"page": page, "per_page": min(limit, 100), "status": filter.status}
        if filter.project:
            params["project"] = filter.project
        async with self._client(ctx) as client:
            response = await client.get("/tickets", params=params)
        raise_for_status(response, "Starter")
        body = response.json()
        next_cursor = encode_cursor({"page": page + 1}) if body.get("has_more") else None
        return Page(items=body["items"], next_cursor=next_cursor)

    # --- webhooks (triggers) ------------------------------------------------

    def webhook_setup_instructions(self, url: str) -> str:
        return f"In Starter, open Settings > Webhooks, add {url} and paste the connection's webhook secret."

    async def verify_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> bool:
        cfg: StarterConnection = ctx.config
        if cfg.webhook_secret is None:
            return False  # refuse unsigned deliveries rather than trusting anyone who finds the URL
        return verify_hmac_sha256(cfg.webhook_secret, body, headers.get(SIGNATURE_HEADER), prefix="sha256=")

    async def parse_webhook(
        self, ctx: ConnectionContext, headers: dict[str, str], body: bytes
    ) -> list[IntegrationEvent]:
        payload = json_body(body)
        ticket = payload.get("ticket") or {}
        user = payload.get("user") or {}
        return [
            IntegrationEvent(
                type=f"ticket.{payload.get('action', 'updated')}",
                id=headers.get("x-starter-delivery"),
                data=payload,
                # Wakes runs waiting on this ticket (wait step `signal: {event: "starter:ticket:OPS-1"}`
                # or a condition wait with `wake_on: ["starter:ticket:OPS-1"]`).
                wake_keys=[f"starter:ticket:{ticket['id']}"] if ticket.get("id") else [],
                actor=actor(email=user.get("email"), display_name=user.get("name"), starter=user.get("id")),
            )
        ]
