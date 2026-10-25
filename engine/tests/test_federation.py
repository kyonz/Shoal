"""A hub and a member deployment, each with its own database, joined by the tenant connector."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from shoal.api.app import create_app
from shoal.config import Settings
from shoal.core import clock
from shoal.engine import Engine
from shoal.federation import protocol as p

ADMIN = "fed-admin-token"
HELLO = {"spec": {"steps": [{"id": "s", "type": "set", "values": {"text": "hi"}}]}}


async def _engine(tmp_path, name: str) -> Engine:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/{name}.db", public_url=f"http://{name}.test")
    settings.admin_token = SecretStr(ADMIN)
    engine = Engine(settings)
    await engine.start()
    return engine


@pytest.fixture
async def world(tmp_path):
    hub, member = await _engine(tmp_path, "hub"), await _engine(tmp_path, "member")
    hub_app, member_app = create_app(engine=hub), create_app(engine=member)
    member.http_transport = httpx.ASGITransport(app=hub_app)  # the connector dials out to the hub
    member.federation_member.app = member_app
    clients = []

    def client(app, token: str | None = None) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                              headers={"Authorization": f"Bearer {token or ADMIN}"})
        clients.append(c)
        return c

    yield hub, member, hub_app, member_app, client
    for c in clients:
        await c.aclose()
    await hub.close()
    await member.close()
    clock.reset()


async def _tenant(app, client, slug: str) -> str:
    r = await client(app).post("/v1/tenants", json={"slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


async def via_hub(member, call):
    """Issue a hub call and let the member's connector serve it (two polls: fetch, then answer)."""
    [membership] = await member.federation_member.memberships()
    task = asyncio.create_task(call)
    for _ in range(50):
        answers = await member.federation_member.poll_once(membership.id, [], wait=0)
        if answers:
            break
        await asyncio.sleep(0.02)
    await member.federation_member.poll_once(membership.id, answers, wait=0)
    return await task


async def test_hub_manages_a_federated_tenant(world):
    hub, member, hub_app, member_app, client = world
    hub_key = await _tenant(hub_app, client, "acme")
    hub_api = client(hub_app, hub_key)

    r = await client(hub_app).post("/v1/federation/links", json={"tenant": "acme", "mode": "read_write"})
    assert r.status_code == 201, r.text
    token = r.json()["enrollment_token"]
    assert r.json()["link"]["status"] == "pending"
    r = await hub_api.get("/v1/namespaces/default/workflows")
    assert r.status_code == 503 and r.json()["code"] == "federation_disconnected"

    # The member's admin joins, exposing one namespace and granting the hub's admin service account.
    member_key = await _tenant(member_app, client, "acme")
    member_api = client(member_app, member_key)
    assert (await member_api.put("/v1/namespaces/private", json={})).status_code == 201
    r = await member_api.put("/v1/federation/membership", json={
        "hub_url": "http://hub.test", "token": token, "max_mode": "read_write", "namespaces": ["default"],
        "grants": [{"role": "tenant-admin", "subject_kind": "hub_user", "subject_id": "sa:admin"}],
    })
    assert r.status_code == 201, r.text
    assert r.json()["effective_mode"] == "read_write"
    reused = await member_api.put("/v1/federation/membership", json={"hub_url": "http://hub.test", "token": token})
    assert reused.status_code == 409

    link = (await client(hub_app).get("/v1/federation/links/acme")).json()
    assert link["status"] == "active" and link["connected"] and link["effective_mode"] == "read_write"

    # Writes through the hub land in the member's database, not the hub's.
    r = await via_hub(member, hub_api.put("/v1/namespaces/default/workflows/hello", json=HELLO))
    assert r.status_code == 201, r.text
    assert r.json()["metadata"]["updated_by"] == "hub:user:sa:admin"
    assert (await member_api.get("/v1/namespaces/default/workflows/hello")).status_code == 200
    assert await hub.store.list_resources((await hub.store.get_tenant_by_slug("acme")).id, "Workflow") == []
    r = await via_hub(member, hub_api.get("/v1/namespaces/default/workflows"))
    assert [w["metadata"]["name"] for w in r.json()] == ["hello"]

    # The member audits the action as coming from the hub; the hub records that it forwarded it.
    entries = (await member_api.get("/v1/audit", params={"action": "workflow.created"})).json()["items"]
    assert entries[0]["actor"] == "hub:user:sa:admin" and entries[0]["source"] == "hub"
    forwarded = [e for e in await hub.store.list_audit((await hub.store.get_tenant_by_slug("acme")).id, 50)
                 if e.action == "federation.put"]
    assert forwarded and forwarded[0].target == "/v1/namespaces/default/workflows/hello"

    # Namespaces the member does not expose are invisible to the hub.
    r = await via_hub(member, hub_api.get("/v1/namespaces/private/workflows"))
    assert r.status_code == 404
    r = await via_hub(member, hub_api.get("/v1/namespaces"))
    assert [n["name"] for n in r.json()] == ["default"]

    # The member lowers its ceiling to read: the hub refuses writes without asking the member.
    assert (await member_api.patch("/v1/federation/membership", json={"max_mode": "read"})).status_code == 200
    [membership] = await member.federation_member.memberships()
    await member.federation_member.poll_once(membership.id, [], wait=0)  # reports the new ceiling
    r = await hub_api.put("/v1/namespaces/default/workflows/other", json=HELLO)
    assert r.status_code == 403 and r.json()["code"] == "federation_read_only"
    assert (await via_hub(member, hub_api.get("/v1/namespaces/default/workflows/hello"))).status_code == 200

    # Hub people the member has not bound get nothing.
    assert (await hub_api.put("/v1/service-accounts/stranger", json={})).status_code == 201
    stranger_key = (await hub_api.post("/v1/api-keys", json={"name": "k", "service_account": "stranger"})).json()["token"]
    r = await via_hub(member, client(hub_app, stranger_key).get("/v1/namespaces/default/workflows"))
    assert r.status_code == 403

    # A forged assertion sent straight to the member is rejected.
    forged = p.sign_assertion("not-the-secret", {"sub": "sa:admin", "method": "GET", "path": "/v1/namespaces/default/workflows"})
    r = await client(member_app).get("/v1/namespaces/default/workflows",
                                     headers={p.ASSERTION_HEADER: forged, "x-shoal-federation-link": membership.link_id})
    assert r.status_code == 401

    # The hub treats a member that stopped polling as disconnected.
    clock.advance(timedelta(minutes=5))
    r = await hub_api.get("/v1/namespaces/default/workflows")
    assert r.status_code == 503 and r.json()["code"] == "federation_disconnected"


