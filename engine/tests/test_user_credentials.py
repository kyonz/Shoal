"""Per-user connection credentials: each person's own token or OAuth grant, used when runs act for them."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from shoal.api.app import create_app
from shoal.core import clock
from shoal.core.models import parse_resource
from shoal.sdk import ConnectionContext, Integration, action

ADMIN = "creds-admin"


class EchoConnection(BaseModel):
    token: SecretStr


class WhoAmI(BaseModel):
    pass


class EchoIntegration(Integration):
    key = "echo"
    title = "Echo"
    connection_model = EchoConnection

    @action(description="Report which token the call used.", input=WhoAmI)
    async def whoami(self, ctx: ConnectionContext, params: WhoAmI) -> dict:
        return {"token": ctx.config.token.get_secret_value(), "acting_as": ctx.acting_as}


def doc(kind: str, name: str, spec: dict) -> object:
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": kind, "metadata": {"name": name}, "spec": spec})


@pytest.fixture
async def world(engine):
    engine.settings.admin_token = SecretStr(ADMIN)
    engine.registry.register("integrations", "echo", EchoIntegration())
    app = create_app(engine=engine)
    clients = []

    def client(token: str) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test",
                              headers={"Authorization": f"Bearer {token}"})
        clients.append(c)
        return c

    r = await client(ADMIN).post("/v1/tenants", json={"slug": "acme"})
    admin = client(r.json()["api_key"])
    tenant = (await engine.store.get_tenant_by_slug("acme")).id
    await engine.secrets.writable().put(tenant, "team-token", "shared-token")
    yield engine, tenant, admin, client
    for c in clients:
        await c.aclose()


async def _person(engine, tenant: str, admin, username: str) -> httpx.AsyncClient:
    """A signed-in person (actor user:<username>) with an API key acting as them."""
    r = await admin.post("/v1/users", json={"username": username, "email": f"{username}@acme.test"})
    assert r.status_code == 201, r.text
    await admin.post("/v1/role-bindings", json={"role": "operator", "subject_kind": "user", "subject_id": r.json()["id"], "namespace": None})
    _, token = await engine.store.create_api_key(tenant, f"{username}-key", ["*"], owner_id=r.json()["id"])
    return token


async def _run(engine, tenant: str, actor: str) -> str:
    run = await engine.executor.start_run(tenant, "who", {}, started_by=actor)
    await engine.run_until_idle()
    return run.id


async def test_per_user_credential_waits_then_resumes(world):
    engine, tenant, admin, client = world
    await engine.store.apply_resource(tenant, doc("Connection", "echo", {
        "integration": "echo", "secrets": {"token": "team-token"}, "credentials": "per_user"}))
    await engine.store.apply_resource(tenant, doc("Workflow", "who", {"steps": [{"id": "call", "type": "action", "action": "echo.whoami"}]}))

    run_id = await _run(engine, tenant, "user:dana")
    step = await engine.store.get_step(run_id, "call")
    assert step.status == "waiting"
    events = [e.type for e in await engine.store.list_events(tenant, run_id)]
    assert "credential.required" in events

    dana = client(await _person(engine, tenant, admin, "dana"))
    assert (await dana.put("/v1/me/credentials/default/echo", json={"values": {"other": "x"}})).status_code == 422
    r = await dana.put("/v1/me/credentials/default/echo", json={"values": {"token": "dana-token"}})
    assert r.status_code == 200, r.text
    assert r.json()["fields"] == ["token"] and "dana-token" not in r.text
    await engine.run_until_idle()
    out = (await engine.store.get_step(run_id, "call")).output
    assert out == {"token": "dana-token", "acting_as": "user:dana"}

    listed = (await dana.get("/v1/me/credentials")).json()
    assert [(c["connection"], c["kind"]) for c in listed] == [("echo", "token")]
    assert "dana-token" not in str(listed)
    assert [c["owner"] for c in (await admin.get("/v1/users/dana/credentials")).json()] == ["user:dana"]

    # Service accounts never use personal credentials: the step fails with a clear message.
    run_id = await _run(engine, tenant, "sa:ci")
    failed = await engine.store.get_step(run_id, "call")
    assert failed.status == "failed" and "service accounts" in failed.error["message"]
    assert (await admin.put("/v1/me/credentials/default/echo", json={"values": {"token": "x"}})).status_code == 403

    assert (await admin.delete("/v1/users/dana/credentials/default/echo")).status_code == 204
    assert (await dana.get("/v1/me/credentials")).json() == []


async def test_per_user_or_shared_falls_back(world):
    engine, tenant, admin, client = world
    await engine.store.apply_resource(tenant, doc("Connection", "echo", {
        "integration": "echo", "secrets": {"token": "team-token"}, "credentials": "per_user_or_shared"}))
    await engine.store.apply_resource(tenant, doc("Workflow", "who", {"steps": [{"id": "call", "type": "action", "action": "echo.whoami"}]}))
    run_id = await _run(engine, tenant, "user:erin")
    assert (await engine.store.get_step(run_id, "call")).output == {"token": "shared-token", "acting_as": None}
    erin = client(await _person(engine, tenant, admin, "erin"))
    await erin.put("/v1/me/credentials/default/echo", json={"values": {"token": "erin-token"}})
    run_id = await _run(engine, tenant, "user:erin")
    assert (await engine.store.get_step(run_id, "call")).output["token"] == "erin-token"
    # Shared connections refuse personal credentials.
    await engine.store.apply_resource(tenant, doc("Connection", "plain", {"integration": "echo", "secrets": {"token": "team-token"}}))
    assert (await erin.put("/v1/me/credentials/default/plain", json={"values": {"token": "x"}})).status_code == 422


async def test_oauth_connect_and_refresh(world):
    engine, tenant, admin, client = world
    issued = []

    def provider(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        issued.append(form["grant_type"][0])
        assert form["client_secret"] == ["oauth-secret"]
        if form["grant_type"] == ["authorization_code"]:
            assert form["code"] == ["the-code"] and form["code_verifier"][0]
            return httpx.Response(200, json={"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600})
        assert form["refresh_token"] == ["rt-1"]
        return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600})

    engine.http_transport = httpx.MockTransport(provider)
    await engine.secrets.writable().put(tenant, "oauth-client", "oauth-secret")
    await engine.store.apply_resource(tenant, doc("Connection", "echo", {
        "integration": "echo", "credentials": "per_user",
        "oauth": {"authorize_url": "https://idp.test/authorize", "token_url": "https://idp.test/token",
                  "client_id": "shoal", "client_secret_secret": "oauth-client", "scopes": ["read", "write"]}}))
    await engine.store.apply_resource(tenant, doc("Workflow", "who", {"steps": [{"id": "call", "type": "action", "action": "echo.whoami"}]}))

    fay = client(await _person(engine, tenant, admin, "fay"))
    url = (await fay.post("/v1/me/credentials/default/echo/oauth")).json()["authorize_url"]
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"] and query["scope"] == ["read write"]
    r = await fay.get("/v1/oauth/callback", params={"code": "the-code", "state": query["state"][0]})
    assert r.status_code == 303 and "connected=default/echo" in r.headers["location"]
    assert (await fay.get("/v1/oauth/callback", params={"code": "x", "state": "forged"})).status_code == 401

    run_id = await _run(engine, tenant, "user:fay")
    assert (await engine.store.get_step(run_id, "call")).output["token"] == "at-1"
    clock.advance(timedelta(hours=1))
    run_id = await _run(engine, tenant, "user:fay")
    assert (await engine.store.get_step(run_id, "call")).output["token"] == "at-2"
    assert issued == ["authorization_code", "refresh_token"]
    clock.reset()
