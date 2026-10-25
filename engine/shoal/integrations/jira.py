"""Jira integration (Cloud REST v3, and Server/Data Center v2 with a PAT).

Webhooks, sent to the connection's URL: Jira admin webhooks (signed with
``X-Hub-Signature`` when a secret is set) and Automation "Send web request"
actions (send the secret in an ``X-Shoal-Token`` header or ``?token=`` query
parameter).

Registered webhooks (``Trigger.spec.webhook``): Shoal creates, updates, refreshes
and deletes the webhook in Jira itself, from the trigger's settings, with its own
callback URL and signing secret:

* **admin** webhooks (``/rest/webhooks/1.0/webhook``; Cloud and Data Center, needs
  a Jira administrator's credential), signed with ``X-Hub-Signature``;
* **dynamic** webhooks (``/rest/api/{v}/webhook``; Cloud only, for OAuth 2.0 and
  Connect apps), which expire after 30 days and are refreshed before then, and are
  signed with a JWT in the ``Authorization`` header.
"""

from __future__ import annotations

import base64
import dataclasses
import fnmatch
import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

import jwt
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from shoal.core import clock
from shoal.integrations.base import (
    ConnectionContext,
    EventDef,
    Integration,
    IntegrationError,
    IntegrationEvent,
    Page,
    WebhookRegistration,
    action,
    raise_for_status,
    resource,
)
from shoal.sdk.helpers import verify_hmac_sha256


class JiraConnection(BaseModel):
    base_url: str = Field(description="e.g. https://acme.atlassian.net")
    email: str | None = Field(None, description="Atlassian account email (Cloud, with api_token).")
    api_token: SecretStr | None = Field(None, description="Atlassian API token (Cloud).")
    personal_access_token: SecretStr | None = Field(None, description="PAT (Server / Data Center).")
    oauth_token: SecretStr | None = Field(
        None, title="OAuth token", description="OAuth 2.0 (3LO) access token (Cloud, with cloud_id); usually each person's, via `oauth:`."
    )
    cloud_id: str | None = Field(None, title="Cloud ID", description="Atlassian cloud id of the site, for OAuth 2.0 calls.")
    oauth_client_secret: SecretStr | None = Field(
        None, title="OAuth client secret", description="The OAuth 2.0 app's client secret: verifies dynamic webhook deliveries."
    )
    connect_app_key: str | None = Field(None, description="Atlassian Connect app key (Cloud, with connect_shared_secret).")
    connect_shared_secret: SecretStr | None = Field(
        None, description="The Connect installation's shared secret: signs API calls, verifies webhook deliveries."
    )
    connect_client_key: str | None = Field(None, description="The Connect installation's client key (checked on deliveries).")
    api_version: str = Field("3", description="'3' for Cloud, '2' for Server/Data Center.")
    webhook_secret: SecretStr | None = Field(
        None, description="Secret for webhooks sent to the connection's URL (registered trigger webhooks have their own)."
    )
    requested_by_trailer: bool = Field(
        True, description="End comments and new issue descriptions with 'Requested by: <person>' (who drove the work)."
    )
    provenance_label: str | None = Field(
        "shoal-automated", description="Label added to issues Shoal creates (empty: none).", pattern=r"^[^\s]*$"
    )

    @model_validator(mode="after")
    def _auth(self) -> JiraConnection:
        if not (
            (self.email and self.api_token)
            or self.personal_access_token
            or self.oauth_token
            or (self.connect_app_key and self.connect_shared_secret)
        ):
            raise ValueError(
                "configure 'email' + 'api_token' (Cloud), 'personal_access_token' (Server/DC), "
                "'oauth_token' + 'cloud_id' (OAuth 2.0 app) or 'connect_app_key' + 'connect_shared_secret' (Connect app)"
            )
        if self.oauth_token and not self.cloud_id:
            raise ValueError("'oauth_token' needs 'cloud_id' (OAuth 2.0 calls go through api.atlassian.com)")
        self.base_url = self.base_url.rstrip("/")
        return self

    @property
    def app_auth(self) -> bool:
        """OAuth 2.0 or Connect: the credentials that can register dynamic webhooks."""
        return bool(self.oauth_token or (self.connect_app_key and self.connect_shared_secret))

    @property
    def api_base(self) -> str:
        if self.oauth_token:
            return f"https://api.atlassian.com/ex/jira/{self.cloud_id}"
        return self.base_url


