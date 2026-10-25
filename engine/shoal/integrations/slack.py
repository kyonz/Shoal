"""Slack integration: messages (Block Kit), interactive approvals and inputs, events and slash commands.

One Slack app per connection. Its request URL (the connection's webhook URL)
receives Events API callbacks, interactivity payloads (buttons, modals) and
slash commands, all verified with the app's signing secret. The connection's
``setup_manifest`` is a ready-to-paste Slack app manifest; connect with a bot
token, or with "Add to Slack" (``client_id`` + ``client_secret``), which stores
the bot token as the connection's secret.

* Approval steps with ``notify: [{integration: slack, to: "#deploys"}]`` post a
  message with Approve / Reject buttons. A click resolves the step only when the
  Slack user maps to a Shoal user (``Identity.slack_id`` or a matching email)
  allowed to approve; the message then shows the outcome.
* ``post_message`` buttons and ``request_input`` (a button opening a modal form)
  send signals to the run; a ``wait`` step with ``signal: {name: ...}`` takes them.
* Mentions, messages, reactions, slash commands, shortcuts and other button
  clicks are trigger events. Thread replies wake ``wait`` steps waiting on
  ``slack:thread:<channel>:<ts>`` (``thread_key`` in ``post_message``'s output).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode

from pydantic import BaseModel, Field, SecretStr

from shoal.core import clock
from shoal.integrations.base import (
    ApprovalRequest,
    ConnectionContext,
    EventDef,
    Integration,
    IntegrationError,
    IntegrationEvent,
    Interaction,
    Page,
    WebhookReply,
    action,
    raise_for_status,
    resource,
)

log = logging.getLogger(__name__)

BOT_SCOPES = [
    "app_mentions:read",
    "channels:history",
    "channels:read",
    "chat:write",
    "chat:write.public",
    "commands",
    "groups:read",
    "im:history",
    "im:write",
    "reactions:read",
    "reactions:write",
    "users:read",
    "users:read.email",
]
BOT_EVENTS = ["app_mention", "message.channels", "message.im", "reaction_added"]
MAX_SKEW = 300  # seconds a signed request may be old (Slack's replay window)
RETRYABLE_ERRORS = {"ratelimited", "service_unavailable", "internal_error", "fatal_error", "request_timeout"}
APPROVE, REJECT, OPEN, INPUT, SIGNAL = "shoal_approve", "shoal_reject", "shoal_open", "shoal_input", "shoal_signal"
COMMENT_VIEW, INPUT_VIEW = "shoal_approval_comment", "shoal_input"
MENTION = re.compile(r"^\s*<@[A-Z0-9]+(?:\|[^>]*)?>\s*")


class SlackConnection(BaseModel):
    bot_token: SecretStr | None = Field(None, description="Bot token (xoxb-...). 'Add to Slack' fills it in.")
    signing_secret: SecretStr | None = Field(
        None, description="The app's signing secret (Basic Information); verifies clicks, events and slash commands."
    )
    client_id: str | None = Field(None, description="The app's client id, for 'Add to Slack'.")
    client_secret: SecretStr | None = Field(None, description="The app's client secret, for 'Add to Slack'.")
    default_channel: str | None = Field(None, description="Channel used when an action or approval names none.")
    api_url: str = Field("https://slack.com/api", description="Slack Web API base URL (change for GovSlack).")


# ----------------------------------------------------------------------------- models


class SignalButton(BaseModel):
    label: str = Field(max_length=75)
    signal: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$", description="Signal sent to this run on click.")
    payload: Any = Field(None, description="Sent as the signal's `value`.")
    style: Literal["primary", "danger"] | None = None


class PostMessage(BaseModel):
    channel: str | None = Field(None, description="Channel id or #name, a user id, or an email (sent as a DM).")
    text: str = Field(description="Message text (mrkdwn); also the notification fallback when blocks are given.")
    blocks: list[dict[str, Any]] = Field(default_factory=list, description="Block Kit blocks.")
    buttons: list[SignalButton] = Field(
        default_factory=list, description="Buttons that send a signal to this run (only from a workflow run)."
    )
    thread_ts: str | None = Field(None, description="Reply in this thread.")
    reply_broadcast: bool = False
    unfurl_links: bool = False


class MessageRef(BaseModel):
    ok: bool = True
    channel: str
    ts: str
    thread_key: str = Field(description="Wake key for replies in this message's thread (wait: signal: event:).")


class UpdateMessage(BaseModel):
    channel: str
    ts: str
    text: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)


class PostEphemeral(BaseModel):
    channel: str
    user: str = Field(description="Slack user id who sees the message.")
    text: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)


class AddReaction(BaseModel):
    channel: str
    ts: str
    name: str = Field(description="Emoji name without colons, e.g. white_check_mark.")


class Ok(BaseModel):
    ok: bool = True


class InputField(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_]{1,50}$")
    label: str = Field(max_length=150)
    type: Literal["text", "multiline", "select", "number"] = "text"
    options: list[str] = Field(default_factory=list, description="select: the choices.")
    optional: bool = False
    placeholder: str | None = None


class RequestInput(BaseModel):
    channel: str | None = None
    text: str = Field(description="What you are asking for.")
    fields: list[InputField] = Field(min_length=1, max_length=20)
    signal: str = Field("slack.input", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$",
                        description="Signal the answers are sent as: {values, user}.")
    button: str = Field("Respond", max_length=75)
    title: str = Field("Shoal", max_length=24, description="Modal title.")
    thread_ts: str | None = None


class LookupUser(BaseModel):
    email: str


class SlackUser(BaseModel):
    id: str
    name: str | None = None
    real_name: str | None = None
    email: str | None = None


class ChannelFilter(BaseModel):
    types: str = Field("public_channel,private_channel", description="Slack conversation types.")
    exclude_archived: bool = True


# ----------------------------------------------------------------------------- helpers


def thread_key(channel: str, ts: str) -> str:
    return f"slack:thread:{channel}:{ts}"


def _plain(text: str, limit: int = 150) -> dict[str, Any]:
    return {"type": "plain_text", "text": (text or " ")[:limit], "emoji": True}


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": (text or " ")[:3000]}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:3000]}]}


def _value(data: dict[str, Any]) -> str:
    value = json.dumps(data, separators=(",", ":"))
    if len(value) > 2000:
        raise IntegrationError("button data is too large for Slack (2000 characters); use fewer or shorter fields")
    return value


def _decode(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _form(body: bytes) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}


def _payload(headers: dict[str, str], body: bytes) -> tuple[str, dict[str, Any]]:
    """("event" | "interactive" | "command" | "unknown", data) for a Slack delivery."""
    if "json" in headers.get("content-type", "") or body.lstrip().startswith(b"{"):
        try:
            return "event", json.loads(body or b"{}")
        except ValueError:
            return "unknown", {}
    form = _form(body)
    if "payload" in form:
        try:
            return "interactive", json.loads(form["payload"])
        except ValueError:
            return "unknown", {}
    if "command" in form:
        return "command", form
    return "unknown", form


def _view_values(view: dict[str, Any]) -> dict[str, Any]:
    """Flatten a submitted modal's state to {action_id: value}."""
    out: dict[str, Any] = {}
    for block in (view.get("state") or {}).get("values", {}).values():
        for action_id, element in block.items():
            kind = element.get("type")
            if kind == "static_select":
                out[action_id] = (element.get("selected_option") or {}).get("value")
            elif kind == "number_input":
                raw = element.get("value")
                out[action_id] = None if raw in (None, "") else float(raw) if "." in raw else int(raw)
            elif kind in ("multi_static_select",):
                out[action_id] = [o.get("value") for o in element.get("selected_options") or []]
            elif kind == "users_select":
                out[action_id] = element.get("selected_user")
            elif kind == "conversations_select":
                out[action_id] = element.get("selected_conversation")
            elif kind == "datepicker":
                out[action_id] = element.get("selected_date")
            else:
                out[action_id] = element.get("value")
    return out


