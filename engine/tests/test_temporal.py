"""The Temporal orchestrator.

The rest of the suite runs against Temporal too when SHOAL_TEST_TEMPORAL_ADDRESS is
set (see conftest); these tests cover what only Temporal does: real timers, cancels
from Temporal, recovering a missed start, and replaying histories.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from shoal.core import clock
from shoal.core.models import parse_resource
from shoal.store import tables as t

from .conftest import TEST_TEMPORAL_ADDRESS

needs_temporal = pytest.mark.skipif(not TEST_TEMPORAL_ADDRESS, reason="set SHOAL_TEST_TEMPORAL_ADDRESS")


def wf(name: str, steps: list[dict]) -> object:
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": name}, "spec": {"steps": steps}})


async def settled(engine, tenant_id: str, run_id: str, status: str, timeout: float = 20.0) -> t.Run:
    """Wait for a run to reach ``status`` with no help from run_until_idle."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        run = await engine.store.get_run(tenant_id, run_id)
        if run.status == status or loop.time() > deadline:
            assert run.status == status, run.error
            return run
        await asyncio.sleep(0.1)


async def test_builtin_workers_leave_other_orchestrators_runs_alone(engine, tenant):
    await engine.store.apply_resource(tenant.id, wf("one", [{"id": "s", "type": "set", "values": {"x": 1}}]))
    run = await engine.executor.start_run(tenant.id, "one", {})
    async with engine.store.session() as s:
        await s.execute(update(t.Run).where(t.Run.id == run.id).values(orchestrator="elsewhere"))
    assert await engine.store.claim_runs("w", limit=10, lease=timedelta(seconds=30)) == []
    assert [r.id for r in await engine.store.due_runs("elsewhere", clock.now())] == [run.id]


@needs_temporal
async def test_timed_wait_is_a_durable_timer(engine, tenant):
    from temporalio.api.enums.v1 import EventType

    await engine.store.apply_resource(tenant.id, wf("nap", [
        {"id": "nap", "type": "wait", "duration": "2s"},
        {"id": "after", "type": "set", "needs": ["nap"], "values": {"ok": True}},
    ]))
    run = await engine.executor.start_run(tenant.id, "nap", {})
    assert run.orchestrator == "temporal"
    await settled(engine, tenant.id, run.id, "waiting")
    await settled(engine, tenant.id, run.id, "succeeded")  # woken by Temporal's timer, not by a poll

    handle = engine.orchestrator.client.get_workflow_handle(f"shoal-run-{run.id}")
    assert await handle.result() == "succeeded"
    events = [e.event_type async for e in handle.fetch_history_events()]
    assert EventType.EVENT_TYPE_TIMER_FIRED in events
    assert events.count(EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED) >= 2


@needs_temporal
async def test_cancelling_the_workflow_cancels_the_run(engine, tenant):
    from temporalio.client import WorkflowExecutionStatus

    await engine.store.apply_resource(tenant.id, wf("gated", [{"id": "ok", "type": "approval", "message": "Ship it?"}]))
    run = await engine.executor.start_run(tenant.id, "gated", {})
    await engine.run_until_idle()
    assert (await engine.store.get_step(run.id, "ok")).status == "waiting"

    handle = engine.orchestrator.client.get_workflow_handle(f"shoal-run-{run.id}")
    await handle.cancel()
    run = await settled(engine, tenant.id, run.id, "cancelled")
    assert (await engine.store.get_step(run.id, "ok")).status == "cancelled"
    for _ in range(100):
        if (await handle.describe()).status == WorkflowExecutionStatus.CANCELED:
            break
        await asyncio.sleep(0.1)
    assert (await handle.describe()).status == WorkflowExecutionStatus.CANCELED


@needs_temporal
async def test_a_run_whose_start_was_missed_is_picked_up(engine, tenant, monkeypatch):
    await engine.store.apply_resource(tenant.id, wf("one", [{"id": "s", "type": "set", "values": {"x": 1}}]))
    client = engine.orchestrator.client

    class Down:
        async def start_workflow(self, *args, **kwargs):
            raise RuntimeError("temporal is down")

    engine.orchestrator.client = Down()
    run = await engine.executor.start_run(tenant.id, "one", {})  # the run is created all the same
    engine.orchestrator.client = client
    await asyncio.sleep(0.5)
    assert (await engine.store.get_run(tenant.id, run.id)).status == "pending"

    await engine.orchestrator.tick()  # not overdue yet
    assert (await engine.store.get_run(tenant.id, run.id)).status == "pending"
    clock.advance(timedelta(seconds=engine.settings.temporal_reconcile_after_seconds + 1))
    await engine.orchestrator.tick()
    await settled(engine, tenant.id, run.id, "succeeded")


@needs_temporal
async def test_histories_replay_deterministically(engine, tenant):
    from temporalio.worker import Replayer

    from shoal.runtime.temporal.workflow import RunWorkflow

    await engine.store.apply_resource(tenant.id, wf("talk", [
        {"id": "nap", "type": "wait", "duration": "1s"},
        {"id": "reply", "type": "wait", "needs": ["nap"], "signal": {"name": "reply"}},
        {"id": "done", "type": "set", "needs": ["reply"], "values": {"said": "{{ steps.reply.output.payload }}"}},
    ]))
    run = await engine.executor.start_run(tenant.id, "talk", {})
    await settled(engine, tenant.id, run.id, "waiting")
    await engine.signals.send(run, "reply", "hello", source="api")
    await settled(engine, tenant.id, run.id, "succeeded")
    assert (await engine.store.get_step(run.id, "done")).output == {"said": "hello"}

    history = await engine.orchestrator.client.get_workflow_handle(f"shoal-run-{run.id}").fetch_history()
    await Replayer(workflows=[RunWorkflow]).replay_workflow(history)