# ----------------------------------------------------------------------------- registered webhooks

# Trigger event (as parse_webhook names it) -> Jira webhook event
JIRA_WEBHOOK_EVENTS = {
    "issue_created": "jira:issue_created",
    "issue_updated": "jira:issue_updated",
    "issue_deleted": "jira:issue_deleted",
    "comment_created": "comment_created",
    "comment_updated": "comment_updated",
    "comment_deleted": "comment_deleted",
}
DYNAMIC_WEBHOOK_TTL = timedelta(days=30)


class JiraWebhook(BaseModel):
    """`Trigger.spec.webhook` for Jira: what Shoal registers in Jira for the trigger."""

    model_config = ConfigDict(extra="forbid")

    jql: str | None = Field(None, title="JQL", description="Only issues matching this JQL, e.g. project = BUG AND issuetype = Bug.")
    statuses: list[str] = Field(
        default_factory=list, description="Fire only when an issue moves into one of these statuses (names or ids)."
    )
    board: int | None = Field(None, description="Board id, with `column` (Jira Software).")
    column: str | None = Field(None, description="Board column: fire when an issue moves into any status mapped to it.")
    events: list[str] = Field(
        default_factory=list, description="Jira webhook events, e.g. jira:issue_updated (default: from the trigger's event)."
    )
    fields: list[str] = Field(
        default_factory=list, description="Dynamic webhooks: send issue updates only when these field ids change."
    )
    mode: Literal["auto", "admin", "dynamic"] = Field(
        "auto",
        description="'admin' webhooks (Cloud and Data Center, need an administrator) or 'dynamic' ones (Cloud OAuth 2.0 "
        "and Connect apps); 'auto' picks dynamic for app credentials, otherwise admin.",
    )
    require_signature: bool = Field(
        True,
        description="Reject unsigned deliveries. Turn off only for Data Center versions that cannot sign webhooks; "
        "the unguessable callback URL is then the only check.",
    )

    @model_validator(mode="after")
    def _column(self) -> JiraWebhook:
        if (self.board is None) != (self.column is None):
            raise ValueError("'board' and 'column' go together")
        if self.column and self.statuses:
            raise ValueError("set 'column' or 'statuses', not both")
        return self


def _jql_value(value: str) -> str:
    return value if value.isdigit() else '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def connect_jwt(app_key: str, secret: str, method: str, path: str, params: dict[str, Any] | None = None) -> str:
    """A Connect app's JWT for one API request (with its query string hash)."""
    query = "&".join(
        f"{quote(str(k), safe='')}={','.join(sorted(quote(str(v), safe='') for v in (vals if isinstance(vals, list) else [vals])))}"
        for k, vals in sorted((params or {}).items())
        if k != "jwt"
    )
    qsh = hashlib.sha256(f"{method.upper()}&{path or '/'}&{query}".encode()).hexdigest()
    now = int(time.time())
    return jwt.encode({"iss": app_key, "iat": now, "exp": now + 180, "qsh": qsh}, secret, algorithm="HS256")


# ----------------------------------------------------------------------------- ADF


