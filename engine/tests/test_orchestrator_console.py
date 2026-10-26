"""Setting up Temporal from the instance console: a saved connection, shared secrets, testing and switching."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import update

from shoal.api.app import create_app
from shoal.config import Settings
from shoal.core import clock
from shoal.core.scope import SHARED_NAMESPACE, use_namespace
from shoal.engine import Engine
from shoal.store import tables as t

from .conftest import TEST_TEMPORAL_ADDRESS
from .test_temporal import settled, wf

needs_temporal = pytest.mark.skipif(not TEST_TEMPORAL_ADDRESS, reason="set SHOAL_TEST_TEMPORAL_ADDRESS")
URL = "/v1/instance/orchestrator"
UNREACHABLE = "127.0.0.1:1"


@pytest.fixture
async def world(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOAL_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/shoal.db", public_url="http://shoal.test",
                        admin_token="root-token")
    engine = Engine(settings)
    await engine.start()
    tenant = await engine.store.create_tenant("ops", "Ops")
    await engine.store.apply_resource(tenant.id, wf("one", [{"id": "s", "type": "set", "values": {"x": 1}}]))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine=engine)), base_url="http://shoal.test",
                               headers={"Authorization": "Bearer root-token"})
    yield engine, tenant, client
    await client.aclose()
    await engine.close()
    clock.reset()


async def put_secret(engine, tenant, name: str, value: str) -> None:
    with use_namespace(SHARED_NAMESPACE):
        await engine.secrets.writable().put(tenant.id, name, value)


async def test_the_environment_decides_until_an_admin_saves_a_setting(world):
    engine, _, client = world
    status = (await client.get(URL)).json()
    assert status["source"] == "environment" and status["setting"] is None
    assert status["orchestrator"] == engine.settings.orchestrator
    assert status["environment"]["temporal_address"] == engine.settings.temporal_address
    assert status["temporal_installed"] is True

    r = await client.put(URL, json={"orchestrator": "temporal"})
    assert r.status_code == 422 and "connect a Temporal server" in r.text

    r = await client.put(URL, json={"orchestrator": "builtin",
                                    "temporal": {"address": UNREACHABLE, "task_queue": "q"}})
    assert r.status_code == 200, r.text  # a connection can be saved before switching to it
    status = r.json()
    assert status["source"] == "console" and status["orchestrator"] == "builtin"
    assert "temporal" in status["driving"] and status["revision"] == 1 and status["updated_by"] == "system:admin"

    r = await client.put(URL, json={"orchestrator": "temporal", "temporal": {"address": UNREACHABLE}})
    assert r.status_code == 422 and "cannot switch runs to Temporal" in r.text
    assert (await client.get(URL)).json()["orchestrator"] == "builtin"  # nothing changed

    reset = await client.delete(URL)
    assert reset.status_code == 200 and reset.json()["source"] == "environment"
    actions = [a.action for a in await engine.store.list_audit(None, 10)]
    saved = "instance.orchestrator_switched" if engine.settings.orchestrator == "temporal" else "instance.orchestrator_updated"
    assert saved in actions and "instance.orchestrator_reset" in actions


async def test_credentials_come_from_a_tenants_shared_secrets(world):
    engine, tenant, client = world
    connection = {"address": UNREACHABLE, "api_key_secret": "temporal-api-key"}
    r = await client.post(f"{URL}/test", json=connection)
    assert r.status_code == 422 and "choose the tenant" in r.text

    connection["secrets_tenant"] = "ops"
    r = await client.post(f"{URL}/test", json=connection)
    assert r.json() == {"ok": False, "detail": "secret 'temporal-api-key' is not set in the shared namespace of tenant 'ops'",
                        "server_version": None, "pollers": None}
    r = await client.put(URL, json={"orchestrator": "builtin", "temporal": connection})
    assert r.status_code == 422 and "temporal-api-key" in r.text

    with use_namespace("default"):  # a namespace's secret is not a shared one
        await engine.secrets.writable().put(tenant.id, "temporal-api-key", "nope")
    assert "is not set" in (await client.post(f"{URL}/test", json=connection)).json()["detail"]

    await put_secret(engine, tenant, "temporal-api-key", "key-123")
    r = await client.post(f"{URL}/test", json=connection)
    assert r.json()["ok"] is False and "is not set" not in r.json()["detail"]  # resolved; the server is what is missing
    r = await client.put(URL, json={"orchestrator": "builtin", "temporal": connection})
    assert r.status_code == 200
    assert engine.orchestrators.active["temporal"].connection.api_key == "key-123"
    assert "key-123" not in r.text

    r = await client.post(f"{URL}/test", json={"address": UNREACHABLE, "tls_cert_secret": "c", "secrets_tenant": "ops"})
    assert r.status_code == 422 and "both the client certificate and the client key" in r.text


async def test_a_connection_with_unfinished_runs_cannot_be_dropped(world):
    engine, tenant, client = world
    if engine.settings.orchestrator == "temporal":
        pytest.skip("the environment's Temporal connection keeps driving these runs")
    assert (await client.put(URL, json={"temporal": {"address": UNREACHABLE}})).status_code == 200
    run = await engine.executor.start_run(tenant.id, "one", {})
    async with engine.store.session() as s:
        await s.execute(update(t.Run).where(t.Run.id == run.id).values(orchestrator="temporal"))
    assert (await client.get(URL)).json()["runs"] == {"temporal": 1}

    r = await client.put(URL, json={"orchestrator": "builtin"})
    assert r.status_code == 409 and "1 unfinished run(s) are on Temporal" in r.text
    assert (await client.delete(URL)).status_code == 409
    await engine.executor.request_cancel(tenant.id, run.id)
    async with engine.store.session() as s:
        await s.execute(update(t.Run).where(t.Run.id == run.id).values(status="cancelled"))
    assert (await client.delete(URL)).status_code == 200


async def test_a_broken_setting_keeps_runs_going_and_says_why(world):
    engine, tenant, client = world
    await put_secret(engine, tenant, "key", "k")
    body = {"temporal": {"address": UNREACHABLE, "secrets_tenant": "ops", "api_key_secret": "key"}}
    assert (await client.put(URL, json=body)).status_code == 200
    with use_namespace(SHARED_NAMESPACE):
        await engine.secrets.writable().delete(tenant.id, "key")
    await engine.store.put_instance_setting("orchestrator", {"orchestrator": "temporal", **body}, actor="someone")
    status = (await client.get(URL)).json()  # refreshes, cannot apply revision 2
    assert "secret 'key' is not set" in status["error"] and status["orchestrator"] == "builtin"
    run = await engine.executor.start_run(tenant.id, "one", {})
    assert run.orchestrator == "builtin"
    await engine.run_until_idle()
    assert (await engine.store.get_run(tenant.id, run.id)).status == "succeeded"


@needs_temporal
async def test_switching_to_temporal_and_back_without_a_restart(world, tmp_path):
    engine, tenant, client = world
    queue = f"shoal-console-{uuid.uuid4().hex[:12]}"
    connection = {"address": TEST_TEMPORAL_ADDRESS, "task_queue": queue}

    r = await client.post(f"{URL}/test", json=connection)
    assert r.json()["ok"] is True and r.json()["pollers"] == 0 and r.json()["server_version"], r.text

    # Another server on the same database (an API server, say) notices the switch by itself.
    other = Engine(engine.settings)
    await other.start()
    serving = asyncio.create_task(engine.orchestrators.serve())
    try:
        r = await client.put(URL, json={"orchestrator": "temporal", "temporal": connection})
        assert r.status_code == 200, r.text
        assert r.json()["orchestrator"] == "temporal" and r.json()["source"] == "console"
        await asyncio.sleep(0.5)
        assert (await client.post(f"{URL}/test", json=connection)).json()["pollers"] >= 1  # this worker polls the queue

        await other.orchestrators.refresh(force=True)
        assert other.orchestrator.name == "temporal" and other.orchestrator.connection.task_queue == queue
        run = await other.executor.start_run(tenant.id, "one", {})
        assert run.orchestrator == "temporal"
        await settled(engine, tenant.id, run.id, "succeeded")

        waiting = await engine.executor.start_run(tenant.id, "one", {})  # still Temporal's after the switch back
        r = await client.put(URL, json={"orchestrator": "builtin", "temporal": connection})
        assert r.status_code == 200 and r.json()["orchestrator"] == "builtin" and "temporal" in r.json()["driving"]
        await settled(engine, tenant.id, waiting.id, "succeeded")
        later = await engine.executor.start_run(tenant.id, "one", {})
        assert later.orchestrator == "builtin"
        await engine.run_until_idle()
        assert (await engine.store.get_run(tenant.id, later.id)).status == "succeeded"
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        await other.close()
