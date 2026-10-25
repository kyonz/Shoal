"""MCP servers as an integration: each connection is one server, its tools are the actions.

A ``Connection`` with ``integration: mcp`` points at a server's Streamable HTTP
endpoint. Shoal lists the server's tools per connection (``tools/list``) and calls
them with ``tools/call``, so MCP tools work everywhere integration actions do:
``action`` steps, agent tools (``mcp:<connection>`` or ``mcp:<connection>.<tool>``),
wait conditions and the API. Credentials follow the connection's ``credentials``
mode, so a server can use one team token or each person's own (pasted or OAuth).

Servers that only speak stdio can be put behind an MCP HTTP proxy for now.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field, SecretStr, field_validator

from shoal.core.models import credential_fields
from shoal.integrations.base import ActionDef, ConnectionContext, Integration, IntegrationError
from shoal.integrations.remote import check_url

PROTOCOL_VERSION = "2025-06-18"
TOOLS_TTL = 60.0
_tools_cache: dict[tuple[str, str, str | None, str | None], tuple[float, list[dict[str, Any]]]] = {}


class McpConnection(BaseModel):
    url: str = Field(description="The server's Streamable HTTP endpoint, e.g. https://mcp.linear.app/mcp.")
    token: SecretStr | None = Field(None, description="Sent as `Authorization: Bearer <token>`.")
    headers: dict[str, str] = Field(default_factory=dict, description="Non-secret headers.")
    tools: list[str] = Field(default_factory=list, description="Allow-list of tool names (globs); empty allows all.")
    timeout: float = Field(60.0, gt=0, le=600)
    allow_insecure: bool = Field(False, description="Allow plain http to a non-loopback host.")

    @field_validator("headers")
    @classmethod
    def _no_credentials(cls, value: dict[str, str]) -> dict[str, str]:
        if bad := credential_fields(value):
            raise ValueError(f"headers {bad} look like credentials; use the token field (a secret)")
        return value


class _Params(BaseModel):
    model_config = {"extra": "allow"}


class McpActionDef(ActionDef):
    def __init__(self, tool: dict[str, Any]):
        annotations = tool.get("annotations") or {}
        super().__init__(
            name=action_name(tool["name"]), description=tool.get("description") or tool.get("title") or tool["name"],
            input_model=_Params, output_model=None, method="", mutating=not annotations.get("readOnlyHint", False),
        )
        self.tool = tool["name"]
        self.input_schema = dict(tool.get("inputSchema") or {"type": "object"})

    def tool_schema(self) -> dict[str, Any]:
        schema = dict(self.input_schema)
        schema.pop("title", None)
        schema.setdefault("type", "object")
        return schema


def action_name(tool: str) -> str:
    """MCP tool names may use '.', '-' or '/'; action and agent tool names may not."""
    return re.sub(r"[^A-Za-z0-9_]", "_", tool).lower()[:64]


class McpIntegration(Integration):
    key = "mcp"
    title = "MCP server"
    description = "Any Model Context Protocol server: its tools become actions and agent tools."
    connection_model = McpConnection

    async def actions_for(self, ctx: ConnectionContext) -> dict[str, ActionDef]:
        config: McpConnection = ctx.config
        defs: dict[str, ActionDef] = {}
        for tool in await self._tools(ctx):
            if config.tools and not any(fnmatch.fnmatchcase(tool["name"], p) for p in config.tools):
                continue
            definition = McpActionDef(tool)
            defs[definition.name] = definition
        return defs

    async def call_action(self, ctx: ConnectionContext, name: str, params: dict[str, Any]) -> dict[str, Any]:
        definition = (await self.actions_for(ctx)).get(name)
        if not isinstance(definition, McpActionDef):
            raise IntegrationError(f"MCP server '{ctx.name}' has no tool '{name}' (or it is not allowed)", status=404)
        result = await self._session(ctx).request("tools/call", {"name": definition.tool, "arguments": params})
        content = result.get("content") or []
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        if result.get("isError"):
            raise IntegrationError(f"MCP tool {definition.tool} failed: {text[:1000]}", status=422)
        return {"text": text, "content": content, "structured": result.get("structuredContent")}

    async def test_connection(self, ctx: ConnectionContext) -> dict[str, Any]:
        info = await self._session(ctx).initialize()
        return {"ok": True, "server": info.get("serverInfo", {}), "tools": len(await self._tools(ctx, refresh=True))}

    async def _tools(self, ctx: ConnectionContext, *, refresh: bool = False) -> list[dict[str, Any]]:
        config: McpConnection = ctx.config
        # Per server and credential (same-named connections in two namespaces may differ); lists can differ per person.
        token = hashlib.sha256(config.token.get_secret_value().encode()).hexdigest() if config.token else None
        key = (ctx.tenant_id, config.url, token, ctx.acting_as)
        cached = _tools_cache.get(key)
        if cached and not refresh and time.monotonic() - cached[0] < TOOLS_TTL:
            return cached[1]
        session, tools, cursor = self._session(ctx), [], None
        while True:
            page = await session.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(page.get("tools") or [])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        _tools_cache[key] = (time.monotonic(), tools)
        return tools

    def _session(self, ctx: ConnectionContext) -> McpSession:
        return McpSession(ctx)


class McpSession:
    """A minimal Streamable HTTP client: initialize once, then JSON-RPC requests (JSON or SSE answers)."""

    def __init__(self, ctx: ConnectionContext):
        config: McpConnection = ctx.config
        self.ctx, self.url = ctx, check_url(config.url, allow_insecure=config.allow_insecure)
        self.headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **config.headers}
        if config.token is not None:
            self.headers["Authorization"] = f"Bearer {config.token.get_secret_value()}"
        self.timeout = config.timeout
        self.session_id: str | None = None
        self._next = 0

    async def initialize(self) -> dict[str, Any]:
        result = await self._post("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "shoal", "version": "1"},
        })
        await self._post("notifications/initialized", None, notification=True)
        return result

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.session_id is None and method != "initialize":
            await self.initialize()
        return await self._post(method, params)

    async def _post(self, method: str, params: dict[str, Any] | None, *, notification: bool = False) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notification:
            self._next += 1
            message["id"] = self._next
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        try:
            async with self.ctx.http(timeout=httpx.Timeout(self.timeout, connect=10.0)) as client:
                response = await client.post(self.url, content=json.dumps(message), headers=headers)
        except httpx.TransportError as exc:
            raise IntegrationError(f"MCP server '{self.ctx.name}' is unreachable: {exc}", retryable=True) from exc
        if response.status_code in (401, 403):
            raise IntegrationError(f"MCP server '{self.ctx.name}' refused the credentials (HTTP {response.status_code})",
                                   status=response.status_code)
        if response.status_code >= 400:
            retryable = response.status_code in (408, 429) or response.status_code >= 500
            raise IntegrationError(f"MCP server '{self.ctx.name}' answered HTTP {response.status_code}: {response.text[:300]}",
                                   status=response.status_code, retryable=retryable)
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        elif method == "initialize":
            self.session_id = ""  # stateless server
        if notification:
            return {}
        reply = self._reply(response, message["id"])
        if "error" in reply:
            error = reply["error"] or {}
            raise IntegrationError(f"MCP {method} failed: {error.get('message', error)}", status=422)
        return reply.get("result") or {}

    @staticmethod
    def _reply(response: httpx.Response, request_id: int) -> dict[str, Any]:
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(data, dict) and data.get("id") == request_id:
                        return data
            raise IntegrationError("MCP server closed the stream without answering", retryable=True)
        try:
            return response.json()
        except ValueError as exc:
            raise IntegrationError("MCP server sent a response that is not JSON-RPC") from exc
