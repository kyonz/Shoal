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
* Custom Block Kit messages hook back into the run: any interactive element
  (buttons, selects, date and time pickers, checkboxes, overflow menus) marked
  ``shoal: {signal: <name>}`` (or every element, with the message's ``signal``)
  sends the run that signal with the chosen value, and a button can open a custom
  modal whose answers are sent instead. ``request_input`` is a ready-made form. A
  ``wait`` step with ``signal: {name: ...}`` takes them; branch on its output.
* Mentions, messages, reactions, slash commands, shortcuts and other button
  clicks are trigger events. Thread replies wake ``wait`` steps waiting on
  ``slack:thread:<channel>:<ts>`` (``thread_key`` in ``post_message``'s output).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import zlib
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode

from pydantic import BaseModel, ConfigDict, Field, SecretStr

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
    SetupFeature,
    SetupPermission,
    WebhookReply,
    action,
    raise_for_status,
    resource,
)

log = logging.getLogger(__name__)


def _p(name: str, why: str) -> SetupPermission:
    return SetupPermission(name=name, why=why)


FEATURES = [
    SetupFeature(
        key="messages", title="Messages, approvals and forms", required=True,
        description="Post and update messages, ask for approvals and input with buttons, menus and forms, and know who "
                    "clicked. Interactivity (clicks and form submissions) is always on.",
        scopes=[
            _p("chat:write", "Post and update messages, approvals and forms."),
            _p("chat:write.public", "Post in public channels without being invited first."),
            _p("im:write", "Send approvals and questions to a person as a direct message."),
            _p("users:read", "Show who clicked a button or answered a form."),
            _p("users:read.email", "Match Slack people to Shoal users by email, so their clicks count as theirs."),
            _p("channels:read", "Turn #channel names into channel ids."),
            _p("groups:read", "Turn private #channel names into ids (once the app is invited)."),
        ],
    ),
    SetupFeature(
        key="mentions", title="Answer @mentions",
        description="Start workflows when someone @-mentions the app (app_mention triggers).",
        scopes=[_p("app_mentions:read", "Receive messages that @-mention the app.")],
        events=[_p("app_mention", "Tells Shoal when the app is mentioned.")],
    ),
    SetupFeature(
        key="channel_messages", title="Channel bots and conversation context",
        description="Wire a channel to a workflow so every message (or every matching one) can start a run, and read "
                    "threads and channel history with slack.get_conversation.",
        scopes=[
            _p("channels:history", "Read messages in public channels the app is in."),
            _p("groups:history", "Read messages in private channels the app is invited to."),
        ],
        events=[
            _p("message.channels", "Tells Shoal about new messages in public channels the app is in."),
            _p("message.groups", "Tells Shoal about new messages in private channels the app is in."),
        ],
    ),
    SetupFeature(
        key="direct_messages", title="Direct messages to the app",
        description="Let people DM the app (message triggers with direct_messages), with the app's Messages tab on.",
        scopes=[_p("im:history", "Read direct messages people send the app.")],
        events=[_p("message.im", "Tells Shoal about direct messages to the app.")],
    ),
    SetupFeature(
        key="reactions", title="Reactions",
        description="Start workflows from emoji reactions and let workflows react (the bot blueprint adds :eyes:).",
        scopes=[
            _p("reactions:read", "Receive reactions added to messages."),
            _p("reactions:write", "Add reactions with slack.add_reaction."),
        ],
        events=[_p("reaction_added", "Tells Shoal when someone adds a reaction.")],
    ),
    SetupFeature(
        key="slash_command", title="Slash command",
        description="A /command that starts workflows as the person who typed it (slash_command triggers).",
        scopes=[_p("commands", "Add the slash command to the workspace.")],
    ),
    SetupFeature(
        key="files", title="Shared files",
        description="Read files shared in messages (message filters with has_files, event.files).",
        scopes=[_p("files:read", "Read details of files shared in channels the app is in.")],
    ),
]
FEATURE_KEYS = [f.key for f in FEATURES]
BOT_SCOPES = list(dict.fromkeys(p.name for f in FEATURES for p in f.scopes))
BOT_EVENTS = list(dict.fromkeys(p.name for f in FEATURES for p in f.events))
SKIPPED_SUBTYPES = {"message_changed", "message_deleted", "message_replied", "channel_join", "channel_leave",
                    "channel_topic", "channel_purpose", "channel_name", "group_join", "group_leave", "pinned_item",
                    "unpinned_item", "ekm_access_denied"}
CHANNEL_CACHE_SECONDS = 300
_channel_names: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}
MAX_SKEW = 300  # seconds a signed request may be old (Slack's replay window)
RETRYABLE_ERRORS = {"ratelimited", "service_unavailable", "internal_error", "fatal_error", "request_timeout"}
APPROVE, REJECT, OPEN, INPUT = "shoal_approve", "shoal_reject", "shoal_open", "shoal_input"
HOOK = "shoal:"  # action_id prefix of elements bound to a run (see bind_blocks)
INTERACTIVE = {
    "button", "static_select", "external_select", "users_select", "conversations_select", "channels_select",
    "multi_static_select", "multi_external_select", "multi_users_select", "multi_conversations_select",
    "multi_channels_select", "overflow", "datepicker", "timepicker", "datetimepicker", "checkboxes", "radio_buttons",
}
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
    app_name: str = Field("Shoal", max_length=35, description="The app's name and bot name in the generated manifest.")
    slash_command: str = Field("/shoal", pattern=r"^/[a-z0-9_-]{1,31}$", description="The slash command in the generated manifest.")
    features: list[str] | None = Field(
        None, description=f"What the app is set up for ({', '.join(FEATURE_KEYS)}); decides the manifest's scopes and "
                          "events. Empty: everything."
    )


