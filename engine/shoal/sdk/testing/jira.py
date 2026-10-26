"""A stateful mock of Jira (Cloud and Data Center) for tests: issues, workflows, boards and webhooks.

::

    jira = MockJira()
    jira.add_issue("BUG-7", "Crash on login", status="To Do")
    async with engine_harness(http=jira) as world:
        ...                                   # a trigger with `webhook:` registers a webhook in the mock
        jira.move("BUG-7", "Ready for AI")    # like dragging the card on the board
        await world.flush(jira)               # Jira delivers to every webhook that matches

Webhooks registered through the REST API (admin ``/rest/webhooks/1.0/webhook`` and dynamic
``/rest/api/{v}/webhook``) are matched against each change by event, JQL and (dynamic)
``fieldIdsFilter``, and delivered signed the way Jira signs them: ``X-Hub-Signature`` for admin
webhooks with a secret, a JWT in ``Authorization`` for dynamic ones. Changes made through the API
(transitions, comments) produce deliveries too, so loops show up in tests.

The JQL evaluator understands ``AND``-joined clauses on project, status (name or id), issuetype,
priority, labels, assignee and key with ``=``, ``!=``, ``in`` and ``not in``; anything else raises,
so a test never passes on JQL the mock silently ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
import jwt

from shoal.core import clock

DEFAULT_STATUSES = {"To Do": "10000", "Ready for AI": "10001", "In Progress": "3", "In Review": "10002", "Done": "10003"}


@dataclass
class Delivery:
    """A webhook request Jira would send."""

    url: str
    headers: dict[str, str]
    body: bytes
    webhook_id: str

    @property
    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class MockWebhook:
    id: str
    kind: str  # admin | dynamic
    url: str
    events: list[str]
    jql: str | None
    fields: list[str] = field(default_factory=list)
    secret: str | None = None
    name: str | None = None
    expires_at: datetime | None = None
    enabled: bool = True


class JQLError(AssertionError):
    """JQL the mock does not understand."""


def _split(text: str, sep: str) -> list[str]:
    """Split on a keyword outside quotes and parentheses."""
    parts, depth, quote, start, i = [], 0, "", 0, 0
    pattern = re.compile(rf"\s+{sep}\s+", re.IGNORECASE)
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and (m := pattern.match(text, i)):
            parts.append(text[start:i])
            start = i = m.end()
            continue
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts]


def _values(text: str) -> list[str]:
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return [v.strip().strip("\"'").replace('\\"', '"').lower() for v in re.findall(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'|[^,\s]+', text)]


def _wrapped(text: str) -> bool:
    """``(...)`` where the first parenthesis closes at the very end."""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    for i, ch in enumerate(text):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0:
            return i == len(text) - 1
    return False


def jql_matches(jql: str | None, issue: dict[str, Any], statuses: dict[str, str]) -> bool:
    if not jql or not jql.strip():
        return True
    if len(_split(jql, "OR")) > 1:
        raise JQLError(f"MockJira does not evaluate OR: {jql}")
    for clause in _split(jql.strip(), "AND"):
        if _wrapped(clause):
            if not jql_matches(clause[1:-1], issue, statuses):
                return False
            continue
        m = re.fullmatch(r"(\w+)\s*(=|!=|not\s+in|in)\s*(.+)", clause, re.IGNORECASE)
        if not m:
            raise JQLError(f"MockJira cannot evaluate '{clause}'")
        name, op, raw = m.group(1).lower(), re.sub(r"\s+", " ", m.group(2).lower()), m.group(3)
        wanted = set(_values(raw))
        f = issue["fields"]
        if name == "project":
            have = {f["project"]["key"].lower()}
        elif name == "status":
            have = {f["status"]["name"].lower(), str(f["status"]["id"])}
        elif name == "issuetype":
            have = {f["issuetype"]["name"].lower()}
        elif name == "priority":
            have = {(f.get("priority") or {}).get("name", "").lower()}
        elif name == "labels":
            have = {label.lower() for label in f.get("labels") or []}
        elif name == "assignee":
            have = {((f.get("assignee") or {}).get("accountId") or "").lower()}
        elif name in ("key", "issuekey"):
            have = {issue["key"].lower()}
        else:
            raise JQLError(f"MockJira cannot evaluate field '{name}' in '{clause}'")
        hit = bool(have & wanted)
        if (op in ("=", "in") and not hit) or (op in ("!=", "not in") and hit):
            return False
    return True


class MockJira:
    """An in-memory Jira site served through ``httpx.MockTransport`` (pass it as ``http=`` to a harness)."""

    def __init__(
        self,
        *,
        host: str = "acme.atlassian.net",
        cloud_id: str = "cloud-1",
        statuses: dict[str, str] | None = None,
        app_secret: str | None = None,
        connect_client_key: str | None = None,
        admin: bool = True,
        dynamic: bool = True,
        dynamic_limit: int = 5,
    ):
        self.host = host
        self.cloud_id = cloud_id
        self.statuses = dict(statuses or DEFAULT_STATUSES)
        self.app_secret = app_secret  # OAuth 2.0 client secret or Connect shared secret: signs dynamic deliveries
        self.connect_client_key = connect_client_key  # set for a Connect app (JWT iss)
        self.admin_enabled = admin  # False: the caller is not a Jira administrator
        self.dynamic_enabled = dynamic  # False: a Data Center site
        self.dynamic_limit = dynamic_limit
        self.issues: dict[str, dict[str, Any]] = {}
        self.comments: list[tuple[str, Any]] = []
        self.webhooks: dict[str, MockWebhook] = {}
        self.boards: dict[int, list[dict[str, Any]]] = {}
        self.outbox: list[Delivery] = []
        self.calls: list[tuple[str, str]] = []
        self.workflow: dict[str, list[str]] | None = None  # status -> allowed targets; None allows any move
        self._ids = itertools.count(1000)
        self._delivery = itertools.count(1)

    # ------------------------------------------------------------------ set-up

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    def add_issue(
        self, key: str, summary: str, description: str = "", *, status: str = "To Do", issue_type: str = "Bug",
        labels: list[str] | None = None, fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        issue = {
            "id": str(10000 + len(self.issues)),
            "key": key,
            "self": f"{self.base_url}/rest/api/3/issue/{key}",
            "fields": {
                "summary": summary,
                "description": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": description or summary}]}]},
                "status": self._status(status),
                "issuetype": {"name": issue_type},
                "priority": {"name": "High"},
                "labels": list(labels or []),
                "project": {"key": key.split("-")[0]},
                "assignee": None,
                "reporter": {"displayName": "Reporter"},
                **(fields or {}),
            },
        }
        self.issues[key] = issue
        return issue

    def add_board(self, board_id: int, columns: dict[str, list[str]]) -> None:
        """A board whose columns map to statuses (by name)."""
        self.boards[board_id] = [
            {"name": name, "statuses": [{"id": self._status(s)["id"], "self": f"{self.base_url}/rest/api/2/status/{self._status(s)['id']}"} for s in names]}
            for name, names in columns.items()
        ]

    def _status(self, name: str) -> dict[str, Any]:
        if name not in self.statuses:
            self.statuses[name] = str(20000 + len(self.statuses))
        return {"name": name, "id": self.statuses[name]}

    # ------------------------------------------------------------------ changes (each queues webhook deliveries)

    def move(self, key: str, status: str, *, user: str = "Dana", account_id: str = "acc-dana") -> list[Delivery]:
        """Move an issue to a status (a board column drag); returns the deliveries it queued."""
        issue = self.issues[key]
        before = issue["fields"]["status"]
        after = self._status(status)
        issue["fields"]["status"] = after
        item = {"field": "status", "fieldId": "status", "fieldtype": "jira", "from": before["id"], "fromString": before["name"],
                "to": after["id"], "toString": after["name"]}
        return self._emit("jira:issue_updated", issue, user, account_id, changelog=[item])

    def update(self, key: str, *, user: str = "Dana", account_id: str = "acc-dana", **fields: Any) -> list[Delivery]:
        issue = self.issues[key]
        items = []
        for name, value in fields.items():
            items.append({"field": name, "fieldId": name, "fieldtype": "jira", "fromString": str(issue["fields"].get(name)), "toString": str(value)})
            issue["fields"][name] = value
        return self._emit("jira:issue_updated", issue, user, account_id, changelog=items)

    def create(self, key: str, summary: str, *, status: str = "To Do", user: str = "Dana", account_id: str = "acc-dana", **kwargs: Any) -> list[Delivery]:
        issue = self.add_issue(key, summary, status=status, **kwargs)
        return self._emit("jira:issue_created", issue, user, account_id)

    def comment(self, key: str, body: str, *, user: str = "Dana", account_id: str = "acc-dana") -> list[Delivery]:
        self.comments.append((key, body))
        return self._emit("comment_created", self.issues[key], user, account_id, comment={"id": str(len(self.comments)), "body": body})

    def _emit(
        self, event: str, issue: dict[str, Any], user: str, account_id: str, *,
        changelog: list[dict[str, Any]] | None = None, comment: dict[str, Any] | None = None,
    ) -> list[Delivery]:
        out = []
        changed = {i.get("fieldId") or i["field"] for i in changelog or []}
        for hook in list(self.webhooks.values()):
            if not hook.enabled or event not in hook.events:
                continue
            if hook.expires_at is not None and hook.expires_at < clock.now():
                continue  # dynamic webhooks stop when they expire
            if hook.kind == "dynamic" and hook.fields and event == "jira:issue_updated" and not changed & set(hook.fields):
                continue
            if not jql_matches(hook.jql, issue, self.statuses):
                continue
            payload: dict[str, Any] = {
                "timestamp": int(time.time() * 1000) + next(self._delivery),
                "webhookEvent": event,
                "user": {"accountId": account_id, "displayName": user},
                "issue": json.loads(json.dumps(issue)),
            }
            if changelog is not None:
                payload["changelog"] = {"id": str(next(self._ids)), "items": changelog}
            if comment is not None:
                payload["comment"] = comment
            if hook.kind == "dynamic":
                payload["matchedWebhookIds"] = [int(hook.id)]
            body = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json", "X-Atlassian-Webhook-Identifier": f"delivery-{next(self._delivery)}"}
            if hook.kind == "admin" and hook.secret:
                headers["X-Hub-Signature"] = "sha256=" + hmac.new(hook.secret.encode(), body, hashlib.sha256).hexdigest()
            if hook.kind == "dynamic" and self.app_secret:
                now = int(time.time())
                claims: dict[str, Any] = {"iat": now, "exp": now + 300}
                if self.connect_client_key:
                    claims["iss"] = self.connect_client_key
                token = jwt.encode(claims, self.app_secret, algorithm="HS256")
                headers["Authorization"] = f"{'JWT' if self.connect_client_key else 'Bearer'} {token}"
            out.append(Delivery(hook.url, headers, body, hook.id))
        self.outbox.extend(out)
        return out

    def take(self) -> list[Delivery]:
        """Queued deliveries, oldest first (and clear the queue)."""
        out, self.outbox = self.outbox, []
        return out

    def expire(self, webhook_id: str) -> None:
        self.webhooks[webhook_id].expires_at = clock.now() - timedelta(seconds=1)

    # ------------------------------------------------------------------ HTTP

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "api.atlassian.com":
            prefix = f"/ex/jira/{self.cloud_id}"
            if not path.startswith(prefix):
                return _error(404, f"unknown cloud id in {path}")
            path = path[len(prefix):] or "/"
        elif request.url.host != self.host:
            return _error(502, f"MockJira: unexpected host {request.url.host}")
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content else {}
        try:
            return self._route(request.method, path, body, request)
        except KeyError as exc:
            return _error(404, f"not found: {exc}")

    def _route(self, method: str, path: str, body: Any, request: httpx.Request) -> httpx.Response:
        if m := re.fullmatch(r"/rest/api/(\d)/myself", path):
            return httpx.Response(200, json={"accountId": "acc-shoal", "displayName": "Shoal", "name": "shoal"})
        if m := re.fullmatch(r"/rest/api/(\d)/issue/([A-Z][A-Z0-9_]+-\d+)", path):
            issue = self.issues[m.group(2)]
            if method == "GET":
                return httpx.Response(200, json=issue)
            if method == "PUT":
                issue["fields"].update(body.get("fields") or {})
                for op in (body.get("update") or {}).get("labels", []):
                    labels = issue["fields"]["labels"]
                    if "add" in op and op["add"] not in labels:
                        labels.append(op["add"])
                    if "remove" in op and op["remove"] in labels:
                        labels.remove(op["remove"])
                return httpx.Response(204)
        if m := re.fullmatch(r"/rest/api/(\d)/issue/([A-Z][A-Z0-9_]+-\d+)/transitions", path):
            key = m.group(2)
            current = self.issues[key]["fields"]["status"]["name"]
            targets = self.workflow.get(current, []) if self.workflow is not None else [s for s in self.statuses if s != current]
            if method == "GET":
                return httpx.Response(200, json={"transitions": [
                    {"id": str(i + 11), "name": f"Move to {t}", "to": self._status(t)} for i, t in enumerate(targets)
                ]})
            index = int(body["transition"]["id"]) - 11
            if not 0 <= index < len(targets):
                return _error(400, "invalid transition")
            self.move(key, targets[index], user="Shoal", account_id="acc-shoal")
            for change in (body.get("update") or {}).get("comment", []):
                self.comment(key, change["add"]["body"], user="Shoal", account_id="acc-shoal")
            return httpx.Response(204)
        if m := re.fullmatch(r"/rest/api/(\d)/issue/([A-Z][A-Z0-9_]+-\d+)/comment", path):
            self.comment(m.group(2), body["body"], user="Shoal", account_id="acc-shoal")
            return httpx.Response(201, json={"id": str(len(self.comments))})
        if m := re.fullmatch(r"/rest/agile/1.0/board/(\d+)/configuration", path):
            board = int(m.group(1))
            if board not in self.boards:
                return _error(404, f"board {board} does not exist")
            return httpx.Response(200, json={"id": board, "columnConfig": {"columns": self.boards[board]}})
        if path.startswith("/rest/webhooks/1.0/webhook"):
            return self._admin(method, path, body)
        if re.fullmatch(r"/rest/api/(\d)/webhook(/refresh)?", path):
            return self._dynamic(method, path, body, request)
        return _error(404, f"MockJira: no route {method} {path}")

    def _admin(self, method: str, path: str, body: Any) -> httpx.Response:
        if not self.admin_enabled:
            return _error(403, "you must be a Jira administrator to manage webhooks")
        webhook_id = path.rstrip("/").rsplit("/", 1)[-1] if path.rstrip("/") != "/rest/webhooks/1.0/webhook" else None
        if method == "GET" and webhook_id is None:
            return httpx.Response(200, json=[self._admin_view(h) for h in self.webhooks.values() if h.kind == "admin"])
        if method == "POST" and webhook_id is None:
            hook = MockWebhook(
                id=str(next(self._ids)), kind="admin", url=body["url"], events=list(body.get("events") or []),
                jql=(body.get("filters") or {}).get("issue-related-events-section") or None, secret=body.get("secret"), name=body.get("name"),
            )
            self.webhooks[hook.id] = hook
            return httpx.Response(201, json=self._admin_view(hook))
        hook = self.webhooks.get(webhook_id or "")
        if hook is None or hook.kind != "admin":
            return _error(404, "webhook not found")
        if method == "GET":
            return httpx.Response(200, json=self._admin_view(hook))
        if method == "PUT":
            hook.url, hook.events, hook.name = body["url"], list(body.get("events") or []), body.get("name")
            hook.jql = (body.get("filters") or {}).get("issue-related-events-section") or None
            hook.secret = body.get("secret", hook.secret)
            return httpx.Response(200, json=self._admin_view(hook))
        if method == "DELETE":
            del self.webhooks[hook.id]
            return httpx.Response(204)
        return _error(405, method)

    def _admin_view(self, hook: MockWebhook) -> dict[str, Any]:
        return {"self": f"{self.base_url}/rest/webhooks/1.0/webhook/{hook.id}", "name": hook.name, "url": hook.url,
                "events": hook.events, "filters": {"issue-related-events-section": hook.jql or ""}, "enabled": hook.enabled}

    def _dynamic(self, method: str, path: str, body: Any, request: httpx.Request) -> httpx.Response:
        if not self.dynamic_enabled:
            return _error(404, "dynamic webhooks are not available on this site")
        auth = request.headers.get("authorization", "")
        if not (auth.startswith("Bearer ") and request.url.host == "api.atlassian.com") and not auth.startswith("JWT "):
            return _error(403, "only OAuth 2.0 and Connect apps can register dynamic webhooks")
        mine = [h for h in self.webhooks.values() if h.kind == "dynamic"]
        if path.endswith("/refresh"):
            expires = clock.now() + timedelta(days=30)
            ids = [str(i) for i in body.get("webhookIds") or []]
            if not all(i in self.webhooks for i in ids):
                return _error(404, "webhook not found")
            for i in ids:
                self.webhooks[i].expires_at = expires
            return httpx.Response(200, json={"expirationDate": expires.isoformat().replace("+00:00", "Z")})
        if method == "GET":
            return httpx.Response(200, json={"values": [
                {"id": int(h.id), "jqlFilter": h.jql, "events": h.events, "fieldIdsFilter": h.fields,
                 "expirationDate": h.expires_at.isoformat() if h.expires_at else None} for h in mine
            ], "isLast": True})
        if method == "POST":
            results = []
            for spec in body.get("webhooks") or []:
                if not spec.get("jqlFilter"):
                    results.append({"errors": ["jqlFilter is required"]})
                    continue
                if len([h for h in self.webhooks.values() if h.kind == "dynamic"]) >= self.dynamic_limit:
                    results.append({"errors": [f"the app can register at most {self.dynamic_limit} webhooks"]})
                    continue
                hook = MockWebhook(
                    id=str(next(self._ids)), kind="dynamic", url=body["url"], events=list(spec.get("events") or []),
                    jql=spec["jqlFilter"], fields=list(spec.get("fieldIdsFilter") or []),
                    expires_at=clock.now() + timedelta(days=30),
                )
                self.webhooks[hook.id] = hook
                results.append({"createdWebhookId": int(hook.id)})
            return httpx.Response(200, json={"webhookRegistrationResult": results})
        if method == "DELETE":
            for i in body.get("webhookIds") or []:
                self.webhooks.pop(str(i), None)
            return httpx.Response(202)
        return _error(405, method)


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"errorMessages": [message], "errors": {}})
