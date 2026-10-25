"""Signals: data sent into a running workflow and taken by `wait` steps."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from shoal.api.app import create_app
from shoal.core import clock
from shoal.core.models import parse_resource
from shoal.integrations.base import IntegrationEvent

ADMIN = "signals-admin"


def wf(name: str, steps: list[dict]) -> object:
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": name}, "spec": {"steps": steps}})


@pytest.fixture
async def api(engine):
    engine.settings.admin_token = SecretStr(ADMIN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine=engine)), base_url="http://shoal.test") as c:
        r = await c.post("/v1/tenants", json={"slug": "acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
        c.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        yield c


async def _tenant_id(engine) -> str:
    return (await engine.store.get_tenant_by_slug("acme")).id


async def test_api_signal_resumes_a_waiting_run(engine, api):
    tenant = await _tenant_id(engine)
    await engine.store.apply_resource(tenant, wf("deploy", [
        {"id": "ask", "type": "set", "values": {"callback": "{{ run.signal_url }}/approved"}},
        {"id": "reply", "type": "wait", "needs": ["ask"], "signal": {"name": "approved", "match": "{{ signal.env == 'prod' }}"}},
        {"id": "done", "type": "set", "needs": ["reply"], "values": {"by": "{{ steps.reply.output.payload.by }}"}},
    ]))
    run = (await api.post("/v1/namespaces/default/workflows/deploy/runs", json={"inputs": {}})).json()
    await engine.run_until_idle()
    assert (await engine.store.get_run(tenant, run["id"])).status == "waiting"

    # A signal that does not match stays queued; the matching one is taken.
    r = await api.post(f"/v1/runs/{run['id']}/signals/approved", json={"payload": {"env": "staging", "by": "x"}})
    assert r.status_code == 202, r.text
    await engine.run_until_idle()
    assert (await engine.store.get_run(tenant, run["id"])).status == "waiting"
    await api.post(f"/v1/runs/{run['id']}/signals/approved", json={"payload": {"env": "prod", "by": "trevor"}})
    await engine.run_until_idle()
    assert (await engine.store.get_run(tenant, run["id"])).status == "succeeded"
    assert (await engine.store.get_step(run["id"], "done")).output == {"by": "trevor"}

    signals = (await api.get(f"/v1/runs/{run['id']}/signals")).json()
    assert [(s["payload"]["env"], s["consumed_by"]) for s in signals] == [("staging", None), ("prod", "reply")]
    audit = (await api.get("/v1/audit", params={"action": "run.signal"})).json()["items"]
    assert len(audit) == 2
    # Finished runs take no more signals.
    assert (await api.post(f"/v1/runs/{run['id']}/signals/approved", json={})).status_code == 409


async def test_callback_url_and_early_signals(engine, api):
    tenant = await _tenant_id(engine)
    await engine.store.apply_resource(tenant, wf("build", [
        {"id": "nap", "type": "wait", "duration": "1h"},
        {"id": "result", "type": "wait", "needs": ["nap"], "signal": {"name": "build.done"}},
    ]))
    run = (await api.post("/v1/namespaces/default/workflows/build/runs", json={"inputs": {}})).json()
    await engine.run_until_idle()
    row = await engine.store.get_run(tenant, run["id"])
    url = engine.signals.callback_url(row)
    path = url.removeprefix(engine.settings.public_url.rstrip("/"))

    # The callback needs no API key but does need the token; the signal arrives before the step waits.
    anonymous = {"Authorization": ""}
    assert (await api.post(path.replace(path.split("/")[5], "wrong") + "/build.done", content=b"{}", headers=anonymous)).status_code == 404
    r = await api.post(path + "/build.done", content=b"artifact ready", headers=anonymous)
    assert r.status_code == 202, r.text
    clock.advance(timedelta(hours=1, seconds=1))
    await engine.run_until_idle()
    out = (await engine.store.get_step(run["id"], "result")).output
    assert out["payload"] == "artifact ready" and out["source"] == "callback"
    assert (await engine.store.get_run(tenant, run["id"])).status == "succeeded"


async def test_integration_event_and_timeout(engine, api):
    tenant = await _tenant_id(engine)
    await engine.store.apply_resource(tenant, wf("review", [
        {"id": "pr", "type": "wait", "signal": {"event": "github:pr:acme/api#{{ 12 }}"}, "max_wait": "2d"},
        {"id": "late", "type": "wait", "needs": ["pr"], "signal": {"name": "never"}, "max_wait": "1h", "on_timeout": "fail"},
    ]))
    run = (await api.post("/v1/namespaces/default/workflows/review/runs", json={"inputs": {}})).json()
    await engine.run_until_idle()
    event = IntegrationEvent(type="pull_request.closed", data={"merged": True}, wake_keys=["github:pr:acme/api#12"])
    await engine.integrations.inject_event(tenant, "github", event)
    await engine.run_until_idle()
    out = (await engine.store.get_step(run["id"], "pr")).output
    assert out["payload"]["data"] == {"merged": True} and out["source"] == "event"

    clock.advance(timedelta(hours=1, seconds=1))
    await engine.run_until_idle()
    late = await engine.store.get_step(run["id"], "late")
    assert late.status == "failed" and "never" in (late.error or {}).get("message", str(late.error))


def test_wait_needs_exactly_one_mode():
    with pytest.raises(ValueError):
        wf("bad", [{"id": "w", "type": "wait", "duration": "1h", "signal": {"name": "x"}}])
    with pytest.raises(ValueError):
        wf("bad", [{"id": "w", "type": "wait", "signal": {"name": "x", "event": "y"}}])
