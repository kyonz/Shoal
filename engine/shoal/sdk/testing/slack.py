"""A fake Slack for tests: the Web API calls Shoal makes, plus signed deliveries from Slack.

::

    slack = MockSlack(signing_secret="s3cret")
    slack.user("U1", "dana", email="dana@acme.com")
    async with engine_harness(http=slack) as world:
        ...                                             # a run posts an approval
        message = slack.last_message("C_DEPLOYS")
        headers, body = slack.click(message, "shoal_approve", user="U1")
        await world.webhook("slack", headers, body)

``MockSlack`` is a :class:`MockHTTP`, so extra routes can be mocked on it and every
call is recorded (``slack.requests("POST", "/api/chat.postMessage")``).
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx

from shoal.core import clock
from shoal.sdk.testing.http import MockHTTP


def slack_signature(signing_secret: str, body: bytes, timestamp: int | str) -> str:
    base = b"v0:" + str(timestamp).encode() + b":" + body
    return "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()


def slack_request(
    signing_secret: str, payload: dict[str, Any] | bytes, *, form: bool = False, timestamp: int | None = None
) -> tuple[dict[str, str], bytes]:
    """``(headers, body)`` signed like Slack: JSON (Events API), or ``form=True`` for slash commands
    (``payload`` is the form) and interactivity (wrap as ``{"payload": json}``)."""
    if isinstance(payload, bytes):
        body = payload
        content_type = "application/x-www-form-urlencoded" if form else "application/json"
    elif form:
        body = urlencode({k: v if isinstance(v, str) else json.dumps(v) for k, v in payload.items()}).encode()
        content_type = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(payload, separators=(",", ":")).encode()
        content_type = "application/json"
    ts = int(timestamp if timestamp is not None else clock.now().timestamp())
    return {
        "Content-Type": content_type,
        "X-Slack-Request-Timestamp": str(ts),
        "X-Slack-Signature": slack_signature(signing_secret, body, ts),
    }, body


@dataclass
class SlackMessage:
    channel: str
    ts: str
    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    thread_ts: str | None = None
    updates: list[dict[str, Any]] = field(default_factory=list)

    def buttons(self) -> list[dict[str, Any]]:
        return [e for b in self.blocks if b.get("type") == "actions" for e in b.get("elements", []) if e.get("type") == "button"]

    def button(self, action_id: str) -> dict[str, Any]:
        for button in self.buttons():
            if button.get("action_id") == action_id or button.get("text", {}).get("text") == action_id:
                return button
        raise AssertionError(f"no button {action_id!r} in message {self.ts} (buttons: {[b.get('action_id') for b in self.buttons()]})")

    def all_text(self) -> str:
        """Text and every mrkdwn / plain text in the blocks, for assertions."""
        parts = [self.text]

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if isinstance(node.get("text"), str):
                    parts.append(node["text"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.blocks)
        return "\n".join(parts)


class MockSlack(MockHTTP):
    """Stateful fake of the Slack Web API (chat, reactions, views, users, conversations, OAuth)."""

    def __init__(self, *, signing_secret: str = "slack-signing-secret", bot_token: str = "xoxb-test", team: str = "T0SHOAL"):
        super().__init__()
        self.signing_secret = signing_secret
        self.bot_token = bot_token
        self.team = team
        self.messages: list[SlackMessage] = []
        self.ephemeral: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []
        self.views: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.users: dict[str, dict[str, Any]] = {}
        self.channels: dict[str, str] = {}  # name -> id
        self.install_codes: dict[str, str] = {"install-code": bot_token}
        self._ts = itertools.count(1)
        api = "slack.com"
        for method, handler in {
            "auth.test": self._auth_test, "chat.postMessage": self._post, "chat.update": self._update,
            "chat.postEphemeral": self._ephemeral, "reactions.add": self._react, "views.open": self._view,
        }.items():
            self.post(f"/api/{method}", handler=handler, host=api)
        for method, handler in {
            "users.info": self._user_info, "users.lookupByEmail": self._lookup, "conversations.list": self._channels,
        }.items():
            self.get(f"/api/{method}", handler=handler, host=api)
        self.post("/api/oauth.v2.access", handler=self._oauth, host=api)
        self.post("/actions/{path:path}", handler=self._response_url, host="hooks.slack.com")

    # ------------------------------------------------------------------ setup

    def user(self, user_id: str, name: str, *, email: str | None = None, real_name: str | None = None) -> None:
        self.users[user_id] = {"id": user_id, "name": name, "real_name": real_name or name,
                               "profile": {"email": email, "real_name": real_name or name}}

    def channel(self, name: str, channel_id: str) -> None:
        self.channels[name.lstrip("#")] = channel_id

    # ------------------------------------------------------------------ inspection

    def channel_messages(self, channel: str) -> list[SlackMessage]:
        channel = self._channel_id(channel)
        return [m for m in self.messages if m.channel == channel]

    def last_message(self, channel: str | None = None) -> SlackMessage:
        messages = self.channel_messages(channel) if channel else self.messages
        assert messages, f"no Slack messages{f' in {channel}' if channel else ''}"
        return messages[-1]

    def message(self, channel: str, ts: str) -> SlackMessage:
        channel = self._channel_id(channel)
        for m in self.messages:
            if m.channel == channel and m.ts == ts:
                return m
        raise AssertionError(f"no message {ts} in {channel}")

    # ------------------------------------------------------------------ deliveries from Slack

    def _user_ref(self, user: str) -> dict[str, Any]:
        known = self.users.get(user, {})
        return {"id": user, "username": known.get("name", user), "name": known.get("name", user), "team_id": self.team}

    def click(
        self, message: SlackMessage, action: str, *, user: str, response_url: str = "https://hooks.slack.com/actions/T/1/x"
    ) -> tuple[dict[str, str], bytes]:
        """A ``block_actions`` delivery for clicking a button of a posted message."""
        button = message.button(action)
        payload = {
            "type": "block_actions", "user": self._user_ref(user), "team": {"id": self.team},
            "trigger_id": f"trig-{next(self._ts)}", "response_url": response_url,
            "channel": {"id": message.channel}, "container": {"type": "message", "message_ts": message.ts},
            "message": {"ts": message.ts, "text": message.text, "blocks": message.blocks},
            "actions": [{"action_id": button["action_id"], "block_id": "b", "type": "button",
                         "value": button.get("value"), "action_ts": "1"}],
        }
        return slack_request(self.signing_secret, {"payload": payload}, form=True)

    def submit(self, view: dict[str, Any], values: dict[str, Any], *, user: str) -> tuple[dict[str, str], bytes]:
        """A ``view_submission`` delivery for a modal Shoal opened (``slack.views[-1]``), with answers by action id."""
        state: dict[str, dict[str, Any]] = {}
        for block in view.get("blocks", []):
            if block.get("type") != "input":
                continue
            element = block["element"]
            action_id, value = element["action_id"], values.get(element["action_id"])
            if element["type"] == "static_select":
                entry = {"type": "static_select", "selected_option": {"value": value} if value is not None else None}
            else:
                entry = {"type": element["type"], "value": None if value is None else str(value)}
            state[block["block_id"]] = {action_id: entry}
        payload = {
            "type": "view_submission", "user": self._user_ref(user), "team": {"id": self.team},
            "view": {**view, "id": f"V{next(self._ts)}", "state": {"values": state}},
        }
        return slack_request(self.signing_secret, {"payload": payload}, form=True)

    def event(self, event: dict[str, Any], *, event_id: str | None = None) -> tuple[dict[str, str], bytes]:
        """An Events API ``event_callback`` delivery."""
        return slack_request(self.signing_secret, {
            "type": "event_callback", "team_id": self.team, "event_id": event_id or f"Ev{next(self._ts)}", "event": event,
        })

    def command(self, text: str, *, user: str, command: str = "/shoal", channel: str = "C0GENERAL") -> tuple[dict[str, str], bytes]:
        """A slash command delivery."""
        return slack_request(self.signing_secret, {
            "command": command, "text": text, "user_id": user, "user_name": self._user_ref(user)["name"],
            "channel_id": channel, "team_id": self.team, "trigger_id": f"trig-{next(self._ts)}",
            "response_url": "https://hooks.slack.com/commands/T/1/x",
        }, form=True)

    def url_verification(self, challenge: str = "challenge-token") -> tuple[dict[str, str], bytes]:
        return slack_request(self.signing_secret, {"type": "url_verification", "challenge": challenge, "token": "x"})

    # ------------------------------------------------------------------ the fake API

    def _authorized(self, request: httpx.Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {self.bot_token}"

    def _channel_id(self, channel: str) -> str:
        if channel.startswith("#"):
            return self.channels.setdefault(channel[1:], f"C{channel[1:].upper().replace('-', '_')}")
        if channel.startswith("U"):
            return "D" + channel[1:]  # a DM with that user
        return channel

    def _new_ts(self) -> str:
        return f"1700000000.{next(self._ts):06d}"

    @staticmethod
    def _body(request: httpx.Request) -> dict[str, Any]:
        return json.loads(request.content or b"{}")

    def _auth_test(self, request: httpx.Request) -> Any:
        if not self._authorized(request):
            return {"ok": False, "error": "invalid_auth"}
        return {"ok": True, "team": "Acme", "team_id": self.team, "user": "shoal", "user_id": "U0BOT", "url": "https://acme.slack.com/"}

    def _post(self, request: httpx.Request) -> Any:
        if not self._authorized(request):
            return {"ok": False, "error": "invalid_auth"}
        body = self._body(request)
        if not body.get("channel"):
            return {"ok": False, "error": "channel_not_found"}
        message = SlackMessage(channel=self._channel_id(body["channel"]), ts=self._new_ts(), text=body.get("text", ""),
                               blocks=body.get("blocks") or [], thread_ts=body.get("thread_ts"))
        self.messages.append(message)
        return {"ok": True, "channel": message.channel, "ts": message.ts, "message": {"text": message.text}}

    def _update(self, request: httpx.Request) -> Any:
        if not self._authorized(request):
            return {"ok": False, "error": "invalid_auth"}
        body = self._body(request)
        try:
            message = self.message(body["channel"], body["ts"])
        except AssertionError:
            return {"ok": False, "error": "message_not_found"}
        message.updates.append({"text": message.text, "blocks": message.blocks})
        message.text, message.blocks = body.get("text", message.text), body.get("blocks") or message.blocks
        return {"ok": True, "channel": message.channel, "ts": message.ts}

    def _ephemeral(self, request: httpx.Request) -> Any:
        self.ephemeral.append(self._body(request))
        return {"ok": True, "message_ts": self._new_ts()}

    def _react(self, request: httpx.Request) -> Any:
        self.reactions.append(self._body(request))
        return {"ok": True}

    def _view(self, request: httpx.Request) -> Any:
        body = self._body(request)
        if not body.get("trigger_id"):
            return {"ok": False, "error": "invalid_trigger_id"}
        self.views.append(body["view"])
        return {"ok": True, "view": {"id": f"V{next(self._ts)}", **body["view"]}}

    def _user_info(self, request: httpx.Request) -> Any:
        user = self.users.get(request.url.params.get("user", ""))
        return {"ok": True, "user": user} if user else {"ok": False, "error": "user_not_found"}

    def _lookup(self, request: httpx.Request) -> Any:
        email = (request.url.params.get("email") or "").lower()
        for user in self.users.values():
            if (user["profile"].get("email") or "").lower() == email:
                return {"ok": True, "user": user}
        return {"ok": False, "error": "users_not_found"}

    def _channels(self, request: httpx.Request) -> Any:
        channels = [{"id": cid, "name": name, "is_private": False, "is_member": True} for name, cid in self.channels.items()]
        return {"ok": True, "channels": channels, "response_metadata": {"next_cursor": ""}}

    def _oauth(self, request: httpx.Request) -> Any:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        token = self.install_codes.get(form.get("code", ""))
        if token is None:
            return {"ok": False, "error": "invalid_code"}
        return {"ok": True, "access_token": token, "token_type": "bot", "team": {"id": self.team, "name": "Acme"}}

    def _response_url(self, request: httpx.Request, path: str) -> Any:
        self.responses.append(self._body(request))
        return {"ok": True}