async def test_metadata_visibility_hides_run_data(world):
    hub, member, hub_app, member_app, client = world
    hub_key = await _tenant(hub_app, client, "acme")
    token = (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme"})).json()["enrollment_token"]
    member_api = client(member_app, await _tenant(member_app, client, "acme"))
    await member_api.put("/v1/federation/membership", json={
        "hub_url": "http://hub.test", "token": token, "max_mode": "read_write", "visibility": "metadata",
        "grants": [{"role": "tenant-admin", "subject_kind": "hub_user", "subject_id": "sa:admin"}],
    })
    await member_api.put("/v1/namespaces/default/workflows/echo", json={
        "spec": {"inputs": {"secret": {"type": "string"}}, "outputs": {"echo": "{{ inputs.secret }}"},
                 "steps": [{"id": "s", "type": "set", "values": {}}]}})
    run = (await member_api.post("/v1/namespaces/default/workflows/echo/runs", json={"inputs": {"secret": "s3cr3t"}})).json()
    await member.run_until_idle()

    r = await via_hub(member, client(hub_app, hub_key).get(f"/v1/runs/{run['id']}"))
    assert r.status_code == 200, r.text
    assert "s3cr3t" not in r.text and r.json()["status"] == "succeeded"


async def test_background_connector_serves_hub_calls(world):
    hub, member, hub_app, member_app, client = world
    hub_api = client(hub_app, await _tenant(hub_app, client, "acme"))
    token = (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme", "mode": "read"})).json()["enrollment_token"]
    member_api = client(member_app, await _tenant(member_app, client, "acme"))
    r = await member_api.put("/v1/federation/membership", json={
        "hub_url": "http://hub.test", "token": token, "max_mode": "read_write",
        "grants": [{"role": "viewer", "subject_kind": "hub_user", "subject_id": "sa:admin"}],
    })
    assert r.json()["effective_mode"] == "read"  # the hub asked for less than the member allows
    await member_api.put("/v1/namespaces/default/workflows/hello", json=HELLO)

    member.federation_member.start(member_app)
    try:
        r = await asyncio.wait_for(hub_api.get("/v1/namespaces/default/workflows/hello"), timeout=10)
        assert r.status_code == 200 and r.json()["metadata"]["name"] == "hello"
        r = await hub_api.delete("/v1/namespaces/default/workflows/hello")
        assert r.status_code == 403 and r.json()["code"] == "federation_read_only"
    finally:
        for task in list(member.federation_member._tasks.values()):
            task.cancel()
        await asyncio.wait(list(member.federation_member._tasks.values()), timeout=5)


def test_cli_grant_parsing():
    from shoal.cli.main import _parse_grant

    assert _parse_grant("viewer=hub_user:alice@acme.com") == {
        "role": "viewer", "subject_kind": "hub_user", "subject_id": "alice@acme.com", "namespace": None}
    assert _parse_grant("editor=hub_group:platform@payments")["namespace"] == "payments"
    with pytest.raises(Exception, match="expected ROLE"):
        _parse_grant("viewer=user:bob")
