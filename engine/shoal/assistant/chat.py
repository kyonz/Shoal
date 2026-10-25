"""The UI chat assistant: a tool loop over Shoal's own public API.

The conversation is stateless on the server: the client sends the history
(Anthropic Messages shape, as returned by the previous call) and gets it back
extended. Tools reach the API through an ``httpx`` client bound to the full
ASGI app with the caller's credentials, so authorization, auditing and
federation forwarding apply exactly as for a direct call.

Reads run straight away. Changes (``apply_resources``, ``api_call``) stop the
loop and come back as ``pending`` actions; the client resends the history with
a decision per action, and the loop executes the approved ones and carries on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import httpx
import yaml

from shoal.agents.providers.base import ModelRequest, ToolDef
from shoal.assistant.prompt import SYSTEM, context_note
from shoal.core.errors import InvalidRequest

if TYPE_CHECKING:
    from shoal.llm.router import ModelClient

MAX_RESULT_CHARS = 24_000
BLOCKED_PREFIXES = ("/v1/assistant", "/v1/auth", "/v1/hooks", "/scim", "/v1/federation/enroll")
MUTATING_TOOLS = frozenset({"apply_resources", "api_call"})
DECLINED = "The user declined this action."

_PATH = {"type": "string", "description": "API path starting with /v1/, e.g. /v1/namespaces/default/workflows"}
TOOLS = [
    ToolDef(
        "list_endpoints",
        "List the Shoal API's endpoints (method, path, summary). Filter by tag or a search string.",
        {"type": "object", "properties": {"tag": {"type": "string"}, "search": {"type": "string"}}},
    ),
    ToolDef(
        "describe_endpoint",
        "Parameters and request body schema of one endpoint.",
        {"type": "object", "properties": {"method": {"type": "string"}, "path": {"type": "string"}},
         "required": ["method", "path"]},
    ),
    ToolDef(
        "api_get",
        "GET an API path and return the JSON response. Runs immediately.",
        {"type": "object", "properties": {"path": _PATH, "query": {"type": "object"}}, "required": ["path"]},
    ),
    ToolDef(
        "apply_resources",
        "Create or update declarative resources from multi-document YAML (apiVersion shoal.dev/v1). Validated "
        "first; invalid documents come back as errors to fix. Valid ones are shown to the user for approval.",
        {
            "type": "object",
            "properties": {
                "yaml": {"type": "string"},
                "namespace": {"type": "string", "description": "Defaults to the user's current namespace."},
                "message": {"type": "string", "description": "Version note stored with the change."},
                "summary": {"type": "string", "description": "One line for the approval card."},
            },
            "required": ["yaml", "summary"],
        },
    ),
    ToolDef(
        "api_call",
        "Call an API endpoint that changes state (POST, PUT, PATCH, DELETE). The user approves it first.",
        {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"]},
                "path": _PATH,
                "query": {"type": "object"},
                "body": {"description": "JSON body."},
                "summary": {"type": "string", "description": "One line for the approval card."},
            },
            "required": ["method", "path", "summary"],
        },
    ),
]


@dataclass
class ChatResult:
    messages: list[dict[str, Any]]
    pending: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, float] = field(default_factory=dict)
    model: str | None = None
    stopped: str | None = None


def _clip(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n[... {len(text) - MAX_RESULT_CHARS} characters truncated; narrow the query ...]"
    return text


def _add_usage(total: dict[str, float], usage: dict[str, float]) -> dict[str, float]:
    for key, value in usage.items():
        total[key] = total.get(key, 0) + value
    return total


def _check_path(path: str) -> str:
    if not isinstance(path, str):
        raise ValueError("path must be an API path starting with /v1/")
    # Check the path as the router will see it: it decodes %-escapes, so /v1/%61ssistant is /v1/assistant.
    decoded = unquote(path)
    if not decoded.startswith("/v1/") or ".." in decoded or "//" in decoded or "%" in decoded:
        raise ValueError("path must be an API path starting with /v1/")
    if decoded.startswith(BLOCKED_PREFIXES):
        raise ValueError(f"{path} is not available to the assistant")
    return path


def _resolve_refs(schema: Any, components: dict[str, Any], depth: int = 0) -> Any:
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[1]
            if depth > 4:
                return {"$ref": name}
            return _resolve_refs(components.get(name, {}), components, depth + 1)
        return {k: _resolve_refs(v, components, depth) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_resolve_refs(v, components, depth) for v in schema]
    return schema


class Assistant:
    """One chat turn: runs the model and tools until it answers or needs approval."""

    def __init__(self, model: ModelClient, api: httpx.AsyncClient, openapi: dict[str, Any], *,
                 namespace: str, actor: str, page: str | None = None, context: str | None = None, max_turns: int = 16,
                 max_tokens: int = 16000, auto_approve: bool = False):
        self.model = model
        self.api = api
        self.openapi = openapi
        self.namespace = namespace
        self.system = f"{SYSTEM}\n# Context\n\n{context_note(namespace, page, actor, context)}\n"
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.auto_approve = auto_approve

    # ------------------------------------------------------------------ loop

    async def chat(self, messages: list[dict[str, Any]], *, message: str | None = None,
                   decisions: dict[str, dict[str, Any]] | None = None) -> ChatResult:
        messages = [dict(m) for m in messages]
        open_calls = _open_tool_uses(messages)
        if open_calls:
            results = []
            for call in open_calls:
                decision = (decisions or {}).get(call["id"])
                if decision is None and not message:
                    raise InvalidRequest(f"tool call {call['id']} is waiting for a decision", details={"pending": call["id"]})
                if decision is not None and decision.get("approve"):
                    results.append(await self._execute(call))
                else:
                    note = (decision or {}).get("note") or ("The user sent a new message instead." if message else "")
                    results.append(_result(call["id"], f"{DECLINED} {note}".strip(), error=True))
            content: list[dict[str, Any]] = results
            if message:
                content.append({"type": "text", "text": message})
            last = messages[-1]
            if last["role"] == "user" and isinstance(last["content"], list):
                messages[-1] = {"role": "user", "content": [*last["content"], *content]}
            else:
                messages.append({"role": "user", "content": content})
        elif message:
            messages.append({"role": "user", "content": message})
        elif not messages or messages[-1]["role"] != "user":
            raise InvalidRequest("send a message")

        usage: dict[str, float] = {}
        model_name: str | None = None
        for _ in range(self.max_turns):
            response = await self.model.complete(
                ModelRequest(model=self.model.active.model, system=self.system, messages=messages, tools=TOOLS,
                             max_tokens=self.max_tokens)
            )
            _add_usage(usage, response.usage)
            model_name = response.model or self.model.active.model
            messages.append({"role": "assistant", "content": response.content})
            calls = response.tool_uses
            if not calls:
                return ChatResult(messages, usage=usage, model=model_name)
            results, pending = [], []
            for call in calls:
                if call["name"] in MUTATING_TOOLS and not self.auto_approve:
                    preview = await self._preview(call)
                    if preview.get("error"):
                        results.append(_result(call["id"], preview["error"], error=True))
                    else:
                        pending.append(preview)
                else:
                    results.append(await self._execute(call))
            if pending:
                # Reads are answered now; the changes wait for the user and their results join this
                # same user turn on the next request (every tool_use needs its result in the next message).
                if results:
                    messages.append({"role": "user", "content": results})
                return ChatResult(messages, pending=pending, usage=usage, model=model_name)
            messages.append({"role": "user", "content": results})
        return ChatResult(messages, usage=usage, model=model_name, stopped="max_turns")

    # ------------------------------------------------------------------ tools

    async def _preview(self, call: dict[str, Any]) -> dict[str, Any]:
        args = call.get("input") or {}
        base = {"id": call["id"], "tool": call["name"], "summary": str(args.get("summary") or call["name"])[:300]}
        try:
            if call["name"] == "apply_resources":
                ns = args.get("namespace") or self.namespace
                status, body = await self._request("POST", "/v1/apply", query={"namespace": ns, "dry_run": "true"},
                                                   content=str(args.get("yaml", "")), yaml_body=True)
                if status >= 400:
                    return {"error": f"Validation failed ({status}); nothing was applied. Fix and retry:\n{_clip(body)}"}
                return {**base, "method": "POST", "path": "/v1/apply", "namespace": ns, "yaml": args.get("yaml"),
                        "plan": body, "kinds": _kinds(args.get("yaml", ""))}
            method = str(args.get("method", "")).upper()
            if method not in ("POST", "PUT", "PATCH", "DELETE"):
                return {"error": "api_call is for POST, PUT, PATCH or DELETE; use api_get to read"}
            path = _check_path(args.get("path", ""))
            return {**base, "method": method, "path": path, "query": args.get("query") or {}, "body": args.get("body")}
        except ValueError as exc:
            return {"error": str(exc)}

    async def _execute(self, call: dict[str, Any]) -> dict[str, Any]:
        name, args = call["name"], call.get("input") or {}
        try:
            if name == "list_endpoints":
                return _result(call["id"], self._list_endpoints(args.get("tag"), args.get("search")))
            if name == "describe_endpoint":
                return _result(call["id"], self._describe(str(args.get("method", "get")), str(args.get("path", ""))))
            if name == "api_get":
                status, body = await self._request("GET", _check_path(args.get("path", "")), query=args.get("query"))
            elif name == "apply_resources":
                query = {"namespace": args.get("namespace") or self.namespace}
                if args.get("message"):
                    query["message"] = args["message"]
                status, body = await self._request("POST", "/v1/apply", query=query, content=str(args.get("yaml", "")),
                                                   yaml_body=True)
            elif name == "api_call":
                method = str(args.get("method", "")).upper()
                if method not in ("POST", "PUT", "PATCH", "DELETE"):
                    raise ValueError("api_call is for POST, PUT, PATCH or DELETE")
                status, body = await self._request(method, _check_path(args.get("path", "")), query=args.get("query"),
                                                   json_body=args.get("body"))
            else:
                return _result(call["id"], f"unknown tool {name}", error=True)
        except ValueError as exc:
            return _result(call["id"], str(exc), error=True)
        return _result(call["id"], {"status": status, "body": body}, error=status >= 400)

    async def _request(self, method: str, path: str, *, query: dict[str, Any] | None = None,
                       json_body: Any = None, content: str | None = None, yaml_body: bool = False) -> tuple[int, Any]:
        params = {k: (json.dumps(v) if isinstance(v, dict | list) else str(v)) for k, v in (query or {}).items()}
        headers = {"content-type": "application/yaml"} if yaml_body else {}
        kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if content is not None:
            kwargs["content"] = content.encode()
        elif json_body is not None:
            kwargs["json"] = json_body
        response = await self.api.request(method, path, **kwargs)
        if response.status_code == 204 or not response.content:
            return response.status_code, None
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    def _operations(self) -> list[tuple[str, str, dict[str, Any]]]:
        ops = []
        for path, item in (self.openapi.get("paths") or {}).items():
            if path.startswith(BLOCKED_PREFIXES) or not path.startswith("/v1/"):
                continue
            for method, op in item.items():
                if method in ("get", "post", "put", "patch", "delete"):
                    ops.append((method.upper(), path, op))
        return ops

    def _list_endpoints(self, tag: str | None, search: str | None) -> str:
        lines = []
        needle = (search or "").lower()
        for method, path, op in self._operations():
            tags = op.get("tags") or []
            if tag and tag not in tags:
                continue
            line = f"{method} {path} — {op.get('summary') or op.get('operationId', '')} [{', '.join(tags)}]"
            if needle and needle not in line.lower():
                continue
            lines.append(line)
        return "\n".join(lines) or "no matching endpoints"

    def _describe(self, method: str, path: str) -> Any:
        components = (self.openapi.get("components") or {}).get("schemas") or {}
        for m, p, op in self._operations():
            if m == method.upper() and p == path:
                body = ((op.get("requestBody") or {}).get("content") or {})
                return _clip({
                    "summary": op.get("summary"),
                    "description": op.get("description"),
                    "parameters": [
                        {"name": prm.get("name"), "in": prm.get("in"), "required": prm.get("required", False),
                         "schema": _resolve_refs(prm.get("schema"), components)}
                        for prm in op.get("parameters") or []
                    ],
                    "request_body": {ct: _resolve_refs(c.get("schema"), components) for ct, c in body.items()},
                })
        return f"no endpoint {method.upper()} {path}; use list_endpoints"


def _result(tool_use_id: str, content: Any, *, error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": _clip(content)}
    if error:
        block["is_error"] = True
    return block


def _kinds(text: str) -> list[dict[str, str]]:
    try:
        docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError:
        return []
    return [{"kind": str(d.get("kind")), "name": str((d.get("metadata") or {}).get("name"))} for d in docs]


def _open_tool_uses(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool calls in the last assistant message that have no result yet."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index]["role"] == "assistant":
            content = messages[index]["content"]
            if not isinstance(content, list):
                return []
            answered = {
                b.get("tool_use_id")
                for m in messages[index + 1:] if isinstance(m["content"], list)
                for b in m["content"] if b.get("type") == "tool_result"
            }
            return [b for b in content if b.get("type") == "tool_use" and b.get("id") not in answered]
    return []