def adf_to_text(node: Any) -> str:
    """Flatten Atlassian Document Format into readable plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji"):
        return node.get("attrs", {}).get("text", "")
    if kind == "inlineCard":
        return node.get("attrs", {}).get("url", "")
    inner = adf_to_text(node.get("content", []))
    if kind == "codeBlock":
        return f"```\n{inner}\n```\n\n"
    if kind == "listItem":
        return f"- {inner.strip()}\n"
    if kind in ("paragraph", "heading", "blockquote"):
        return inner + "\n\n"
    return inner


def text_to_adf(text: str) -> dict[str, Any]:
    """Convert simple markdown-ish text (paragraphs, bullets, ``` code, links) to ADF."""
    content: list[dict[str, Any]] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip() or None
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            node: dict[str, Any] = {"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(block) or " "}]}
            if lang:
                node["attrs"] = {"language": lang}
            content.append(node)
            i += 1
            continue
        if re.match(r"^\s*[-*] ", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*] ", lines[i]):
                items.append({"type": "listItem", "content": [{"type": "paragraph", "content": _inline(lines[i].strip()[2:])}]})
                i += 1
            content.append({"type": "bulletList", "content": items})
            continue
        if line.strip():
            para = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith("```") and not re.match(r"^\s*[-*] ", lines[i]):
                para.append(lines[i])
                i += 1
            inline: list[dict[str, Any]] = []
            for n, part in enumerate(para):
                if n:
                    inline.append({"type": "hardBreak"})
                inline += _inline(part)
            content.append({"type": "paragraph", "content": inline})
            continue
        i += 1
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": []}]}


_LINK = re.compile(r"(https?://[^\s)]+)")


def _inline(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, part in enumerate(_LINK.split(text)):
        if not part:
            continue
        if idx % 2:
            out.append({"type": "text", "text": part, "marks": [{"type": "link", "attrs": {"href": part}}]})
        else:
            out.append({"type": "text", "text": part})
    return out


# ----------------------------------------------------------------------------- models


class IssueRef(BaseModel):
    key: str = Field(pattern=r"^[A-Z][A-Z0-9_]+-\d+$")


class GetIssue(IssueRef):
    fields: list[str] = Field(default_factory=list, description="Extra field ids to include (e.g. customfield_10042).")


class IssueModel(BaseModel):
    key: str
    id: str
    url: str
    summary: str
    description: str
    status: str
    issue_type: str | None = None
    priority: str | None = None
    labels: list[str] = Field(default_factory=list)
    assignee: str | None = None
    reporter: str | None = None
    project: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class AddJiraComment(IssueRef):
    body: str = Field(description="Plain text / light markdown (paragraphs, bullets, ``` code, links).")


class JiraComment(BaseModel):
    id: str
    url: str


class TransitionIssue(IssueRef):
    to: str = Field(description="Target status or transition name, e.g. 'In Review'.")
    comment: str | None = None


class TransitionResult(BaseModel):
    transitioned: bool
    status: str
    reason: str | None = None


class UpdateIssue(IssueRef):
    fields: dict[str, Any] = Field(default_factory=dict)
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)


class CreateJiraIssue(BaseModel):
    project: str
    summary: str
    description: str = ""
    issue_type: str = "Task"
    labels: list[str] = Field(default_factory=list)


class Ok(BaseModel):
    ok: bool = True
    key: str | None = None


class IssueFilter(BaseModel):
    jql: str = Field(description="JQL, e.g. project = BUG AND status = 'Ready for AI'")
    fields: list[str] = Field(default_factory=lambda: ["summary", "status", "labels", "assignee", "issuetype", "priority"])


class JiraIntegration(Integration):
    key = "jira"
    title = "Jira"
    description = "Issues, comments, transitions and webhooks for Jira Cloud and Data Center."
    connection_model = JiraConnection
    events = [
        EventDef("issue_updated", "An issue changed (status moves appear in `event.status_change`).", {"issue_key": "BUG-12", "status_change": {"from": "To Do", "to": "Ready for AI"}}),
        EventDef("issue_created", "An issue was created."),
        EventDef("comment_created", "A comment was added."),
        EventDef("automation", "A Jira Automation 'Send web request' payload."),
    ]
    webhook_model = JiraWebhook

    def _headers(self, cfg: JiraConnection, method: str = "GET", path: str = "/", params: dict[str, Any] | None = None) -> dict[str, str]:
        if cfg.oauth_token:
            auth = f"Bearer {cfg.oauth_token.get_secret_value()}"
        elif cfg.personal_access_token:
            auth = f"Bearer {cfg.personal_access_token.get_secret_value()}"
        elif cfg.email and cfg.api_token:
            raw = f"{cfg.email}:{cfg.api_token.get_secret_value()}"
            auth = "Basic " + base64.b64encode(raw.encode()).decode()
        else:
            assert cfg.connect_app_key and cfg.connect_shared_secret
            auth = "JWT " + connect_jwt(cfg.connect_app_key, cfg.connect_shared_secret.get_secret_value(), method, path, params)
        return {"Authorization": auth, "Accept": "application/json"}

    async def _send(self, ctx: ConnectionContext, method: str, path: str, **kwargs: Any) -> Any:
        cfg: JiraConnection = ctx.config
        path = path.replace("{v}", cfg.api_version)
        headers = self._headers(cfg, method, path, kwargs.get("params"))
        async with ctx.http(base_url=cfg.api_base, headers=headers) as client:
            return await client.request(method, path, **kwargs)

    async def _request(self, ctx: ConnectionContext, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._send(ctx, method, path, **kwargs)
        raise_for_status(response, "Jira")
        return response.json() if response.content else {}

    def _v3(self, ctx: ConnectionContext) -> bool:
        return ctx.config.api_version == "3"

    def _rich(self, ctx: ConnectionContext, text: str, *, attributed: bool = False) -> Any:
        """Text as ADF (Cloud) or wiki text (Server/DC); ``attributed`` adds the Requested-by line."""
        who = self._requested_by(ctx) if attributed else None
        if not self._v3(ctx):
            return f"{text.rstrip()}\n\nRequested by: {who[0]}" if who else text
        doc = text_to_adf(text)
        if who:
            name, account_id = who
            person = (
                {"type": "mention", "attrs": {"id": account_id, "text": f"@{name}"}} if account_id else {"type": "text", "text": name}
            )
            doc["content"].append({"type": "paragraph", "content": [{"type": "text", "text": "Requested by: "}, person]})
        return doc

    def _requested_by(self, ctx: ConnectionContext) -> tuple[str, str | None] | None:
        """(name, Jira account id) of the person the work is for, when the connection adds attribution."""
        initiator = ctx.initiator or {}
        if not ctx.config.requested_by_trailer or not initiator:
            return None
        name = initiator.get("display_name") or initiator.get("email") or initiator.get("actor")
        if not name:
            return None
        return str(name), initiator.get("jira_account_id")

    async def test_connection(self, ctx: ConnectionContext) -> dict[str, Any]:
        me = await self._request(ctx, "GET", "/rest/api/{v}/myself")
        return {"ok": True, "account": me.get("displayName") or me.get("name")}

    def _issue(self, ctx: ConnectionContext, data: dict[str, Any], extra: list[str]) -> IssueModel:
        f = data.get("fields") or {}
        description = f.get("description")
        return IssueModel(
            key=data["key"],
            id=str(data.get("id")),
            url=f"{ctx.config.base_url}/browse/{data['key']}",
            summary=f.get("summary") or "",
            description=adf_to_text(description).strip() if isinstance(description, dict) else (description or ""),
            status=(f.get("status") or {}).get("name", ""),
            issue_type=(f.get("issuetype") or {}).get("name"),
            priority=(f.get("priority") or {}).get("name"),
            labels=f.get("labels") or [],
            assignee=(f.get("assignee") or {}).get("displayName"),
            reporter=(f.get("reporter") or {}).get("displayName"),
            project=(f.get("project") or {}).get("key"),
            fields={k: f.get(k) for k in extra},
        )

    # ------------------------------------------------------------------ actions

    @action(description="Fetch an issue with its description as plain text.", input=GetIssue, output=IssueModel, mutating=False)
    async def get_issue(self, ctx: ConnectionContext, p: GetIssue) -> IssueModel:
        base = ["summary", "description", "status", "issuetype", "priority", "labels", "assignee", "reporter", "project"]
        data = await self._request(ctx, "GET", f"/rest/api/{{v}}/issue/{p.key}", params={"fields": ",".join(base + p.fields)})
        return self._issue(ctx, data, p.fields)

    @action(description="Add a comment to an issue.", input=AddJiraComment, output=JiraComment)
    async def add_comment(self, ctx: ConnectionContext, p: AddJiraComment) -> JiraComment:
        data = await self._request(ctx, "POST", f"/rest/api/{{v}}/issue/{p.key}/comment", json={"body": self._rich(ctx, p.body, attributed=True)})
        return JiraComment(id=str(data.get("id")), url=f"{ctx.config.base_url}/browse/{p.key}?focusedCommentId={data.get('id')}")

    @action(description="Move an issue to a status (by status or transition name).", input=TransitionIssue, output=TransitionResult)
    async def transition_issue(self, ctx: ConnectionContext, p: TransitionIssue) -> TransitionResult:
        issue = await self._request(ctx, "GET", f"/rest/api/{{v}}/issue/{p.key}", params={"fields": "status"})
        current = issue["fields"]["status"]["name"]
        if current.lower() == p.to.lower():
            return TransitionResult(transitioned=False, status=current, reason="already in that status")
        transitions = (await self._request(ctx, "GET", f"/rest/api/{{v}}/issue/{p.key}/transitions")).get("transitions", [])
        wanted = p.to.lower()
        match = next(
            (t for t in transitions if t["name"].lower() == wanted or (t.get("to") or {}).get("name", "").lower() == wanted),
            None,
        )
        if match is None:
            names = ", ".join(sorted({(t.get("to") or {}).get("name", t["name"]) for t in transitions}))
            raise IntegrationError(f"no transition from '{current}' to '{p.to}' (available: {names})")
        body: dict[str, Any] = {"transition": {"id": match["id"]}}
        if p.comment:
            body["update"] = {"comment": [{"add": {"body": self._rich(ctx, p.comment, attributed=True)}}]}
        await self._request(ctx, "POST", f"/rest/api/{{v}}/issue/{p.key}/transitions", json=body)
        return TransitionResult(transitioned=True, status=(match.get("to") or {}).get("name", p.to))

    @action(description="Update fields and labels of an issue.", input=UpdateIssue, output=Ok)
    async def update_issue(self, ctx: ConnectionContext, p: UpdateIssue) -> Ok:
        body: dict[str, Any] = {"fields": p.fields}
        labels = [{"add": label} for label in p.add_labels] + [{"remove": label} for label in p.remove_labels]
        if labels:
            body["update"] = {"labels": labels}
        await self._request(ctx, "PUT", f"/rest/api/{{v}}/issue/{p.key}", json=body)
        return Ok(key=p.key)

    @action(description="Create an issue.", input=CreateJiraIssue, output=Ok)
    async def create_issue(self, ctx: ConnectionContext, p: CreateJiraIssue) -> Ok:
        fields = {
            "project": {"key": p.project},
            "summary": p.summary,
            "issuetype": {"name": p.issue_type},
            "description": self._rich(ctx, p.description, attributed=True),
            "labels": list(dict.fromkeys([*p.labels, *([ctx.config.provenance_label] if ctx.config.provenance_label else [])])),
        }
        data = await self._request(ctx, "POST", "/rest/api/{v}/issue", json={"fields": fields})
        return Ok(key=data.get("key"))

    # ------------------------------------------------------------------ resources

    @resource("issue", description="Issues matching a JQL query.", filter=IssueFilter, key_field="key")
    async def search_issues(self, ctx: ConnectionContext, f: IssueFilter, cursor: str | None, limit: int) -> Page:
        if self._v3(ctx):
            body: dict[str, Any] = {"jql": f.jql, "fields": f.fields, "maxResults": min(limit, 100)}
            if cursor:
                body["nextPageToken"] = cursor
            data = await self._request(ctx, "POST", "/rest/api/3/search/jql", json=body)
            next_cursor = data.get("nextPageToken")
        else:
            start = int(cursor or 0)
            data = await self._request(
                ctx, "POST", "/rest/api/2/search", json={"jql": f.jql, "fields": f.fields, "startAt": start, "maxResults": min(limit, 100)}
            )
            got = start + len(data.get("issues", []))
            next_cursor = str(got) if got < data.get("total", 0) else None
        items = []
        for raw in data.get("issues", []):
            issue = self._issue(ctx, raw, [])
            items.append({"id": issue.key, **issue.model_dump(exclude={"fields"})})
        return Page(items=items, next_cursor=next_cursor)

    # ------------------------------------------------------------------ webhooks

    async def verify_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> bool:
        secret = ctx.config.webhook_secret
        if secret is None:
            return False  # an unsigned URL would let anyone who finds it fire triggers
        value = secret.get_secret_value()
        token = headers.get("x-shoal-token")
        if token:
            return hmac.compare_digest(token, value)
        signature = headers.get("x-hub-signature", "")
        expected = "sha256=" + hmac.new(value.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    async def parse_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> list[IntegrationEvent]:
        payload = json.loads(body or b"{}")
        raw_type = payload.get("webhookEvent")
        event_type = raw_type.split(":", 1)[-1] if raw_type else "automation"
        issue = payload.get("issue") or {}
        fields = issue.get("fields") or {}
        key = issue.get("key") or payload.get("issue_key") or payload.get("key")
        status_change = None
        for item in (payload.get("changelog") or {}).get("items", []):
            if item.get("field") == "status":
                status_change = {
                    "from": item.get("fromString"), "to": item.get("toString"), "from_id": item.get("from"), "to_id": item.get("to"),
                }
        data = {
            **payload,
            "issue_key": key,
            "status": (fields.get("status") or {}).get("name"),
            "summary": fields.get("summary"),
            "project": (fields.get("project") or {}).get("key"),
            "labels": fields.get("labels") or [],
            "status_change": status_change,
            "user": (payload.get("user") or {}).get("displayName"),
        }
        event_id = headers.get("x-atlassian-webhook-identifier") or (
            f"{key}:{payload.get('timestamp')}" if key and payload.get("timestamp") else None
        )
        user = payload.get("user") or {}
        actor = {
            "jira_account_id": user.get("accountId") or user.get("key"),
            "email": user.get("emailAddress"),
            "display_name": user.get("displayName"),
        }
        return [
            IntegrationEvent(
                type=event_type,
                id=event_id,
                data=data,
                wake_keys=[f"jira:issue:{key}"] if key else [],
                actor={k: v for k, v in actor.items() if v},
            )
        ]

    def webhook_setup_instructions(self, url: str) -> str:
        return (
            f"Jira settings > System > WebHooks: create a webhook to {url} with the connection's secret and "
            "'Issue updated' events (optionally a JQL filter). For Jira Automation, add a 'Send web request' action "
            f"to {url} with header X-Shoal-Token set to the webhook secret and the issue data as the body. "
            "Or give a trigger a 'webhook' block (jql, board and column, or statuses) and Shoal registers the webhook itself."
        )

    # ------------------------------------------------------------------ registered webhooks

    def _mode(self, ctx: ConnectionContext, config: JiraWebhook) -> str:
        cfg: JiraConnection = ctx.config
        if config.mode == "dynamic":
            if cfg.api_version != "3" and not cfg.app_auth:
                raise IntegrationError("dynamic webhooks are a Jira Cloud feature for OAuth 2.0 and Connect apps; use mode: admin")
            if not cfg.app_auth:
                raise IntegrationError("dynamic webhooks need an OAuth 2.0 ('oauth_token') or Connect app credential")
            return "dynamic"
        if config.mode == "admin":
            return "admin"
        return "dynamic" if cfg.app_auth else "admin"

    def _events(self, event: str, config: JiraWebhook) -> list[str]:
        if config.events:
            return list(config.events)
        events = [jira for ours, jira in JIRA_WEBHOOK_EVENTS.items() if fnmatch.fnmatch(ours, event)]
        if not events:
            raise IntegrationError(
                f"no Jira webhook event matches the trigger's event '{event}'; set webhook.events "
                f"(one of {', '.join(JIRA_WEBHOOK_EVENTS.values())})"
            )
        return events

    async def _statuses(self, ctx: ConnectionContext, config: JiraWebhook) -> list[str]:
        if config.column is None:
            return list(config.statuses)
        board = await self._request(ctx, "GET", f"/rest/agile/1.0/board/{config.board}/configuration")
        columns = (board.get("columnConfig") or {}).get("columns") or []
        wanted = config.column.lower()
        match = next((c for c in columns if (c.get("name") or "").lower() == wanted), None)
        if match is None:
            names = ", ".join(c.get("name", "?") for c in columns) or "none"
            raise IntegrationError(f"board {config.board} has no column '{config.column}' (columns: {names})")
        statuses = [str(s["id"]) for s in match.get("statuses") or [] if s.get("id") is not None]
        if not statuses:
            raise IntegrationError(f"column '{config.column}' on board {config.board} has no statuses mapped to it")
        return statuses

    @staticmethod
    def _jql(config: JiraWebhook, statuses: list[str]) -> str | None:
        parts = [f"({config.jql})"] if config.jql else []
        if statuses:
            parts.append(f"status in ({', '.join(_jql_value(s) for s in statuses)})")
        return " AND ".join(parts) or None

    def describe_webhook(self, registration: WebhookRegistration) -> dict[str, Any]:
        config: JiraWebhook = registration.config
        statuses = registration.state.get("statuses") or config.statuses
        out: dict[str, Any] = {"events": self._events(registration.event, config), "jql": self._jql(config, statuses)}
        if config.column and "statuses" not in registration.state:
            out["column"] = f"{config.column} (board {config.board}; resolved to statuses when registered)"
        return out

    async def register_webhook(self, ctx: ConnectionContext, registration: WebhookRegistration) -> dict[str, Any]:
        config: JiraWebhook = registration.config
        mode = self._mode(ctx, config)
        events = self._events(registration.event, config)
        statuses = await self._statuses(ctx, config)
        jql = self._jql(config, statuses)
        previous = registration.state
        base = {"mode": mode, "events": events, "jql": jql, "statuses": statuses}
        if mode == "admin":
            body = {
                "name": registration.name,
                "url": registration.url,
                "events": events,
                "filters": {"issue-related-events-section": jql or ""},
                "excludeBody": False,
                "secret": registration.secret,
            }
            webhook_id = previous.get("webhook_id") if previous.get("mode") == "admin" else None
            if webhook_id:
                response = await self._send(ctx, "PUT", f"/rest/webhooks/1.0/webhook/{webhook_id}", json=body)
                if response.status_code != 404:
                    raise_for_status(response, "Jira")
                    return {**base, "webhook_id": webhook_id}
            created = await self._request(ctx, "POST", "/rest/webhooks/1.0/webhook", json=body)
            if previous.get("mode") == "dynamic":
                await self.unregister_webhook(ctx, registration)
            return {**base, "webhook_id": str(created.get("self", "")).rstrip("/").rsplit("/", 1)[-1] or str(created.get("id"))}

        if not jql:
            raise IntegrationError("dynamic webhooks need a JQL filter: set webhook.jql (e.g. project = BUG) or statuses/column")
        hook: dict[str, Any] = {"events": events, "jqlFilter": jql}
        fields = config.fields or (["status"] if statuses else [])
        if fields and "jira:issue_updated" in events:
            hook["fieldIdsFilter"] = fields
        data = await self._request(ctx, "POST", "/rest/api/{v}/webhook", json={"url": registration.url, "webhooks": [hook]})
        results = data.get("webhookRegistrationResult") or []
        errors = [e for r in results for e in r.get("errors") or []]
        ids = [r["createdWebhookId"] for r in results if r.get("createdWebhookId") is not None]
        if errors or not ids:
            raise IntegrationError(f"Jira refused the webhook: {'; '.join(map(str, errors)) or data}")
        await self.unregister_webhook(ctx, registration)  # the old one, now that the new one exists (there is no update)
        expires = clock.now() + DYNAMIC_WEBHOOK_TTL
        return {**base, "webhook_ids": ids, "expires_at": expires.isoformat()}

    async def refresh_webhook(self, ctx: ConnectionContext, registration: WebhookRegistration) -> dict[str, Any]:
        state = registration.state
        if state.get("mode") != "dynamic" or not state.get("webhook_ids"):
            return await self.register_webhook(ctx, registration)
        response = await self._send(ctx, "PUT", "/rest/api/{v}/webhook/refresh", json={"webhookIds": state["webhook_ids"]})
        if response.status_code == 404:
            return await self.register_webhook(ctx, dataclasses.replace(registration, state={}))  # expired and gone
        raise_for_status(response, "Jira")
        data = response.json() if response.content else {}
        expires = data.get("expirationDate")
        if isinstance(expires, int | float):  # epoch millis on some versions of the API
            expires = datetime.fromtimestamp(expires / 1000, UTC).isoformat()
        return {**state, "expires_at": expires or (clock.now() + DYNAMIC_WEBHOOK_TTL).isoformat()}

    async def unregister_webhook(self, ctx: ConnectionContext, registration: WebhookRegistration) -> None:
        state = registration.state
        if state.get("mode") == "admin" and state.get("webhook_id"):
            response = await self._send(ctx, "DELETE", f"/rest/webhooks/1.0/webhook/{state['webhook_id']}")
        elif state.get("mode") == "dynamic" and state.get("webhook_ids"):
            response = await self._send(ctx, "DELETE", "/rest/api/{v}/webhook", json={"webhookIds": state["webhook_ids"]})
        else:
            return
        if response.status_code != 404:
            raise_for_status(response, "Jira")

    async def verify_registered_webhook(
        self, ctx: ConnectionContext, registration: WebhookRegistration, headers: dict[str, str], body: bytes
    ) -> bool:
        cfg: JiraConnection = ctx.config
        state = registration.state
        if state.get("mode") == "dynamic":
            scheme, _, token = headers.get("authorization", "").partition(" ")
            secret = cfg.connect_shared_secret if cfg.connect_app_key else cfg.oauth_client_secret
            if scheme.lower() not in ("jwt", "bearer") or not token or secret is None:
                return False
            try:
                claims = jwt.decode(token, secret.get_secret_value(), algorithms=["HS256"], options={"verify_aud": False})
            except jwt.PyJWTError:
                return False
            if cfg.connect_app_key and cfg.connect_client_key and claims.get("iss") != cfg.connect_client_key:
                return False
            try:
                matched = json.loads(body or b"{}").get("matchedWebhookIds")
            except (ValueError, AttributeError):
                return False
            if matched is not None and not {str(i) for i in matched} & {str(i) for i in state.get("webhook_ids") or []}:
                return False
            return True
        signature = headers.get("x-hub-signature")
        if signature:
            return verify_hmac_sha256(registration.secret, body, signature, prefix="sha256=")
        return not registration.config.require_signature

    def registered_event_matches(self, registration: WebhookRegistration, event: IntegrationEvent) -> bool:
        statuses = registration.state.get("statuses") or registration.config.statuses
        if not statuses:
            return True
        wanted = {str(s).lower() for s in statuses}
        change = event.data.get("status_change")
        if change:
            return str(change.get("to") or "").lower() in wanted or str(change.get("to_id") or "") in wanted
        if event.type == "issue_created":
            status = ((event.data.get("issue") or {}).get("fields") or {}).get("status") or {}
            return str(status.get("name") or "").lower() in wanted or str(status.get("id") or "") in wanted
        return False
