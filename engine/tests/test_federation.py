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


async def test_hub_streams_live_run_events(world):
    """The hub relays a member run's events as SSE, and a long-polled stream never blocks other calls."""
    import json
    import time

    hub, member, hub_app, member_app, client = world
    hub_api = client(hub_app, await _tenant(hub_app, client, "acme"))
    token = (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme"})).json()["enrollment_token"]
    member_api = client(member_app, await _tenant(member_app, client, "acme"))
    await member_api.put("/v1/federation/membership", json={
        "hub_url": "http://hub.test", "token": token, "max_mode": "read_write",
        "grants": [{"role": "viewer", "subject_kind": "hub_user", "subject_id": "sa:admin"}],
    })
    await member_api.put("/v1/namespaces/default/workflows/gate", json={"spec": {"steps": [
        {"id": "hold", "type": "wait", "signal": {"name": "go"}},
        {"id": "done", "type": "set", "values": {"ok": True}}]}})
    run = (await member_api.post("/v1/namespaces/default/workflows/gate/runs", json={"inputs": {}})).json()
    await member.run_until_idle()

    member.federation_member.start(member_app)
    try:
        stream = asyncio.create_task(hub_api.get(f"/v1/runs/{run['id']}/events", headers={"accept": "text/event-stream"}))
        await asyncio.sleep(0.5)  # the hub is now long-polling the member for new events
        started = time.monotonic()
        r = await asyncio.wait_for(hub_api.get("/v1/namespaces/default/workflows/gate"), timeout=10)
        assert r.status_code == 200 and time.monotonic() - started < 5

        assert (await member_api.post(f"/v1/runs/{run['id']}/signals/go", json={"payload": {}})).status_code < 300
        await member.run_until_idle()
        r = await asyncio.wait_for(stream, timeout=20)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        kinds = [line.removeprefix("event: ").strip() for line in r.text.splitlines() if line.startswith("event: ")]
        assert kinds[-1] == "end" and "run.succeeded" in kinds, kinds
        ids = [int(line.removeprefix("id: ")) for line in r.text.splitlines() if line.startswith("id: ")]
        assert ids == sorted(set(ids)), "each event once, in order"
        end = [line for line in r.text.splitlines() if line.startswith("data: ")][-1]
        assert json.loads(end.removeprefix("data: ")) == {"status": "succeeded"}

        missing = await asyncio.wait_for(hub_api.get("/v1/runs/run_nope/events", headers={"accept": "text/event-stream"}), timeout=10)
        assert missing.status_code == 404
    finally:
        for task in list(member.federation_member._tasks.values()):
            task.cancel()
        await asyncio.wait(list(member.federation_member._tasks.values()), timeout=5)


async def test_revoked_tenant_can_be_federated_again(world):
    _, _, hub_app, _, client = world
    await _tenant(hub_app, client, "acme")
    first = (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme"})).json()["link"]
    assert (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme"})).status_code == 409
    assert (await client(hub_app).delete("/v1/federation/links/acme")).status_code == 204
    assert (await client(hub_app).get("/v1/federation/links/acme")).json()["status"] == "revoked"
    r = await client(hub_app).post("/v1/federation/links", json={"tenant": "acme", "mode": "read"})
    assert r.status_code == 201, r.text
    assert r.json()["link"]["id"] != first["id"] and r.json()["link"]["status"] == "pending"


def test_cli_grant_parsing():
    from shoal.cli.main import _parse_grant

    assert _parse_grant("viewer=hub_user:alice@acme.com") == {
        "role": "viewer", "subject_kind": "hub_user", "subject_id": "alice@acme.com", "namespace": None}
    assert _parse_grant("editor=hub_group:platform@payments")["namespace"] == "payments"
    with pytest.raises(Exception, match="expected ROLE"):
        _parse_grant("viewer=user:bob")


async def test_revoked_link_is_refused_not_served_as_hosted(world):
    hub, member, hub_app, member_app, client = world
    hub_api = client(hub_app, await _tenant(hub_app, client, "acme"))
    token = (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme"})).json()["enrollment_token"]
    member_api = client(member_app, await _tenant(member_app, client, "acme"))
    await member_api.put("/v1/federation/membership", json={"hub_url": "http://hub.test", "token": token, "max_mode": "read_write"})
    assert (await hub_api.get("/v1/me")).json()["federated"] is True
    assert (await client(hub_app).post("/v1/federation/links/acme/enrollment")).status_code == 200

    assert (await client(hub_app).delete("/v1/federation/links/acme")).status_code == 204
    for call in (hub_api.get("/v1/namespaces/default/workflows"), hub_api.put("/v1/namespaces/default/workflows/w", json=HELLO)):
        r = await call
        assert r.status_code == 503 and r.json()["code"] == "federation_revoked", r.text
    assert (await client(hub_app).post("/v1/federation/links/acme/enrollment")).status_code == 409
    # Nothing landed on the hub, so the tenant can be federated again.
    assert (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme"})).status_code == 201

    # The member hears it was rejected, and says so.
    [membership] = await member.federation_member.memberships()
    member.federation_member.start_connector(membership)
    for _ in range(100):
        if membership.id in member.federation_member.connector_errors:
            break
        await asyncio.sleep(0.02)
    r = (await member_api.get("/v1/federation/membership")).json()
    assert "revoked" in r["connector_error"]
    for task in list(member.federation_member._tasks.values()):
        task.cancel()


async def test_ceiling_and_exposure_hold_for_calls_already_handed_out(world):
    hub, member, hub_app, member_app, client = world
    hub_api = client(hub_app, await _tenant(hub_app, client, "acme"))
    token = (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme"})).json()["enrollment_token"]
    member_api = client(member_app, await _tenant(member_app, client, "acme"))
    assert (await member_api.put("/v1/namespaces/private", json={})).status_code == 201
    await member_api.put("/v1/federation/membership", json={
        "hub_url": "http://hub.test", "token": token, "max_mode": "read_write", "namespaces": ["default"],
        "grants": [{"role": "tenant-admin", "subject_kind": "hub_user", "subject_id": "sa:admin"}],
    })
    await member_api.put("/v1/namespaces/private/workflows/hidden", json=HELLO)
    await member_api.put("/v1/namespaces/default/workflows/echo", json={
        "spec": {"inputs": {"secret": {"type": "string"}}, "outputs": {"echo": "{{ inputs.secret }}"},
                 "steps": [{"id": "s", "type": "set", "values": {}}]}})
    run = (await member_api.post("/v1/namespaces/default/workflows/echo/runs", json={"inputs": {"secret": "s3cr3t"}})).json()
    await member.run_until_idle()

    # The audit log through the hub leaves out namespaces the member does not expose.
    r = await via_hub(member, hub_api.get("/v1/audit"))
    assert r.status_code == 200 and "private" not in r.text, r.text
    assert any(e["target"] == "default/echo" for e in r.json()["items"])

    # A row read before the member tightened its ceiling is not what the call is checked against.
    [stale] = await member.federation_member.memberships()
    await member_api.patch("/v1/federation/membership", json={"visibility": "metadata"})
    assertion = p.sign_assertion(member.secrets.cipher.decrypt(stale.secret_ciphertext),
                                 {"sub": "sa:admin", "method": "GET", "path": f"/v1/runs/{run['id']}"})
    answer = await member.federation_member.handle(stale, {
        "id": "fwd_1", "method": "GET", "path": f"/v1/runs/{run['id']}", "query": "", "assertion": assertion, "headers": {}, "body": ""})
    assert answer["status"] == 200 and b"s3cr3t" not in __import__("base64").b64decode(answer["body"])

    # A member-side failure still answers, so the hub isn't left waiting.
    app, member.federation_member.app = member.federation_member.app, object()
    answer = await member.federation_member.handle(stale, {"id": "fwd_2", "method": "GET", "path": "/v1/namespaces", "assertion": assertion})
    member.federation_member.app = app
    assert answer["status"] == 502

    # Forwarded calls are not kept on the hub once answered.
    from sqlalchemy import func, select

    from shoal.store import tables as t
    async with hub.store.session() as s:
        assert (await s.execute(select(func.count()).select_from(t.FederationRequest))).scalar() == 0


async def test_federated_tenant_cannot_join_another_hub(world):
    _, _, hub_app, _, client = world
    hub_api = client(hub_app, await _tenant(hub_app, client, "acme"))
    token = (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme"})).json()["enrollment_token"]
    r = await hub_api.put("/v1/federation/membership", json={"hub_url": "http://other.test", "token": token})
    assert r.status_code == 409, r.text


async def test_leaving_tells_the_hub_and_removes_hub_roles(world):
    hub, member, hub_app, member_app, client = world
    hub_api = client(hub_app, await _tenant(hub_app, client, "acme"))
    token = (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme"})).json()["enrollment_token"]
    member_api = client(member_app, await _tenant(member_app, client, "acme"))
    await member_api.put("/v1/federation/membership", json={
        "hub_url": "http://hub.test", "token": token, "max_mode": "read_write",
        "grants": [{"role": "viewer", "subject_kind": "hub_user", "subject_id": "sa:admin"},
                   {"role": "viewer", "subject_kind": "hub_group", "subject_id": "platform"}],
    })
    local = len(await member.directory.list_bindings((await member.store.get_tenant_by_slug("acme")).id))

    assert (await member_api.delete("/v1/federation/membership")).status_code == 204
    bindings = (await member_api.get("/v1/role-bindings")).json()
    assert not [b for b in bindings if b["subject_kind"] in ("hub_user", "hub_group")]
    assert len(bindings) == local - 2
    entry = (await member_api.get("/v1/audit", params={"action": "federation.left"})).json()["items"][0]
    assert entry["data"]["hub_notified"] is True and entry["data"]["bindings_removed"] == 2

    assert (await client(hub_app).get("/v1/federation/links/acme")).json()["status"] == "left"
    r = await hub_api.get("/v1/namespaces/default/workflows")
    assert r.status_code == 503 and r.json()["code"] == "federation_revoked" and "left" in r.json()["detail"]
    assert (await client(hub_app).post("/v1/federation/links/acme/enrollment")).status_code == 409
    assert (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme"})).status_code == 201


async def test_leaving_works_when_the_hub_is_gone(world):
    hub, member, hub_app, member_app, client = world
    await _tenant(hub_app, client, "acme")
    token = (await client(hub_app).post("/v1/federation/links", json={"tenant": "acme"})).json()["enrollment_token"]
    member_api = client(member_app, await _tenant(member_app, client, "acme"))
    await member_api.put("/v1/federation/membership", json={"hub_url": "http://hub.test", "token": token})

    def gone(request):
        raise httpx.ConnectError("hub unreachable")

    member.http_transport = httpx.MockTransport(gone)
    assert (await member_api.delete("/v1/federation/membership")).status_code == 204
    assert (await member_api.get("/v1/federation/membership")).status_code == 404