def _field_block(field: dict[str, Any]) -> dict[str, Any]:
    kind = field.get("type", "text")
    element: dict[str, Any]
    if kind == "select":
        element = {"type": "static_select", "options": [
            {"text": _plain(o, 75), "value": o[:150]} for o in field.get("options", [])[:100]
        ]}
    elif kind == "number":
        element = {"type": "number_input", "is_decimal_allowed": True}
    else:
        element = {"type": "plain_text_input", "multiline": kind == "multiline"}
    element["action_id"] = field["name"]
    if field.get("placeholder"):
        element["placeholder"] = _plain(field["placeholder"])
    return {"type": "input", "block_id": f"f_{field['name']}", "label": _plain(field["label"]),
            "optional": bool(field.get("optional")), "element": element}


# ----------------------------------------------------------------------------- integration


class SlackIntegration(Integration):
    key = "slack"
    title = "Slack"
    description = "Messages with buttons and forms, interactive approvals, mentions, slash commands and reactions."
    connection_model = SlackConnection
    supports_approvals = True
    install_fields = ["bot_token"]
    events = [
        EventDef("app_mention", "Someone mentioned the app (`event.command_text` is the text after the mention).",
                 {"channel": "C0123", "user": "U0123", "text": "<@U0BOT> deploy api", "command_text": "deploy api"}),
        EventDef("message", "A message in a channel the app is in, or a DM to the app (bot messages are skipped)."),
        EventDef("reaction_added", "Someone added an emoji reaction (`event.reaction`, `event.item`)."),
        EventDef("slash_command", "The app's slash command (`event.command`, `event.text`, `event.args`).",
                 {"command": "/shoal", "text": "deploy api", "args": ["deploy", "api"], "user_id": "U0123"}),
        EventDef("block_actions", "Someone used a button or menu Shoal does not handle itself (`event.action_id`)."),
        EventDef("view_submission", "Someone submitted a modal Shoal does not handle itself (`event.values`)."),
        EventDef("shortcut", "A global shortcut (`event.callback_id`)."),
        EventDef("message_action", "A message shortcut (`event.callback_id`, `event.message`)."),
    ]

    # ------------------------------------------------------------------ Web API

    def _token(self, ctx: ConnectionContext) -> str:
        token = ctx.config.bot_token
        if token is None or not token.get_secret_value():
            raise IntegrationError(f"Slack connection '{ctx.name}' has no bot token: set one or use 'Add to Slack'")
        return token.get_secret_value()

    async def _api(
        self, ctx: ConnectionContext, method: str, body: dict[str, Any] | None = None, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call a Web API method: JSON POST for writes, GET with query parameters for reads."""
        url = f"{ctx.config.api_url.rstrip('/')}/{method}"
        headers = {"Authorization": f"Bearer {self._token(ctx)}"}
        async with ctx.http() as client:
            if params is not None:
                response = await client.get(url, params=params, headers=headers)
            else:
                response = await client.post(url, json=body or {}, headers={**headers, "Content-Type": "application/json; charset=utf-8"})
        raise_for_status(response, "Slack")
        data = response.json()
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            raise IntegrationError(f"Slack {method} failed: {error}", status=response.status_code,
                                   retryable=error in RETRYABLE_ERRORS)
        return data

    async def _channel(self, ctx: ConnectionContext, channel: str | None) -> str:
        """A channel id/name, a user id, or an email (resolved to the user, which posts a DM)."""
        channel = channel or ctx.config.default_channel
        if not channel:
            raise IntegrationError("no Slack channel given and the connection has no default_channel")
        if "@" in channel and not channel.startswith("@"):
            return (await self._lookup(ctx, channel)).id
        return channel

    async def _lookup(self, ctx: ConnectionContext, email: str) -> SlackUser:
        data = await self._api(ctx, "users.lookupByEmail", params={"email": email})
        return self._user(data["user"])

    @staticmethod
    def _user(user: dict[str, Any]) -> SlackUser:
        profile = user.get("profile") or {}
        return SlackUser(id=user["id"], name=user.get("name"), real_name=user.get("real_name") or profile.get("real_name"),
                         email=profile.get("email"))

    async def _post(self, ctx: ConnectionContext, body: dict[str, Any]) -> MessageRef:
        data = await self._api(ctx, "chat.postMessage", body)
        channel, ts = data["channel"], data["ts"]
        return MessageRef(channel=channel, ts=ts, thread_key=thread_key(channel, body.get("thread_ts") or ts))

    async def test_connection(self, ctx: ConnectionContext) -> dict[str, Any]:
        data = await self._api(ctx, "auth.test")
        return {"ok": True, "team": data.get("team"), "bot": data.get("user"), "url": data.get("url"),
                "verifies_requests": ctx.config.signing_secret is not None}

    # ------------------------------------------------------------------ actions

    @action(description="Post a message (text and optional Block Kit blocks, optionally buttons that signal this run).",
            input=PostMessage, output=MessageRef)
    async def post_message(self, ctx: ConnectionContext, p: PostMessage) -> MessageRef:
        blocks = list(p.blocks)
        if p.buttons:
            if not ctx.run_id:
                raise IntegrationError("buttons signal a workflow run, so post_message with buttons only works in a run")
            if not blocks:
                blocks.append(_section(p.text))
            blocks.append({"type": "actions", "block_id": "shoal_signal", "elements": [
                {"type": "button", "action_id": f"{SIGNAL}:{i}", "text": _plain(b.label, 75),
                 "value": _value({"r": ctx.run_id, "n": b.signal, "p": b.payload, "l": b.label}),
                 **({"style": b.style} if b.style else {})}
                for i, b in enumerate(p.buttons)
            ]})
        body: dict[str, Any] = {"channel": await self._channel(ctx, p.channel), "text": p.text,
                                "unfurl_links": p.unfurl_links}
        if blocks:
            body["blocks"] = blocks
        if p.thread_ts:
            body["thread_ts"], body["reply_broadcast"] = p.thread_ts, p.reply_broadcast
        return await self._post(ctx, body)

    @action(description="Replace a message's text and blocks.", input=UpdateMessage, output=MessageRef)
    async def update_message(self, ctx: ConnectionContext, p: UpdateMessage) -> MessageRef:
        body: dict[str, Any] = {"channel": p.channel, "ts": p.ts, "text": p.text}
        if p.blocks:
            body["blocks"] = p.blocks
        data = await self._api(ctx, "chat.update", body)
        return MessageRef(channel=data["channel"], ts=data["ts"], thread_key=thread_key(data["channel"], data["ts"]))

    @action(description="Post a message only one person in the channel sees.", input=PostEphemeral, output=Ok)
    async def post_ephemeral(self, ctx: ConnectionContext, p: PostEphemeral) -> Ok:
        body: dict[str, Any] = {"channel": p.channel, "user": p.user, "text": p.text}
        if p.blocks:
            body["blocks"] = p.blocks
        await self._api(ctx, "chat.postEphemeral", body)
        return Ok()

    @action(description="Add an emoji reaction to a message.", input=AddReaction, output=Ok)
    async def add_reaction(self, ctx: ConnectionContext, p: AddReaction) -> Ok:
        try:
            await self._api(ctx, "reactions.add", {"channel": p.channel, "timestamp": p.ts, "name": p.name.strip(":")})
        except IntegrationError as exc:
            if "already_reacted" not in str(exc):
                raise
        return Ok()

    @action(description="Ask for input: a message with a button that opens a form; answers are sent to this run "
                        "as a signal ({values, user}).", input=RequestInput, output=MessageRef)
    async def request_input(self, ctx: ConnectionContext, p: RequestInput) -> MessageRef:
        if not ctx.run_id:
            raise IntegrationError("request_input sends the answers to a workflow run, so it only works in a run")
        value = _value({"r": ctx.run_id, "n": p.signal, "t": p.title, "f": [f.model_dump(exclude_defaults=True) for f in p.fields]})
        blocks = [_section(p.text), {"type": "actions", "block_id": "shoal_input", "elements": [
            {"type": "button", "action_id": INPUT, "style": "primary", "text": _plain(p.button, 75), "value": value}
        ]}]
        body: dict[str, Any] = {"channel": await self._channel(ctx, p.channel), "text": p.text, "blocks": blocks}
        if p.thread_ts:
            body["thread_ts"] = p.thread_ts
        return await self._post(ctx, body)

    @action(description="Find a Slack user by email.", input=LookupUser, output=SlackUser, mutating=False)
    async def lookup_user(self, ctx: ConnectionContext, p: LookupUser) -> SlackUser:
        return await self._lookup(ctx, p.email)

    # ------------------------------------------------------------------ resources

    @resource("channel", description="Channels the app can see.", filter=ChannelFilter)
    async def list_channels(self, ctx: ConnectionContext, f: ChannelFilter, cursor: str | None, limit: int) -> Page:
        params: dict[str, Any] = {"types": f.types, "exclude_archived": str(f.exclude_archived).lower(), "limit": min(limit, 200)}
        if cursor:
            params["cursor"] = cursor
        data = await self._api(ctx, "conversations.list", params=params)
        items = [{"id": c["id"], "name": c.get("name"), "is_private": c.get("is_private", False),
                  "is_member": c.get("is_member", False)} for c in data.get("channels", [])]
        return Page(items=items, next_cursor=(data.get("response_metadata") or {}).get("next_cursor") or None)

    # ------------------------------------------------------------------ approvals

    def _approval_blocks(self, request: ApprovalRequest) -> list[dict[str, Any]]:
        details = [f"*{request.workflow}*", f"<{request.run_url}|run {request.run_id}>"]
        if request.initiator:
            details.append(f"for {request.initiator}")
        if request.approvers:
            details.append("approvers: " + ", ".join(request.approvers))
        if request.deadline:
            details.append(f"decide by {request.deadline[:16].replace('T', ' ')} UTC")
        return [
            {"type": "header", "text": _plain(request.options.get("title") or "Approval needed")},
            _section(request.message),
            _context(" · ".join(details)),
        ]

    async def request_approval(self, ctx: ConnectionContext, request: ApprovalRequest) -> dict[str, Any]:
        comment = bool(request.options.get("ask_comment"))
        value = {"r": request.run_id, "s": request.step_id, **({"c": 1} if comment else {})}
        buttons = [
            {"type": "button", "action_id": APPROVE, "style": "primary", "text": _plain(request.options.get("approve_label") or "Approve", 75),
             "value": _value(value)},
            {"type": "button", "action_id": REJECT, "style": "danger", "text": _plain(request.options.get("reject_label") or "Reject", 75),
             "value": _value(value)},
            {"type": "button", "action_id": OPEN, "text": _plain("Open in Shoal"), "url": request.run_url},
        ]
        blocks = [*self._approval_blocks(request), {"type": "actions", "block_id": "shoal_approval", "elements": buttons}]
        body: dict[str, Any] = {"channel": await self._channel(ctx, request.to), "text": f"Approval needed: {request.message}"[:3000],
                                "blocks": blocks}
        if request.options.get("thread_ts"):
            body["thread_ts"] = request.options["thread_ts"]
        ref = await self._post(ctx, body)
        return {"channel": ref.channel, "ts": ref.ts}

    async def approval_resolved(
        self, ctx: ConnectionContext, request: ApprovalRequest, ref: dict[str, Any], outcome: dict[str, Any]
    ) -> None:
        if outcome.get("timed_out"):
            verdict = ":hourglass: Timed out, " + ("approved automatically" if outcome.get("approved") else "rejected automatically")
        elif outcome.get("approved"):
            verdict = f":white_check_mark: Approved by {outcome.get('by') or 'someone'}"
        else:
            verdict = f":x: Rejected by {outcome.get('by') or 'someone'}"
        if outcome.get("comment"):
            verdict += f": {outcome['comment']}"
        blocks = [*self._approval_blocks(request), _section(verdict)]
        await self._api(ctx, "chat.update", {"channel": ref["channel"], "ts": ref["ts"],
                                             "text": f"{verdict} ({request.message})"[:3000], "blocks": blocks})

    # ------------------------------------------------------------------ app install

    def install_url(self, config: Any, redirect_uri: str, state: str) -> str:
        if not config.client_id or config.client_secret is None:
            raise IntegrationError("set the Slack app's client_id and client_secret on the connection to use 'Add to Slack'")
        query = urlencode({"client_id": config.client_id, "scope": ",".join(BOT_SCOPES), "redirect_uri": redirect_uri, "state": state})
        return f"https://slack.com/oauth/v2/authorize?{query}"

    async def complete_install(self, ctx: ConnectionContext, code: str, redirect_uri: str) -> dict[str, str]:
        cfg: SlackConnection = ctx.config
        if not cfg.client_id or cfg.client_secret is None:
            raise IntegrationError("the connection has no client_id / client_secret")
        async with ctx.http() as client:
            response = await client.post(f"{cfg.api_url.rstrip('/')}/oauth.v2.access", data={
                "code": code, "client_id": cfg.client_id, "client_secret": cfg.client_secret.get_secret_value(),
                "redirect_uri": redirect_uri,
            })
        raise_for_status(response, "Slack")
        data = response.json()
        if not data.get("ok") or not data.get("access_token"):
            raise IntegrationError(f"Slack refused the install: {data.get('error', 'no bot token returned')}")
        return {"bot_token": data["access_token"]}

    # ------------------------------------------------------------------ setup

    def setup_manifest(self, webhook_url: str, install_redirect_url: str, connection: str) -> dict[str, Any]:
        return {
            "display_information": {"name": "Shoal", "description": "Workflow approvals, inputs and notifications from Shoal.",
                                    "background_color": "#0b3d5c"},
            "features": {
                "bot_user": {"display_name": "Shoal", "always_online": True},
                "slash_commands": [{"command": "/shoal", "url": webhook_url, "description": "Talk to Shoal workflows",
                                    "usage_hint": "[what to do]", "should_escape": False}],
            },
            "oauth_config": {"redirect_urls": [install_redirect_url], "scopes": {"bot": BOT_SCOPES}},
            "settings": {
                "event_subscriptions": {"request_url": webhook_url, "bot_events": BOT_EVENTS},
                "interactivity": {"is_enabled": True, "request_url": webhook_url},
                "org_deploy_enabled": False,
                "socket_mode_enabled": False,
                "token_rotation_enabled": False,
            },
        }

    def webhook_setup_instructions(self, url: str) -> str:
        return (
            "Create a Slack app from the manifest below (api.slack.com/apps > Create New App > From a manifest); "
            f"it points Event Subscriptions, Interactivity and the /shoal command at {url}. Put the app's signing "
            "secret on this connection, then install the app: 'Add to Slack' (with client id and secret) or paste "
            "the bot token. Invite the app to the channels it should post in or listen to."
        )

    # ------------------------------------------------------------------ inbound

    async def verify_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> bool:
        """Slack's v0 signature over ``v0:<timestamp>:<body>``, at most five minutes old."""
        secret = ctx.config.signing_secret
        signature, timestamp = headers.get("x-slack-signature"), headers.get("x-slack-request-timestamp")
        if secret is None or not signature or not timestamp:
            return False
        try:
            if abs(clock.now().timestamp() - int(timestamp)) > MAX_SKEW:
                return False
        except ValueError:
            return False
        base = b"v0:" + timestamp.encode() + b":" + body
        expected = "v0=" + hmac.new(secret.get_secret_value().encode(), base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    async def _actor(self, ctx: ConnectionContext, user: dict[str, Any] | str | None) -> dict[str, Any]:
        """Identity hints for a Slack user; the email comes from users.info when the app may read it."""
        if isinstance(user, str):
            user = {"id": user}
        user = user or {}
        hints: dict[str, Any] = {"slack_id": user.get("id"), "display_name": user.get("name") or user.get("username")}
        if user.get("id") and ctx.config.bot_token is not None:
            try:
                info = self._user((await self._api(ctx, "users.info", params={"user": user["id"]}))["user"])
                hints["email"] = info.email
                hints["display_name"] = info.real_name or hints["display_name"]
            except Exception as exc:  # mapping falls back to slack_id alone
                log.info("slack users.info for %s failed: %s", user.get("id"), exc)
        return {k: v for k, v in hints.items() if v}

    async def parse_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> list[IntegrationEvent]:
        kind, data = _payload(headers, body)
        if kind == "event":
            return self._parse_event(data)
        if kind == "command":
            return [IntegrationEvent(
                type="slash_command",
                id=data.get("trigger_id"),
                data={**data, "args": (data.get("text") or "").split()},
                actor={k: v for k, v in {"slack_id": data.get("user_id"), "display_name": data.get("user_name")}.items() if v},
            )]
        if kind == "interactive":
            return await self._parse_interactive(ctx, data)
        return []

    def _parse_event(self, data: dict[str, Any]) -> list[IntegrationEvent]:
        if data.get("type") != "event_callback":
            return []  # url_verification is answered in webhook_reply
        event = data.get("event") or {}
        kind = event.get("type")
        if kind == "message" and (event.get("bot_id") or event.get("subtype")):
            return []  # bot messages (ours included), edits and deletions
        if kind not in ("app_mention", "message", "reaction_added"):
            return []
        channel = event.get("channel") or (event.get("item") or {}).get("channel")
        out = {**event, "team_id": data.get("team_id"), "event_id": data.get("event_id"), "channel": channel}
        keys = []
        if kind == "reaction_added":
            item = event.get("item") or {}
            if channel and item.get("ts"):
                keys.append(f"slack:message:{channel}:{item['ts']}")
        else:
            out["command_text"] = MENTION.sub("", event.get("text") or "").strip()
            if channel and event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
                keys.append(thread_key(channel, event["thread_ts"]))
            if channel:
                keys.append(f"slack:channel:{channel}")
        return [IntegrationEvent(type=kind, id=data.get("event_id"), data=out, wake_keys=keys,
                                 actor={"slack_id": event["user"]} if event.get("user") else {})]

    async def _parse_interactive(self, ctx: ConnectionContext, data: dict[str, Any]) -> list[IntegrationEvent]:
        kind = data.get("type")
        user = data.get("user") or {}
        if kind == "block_actions":
            act = (data.get("actions") or [{}])[0]
            action_id = act.get("action_id") or ""
            value = _decode(act.get("value"))
            if action_id in (APPROVE, REJECT):
                if value.get("c"):
                    return []  # the comment modal opens in webhook_reply; its submission decides
                interaction = Interaction(kind="approval", run_id=value.get("r", ""), step_id=value.get("s"),
                                          approved=action_id == APPROVE)
                return [IntegrationEvent(type="block_actions", id=data.get("trigger_id"), interaction=interaction,
                                         actor=await self._actor(ctx, user))]
            if action_id in (OPEN, INPUT):
                return []  # a link, or a form that opens in webhook_reply
            if action_id.startswith(SIGNAL):
                interaction = Interaction(kind="signal", run_id=value.get("r", ""), signal=value.get("n"),
                                          payload={"value": value.get("p"), "label": value.get("l"), "user": self._who(user)})
                return [IntegrationEvent(type="block_actions", id=data.get("trigger_id"), interaction=interaction,
                                         actor=await self._actor(ctx, user))]
            return [self._generic_action(data, act)]
        if kind == "view_submission":
            view = data.get("view") or {}
            meta = _decode(view.get("private_metadata"))
            values = _view_values(view)
            if view.get("callback_id") == COMMENT_VIEW:
                interaction = Interaction(kind="approval", run_id=meta.get("r", ""), step_id=meta.get("s"),
                                          approved=bool(meta.get("a")), comment=values.get("comment") or None)
            elif view.get("callback_id") == INPUT_VIEW:
                interaction = Interaction(kind="signal", run_id=meta.get("r", ""), signal=meta.get("n"),
                                          payload={"values": values, "user": self._who(user)})
            else:
                return [IntegrationEvent(type="view_submission", id=view.get("id"), data={
                    "callback_id": view.get("callback_id"), "private_metadata": view.get("private_metadata"),
                    "values": values, "user": self._who(user)}, actor=await self._actor(ctx, user))]
            return [IntegrationEvent(type="view_submission", id=view.get("id"), interaction=interaction,
                                     actor=await self._actor(ctx, user))]
        if kind in ("shortcut", "message_action"):
            channel = (data.get("channel") or {}).get("id")
            out = {"callback_id": data.get("callback_id"), "trigger_id": data.get("trigger_id"), "user": self._who(user),
                   "channel": channel, "message": data.get("message"), "response_url": data.get("response_url")}
            return [IntegrationEvent(type=kind, id=data.get("trigger_id"), data=out, actor=await self._actor(ctx, user))]
        return []

    @staticmethod
    def _who(user: dict[str, Any]) -> dict[str, Any]:
        return {"id": user.get("id"), "name": user.get("name") or user.get("username")}

    def _generic_action(self, data: dict[str, Any], act: dict[str, Any]) -> IntegrationEvent:
        channel = (data.get("channel") or {}).get("id")
        message = data.get("message") or {}
        keys = [f"slack:action:{act.get('action_id')}"]
        if channel and message.get("ts"):
            keys.append(f"slack:message:{channel}:{message['ts']}")
        return IntegrationEvent(
            type="block_actions",
            id=data.get("trigger_id"),
            data={
                "action_id": act.get("action_id"), "block_id": act.get("block_id"), "value": act.get("value"),
                "selected": (act.get("selected_option") or {}).get("value"), "user": self._who(data.get("user") or {}),
                "channel": channel, "message_ts": message.get("ts"), "thread_ts": message.get("thread_ts"),
                "trigger_id": data.get("trigger_id"), "response_url": data.get("response_url"),
            },
            wake_keys=keys,
            actor={"slack_id": (data.get("user") or {}).get("id")} if (data.get("user") or {}).get("id") else {},
        )

    # ------------------------------------------------------------------ answering Slack

    async def webhook_reply(
        self, ctx: ConnectionContext, headers: dict[str, str], body: bytes, result: dict[str, Any]
    ) -> WebhookReply | None:
        kind, data = _payload(headers, body)
        if kind == "event":
            if data.get("type") == "url_verification":
                return WebhookReply(body={"challenge": data.get("challenge")})
            return WebhookReply()
        if kind == "command":
            return WebhookReply(body={"response_type": "ephemeral", "text": self._command_answer(ctx, data, result)})
        if kind != "interactive":
            return WebhookReply()
        outcome = (result.get("interactions") or [None])[0]
        if data.get("type") == "block_actions":
            await self._after_action(ctx, data, outcome)
            return WebhookReply()
        if data.get("type") == "view_submission" and outcome and not outcome.get("ok"):
            view = data.get("view") or {}
            block = next((b.get("block_id") for b in view.get("blocks", []) if b.get("type") == "input"), None)
            if block:
                return WebhookReply(body={"response_action": "errors", "errors": {block: outcome.get("error", "refused")[:150]}})
        return WebhookReply()

    def _command_answer(self, ctx: ConnectionContext, data: dict[str, Any], result: dict[str, Any]) -> str:
        text = f"{data.get('command', '')} {data.get('text', '')}".strip()
        fired = result.get("triggers") or []
        if not fired:
            return f"No Shoal trigger handles `{text}`."
        base = getattr(getattr(ctx.engine, "settings", None), "public_url", "").rstrip("/")
        lines = []
        for f in fired:
            if f.get("run_id"):
                link = f"<{base}/ns/{f.get('namespace', 'default')}/runs/{f['run_id']}|{f['run_id']}>" if base else f["run_id"]
                lines.append(f":rocket: {f['trigger']} started run {link}")
            elif f.get("status") == "fired":
                lines.append(f":rocket: {f['trigger']} started")
            else:
                lines.append(f":warning: {f['trigger']}: {f.get('status')} {f.get('reason') or ''}".rstrip())
        return "\n".join(lines)

    async def _after_action(self, ctx: ConnectionContext, data: dict[str, Any], outcome: dict[str, Any] | None) -> None:
        act = (data.get("actions") or [{}])[0]
        action_id = act.get("action_id") or ""
        value = _decode(act.get("value"))
        try:
            if action_id == INPUT:
                await self._open_input(ctx, data, value)
            elif action_id in (APPROVE, REJECT) and value.get("c"):
                await self._open_comment(ctx, data, value, approved=action_id == APPROVE)
            elif outcome is not None and not outcome.get("ok"):
                await self._respond(ctx, data, {"response_type": "ephemeral", "replace_original": False,
                                                "text": f":no_entry: {outcome.get('error', 'not allowed')}"})
            elif outcome is not None and action_id.startswith(SIGNAL):
                blocks = [b for b in (data.get("message") or {}).get("blocks", []) if b.get("block_id") != "shoal_signal"]
                blocks.append(_context(f"*{outcome.get('by') or 'Someone'}* chose *{value.get('l')}*"))
                await self._respond(ctx, data, {"replace_original": True, "blocks": blocks,
                                                "text": (data.get("message") or {}).get("text") or value.get("l") or ""})
        except Exception as exc:  # Slack only needs the 200; a failed follow-up is logged
            log.warning("slack follow-up for %s failed: %s", action_id, exc)

    async def _respond(self, ctx: ConnectionContext, data: dict[str, Any], message: dict[str, Any]) -> None:
        url = data.get("response_url")
        if not url or not url.startswith("https://"):
            return
        async with ctx.http() as client:
            await client.post(url, json=message)

    async def _open_input(self, ctx: ConnectionContext, data: dict[str, Any], value: dict[str, Any]) -> None:
        view = {
            "type": "modal", "callback_id": INPUT_VIEW, "title": _plain(value.get("t") or "Shoal", 24),
            "submit": _plain("Submit", 24), "close": _plain("Cancel", 24),
            "private_metadata": json.dumps({"r": value.get("r"), "n": value.get("n")}),
            "blocks": [_field_block(f) for f in value.get("f", [])],
        }
        await self._api(ctx, "views.open", {"trigger_id": data.get("trigger_id"), "view": view})

    async def _open_comment(self, ctx: ConnectionContext, data: dict[str, Any], value: dict[str, Any], *, approved: bool) -> None:
        view = {
            "type": "modal", "callback_id": COMMENT_VIEW, "title": _plain("Approve" if approved else "Reject", 24),
            "submit": _plain("Approve" if approved else "Reject", 24), "close": _plain("Cancel", 24),
            "private_metadata": json.dumps({"r": value.get("r"), "s": value.get("s"), "a": approved}),
            "blocks": [{"type": "input", "block_id": "shoal_comment", "optional": approved, "label": _plain("Comment"),
                        "element": {"type": "plain_text_input", "action_id": "comment", "multiline": True}}],
        }
        await self._api(ctx, "views.open", {"trigger_id": data.get("trigger_id"), "view": view})
