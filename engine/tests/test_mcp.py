"""MCP servers as an integration: tools become actions and agent tools, with per-user credentials."""

from __future__ import annotations

import json

import httpx
import pytest

from shoal.core.models import parse_resource


def doc(kind: str, name: str, spec: dict) -> object:
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": kind, "metadata": {"name": name}, "spec": spec})


class FakeMcpServer:
    """Streamable HTTP MCP server: sessions, tools/list (paged), tools/call, SSE answers for tool calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.tokens: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.tokens.append(request.headers.get("authorization"))
        message = json.loads(request.content)
        method, params = message["method"], message.get("params") or {}
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": "2025-06-18", "serverInfo": {"name": "tickets"}, "capabilities": {"tools": {}}}},
                headers={"mcp-session-id": "sess-1"})
        assert request.headers["mcp-session-id"] == "sess-1"
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            tools = [
                {"name": "create-issue", "description": "Create an issue.",
                 "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}},
                {"name": "search.issues", "description": "Search.", "annotations": {"readOnlyHint": True},
                 "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
                {"name": "delete_everything", "description": "Not allowed by the connection.", "inputSchema": {"type": "object"}},
            ]
            page = tools[:2] if not params.get("cursor") else tools[2:]
            result = {"tools": page, **({"nextCursor": "p2"} if not params.get("cursor") else {})}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})
        if method == "tools/call":
            self.calls.append((params["name"], params["arguments"]))
            who = request.headers.get("authorization", "").removeprefix("Bearer ")
            result = {"content": [{"type": "text", "text": f"created {params['arguments'].get('title')} as {who}"}],
                      "structuredContent": {"id": "ISS-1"}}
            body = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result})}\n\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "nope"}})


@pytest.fixture
async def server(engine, tenant):
    fake = FakeMcpServer()
    engine.http_transport = httpx.MockTransport(fake)
    await engine.secrets.writable().put(tenant.id, "tickets-token", "team-token")
    await engine.store.apply_resource(tenant.id, doc("Connection", "tickets", {
        "integration": "mcp", "config": {"url": "https://mcp.tickets.test/mcp", "tools": ["create-issue", "search.*"]},
        "secrets": {"token": "tickets-token"}, "credentials": "per_user_or_shared"}))
    return fake


async def test_mcp_tools_are_actions(engine, tenant, server):
    ctx = await engine.integrations.connection(tenant.id, "tickets")
    actions = await ctx.integration.actions_for(ctx)
    assert sorted(actions) == ["create_issue", "search_issues"]  # allow-list applied, names made safe
    assert actions["search_issues"].mutating is False
    assert actions["create_issue"].tool_schema()["required"] == ["title"]

    await engine.store.apply_resource(tenant.id, doc("Workflow", "file", {"steps": [
        {"id": "open", "type": "action", "action": "mcp.create_issue", "connection": "tickets", "with": {"title": "Bug"}}]}))
    run = await engine.executor.start_run(tenant.id, "file", {}, started_by="user:dana")
    await engine.run_until_idle()
    out = (await engine.store.get_step(run.id, "open")).output
    assert out["text"] == "created Bug as team-token" and out["structured"] == {"id": "ISS-1"}

    # Dana connects her own token: the same workflow now acts as her.
    await engine.user_credentials.put(tenant.id, "user:dana", "default", "tickets", {"token": "dana-token"})
    run = await engine.executor.start_run(tenant.id, "file", {}, started_by="user:dana")
    await engine.run_until_idle()
    assert (await engine.store.get_step(run.id, "open")).output["text"] == "created Bug as dana-token"


async def test_agents_use_mcp_tools(engine, tenant, server):
    await engine.store.apply_resource(tenant.id, doc("Agent", "triager", {
        "provider": "scripted", "tools": ["mcp:tickets"],
        "provider_config": {"script": [{"tool_calls": [{"name": "tickets__create_issue", "input": {"title": "From agent"}}]}],
                            "final_outputs": {"result": "{{ last_result.text }}"}}}))
    await engine.store.apply_resource(tenant.id, doc("Workflow", "triage", {"steps": [
        {"id": "t", "type": "agent", "agent": "triager", "prompt": "file it", "outputs": {"result": {"type": "string"}}}]}))
    run = await engine.executor.start_run(tenant.id, "triage", {})
    await engine.run_until_idle()
    step = await engine.store.get_step(run.id, "t")
    assert step.status == "succeeded", step.error
    assert step.output["result"] == "created From agent as team-token"
    assert server.calls == [("create-issue", {"title": "From agent"})]
    actions = [e.data for e in await engine.store.list_events(tenant.id, run.id) if e.type == "agent.action"]
    assert actions == [{"action": "mcp.create_issue", "connection": "tickets", "params": {"title": "From agent"}}]


async def test_mcp_rejects_clear_text_auth_headers(engine, tenant, server):
    await engine.store.apply_resource(tenant.id, doc("Connection", "leaky", {
        "integration": "mcp", "config": {"url": "https://mcp.tickets.test/mcp", "headers": {"x-api-key": "sk"}}}))
    with pytest.raises(ValueError, match="look like credentials"):
        await engine.integrations.connection(tenant.id, "leaky")
