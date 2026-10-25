"""The open integrations platform: SDK, test kit, remote protocol and project template."""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from shoal.config import RemoteIntegration
from shoal.integrations.base import IntegrationError
from shoal.integrations.github import GitHubIntegration
from shoal.integrations.jira import JiraIntegration
from shoal.integrations.remote import check_url, register_remote_integration
from shoal.integrations.webhook import WebhookIntegration
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
    decode_cursor,
    encode_cursor,
    json_body,
    manifest_for,
    raise_for_status,
    resource,
    verify_hmac_sha256,
)
from shoal.sdk.scaffold import new_integration
from shoal.sdk.server import create_app
from shoal.sdk.testing import (
    IntegrationHarness,
    MockHTTP,
    UnmatchedRequest,
    check_integration,
    check_remote_service,
    engine_harness,
    signed_webhook,
)

ROOT = Path(__file__).resolve().parents[2]
SERVICE_TOKEN = "svc-token"


# --------------------------------------------------------------------------- a small third-party integration


class TicketsConnection(BaseModel):
    api_token: SecretStr
    webhook_secret: SecretStr | None = None


class OpenTicket(BaseModel):
    title: str
    priority: int = Field(3, ge=1, le=5)


class TicketsFilter(BaseModel):
    status: str = "open"


class TicketsIntegration(Integration):
    key = "tickets"
    title = "Tickets"
    description = "A third-party ticket tracker."
    connection_model = TicketsConnection
    events = [EventDef("ticket.*", "Ticket lifecycle events.")]

    def _client(self, ctx: ConnectionContext) -> httpx.AsyncClient:
        return ctx.http(base_url="https://tickets.example", headers={"Authorization": f"Bearer {ctx.config.api_token.get_secret_value()}"})

    @action(description="Open a ticket.", input=OpenTicket)
    async def open_ticket(self, ctx: ConnectionContext, params: OpenTicket) -> dict[str, Any]:
        async with self._client(ctx) as client:
            response = await client.post("/tickets", json=params.model_dump())
        raise_for_status(response, "Tickets")
        return response.json()

    @resource("ticket", description="Tickets by status.", filter=TicketsFilter)
    async def tickets(self, ctx: ConnectionContext, filter: TicketsFilter, cursor: str | None, limit: int) -> Page:
        page = decode_cursor(cursor, {"page": 1})["page"]
        async with self._client(ctx) as client:
            response = await client.get("/tickets", params={"status": filter.status, "page": page})
        raise_for_status(response, "Tickets")
        body = response.json()
        return Page(items=body["items"], next_cursor=encode_cursor({"page": page + 1}) if body["more"] else None)

    async def verify_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> bool:
        return verify_hmac_sha256(ctx.config.webhook_secret, body, headers.get("x-signature-256"), prefix="sha256=")

    async def parse_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> list[IntegrationEvent]:
        payload = json_body(body)
        return [IntegrationEvent(type=f"ticket.{payload['action']}", data=payload, wake_keys=[f"ticket:{payload['id']}"])]


def tickets_api() -> MockHTTP:
    api = MockHTTP(host="tickets.example")
    api.post("/tickets", handler=lambda request: (201, {"id": "T-1", **__import__("json").loads(request.content)}))
    api.get("/tickets", handler=lambda request: {"items": [{"id": f"T-{request.url.params['page']}"}], "more": request.url.params["page"] == "1"})
    return api


class HostRouter(httpx.AsyncBaseTransport):
    """Send each host to its own transport (remote service vs. provider API)."""

    def __init__(self, routes: dict[str, httpx.AsyncBaseTransport]):
        self.routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.routes[request.url.host].handle_async_request(request)


def served(api: MockHTTP) -> httpx.AsyncBaseTransport:
    return httpx.ASGITransport(app=create_app(TicketsIntegration, token=SERVICE_TOKEN, http_transport=api.transport))


# --------------------------------------------------------------------------- contract


@pytest.mark.parametrize("cls", [GitHubIntegration, JiraIntegration, WebhookIntegration, TicketsIntegration])
def test_builtins_and_third_party_pass_the_contract(cls):
    assert check_integration(cls) == []


def test_contract_catches_common_mistakes():
    class Loose(BaseModel):
        api_token: str  # a credential that is not a SecretStr

    class Bad(Integration):
        key = "bad.key"
        title = "Bad"
        connection_model = Loose

        @action("Make-Thing", description="", input=OpenTicket)
        def make(self, ctx, params):  # not async
            return {}

    problems = "\n".join(check_integration(Bad))
    assert "key 'bad.key'" in problems
    assert "'api_token' looks like a credential" in problems
    assert "action 'Make-Thing' must match" in problems
    assert "has no description" in problems
    assert "must be async" in problems


# --------------------------------------------------------------------------- test kit


async def test_harness_calls_actions_and_paginates():
    api = tickets_api()
    harness = IntegrationHarness(TicketsIntegration, config={"api_token": "t0k"}, http=api)

    assert await harness.call("open_ticket", {"title": "Broken"}) == {"id": "T-1", "title": "Broken", "priority": 3}
    assert api.requests("POST", "/tickets")[0].headers["authorization"] == "Bearer t0k"
    assert [i["id"] for i in await harness.query_all("ticket")] == ["T-1", "T-2"]
    api.assert_all_called()