# ----------------------------------------------------------------------------- models


class SignalButton(BaseModel):
    label: str = Field(max_length=75)
    signal: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$", description="Signal sent to this run on click.")
    payload: Any = Field(None, description="Sent as the signal's `value`.")
    style: Literal["primary", "danger"] | None = None


AfterClick = Literal["disable", "keep"]

BLOCKS_HELP = (
    "Block Kit blocks (a list, or the JSON Block Kit Builder gives you), with expressions. Interactive elements can "
    "carry a Shoal-only `shoal:` key: {signal: <name>} sends this run the signal with the chosen value, "
    "{signal: <name>, modal: <view>} opens that modal and sends its answers, {event: true} leaves the element to "
    "triggers (block_actions); add `after: keep` to leave the message as it is after a click."
)
SIGNAL_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$"


class PostMessage(BaseModel):
    channel: str | None = Field(None, description="Channel id or #name, a user id, or an email (sent as a DM).")
    text: str = Field(description="Message text (mrkdwn); also the notification fallback when blocks are given.")
    blocks: list[dict[str, Any]] | str = Field(default_factory=list, description=BLOCKS_HELP)
    signal: str | None = Field(
        None, pattern=SIGNAL_NAME,
        description="Send this signal to the run when someone uses any interactive element without its own `shoal:` "
        "key. The payload is {action_id, value, label, user, channel, message_ts}.",
    )
    after_click: AfterClick = Field(
        "disable", description="After a click that signals the run: replace the message's controls with who chose what "
        "(disable), or leave the message alone (keep).",
    )
    buttons: list[SignalButton] = Field(
        default_factory=list, description="Shorthand for buttons that send a signal to this run (only from a workflow run)."
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
    blocks: list[dict[str, Any]] | str = Field(default_factory=list, description=BLOCKS_HELP)
    signal: str | None = Field(None, pattern=SIGNAL_NAME)
    after_click: AfterClick = "disable"


class PostEphemeral(BaseModel):
    channel: str
    user: str = Field(description="Slack user id who sees the message.")
    text: str
    blocks: list[dict[str, Any]] | str = Field(default_factory=list, description="Block Kit blocks (not bound to the run).")


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


class Reply(BaseModel):
    channel: str = Field(description="The channel of the message to answer (`event.channel`).")
    ts: str = Field(description="The message to answer (`event.reply_ts` keeps a thread together).")
    text: str
    blocks: list[dict[str, Any]] | str = Field(default_factory=list, description=BLOCKS_HELP)
    signal: str | None = Field(None, pattern=SIGNAL_NAME)
    after_click: AfterClick = "disable"
    broadcast: bool = Field(False, description="Also send the reply to the channel.")


class GetConversation(BaseModel):
    channel: str
    thread_ts: str | None = Field(None, description="A thread's root ts: its messages; omitted, the channel's latest.")
    limit: int = Field(20, ge=1, le=200)
    names: bool = Field(True, description="Add each author's name (one users.info per person).")


class ConversationMessage(BaseModel):
    ts: str
    user: str | None = None
    name: str | None = None
    text: str
    bot: bool = False
    thread_ts: str | None = None


class Conversation(BaseModel):
    channel: str
    messages: list[ConversationMessage]
    transcript: str = Field(description="The messages as 'name: text' lines, oldest first, for prompts.")


class SlackTriggerFilter(BaseModel):
    """Which Slack messages fire a trigger (``match:``). Every condition set must hold."""

    model_config = ConfigDict(extra="forbid")

    channels: list[str] = Field(default_factory=list, description="Channel ids or #names; empty: any channel the app is in.")
    mentioned: bool | None = Field(None, description="True: only messages mentioning the app; False: only without.")
    direct_messages: bool | None = Field(None, description="True: only DMs to the app; False: never DMs.")
    contains: list[str] = Field(default_factory=list, description="Fire when the text contains any of these.")
    contains_all: list[str] = Field(default_factory=list, description="Fire only when the text contains all of these.")
    excludes: list[str] = Field(default_factory=list, description="Never fire when the text contains any of these.")
    regex: str | None = Field(None, description="Fire when the text matches (search); named groups land in `event.match`.")
    case_sensitive: bool = False
    users: list[str] = Field(default_factory=list, description="Only from these Slack user ids.")
    exclude_users: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list, description="Only from people in these Shoal groups (or identities).")
    threads: Literal["any", "top_level", "replies"] = "any"
    include_bots: bool = Field(False, description="Also fire on other bots' messages (never on the app's own).")
    has_files: bool | None = None
    reactions: list[str] = Field(default_factory=list, description="reaction_added: only these emoji names.")


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


