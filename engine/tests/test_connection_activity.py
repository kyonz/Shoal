"""A connection's usage history: webhook deliveries (accepted or rejected, and what they fired), uses by runs, retention."""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from shoal.api.app import create_app
from shoal.core import clock
from shoal.core.models import parse_resource
from shoal.integrations.base import ConnectionContext, Integration, action
from shoal.store import tables as t

ADMIN = "activity-admin"
BODY = b'{"message": "hello"}'
HISTORY = "/v1/namespaces/default/connections/hooks/activity"


class Ping(BaseModel):
    pass


class EchoConnection(BaseModel):
    pass


class EchoIntegration(Integration):
    key = "echo"
    title = "Echo"
    connection_model = EchoConnection

    @action(description="Answer pong.", input=Ping)
    async def ping(self, ctx: ConnectionContext, params: Ping) -> dict:
        return {"pong": True}


def doc(kind: str, name: str, spec: dict) -> object:
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": kind, "metadata": {"name": name}, "spec": spec})


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
async def api(engine):
    engine.settings.admin_token = SecretStr(ADMIN)
    engine.registry.register("integrations", "echo", EchoIntegration())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine=engine)), base_url="http://shoal.test") as c:
        r = await c.post("/v1/tenants", json={"slug": "acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
        c.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        yield c


async def _tenant(engine) -> str:
    return (await engine.store.get_tenant_by_slug("acme")).id


async def _hooks(api, engine, *, allow_unsigned: bool = False) -> str:
    assert (await api.put("/v1/namespaces/default/secrets/hook-secret", json={"value": "s3cret"})).status_code < 300
    spec = {"integration": "webhook", "config": {"allow_unsigned": allow_unsigned}, "secrets": {"secret": "hook-secret"}}
    assert (await api.put("/v1/namespaces/default/connections/hooks", json={"spec": spec})).status_code < 300
    tenant = await _tenant(engine)
    await engine.store.apply_resource(tenant, doc("Workflow", "greet", {"steps": [{"id": "s", "type": "set", "values": {"ok": True}}]}))
    await engine.store.apply_resource(tenant, doc("Trigger", "on-hello", {"event": "received", "connection": "hooks", "workflow": "greet"}))
    return (await api.get("/v1/namespaces/default/connections/hooks/webhook")).json()["url"].removeprefix("http://shoal.test")


@pytest.mark.parametrize("processing", ["queue", "inline"])
async def test_deliveries_show_what_they_fired_and_why_others_were_rejected(api, engine, processing):
    engine.settings.webhook_processing = processing
    url = await _hooks(api, engine)
    assert (await api.post(url, content=BODY, headers={"X-Signature-256": _sign("s3cret", BODY), "User-Agent": "acme/1"})).status_code == 202
    assert (await api.post(url, content=BODY)).status_code == 422
    assert (await api.post(url, content=BODY, headers={"X-Signature-256": "sha256=bad"})).status_code == 422
    assert (await api.post(url, content=BODY, headers={"X-Shoal-Token": "nope"})).status_code == 422
    await engine.run_until_idle()

    page = (await api.get(HISTORY)).json()
    bad_token, bad_signature, unsigned, accepted = page["items"]
    assert accepted["kind"] == "delivery" and accepted["status"] == "accepted"
    assert accepted["data"]["auth"] == "signed" and accepted["data"]["event_types"] == ["received"]
    assert accepted["data"]["request"]["user_agent"] == "acme/1"
    [fired] = accepted["data"]["triggers"]
    assert fired["trigger"] == "on-hello" and fired["status"] == "fired" and fired["run_id"].startswith("run")
    assert unsigned["status"] == "rejected" and "unsigned" in unsigned["reason"] and "'Allow unsigned' is off" in unsigned["reason"]
    assert bad_signature["status"] == "rejected" and bad_signature["reason"].startswith("bad signature")
    assert bad_token["status"] == "rejected" and bad_token["reason"].startswith("wrong token")
    assert page["counts"]["delivery"] == {"accepted": 1, "rejected": 3}
    assert page["retention_days"] == 30 and page["max_entries"] == 5000

    # The trigger's run uses no connection, so the delivery is the only trace of it.
    run = (await api.get(f"/v1/runs/{fired['run_id']}")).json()
    assert run["trigger_name"] == "on-hello"


async def test_queued_deliveries_show_as_queued_until_a_worker_takes_them(api, engine):
    url = await _hooks(api, engine, allow_unsigned=True)
    assert (await api.post(url, content=BODY)).status_code == 202
    [entry] = (await api.get(HISTORY)).json()["items"]
    assert entry["status"] == "queued" and entry["data"]["auth"] == "unsigned"
    await engine.run_until_idle()
    [entry] = (await api.get(HISTORY)).json()["items"]
    assert entry["status"] == "accepted" and entry["data"]["triggers"][0]["status"] == "fired"
    assert entry["data"]["request"]["bytes"] == len(BODY)  # kept from when it was received


async def test_filters_and_pages(api, engine):
    url = await _hooks(api, engine, allow_unsigned=True)
    for _ in range(3):
        await api.post(url, content=BODY)
    await api.post(url, content=BODY, headers={"X-Shoal-Token": "nope"})
    await engine.run_until_idle()

    rejected = (await api.get(HISTORY, params={"status": "rejected"})).json()["items"]
    assert [e["status"] for e in rejected] == ["rejected"]
    first = (await api.get(HISTORY, params={"limit": 2})).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    rest = (await api.get(HISTORY, params={"limit": 2, "before": first["next_cursor"]})).json()
    assert len(rest["items"]) == 2 and rest["next_cursor"] is None
    assert {e["id"] for e in first["items"]}.isdisjoint(e["id"] for e in rest["items"])
    assert (await api.get(HISTORY, params={"kind": "use"})).json()["items"] == []


async def test_runs_steps_and_api_calls_that_used_a_connection(api, engine):
    tenant = await _tenant(engine)
    await engine.store.apply_resource(tenant, doc("Connection", "pinger", {"integration": "echo"}))
    await engine.store.apply_resource(tenant, doc("Workflow", "pings", {"steps": [
        {"id": "one", "type": "action", "action": "echo.ping"},
        {"id": "two", "type": "action", "action": "echo.ping", "needs": ["one"]},
    ]}))
    run = (await api.post("/v1/namespaces/default/workflows/pings/runs", json={"inputs": {}})).json()
    await engine.run_until_idle()
    assert (await api.post("/v1/namespaces/default/connections/pinger/actions/ping", json={"params": {}})).status_code == 200

    items = (await api.get("/v1/namespaces/default/connections/pinger/activity")).json()["items"]
    call, *uses = items
    assert call["kind"] == "call" and call["status"] == "ok" and call["data"]["action"] == "ping" and call["actor"]
    assert sorted(u["step_id"] for u in uses) == ["one", "two"]
    for use in uses:
        assert use["kind"] == "use" and use["run_id"] == run["id"] and use["workflow_name"] == "pings"
        assert use["data"]["actions"] == ["ping"] and use["count"] == 1
    by_run = (await api.get("/v1/namespaces/default/connections/pinger/activity", params={"run_id": run["id"]})).json()
    assert len(by_run["items"]) == 2


async def test_repeated_calls_from_one_step_are_counted_on_one_entry(engine, tenant):
    store = engine.store
    for _ in range(3):
        await store.record_connection_use(tenant.id, "default", "pinger", "run_x", "agent", "ping")
    await store.record_connection_use(tenant.id, "default", "pinger", "run_x", "agent", "pong")
    [row] = await store.list_connection_activity(tenant.id, "default", "pinger")
    assert row.count == 4 and row.data["actions"] == ["ping", "pong"]


async def test_history_is_bounded_by_age_and_by_entries_per_connection(engine, tenant):
    store = engine.store

    async def add(connection: str) -> None:
        await store.add_connection_activity(t.ConnectionActivity(
            tenant_id=tenant.id, namespace="default", connection_name=connection, kind="delivery", status="accepted", data={},
        ))

    for _ in range(2):
        await add("old")
    clock.advance(timedelta(days=31))
    for _ in range(5):
        await add("busy")
    await add("quiet")

    engine.settings.connection_activity_max_entries = 3
    await engine._prune_audit()
    assert await store.list_connection_activity(tenant.id, "default", "old") == []
    busy = await store.list_connection_activity(tenant.id, "default", "busy")
    assert len(busy) == 3 and [r.id for r in busy] == sorted((r.id for r in busy), reverse=True)
    assert len(await store.list_connection_activity(tenant.id, "default", "quiet")) == 1