async def test_mock_http_fails_loudly_on_unmocked_calls():
    api = MockHTTP()
    api.get("/repos/{owner}/{repo}", json={"ok": True})
    route = api.get("/files/{path:path}", handler=lambda request, path: {"path": path})
    async with httpx.AsyncClient(transport=api.transport, base_url="https://x.test") as client:
        assert (await client.get("/repos/acme/api")).json() == {"ok": True}
        assert (await client.get("/files/a/b/c.txt")).json() == {"path": "a/b/c.txt"}
        with pytest.raises(UnmatchedRequest, match="unmocked request DELETE"):
            await client.delete("/repos/acme/api")
    assert route.last.path == "/files/a/b/c.txt"


async def test_harness_webhook_verification():
    harness = IntegrationHarness(TicketsIntegration, config={"api_token": "t", "webhook_secret": "s3"})
    headers, body = signed_webhook("s3", {"action": "closed", "id": "T-4"})
    [event] = await harness.webhook(headers, body)
    assert (event.type, event.wake_keys) == ("ticket.closed", ["ticket:T-4"])
    with pytest.raises(IntegrationError, match="verification failed"):
        await harness.webhook(signed_webhook("wrong", {"action": "closed", "id": "T-4"})[0], body)


def test_helpers():
    assert decode_cursor(encode_cursor({"page": 3})) == {"page": 3}
    assert decode_cursor(None, {"page": 1}) == {"page": 1}
    headers, body = signed_webhook("k", b"payload")
    assert verify_hmac_sha256("k", body, headers["X-Signature-256"], prefix="sha256=")
    assert not verify_hmac_sha256(None, body, headers["X-Signature-256"], prefix="sha256=")
    assert json_body(b"[1]") == {"items": [1]}


# --------------------------------------------------------------------------- remote protocol


def test_manifest_describes_capabilities():
    manifest = manifest_for(TicketsIntegration)
    assert manifest.key == "tickets"
    assert manifest.webhooks and not manifest.test
    assert manifest.secret_fields == ["api_token", "webhook_secret"]
    assert [a.name for a in manifest.actions] == ["open_ticket"]
    assert manifest.resources[0].key_field == "id"


async def test_served_python_integration_passes_remote_contract():
    assert await check_remote_service("https://svc.test", SERVICE_TOKEN, transport=served(tickets_api())) == []


def test_server_requires_a_token():
    with pytest.raises(ValueError, match="token is required"):
        create_app(TicketsIntegration)


def test_remote_urls_must_be_https_except_loopback():
    assert check_url("https://svc.example/") == "https://svc.example"
    assert check_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert check_url("http://localhost:8080") == "http://localhost:8080"
    with pytest.raises(IntegrationError, match="not https"):
        check_url("http://svc.internal")
    with pytest.raises(IntegrationError, match="embed credentials"):
        check_url("https://user:pw@svc.example")
    assert check_url("http://svc.internal", allow_insecure=True) == "http://svc.internal"


async def test_remote_connection_end_to_end():
    api = tickets_api()
    transport = HostRouter({"svc.test": served(api), "tickets.example": api.transport})
    async with engine_harness(http=transport) as world:
        await world.connect(
            "tix",
            "remote",
            config={"url": "https://svc.test"},
            secrets={"token": SERVICE_TOKEN, "api_token": "t0k", "webhook_secret": "s3"},
        )
        # Actions and resources go through the remote service, which calls the provider API.
        assert await world.call("remote.open_ticket", {"title": "From afar"}, connection="tix") == {
            "id": "T-1",
            "title": "From afar",
            "priority": 3,
        }
        assert api.requests("POST", "/tickets")[0].headers["authorization"] == "Bearer t0k"
        page = await world.query("tix", "ticket", {"status": "open"})
        assert page.items == [{"id": "T-1"}] and page.next_cursor
        # Remote validation errors surface as non-retryable integration errors.
        with pytest.raises(IntegrationError) as err:
            await world.call("remote.open_ticket", {"title": "x", "priority": 9}, connection="tix")
        assert err.value.status == 422 and not err.value.retryable
        with pytest.raises(IntegrationError, match="missing required field"):
            await world.call("remote.open_ticket", {}, connection="tix")

        # Per-connection action listing, which agent tools and the API use.
        ctx = await world.engine.integrations.connection(world.tenant_id, "tix")
        assert list(await ctx.integration.actions_for(ctx)) == ["open_ticket"]
        assert (await ctx.integration.test_connection(ctx))["integration"] == "tickets"

        # A signed webhook is verified and parsed remotely, then fires a trigger.
        await world.apply(
            {"apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": "on-close"},
             "spec": {"inputs": {"id": {"type": "string"}}, "steps": [{"id": "s", "type": "set", "values": {"id": "{{ inputs.id }}"}}]}},
            {"apiVersion": "shoal.dev/v1", "kind": "Trigger", "metadata": {"name": "closed"},
             "spec": {"connection": "tix", "event": "ticket.closed", "workflow": "on-close", "inputs": {"id": "{{ event.id }}"}}},
        )
        headers, body = signed_webhook("s3", {"action": "closed", "id": "T-9"})
        result = await world.webhook("tix", headers, body)
        assert result["events"] == 1 and [t["trigger"] for t in result["triggers"]] == ["closed"]
        with pytest.raises(Exception, match="verification failed"):
            await world.webhook("tix", {"X-Signature-256": "sha256=bad"}, body)