def _without_controls(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A message's blocks with its bound controls removed (actions blocks and bound accessories)."""
    out = []
    for block in blocks:
        if block.get("type") == "actions":
            kept = [e for e in block.get("elements", []) if not str(e.get("action_id", "")).startswith(HOOK)]
            if kept:
                out.append({**block, "elements": kept})
            continue
        accessory = block.get("accessory") or {}
        if str(accessory.get("action_id", "")).startswith(HOOK):
            block = {k: v for k, v in block.items() if k != "accessory"}
        out.append(block)
    return out


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


def _blocks(value: list[dict[str, Any]] | str) -> list[dict[str, Any]]:
    """Blocks as given, or parsed from JSON (``{"blocks": [...]}`` as Block Kit Builder copies it, or a list)."""
    if not isinstance(value, str):
        return [dict(b) for b in value]
    if not value.strip():
        return []
    try:
        data = json.loads(value)
    except ValueError as exc:
        raise IntegrationError(f"blocks is not valid JSON: {exc}") from exc
    data = data.get("blocks", []) if isinstance(data, dict) else data
    if not isinstance(data, list):
        raise IntegrationError("blocks must be a list of Block Kit blocks")
    return data


def _pack(data: dict[str, Any]) -> str:
    """A compact, compressed encoding for data carried in a button's value (Slack allows 2000 characters)."""
    raw = zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9)
    value = "z:" + base64.urlsafe_b64encode(raw).decode()
    if len(value) > 2000:
        raise IntegrationError("a modal bound to a button must stay under about 4 KB of Block Kit JSON")
    return value


def _unpack(value: str | None) -> dict[str, Any]:
    if not value or not value.startswith("z:"):
        return _decode(value)
    try:
        return json.loads(zlib.decompress(base64.urlsafe_b64decode(value[2:].encode())))
    except (ValueError, zlib.error):
        return {}


