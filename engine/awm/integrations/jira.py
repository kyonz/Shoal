"""Jira integration (Cloud REST v3, and Server/Data Center v2 with a PAT).

Webhooks: Jira admin webhooks (signed with ``X-Hub-Signature`` when a secret is
set) and Automation "Send web request" actions (send the secret in an
``X-AWM-Token`` header or ``?token=`` query parameter).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import Any

from pydantic import BaseModel, Field, SecretStr, model_validator

from awm.integrations.base import (
    ConnectionContext,
    EventDef,
    Integration,
    IntegrationError,
    IntegrationEvent,
    Page,
    action,
    raise_for_status,
    resource,
)


class JiraConnection(BaseModel):
    base_url: str = Field(description="e.g. https://acme.atlassian.net")
    email: str | None = Field(None, description="Atlassian account email (Cloud, with api_token).")
    api_token: SecretStr | None = Field(None, description="Atlassian API token (Cloud).")
    personal_access_token: SecretStr | None = Field(None, description="PAT (Server / Data Center).")
    api_version: str = Field("3", description="'3' for Cloud, '2' for Server/Data Center.")
    webhook_secret: SecretStr | None = None

    @model_validator(mode="after")
    def _auth(self) -> JiraConnection:
        if not ((self.email and self.api_token) or self.personal_access_token):
            raise ValueError("configure 'email' + 'api_token' (Cloud) or 'personal_access_token' (Server/DC)")
        self.base_url = self.base_url.rstrip("/")
        return self


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

    def _headers(self, cfg: JiraConnection) -> dict[str, str]:
        if cfg.personal_access_token:
            auth = f"Bearer {cfg.personal_access_token.get_secret_value()}"
        else:
            raw = f"{cfg.email}:{cfg.api_token.get_secret_value() if cfg.api_token else ''}"
            auth = "Basic " + base64.b64encode(raw.encode()).decode()
        return {"Authorization": auth, "Accept": "application/json"}

    async def _request(self, ctx: ConnectionContext, method: str, path: str, **kwargs: Any) -> Any:
        cfg: JiraConnection = ctx.config
        path = path.replace("{v}", cfg.api_version)
        async with ctx.http(base_url=cfg.base_url, headers=self._headers(cfg)) as client:
            response = await client.request(method, path, **kwargs)
        raise_for_status(response, "Jira")
        return response.json() if response.content else {}

    def _v3(self, ctx: ConnectionContext) -> bool:
        return ctx.config.api_version == "3"

    def _rich(self, ctx: ConnectionContext, text: str) -> Any:
        return text_to_adf(text) if self._v3(ctx) else text

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
        data = await self._request(ctx, "POST", f"/rest/api/{{v}}/issue/{p.key}/comment", json={"body": self._rich(ctx, p.body)})
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
            body["update"] = {"comment": [{"add": {"body": self._rich(ctx, p.comment)}}]}
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
            "description": self._rich(ctx, p.description),
            "labels": p.labels,
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
            return True
        value = secret.get_secret_value()
        token = headers.get("x-awm-token")
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
                status_change = {"from": item.get("fromString"), "to": item.get("toString")}
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
            f"to {url} with header X-AWM-Token set to the webhook secret and the issue data as the body."
        )