async def test_remote_service_rejects_a_wrong_token():
    api = tickets_api()
    async with engine_harness(http=HostRouter({"svc.test": served(api)})) as world:
        await world.connect("tix", "remote", config={"url": "https://svc.test"}, secrets={"token": "nope", "api_token": "t"})
        with pytest.raises(IntegrationError) as err:
            await world.call("remote.open_ticket", {"title": "x"}, connection="tix")
        assert err.value.status == 401


async def test_registered_remote_integration_behaves_like_a_builtin():
    api = tickets_api()
    transport = HostRouter({"svc.test": served(api), "tickets.example": api.transport})
    async with engine_harness(http=transport) as world:
        cls = await register_remote_integration(world.engine.registry, "https://svc.test", SERVICE_TOKEN, transport=transport)
        assert cls.key == "tickets"
        catalog = {c["key"]: c for c in await world.engine.integrations.catalog()}
        assert catalog["tickets"]["remote"]["url"] == "https://svc.test"
        assert catalog["tickets"]["actions"][0]["input_schema"]["required"] == ["title"]
        assert "remote" in catalog  # the generic adapter is listed too

        await world.connect("tickets-main", "tickets", secrets={"api_token": "t0k"})
        assert (await world.call("tickets.open_ticket", {"title": "Native"}))["id"] == "T-1"
        assert "open_ticket" in world.engine.integrations.get("tickets").actions  # static, so tools need no lookup


# --------------------------------------------------------------------------- any language: the Node example


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def node_service():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    port = _free_port()
    env = {**os.environ, "SHOAL_INTEGRATION_TOKEN": SERVICE_TOKEN, "PORT": str(port)}
    proc = await asyncio.create_subprocess_exec(
        "node", str(ROOT / "examples/remote-integration-node/server.mjs"), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    if (await client.get(f"{url}/healthz")).status_code == 200:
                        break
            except httpx.HTTPError:
                await asyncio.sleep(0.05)
        else:
            pytest.fail("node service did not start")
        yield url
    finally:
        proc.terminate()
        await proc.wait()


async def test_node_remote_integration(node_service):
    assert await check_remote_service(node_service, SERVICE_TOKEN) == []
    async with engine_harness() as world:
        await world.connect(
            "incidents", "remote", config={"url": node_service}, secrets={"token": SERVICE_TOKEN, "api_key": "k", "webhook_secret": "w"}
        )
        opened = await world.call("remote.create_incident", {"title": "DB down", "severity": "sev1"}, connection="incidents")
        assert opened == {"id": "inc-1", "status": "open"}
        assert (await world.query("incidents", "incident", {"status": "open"})).items[0]["title"] == "DB down"
        headers, body = signed_webhook("w", {"action": "resolved", "incident": {"id": "inc-1"}}, header="X-Incident-Signature")
        assert (await world.webhook("incidents", headers, body))["events"] == 1


# --------------------------------------------------------------------------- project template


def test_scaffold_creates_a_working_project(tmp_path):
    project = new_integration("service-now", tmp_path / "shoal-service-now", title="ServiceNow")
    pyproject = (project / "pyproject.toml").read_text()
    assert 'name = "shoal-service-now"' in pyproject
    assert 'service_now = "shoal_service_now:ServiceNowIntegration"' in pyproject
    assert (project / "src/shoal_service_now/integration.py").exists()
    assert "starter" not in "".join(p.read_text().lower() for p in project.rglob("*") if p.is_file())

    sys.path.insert(0, str(project / "src"))
    try:
        module = importlib.import_module("shoal_service_now")
        assert module.ServiceNowIntegration.key == "service_now"
        assert check_integration(module.ServiceNowIntegration) == []
    finally:
        sys.path.remove(str(project / "src"))
        for name in [m for m in sys.modules if m.startswith("shoal_service_now")]:
            del sys.modules[name]
    with pytest.raises(FileExistsError):
        new_integration("service-now", project)


def test_template_project_tests_pass():
    template = ROOT / "templates/integration"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(template / "tests")],
        cwd=template,
        env={**os.environ, "PYTHONPATH": str(template / "src")},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_operator_remote_integrations_register_at_start():
    api = tickets_api()
    transport = HostRouter({"svc.test": served(api), "tickets.example": api.transport})
    async with engine_harness(http=transport) as world:
        world.engine.settings.remote_integrations = [
            RemoteIntegration(url="https://svc.test", token=SERVICE_TOKEN),
            RemoteIntegration(url="https://down.test"),  # unreachable: logged, start carries on
        ]
        await world.engine.start()
        assert "tickets" in {c["key"] for c in await world.engine.integrations.catalog()}
