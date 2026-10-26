"""Dispatch queues: wake-ups through Amazon SQS (an in-memory fake, or a real endpoint such as LocalStack).

Run the live test against LocalStack (or ``moto_server``) with e.g.
    SHOAL_TEST_SQS_ENDPOINT=http://localhost:4566 uv run pytest tests/test_dispatch_queues.py
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from shoal.cloud.aws import AmbientCredentials, sigv4_headers
from shoal.config import Settings
from shoal.core import clock
from shoal.core.models import parse_resource
from shoal.engine import Engine
from shoal.runtime.queues import QueueError, Wakeup
from shoal.runtime.queues.sqs import SQSQueue
from shoal.sdk.testing import check_dispatch_queue
from tests.conftest import TEST_DATABASE_URL, _reset_postgres
from tests.fake_sqs import FakeSQS

TEST_SQS_ENDPOINT = os.environ.get("SHOAL_TEST_SQS_ENDPOINT")


def workflow(name: str, steps: list[dict]) -> object:
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": name},
                           "spec": {"steps": steps}})


SET = [{"id": "a", "type": "set", "values": {"x": 1}}]


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)


@pytest.fixture
def sqs():
    return FakeSQS()


@pytest.fixture
async def make_engine(tmp_path, monkeypatch, aws_env, sqs):
    monkeypatch.setenv("SHOAL_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    if TEST_DATABASE_URL:
        await _reset_postgres(TEST_DATABASE_URL)
    engines: list[Engine] = []

    async def make(**overrides) -> Engine:
        config = {
            "database_url": TEST_DATABASE_URL or f"sqlite+aiosqlite:///{tmp_path}/shoal.db", "public_url": "http://shoal.test",
            "dispatch_queue": "sqs", "dispatch_sqs_queue_url": sqs.create("shoal"), "dispatch_wait_seconds": 1,
            **overrides,
        }
        engine = Engine(Settings(**config), http_transport=httpx.MockTransport(sqs.handle))
        await engine.start()
        engines.append(engine)
        return engine

    yield make
    for engine in engines:
        await engine.close()
    clock.reset()


async def settle(engine: Engine) -> None:
    await engine.dispatch.flush()


async def advance_from_queue(engine: Engine, *, lanes=("standard", "agent"), limit: int = 10) -> list[str]:
    """One receive and claim, then advance what it claimed (what a worker's queue loop does)."""
    await settle(engine)
    claimed: list[str] = []
    for group in engine.dispatch.queue.groups(list(lanes)):
        claimed += await engine.dispatch.consume(engine.worker_id, group, limit=limit, wait=0)
    await asyncio.gather(*(engine.executor.advance(r, engine.worker_id) for r in claimed))
    await settle(engine)
    return claimed


# --------------------------------------------------------------------------- the wire


async def test_wake_ups_are_signed_json_calls_grouped_by_tenant(make_engine, sqs):
    engine = await make_engine()
    tenant = await engine.store.create_tenant("acme")
    await engine.store.apply_resource(tenant.id, workflow("w", SET))
    run = await engine.executor.start_run(tenant.id, "w")
    await settle(engine)

    action, payload, headers = sqs.calls[-1]
    assert action == "SendMessageBatch"
    assert headers["content-type"] == "application/x-amz-json-1.0"
    assert "/eu-west-1/sqs/aws4_request" in headers["authorization"]
    (entry,) = payload["Entries"]
    assert entry["MessageGroupId"] == tenant.id and entry["DelaySeconds"] == 0
    assert json.loads(entry["MessageBody"]) | {"due": None} == {
        "v": 1, "run": run.id, "tenant": tenant.id, "lane": "standard", "due": None}


async def test_fair_queues_can_be_turned_off_and_fifo_queues_are_refused(make_engine, sqs):
    engine = await make_engine(dispatch_sqs_fair_queues=False)
    await engine.dispatch.publish([Wakeup("run_x", "ten_x", "standard", clock.now())])
    await settle(engine)
    assert "MessageGroupId" not in sqs.calls[-1][1]["Entries"][0]

    queue = SQSQueue()
    queue.setup(SimpleNamespace(settings=Settings(dispatch_sqs_queue_url=sqs.create("shoal.fifo")), http_transport=None))
    with pytest.raises(QueueError, match="not FIFO"):
        await queue.start()
    queue.setup(SimpleNamespace(settings=Settings(), http_transport=None))
    with pytest.raises(QueueError, match="DISPATCH_SQS_QUEUE_URL"):
        await queue.start()


async def test_contract(make_engine):
    engine = await make_engine()
    await check_dispatch_queue(engine.dispatch.queue, wait=0.2)


# --------------------------------------------------------------------------- runs moved by wake-ups


async def test_a_run_is_advanced_from_its_wake_up(make_engine, sqs):
    engine = await make_engine()
    tenant = await engine.store.create_tenant("acme")
    await engine.store.apply_resource(tenant.id, workflow("w", SET))
    run = await engine.executor.start_run(tenant.id, "w")

    assert await advance_from_queue(engine) == [run.id]
    assert (await engine.store.get_run(tenant.id, run.id)).status == "succeeded"
    assert sqs.queues[engine.settings.dispatch_sqs_queue_url] == []  # acked


async def test_timers_wait_on_the_queue_beyond_its_longest_delay(make_engine, sqs):
    engine = await make_engine()
    url = engine.settings.dispatch_sqs_queue_url
    tenant = await engine.store.create_tenant("acme")
    await engine.store.apply_resource(tenant.id, workflow("w", [
        {"id": "nap", "type": "wait", "duration": "40m"}, {"id": "after", "type": "set", "values": {"x": 1}, "needs": ["nap"]},
    ]))
    run = await engine.executor.start_run(tenant.id, "w")
    assert await advance_from_queue(engine) == [run.id]
    (parked,) = sqs.messages(url)
    assert 899 <= parked["_delay"] <= 900  # 40 minutes, capped at SQS's 15

    clock.advance(timedelta(minutes=15, seconds=1))
    assert await advance_from_queue(engine) == []  # early: sent again for the rest of the wait
    (resent,) = sqs.messages(url)
    assert resent["run"] == run.id and 899 <= resent["_delay"] <= 900

    clock.advance(timedelta(minutes=15, seconds=1))
    assert await advance_from_queue(engine) == []
    clock.advance(timedelta(minutes=10))
    assert await advance_from_queue(engine) == [run.id]
    assert (await engine.store.get_run(tenant.id, run.id)).status == "succeeded"
    assert sqs.messages(url) == []


async def test_signals_wake_waiting_runs_and_stale_wake_ups_are_dropped(make_engine, sqs):
    engine = await make_engine()
    url = engine.settings.dispatch_sqs_queue_url
    tenant = await engine.store.create_tenant("acme")
    await engine.store.apply_resource(tenant.id, workflow("w", [
        {"id": "nap", "type": "wait", "duration": "2h"}, {"id": "after", "type": "set", "values": {"x": 1}, "needs": ["nap"]},
    ]))
    run = await engine.executor.start_run(tenant.id, "w")
    await advance_from_queue(engine)
    assert len(sqs.messages(url)) == 1

    await engine.store.wake_run(run.id)  # e.g. a signal arrived: due now
    await settle(engine)
    assert len(sqs.messages(url)) == 2
    assert await advance_from_queue(engine) == [run.id]  # still waiting (the timer has not passed), parked again
    clock.advance(timedelta(minutes=16))
    # the first timer's copy and the new park's copy: one is current and sent again, the other dropped
    await advance_from_queue(engine)
    assert len(sqs.messages(url)) == 1


async def test_runs_held_by_another_worker_or_finished_are_acked(make_engine, sqs):
    engine = await make_engine()
    url = engine.settings.dispatch_sqs_queue_url
    tenant = await engine.store.create_tenant("acme")
    await engine.store.apply_resource(tenant.id, workflow("w", SET))
    held = await engine.executor.start_run(tenant.id, "w")
    done = await engine.executor.start_run(tenant.id, "w")
    await settle(engine)
    assert await engine.store.claim_runs("other-worker", limit=1, lease=timedelta(minutes=1), run_ids=[held.id])
    await engine.executor.advance(done.id, "elsewhere")
    await engine.dispatch.publish([Wakeup(done.id, tenant.id, "standard", clock.now())])

    assert await advance_from_queue(engine) == []
    assert sqs.messages(url) == [] or all(m["run"] == done.id for m in sqs.messages(url))
    assert held.id not in [m["run"] for m in sqs.messages(url)]


async def test_tenants_take_turns_and_capped_tenants_wait_on_the_queue(make_engine, sqs):
    engine = await make_engine()
    url = engine.settings.dispatch_sqs_queue_url
    big = await engine.store.create_tenant("big")
    small = await engine.store.create_tenant("small")
    for tenant in (big, small):
        await engine.store.apply_resource(tenant.id, workflow("w", SET))
    await engine.store.update_tenant_settings(big.id, quotas={"max_concurrent_runs": 1})
    big_runs = [await engine.executor.start_run(big.id, "w") for _ in range(4)]
    small_run = await engine.executor.start_run(small.id, "w")
    await settle(engine)

    claimed = await engine.dispatch.consume(engine.worker_id, ("standard",), limit=10, wait=0)
    assert small_run.id in claimed and len([r for r in claimed if r in {b.id for b in big_runs}]) == 1
    waiting = [m for m in sqs.queues[url]]
    assert len(waiting) == 3 and all(m.visible_at > clock.now() for m in waiting)  # handed back for a moment
    assert {m.group for m in waiting} == {big.id}

    await asyncio.gather(*(engine.executor.advance(r, engine.worker_id) for r in claimed))
    clock.advance(timedelta(seconds=2))
    again = await engine.dispatch.consume(engine.worker_id, ("standard",), limit=10, wait=0)
    assert len(again) == 1


async def test_a_queue_per_lane(make_engine, sqs):
    agent_url = sqs.create("shoal-agent")
    engine = await make_engine(dispatch_sqs_lane_queue_urls={"agent": agent_url})
    assert engine.dispatch.queue.groups(["standard", "agent"]) == [("standard",), ("agent",)]
    await engine.dispatch.publish([Wakeup("run_a", "ten", "agent", clock.now()),
                                   Wakeup("run_s", "ten", "standard", clock.now())])
    await settle(engine)
    assert [m["run"] for m in sqs.messages(agent_url)] == ["run_a"]
    assert [m["run"] for m in sqs.messages(engine.settings.dispatch_sqs_queue_url)] == ["run_s"]


# --------------------------------------------------------------------------- when the queue fails


async def test_a_queue_outage_never_blocks_runs(make_engine, sqs):
    engine = await make_engine(dispatch_sweep_seconds=0.05, poll_interval_seconds=0.05)
    tenant = await engine.store.create_tenant("acme")
    await engine.store.apply_resource(tenant.id, workflow("w", SET))
    sqs.fail = True
    run = await engine.executor.start_run(tenant.id, "w")  # publishing fails in the background
    await settle(engine)
    engine.start_background_worker()  # the queue loop keeps failing; the sweep finds the run
    for _ in range(100):
        if (await engine.store.get_run(tenant.id, run.id)).status == "succeeded":
            break
        await asyncio.sleep(0.05)
    assert (await engine.store.get_run(tenant.id, run.id)).status == "succeeded"


async def test_a_worker_runs_from_the_queue_without_polling_the_database(make_engine, sqs):
    engine = await make_engine(dispatch_sweep_seconds=3600)
    tenant = await engine.store.create_tenant("acme")
    await engine.store.apply_resource(tenant.id, workflow("w", SET))
    engine.start_background_worker()
    await asyncio.sleep(0.2)  # the first sweep has run
    runs = [await engine.executor.start_run(tenant.id, "w") for _ in range(3)]
    for _ in range(100):
        statuses = {(await engine.store.get_run(tenant.id, r.id)).status for r in runs}
        if statuses == {"succeeded"}:
            break
        await asyncio.sleep(0.05)
    assert statuses == {"succeeded"}
    assert "ReceiveMessage" in sqs.actions() and "DeleteMessageBatch" in sqs.actions()


async def test_the_database_queue_sends_nothing(engine, tenant):
    assert engine.dispatch.queue.name == "database" and not engine.dispatch.external
    assert engine.store.due_hooks == []


# --------------------------------------------------------------------------- a real SQS endpoint


@pytest.mark.skipif(not TEST_SQS_ENDPOINT, reason="set SHOAL_TEST_SQS_ENDPOINT (LocalStack, moto_server) to run")
async def test_live_sqs_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", os.environ.get("AWS_ACCESS_KEY_ID", "test"))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", os.environ.get("AWS_SECRET_ACCESS_KEY", "test"))
    monkeypatch.setenv("SHOAL_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    region = os.environ.get("AWS_REGION", "us-east-1")
    async with httpx.AsyncClient() as client:
        creds = await AmbientCredentials(SimpleNamespace(http_transport=None)).get()
        body = json.dumps({"QueueName": f"shoal-test-{uuid.uuid4().hex[:8]}"}).encode()
        headers = sigv4_headers("POST", TEST_SQS_ENDPOINT, body, creds, region, "sqs", clock.now(), {
            "Content-Type": "application/x-amz-json-1.0", "X-Amz-Target": "AmazonSQS.CreateQueue"})
        response = await client.post(TEST_SQS_ENDPOINT, content=body, headers=headers)
        assert response.status_code == 200, response.text
        url = response.json()["QueueUrl"]

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/shoal.db", dispatch_queue="sqs", dispatch_sqs_queue_url=url,
        dispatch_sqs_endpoint=TEST_SQS_ENDPOINT, dispatch_sqs_region=region, dispatch_wait_seconds=1,
        dispatch_sweep_seconds=3600,
    )
    engine = Engine(settings)
    await engine.start()
    try:
        await check_dispatch_queue(engine.dispatch.queue, wait=1)
        await asyncio.sleep(0)  # the contract leaves one delayed message: the worker drops it (no such run)
        tenant = await engine.store.create_tenant("acme")
        await engine.store.apply_resource(tenant.id, workflow("w", SET))
        engine.start_background_worker()
        run = await engine.executor.start_run(tenant.id, "w")
        for _ in range(100):
            if (await engine.store.get_run(tenant.id, run.id)).status == "succeeded":
                break
            await asyncio.sleep(0.1)
        assert (await engine.store.get_run(tenant.id, run.id)).status == "succeeded"
    finally:
        await engine.close()