def _elements(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every interactive element of a message: in actions blocks and as section accessories."""
    out = []
    for block in blocks:
        if block.get("type") == "actions":
            out += [e for e in block.get("elements", []) if isinstance(e, dict)]
        elif block.get("type") == "section" and isinstance(block.get("accessory"), dict):
            out.append(block["accessory"])
    return [e for e in out if e.get("type") in INTERACTIVE]


def bind_blocks(blocks: list[dict[str, Any]], run_id: str | None, *, signal: str | None, after: str) -> list[dict[str, Any]]:
    """Wire interactive elements to a run: a bound element's action_id carries ``shoal:{run, signal, ...}``.

    The Shoal-only ``shoal:`` key is removed before the blocks go to Slack. Elements left unbound
    (no ``shoal:`` key and no message ``signal``, or ``shoal: {event: true}``) reach triggers as
    ``block_actions`` events.
    """
    blocks = json.loads(json.dumps(blocks))  # never change the caller's (rendered) blocks
    for index, element in enumerate(_elements(blocks)):
        hook = element.pop("shoal", None)
        if hook is None and (not signal or element.get("url")):
            continue
        hook = {"signal": hook} if isinstance(hook, str) else dict(hook or {})
        if hook.get("event"):
            continue
        name = hook.get("signal") or signal
        if not name or not re.match(SIGNAL_NAME, str(name)):
            raise IntegrationError(f"element {element.get('action_id') or index}: `shoal:` needs a valid `signal` name")
        if not run_id:
            raise IntegrationError("elements bound to a signal need a workflow run, so post them from a workflow")
        bound: dict[str, Any] = {"r": run_id, "n": name, "k": element.get("action_id") or str(index)}
        if (hook.get("after") or after) == "keep":
            bound["a"] = "k"
        modal = hook.get("modal")
        if modal is not None:
            if element.get("type") != "button":
                raise IntegrationError("only buttons can open a modal")
            element["value"] = _pack({"v": modal, "t": element.get("text", {}).get("text")})
            bound["m"] = 1
        action_id = HOOK + json.dumps(bound, separators=(",", ":"))
        if len(action_id) > 255:
            raise IntegrationError(f"action_id {bound['k']!r} is too long to bind (keep action ids and signal names short)")
        element["action_id"] = action_id
    return blocks


def _hook(action_id: str | None) -> dict[str, Any] | None:
    if not action_id or not action_id.startswith(HOOK):
        return None
    return _decode(action_id[len(HOOK):]) or None


def _option(option: dict[str, Any] | None) -> tuple[Any, Any]:
    option = option or {}
    return option.get("value"), (option.get("text") or {}).get("text")


def action_value(act: dict[str, Any]) -> tuple[Any, Any]:
    """(value, label) of a used element, for every interactive element type."""
    kind = act.get("type")
    if kind == "button":
        return act.get("value"), (act.get("text") or {}).get("text")
    if kind in ("static_select", "external_select", "overflow", "radio_buttons"):
        return _option(act.get("selected_option"))
    if kind in ("multi_static_select", "multi_external_select", "checkboxes"):
        options = act.get("selected_options") or []
        return [o.get("value") for o in options], ", ".join(str((o.get("text") or {}).get("text")) for o in options)
    single = {"users_select": "selected_user", "conversations_select": "selected_conversation",
              "channels_select": "selected_channel", "datepicker": "selected_date", "timepicker": "selected_time",
              "datetimepicker": "selected_date_time"}
    if kind in single:
        return act.get(single[kind]), act.get(single[kind])
    multi = {"multi_users_select": "selected_users", "multi_conversations_select": "selected_conversations",
             "multi_channels_select": "selected_channels"}
    if kind in multi:
        return act.get(multi[kind]) or [], None
    return act.get("value"), None


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
            if element.get("type") == "number_input":
                raw = element.get("value")
                out[action_id] = None if raw in (None, "") else float(raw) if "." in raw else int(raw)
            elif element.get("type") in ("plain_text_input", "email_text_input", "url_text_input", "rich_text_input"):
                out[action_id] = element.get("value")
            else:
                out[action_id] = action_value(element)[0]
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
    trigger_filter_model = SlackTriggerFilter
    supports_approvals = True
    install_fields = ["bot_token"]
    events = [
        EventDef("app_mention", "Someone mentioned the app (`event.command_text` is the text after the mention).",
                 {"channel": "C0123", "user": "U0123", "text": "<@U0BOT> deploy api", "command_text": "deploy api"}),
        EventDef("message", "A message in a channel the app is in, or a DM to the app. Wire a channel to a workflow "
                 "with `match:` filters; `event.reply_ts` answers in the thread.",
                 {"channel": "C0123", "user": "U0123", "text": "<@U0BOT> what broke?", "command_text": "what broke?",
                  "mentioned": True, "is_dm": False, "reply_ts": "1700000000.000100"}),
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
        """Check the token, and that the app was granted every scope its features need."""
        url = f"{ctx.config.api_url.rstrip('/')}/auth.test"
        async with ctx.http() as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {self._token(ctx)}"})
        raise_for_status(response, "Slack")
        data = response.json()
        if not data.get("ok"):
            raise IntegrationError(f"Slack auth.test failed: {data.get('error', 'unknown_error')}")
        out: dict[str, Any] = {"ok": True, "team": data.get("team"), "bot": data.get("user"), "url": data.get("url"),
                               "verifies_requests": ctx.config.signing_secret is not None}
        header = response.headers.get("x-oauth-scopes")
        if header is not None:
            granted = {s.strip() for s in header.split(",") if s.strip()}
            needed, _ = self.setup_permissions(self.enabled_features(ctx.config))
            out["scopes"] = sorted(granted)
            out["missing_scopes"] = [s for s in needed if s not in granted]
            if out["missing_scopes"]:
                out["ok"] = False
                out["error"] = ("the app is missing scopes its features need: " + ", ".join(out["missing_scopes"])
                                + ". Update the app from the new manifest and reinstall it.")
        if ctx.config.signing_secret is None:
            out["warning"] = "no signing secret: clicks, events and slash commands are rejected until you add it"
        return out

    # ------------------------------------------------------------------ actions

    @action(description="Post a message (text and optional Block Kit blocks, optionally buttons that signal this run).",
            input=PostMessage, output=MessageRef)
    async def post_message(self, ctx: ConnectionContext, p: PostMessage) -> MessageRef:
        blocks = _blocks(p.blocks)
        if p.buttons:
            if not blocks:
                blocks.append(_section(p.text))
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "action_id": f"b{i}", "text": _plain(b.label, 75),
                 "value": (b.payload if isinstance(b.payload, str) else json.dumps(b.payload)) if b.payload is not None else b.label,
                 "shoal": {"signal": b.signal}, **({"style": b.style} if b.style else {})}
                for i, b in enumerate(p.buttons)
            ]})
        blocks = bind_blocks(blocks, ctx.run_id, signal=p.signal, after=p.after_click)
        body: dict[str, Any] = {"channel": await self._channel(ctx, p.channel), "text": p.text,
                                "unfurl_links": p.unfurl_links}
        if blocks:
            body["blocks"] = blocks
        if p.thread_ts:
            body["thread_ts"], body["reply_broadcast"] = p.thread_ts, p.reply_broadcast
        return await self._post(ctx, body)

    @action(description="Replace a message's text and blocks (blocks can be bound to the run like post_message's).",
            input=UpdateMessage, output=MessageRef)
    async def update_message(self, ctx: ConnectionContext, p: UpdateMessage) -> MessageRef:
        body: dict[str, Any] = {"channel": p.channel, "ts": p.ts, "text": p.text}
        blocks = bind_blocks(_blocks(p.blocks), ctx.run_id, signal=p.signal, after=p.after_click)
        if blocks:
            body["blocks"] = blocks
        data = await self._api(ctx, "chat.update", body)
        return MessageRef(channel=data["channel"], ts=data["ts"], thread_key=thread_key(data["channel"], data["ts"]))

    @action(description="Post a message only one person in the channel sees.", input=PostEphemeral, output=Ok)
    async def post_ephemeral(self, ctx: ConnectionContext, p: PostEphemeral) -> Ok:
        body: dict[str, Any] = {"channel": p.channel, "user": p.user, "text": p.text}
        if p.blocks:
            body["blocks"] = _blocks(p.blocks)
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

    @action(description="Reply to a message in its thread (bots: `ts: \"{{ event.reply_ts }}\"`).", input=Reply, output=MessageRef)
    async def reply(self, ctx: ConnectionContext, p: Reply) -> MessageRef:
        return await self.post_message(ctx, PostMessage(
            channel=p.channel, text=p.text, blocks=p.blocks, signal=p.signal, after_click=p.after_click,
            thread_ts=p.ts, reply_broadcast=p.broadcast,
        ))

    @action(description="Read a thread or a channel's latest messages, oldest first, as context for an agent.",
            input=GetConversation, output=Conversation, mutating=False)
    async def get_conversation(self, ctx: ConnectionContext, p: GetConversation) -> Conversation:
        if p.thread_ts:
            data = await self._api(ctx, "conversations.replies", params={"channel": p.channel, "ts": p.thread_ts, "limit": p.limit})
            raw = data.get("messages", [])[-p.limit:]
        else:
            data = await self._api(ctx, "conversations.history", params={"channel": p.channel, "limit": p.limit})
            raw = list(reversed(data.get("messages", [])))
        names: dict[str, str | None] = {}
        messages = []
        for m in raw:
            user = m.get("user")
            if p.names and user and user not in names:
                try:
                    info = self._user((await self._api(ctx, "users.info", params={"user": user}))["user"])
                    names[user] = info.real_name or info.name
                except IntegrationError:
                    names[user] = None
            messages.append(ConversationMessage(
                ts=m.get("ts", ""), user=user, name=names.get(user) or (m.get("bot_profile") or {}).get("name") or m.get("username"),
                text=m.get("text") or "", bot=bool(m.get("bot_id")), thread_ts=m.get("thread_ts"),
            ))
        transcript = "\n".join(f"{m.name or m.user or 'unknown'}: {m.text}" for m in messages)
        return Conversation(channel=p.channel, messages=messages, transcript=transcript)

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
        scopes, _ = self.setup_permissions(self.enabled_features(config))
        query = urlencode({"client_id": config.client_id, "scope": ",".join(scopes), "redirect_uri": redirect_uri, "state": state})
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

    setup_features = FEATURES

    def setup_manifest(
        self, webhook_url: str, install_redirect_url: str, connection: str, *, config: Any = None,
        features: list[str] | None = None,
    ) -> dict[str, Any]:
        enabled = self.enabled_features(config, features)
        scopes, events = self.setup_permissions(enabled)
        name = getattr(config, "app_name", None) or "Shoal"
        bot: dict[str, Any] = {"bot_user": {"display_name": name, "always_online": True}}
        if "direct_messages" in enabled:
            bot["app_home"] = {"home_tab_enabled": False, "messages_tab_enabled": True, "messages_tab_read_only_enabled": False}
        if "slash_command" in enabled:
            bot["slash_commands"] = [{"command": getattr(config, "slash_command", None) or "/shoal", "url": webhook_url,
                                      "description": f"Talk to {name} workflows", "usage_hint": "[what to do]",
                                      "should_escape": False}]
        settings: dict[str, Any] = {}
        if events:
            settings["event_subscriptions"] = {"request_url": webhook_url, "bot_events": events}
        settings.update({
            "interactivity": {"is_enabled": True, "request_url": webhook_url},
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        })
        return {
            "display_information": {"name": name, "description": "Workflow approvals, inputs and notifications from Shoal.",
                                    "background_color": "#0b3d5c"},
            "features": bot,
            "oauth_config": {"redirect_urls": [install_redirect_url], "scopes": {"bot": scopes}},
            "settings": settings,
        }

    def setup_create_url(self, manifest: dict[str, Any]) -> str:
        return "https://api.slack.com/apps?" + urlencode({"new_app": "1", "manifest_json": json.dumps(manifest, separators=(",", ":"))})

    def features_used(self, triggers: list[Any], workflows: list[Any]) -> dict[str, list[str]]:
        used: dict[str, list[str]] = {}

        def note(key: str, why: str) -> None:
            used.setdefault(key, [])
            if why not in used[key]:
                used[key].append(why)

        for trigger in triggers:
            spec, why = trigger.spec, f"trigger {trigger.metadata.name}"
            match = spec.match or {}
            if spec.event == "app_mention":
                note("mentions", why)
            elif spec.event == "message":
                if match.get("direct_messages") is True:
                    note("direct_messages", why)
                else:
                    note("channel_messages", why)
                    if match.get("direct_messages") is None and not match.get("channels"):
                        note("direct_messages", why)
                if match.get("has_files"):
                    note("files", why)
            elif spec.event == "reaction_added":
                note("reactions", why)
            elif spec.event == "slash_command":
                note("slash_command", why)
        actions = {"slack.add_reaction": "reactions", "slack.get_conversation": "channel_messages"}
        for workflow in workflows:
            for step in workflow.spec.steps:
                feature = actions.get(getattr(step, "action", None) or "")
                if feature:
                    note(feature, f"workflow {workflow.metadata.name}")
        return used

    def webhook_setup_instructions(self, url: str) -> str:
        return (
            "Create a Slack app from the manifest below (api.slack.com/apps > Create New App > From a manifest); "
            f"it points Event Subscriptions, Interactivity and the slash command at {url}. Put the app's signing "
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
        if kind not in ("app_mention", "message", "reaction_added"):
            return []
        bot_user = next((a.get("user_id") for a in data.get("authorizations") or [] if a.get("is_bot")), None)
        if kind == "message" and (event.get("subtype") in SKIPPED_SUBTYPES or event.get("hidden")):
            return []  # edits, deletions and channel housekeeping
        if bot_user and event.get("user") == bot_user:
            return []  # the app's own messages and reactions never come back as events
        channel = event.get("channel") or (event.get("item") or {}).get("channel")
        out = {**event, "team_id": data.get("team_id"), "event_id": data.get("event_id"), "channel": channel,
               "bot_user_id": bot_user}
        keys = []
        if kind == "reaction_added":
            item = event.get("item") or {}
            if channel and item.get("ts"):
                keys.append(f"slack:message:{channel}:{item['ts']}")
        else:
            text = event.get("text") or ""
            thread_ts = event.get("thread_ts")
            out.update({
                "command_text": MENTION.sub("", text).strip(),
                "mentioned": kind == "app_mention" or bool(bot_user and f"<@{bot_user}" in text),
                "is_dm": event.get("channel_type") == "im",
                "is_reply": bool(thread_ts and thread_ts != event.get("ts")),
                "reply_ts": thread_ts or event.get("ts"),
                "from_bot": bool(event.get("bot_id") or event.get("subtype") == "bot_message"),
                "has_files": bool(event.get("files")),
                "files": [{"name": f.get("name"), "mimetype": f.get("mimetype"), "url": f.get("url_private")}
                          for f in event.get("files") or []],
            })
            if channel and out["is_reply"]:
                keys.append(thread_key(channel, thread_ts))
            if channel:
                keys.append(f"slack:channel:{channel}")
        return [IntegrationEvent(type=kind, id=data.get("event_id"), data=out, wake_keys=keys,
                                 actor={"slack_id": event["user"]} if event.get("user") else {})]

    # ------------------------------------------------------------------ trigger filters

    async def _channel_ids(self, ctx: ConnectionContext, wanted: list[str]) -> set[str]:
        """Channel ids for ids and #names (names resolved with conversations.list, cached for five minutes)."""
        ids = {c for c in wanted if not c.startswith("#")}
        names = [c[1:] for c in wanted if c.startswith("#")]
        if names:
            key = (ctx.tenant_id, ctx.name)
            cached = _channel_names.get(key)
            if cached is None or clock.now().timestamp() - cached[0] > CHANNEL_CACHE_SECONDS:
                found: dict[str, str] = {}
                cursor = None
                for _ in range(20):
                    page = await self.list_channels(ctx, ChannelFilter(), cursor, 200)
                    found.update({c["name"]: c["id"] for c in page.items if c.get("name")})
                    if not page.next_cursor:
                        break
                    cursor = page.next_cursor
                cached = (clock.now().timestamp(), found)
                _channel_names[key] = cached
            ids |= {cached[1][n] for n in names if n in cached[1]}
        return ids

    async def trigger_matches(self, ctx: ConnectionContext, event: IntegrationEvent, options: BaseModel | None) -> str | None:
        f = options if isinstance(options, SlackTriggerFilter) else SlackTriggerFilter()
        d = event.data
        if event.type not in ("message", "app_mention", "reaction_added"):
            return None  # commands, clicks and forms: only the trigger's own filter applies
        if d.get("from_bot") and not f.include_bots:
            return "message from a bot"
        if f.channels and d.get("channel") not in await self._channel_ids(ctx, f.channels):
            return "another channel"
        user = d.get("user")
        if f.users and user not in f.users:
            return "from someone not in `users`"
        if user and user in f.exclude_users:
            return "from someone in `exclude_users`"
        if event.type == "reaction_added":
            if f.reactions and d.get("reaction") not in f.reactions:
                return "another reaction"
            return await self._group_check(ctx, event, f)
        if f.mentioned is not None and bool(d.get("mentioned")) != f.mentioned:
            return "the app is not mentioned" if f.mentioned else "the app is mentioned"
        if f.direct_messages is not None and bool(d.get("is_dm")) != f.direct_messages:
            return "not a direct message" if f.direct_messages else "a direct message"
        if f.threads == "top_level" and d.get("is_reply"):
            return "a thread reply"
        if f.threads == "replies" and not d.get("is_reply"):
            return "not a thread reply"
        if f.has_files is not None and bool(d.get("has_files")) != f.has_files:
            return "files do not match `has_files`"
        text = d.get("text") or ""
        folded = text if f.case_sensitive else text.lower()
        fold = (lambda w: w) if f.case_sensitive else (lambda w: w.lower())
        if f.contains and not any(fold(w) in folded for w in f.contains):
            return "text contains none of `contains`"
        if f.contains_all and not all(fold(w) in folded for w in f.contains_all):
            return "text lacks some of `contains_all`"
        if any(fold(w) in folded for w in f.excludes):
            return "text contains one of `excludes`"
        if f.regex:
            try:
                found = re.search(f.regex, d.get("command_text") or text, 0 if f.case_sensitive else re.IGNORECASE)
            except re.error as exc:
                return f"invalid match: regex: {exc}"
            if not found:
                return "text does not match `regex`"
            d["match"] = {"text": found.group(0), "groups": list(found.groups()), **found.groupdict()}
        return await self._group_check(ctx, event, f)

    async def _group_check(self, ctx: ConnectionContext, event: IntegrationEvent, f: SlackTriggerFilter) -> str | None:
        if not f.groups:
            return None
        interactions = getattr(ctx.engine, "interactions", None)
        if interactions is None:
            return "groups need the Shoal engine"
        initiator, principal = await interactions.person(ctx.tenant_id, event.actor, source=self.key)
        names = {str(initiator.get("identity") or "").lower()}
        if principal is not None and principal.user_id:
            for group_id in await ctx.engine.directory.groups_of(principal.user_id):
                try:
                    names.add((await ctx.engine.directory.get_group(ctx.tenant_id, group_id)).display_name.lower())
                except Exception:  # a group deleted meanwhile
                    continue
        if names & {g.lower() for g in f.groups}:
            return None
        return "from someone outside `groups`"

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
            hook = _hook(action_id)
            if hook is not None:
                if hook.get("m"):
                    return []  # the bound modal opens in webhook_reply; its submission signals the run
                chosen, label = action_value(act)
                message = data.get("message") or {}
                interaction = Interaction(kind="signal", run_id=hook.get("r", ""), signal=hook.get("n"), payload={
                    "action_id": hook.get("k"), "block_id": act.get("block_id"), "value": chosen, "label": label,
                    "user": self._who(user), "channel": (data.get("channel") or {}).get("id"),
                    "message_ts": message.get("ts"),
                })
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
                payload: dict[str, Any] = {"values": values, "user": self._who(user)}
                if meta.get("k"):
                    payload["action_id"] = meta["k"]
                interaction = Interaction(kind="signal", run_id=meta.get("r", ""), signal=meta.get("n"), payload=payload)
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
                "action_id": act.get("action_id"), "block_id": act.get("block_id"), "value": action_value(act)[0],
                "label": action_value(act)[1], "user": self._who(data.get("user") or {}),
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
        hook = _hook(action_id)
        try:
            if action_id == INPUT:
                await self._open_input(ctx, data, value)
            elif action_id in (APPROVE, REJECT) and value.get("c"):
                await self._open_comment(ctx, data, value, approved=action_id == APPROVE)
            elif hook is not None and hook.get("m"):
                await self._open_bound_modal(ctx, data, hook, _unpack(act.get("value")))
            elif outcome is not None and not outcome.get("ok"):
                await self._respond(ctx, data, {"response_type": "ephemeral", "replace_original": False,
                                                "text": f":no_entry: {outcome.get('error', 'not allowed')}"})
            elif outcome is not None and hook is not None and hook.get("a") != "k":
                message = data.get("message") or {}
                label = action_value(act)[1] or action_value(act)[0]
                blocks = _without_controls(message.get("blocks") or [])
                blocks.append(_context(f"*{outcome.get('by') or 'Someone'}* chose *{label}*"))
                await self._respond(ctx, data, {"replace_original": True, "blocks": blocks, "text": message.get("text") or str(label)})
        except Exception as exc:  # Slack only needs the 200; a failed follow-up is logged
            log.warning("slack follow-up for %s failed: %s", action_id, exc)

    async def _open_bound_modal(self, ctx: ConnectionContext, data: dict[str, Any], hook: dict[str, Any], packed: dict[str, Any]) -> None:
        view = dict(packed.get("v") or {})
        view.setdefault("type", "modal")
        view.setdefault("title", _plain(packed.get("t") or "Shoal", 24))
        if any(b.get("type") == "input" for b in view.get("blocks", [])):
            view.setdefault("submit", _plain("Submit", 24))
        view["callback_id"] = INPUT_VIEW
        view["private_metadata"] = json.dumps({"r": hook.get("r"), "n": hook.get("n"), "k": hook.get("k")})
        await self._api(ctx, "views.open", {"trigger_id": data.get("trigger_id"), "view": view})

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
