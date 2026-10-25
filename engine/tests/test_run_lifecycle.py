"""Run lifecycle edges: failing parents and their children, approvers, schedule edits, overlap, campaign resume."""

from __future__ import annotations

from datetime import timedelta

import pytest

from shoal.auth.rbac import Principal
from shoal.core import clock
from shoal.core.errors import Forbidden, InvalidRequest
from shoal.core.models import parse_resource
from shoal.core.scope import use_namespace


def res(kind: str, name: str, spec: dict) -> object:
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": kind, "metadata": {"name": name}, "spec": spec})


async def test_failed_or_timed_out_parent_cancels_children_and_steps(engine, tenant):
    await engine.store.apply_resource(tenant.id, res("Workflow", "child", {"steps": [{"id": "w", "type": "wait", "duration": "7d"}]}))
    await engine.store.apply_resource(tenant.id, res("Workflow", "fails", {"steps": [
        {"id": "c", "type": "workflow", "workflow": "child"},
        {"id": "w", "type": "wait", "duration": "1h", "needs": []},
        {"id": "boom", "type": "script", "run": "exit 1", "needs": ["w"]},
    ]}))
    await engine.store.apply_resource(tenant.id, res("Workflow", "slow", {"timeout": "1h", "steps": [
        {"id": "w", "type": "wait", "duration": "7d"},
        {"id": "c", "type": "workflow", "workflow": "child", "needs": []},
    ]}))
    failing = await engine.executor.start_run(tenant.id, "fails", {})
    slow = await engine.executor.start_run(tenant.id, "slow", {})
    await engine.run_until_idle()
    clock.advance(timedelta(hours=2))
    await engine.run_until_idle()

    for run, status in ((failing, "failed"), (slow, "failed")):
        run = await engine.store.get_run(tenant.id, run.id)
        assert run.status == status
        children = await engine.store.list_runs(tenant.id, parent_run_id=run.id)
        assert children and all(c.status == "cancelled" for c in children)
        assert all(s.status in ("succeeded", "failed", "cancelled") for s in await engine.store.get_steps(run.id))
    assert {s.step_id: s.status for s in await engine.store.get_steps(slow.id)} == {"w": "cancelled", "c": "cancelled"}


async def test_approvers_are_enforced(engine, tenant):
    await engine.store.apply_resource(tenant.id, res("Workflow", "gated", {"steps": [
        {"id": "ok", "type": "approval", "message": "Ship it?", "approvers": ["dana@acme.com", "group:release"]},
    ]}))
    directory = engine.directory
    dana = await directory.create_user(tenant.id, "dana", email="Dana@acme.com")
    sam = await directory.create_user(tenant.id, "sam", email="sam@acme.com")
    eve = await directory.create_user(tenant.id, "eve", email="eve@acme.com")
    release = await directory.create_group(tenant.id, "release")
    await directory.set_group_members(tenant.id, release.id, [sam.id])

    def who(user) -> Principal:
        return Principal(tenant_id=tenant.id, actor=f"user:{user.username}", user_id=user.id, email=user.email)

    run = await engine.executor.start_run(tenant.id, "gated", {})
    await engine.run_until_idle()
    with pytest.raises(Forbidden):
        await engine.executor.submit_approval(tenant.id, run.id, "ok", approved=True, by="eve", decider=who(eve))
    assert await engine.executor.may_approve(tenant.id, ["user:dana@acme.com"], who(dana))
    assert await engine.executor.may_approve(tenant.id, ["group:release"], who(sam))
    assert await engine.executor.may_approve(tenant.id, ["sam@acme.com"], Principal.system(tenant.id))
    await engine.executor.submit_approval(tenant.id, run.id, "ok", approved=True, by="sam", decider=who(sam))
    await engine.run_until_idle()
    assert (await engine.store.get_run(tenant.id, run.id)).status == "succeeded"


async def test_editing_a_schedule_reschedules(engine, tenant):
    await engine.store.apply_resource(tenant.id, res("Workflow", "hello", {"steps": [{"id": "a", "type": "set", "values": {}}]}))
    await engine.store.apply_resource(tenant.id, res("Trigger", "tick", {"workflow": "hello", "event": "schedule", "schedule": "0 0 1 * *"}))
    await engine.triggers.tick_schedules()
    monthly = (await engine.store.get_trigger_state(tenant.id, "tick")).next_fire_at
    await engine.store.apply_resource(tenant.id, res("Trigger", "tick", {"workflow": "hello", "event": "schedule", "schedule": "*/5 * * * *"}))
    await engine.triggers.tick_schedules()
    state = await engine.store.get_trigger_state(tenant.id, "tick")
    assert state.next_fire_at < monthly and state.next_fire_at - clock.now() <= timedelta(minutes=5)
    clock.advance(timedelta(minutes=5, seconds=1))
    assert await engine.triggers.tick_schedules() == 1


async def test_overlap_skip_is_per_namespace(engine, tenant):
    fired = []
    for ns in ("default", "team"):
        with use_namespace(ns):
            await engine.store.apply_resource(tenant.id, res("Workflow", "sleepy", {"steps": [{"id": "w", "type": "wait", "duration": "1d"}]}), namespace=ns)
            await engine.store.apply_resource(tenant.id, res("Trigger", "nightly", {
                "workflow": "sleepy", "event": "schedule", "schedule": "0 0 * * *", "overlap": "skip",
            }), namespace=ns)
            fired.append((await engine.triggers.fire_manual(tenant.id, "nightly"))["status"])
    with use_namespace("team"):
        fired.append((await engine.triggers.fire_manual(tenant.id, "nightly"))["status"])
    assert fired == ["fired", "fired", "skipped"]


async def test_resume_after_max_failures_continues(engine, tenant):
    await engine.store.apply_resource(tenant.id, res("Workflow", "maybe", {
        "inputs": {"ok": {"type": "boolean"}},
        "steps": [{"id": "s", "type": "script", "run": "{{ 'true' if inputs.ok else 'false' }}"}],
    }))
    items = [{"name": f"t{i}", "ok": i not in (0, 1, 4)} for i in range(6)]
    await engine.store.apply_resource(tenant.id, res("Campaign", "c", {
        "workflow": "maybe", "targets": {"items": items, "key": "{{ target.name }}"}, "inputs": {"ok": "{{ target.ok }}"},
        "policy": {"concurrency": 1, "max_failures": 2},
    }))
    row = await engine.campaigns.launch(tenant.id, "c")

    async def settle() -> object:
        for _ in range(8):
            await engine.run_until_idle()
            clock.advance(timedelta(seconds=31))
        return await engine.store.get_campaign(tenant.id, row.id)

    paused = await settle()
    assert paused.status == "paused" and paused.stats["failed"] == 2
    await engine.campaigns.resume(tenant.id, row.id)
    assert (await engine.store.get_campaign(tenant.id, row.id)).error is None
    done = await settle()
    assert done.status == "completed", done.error
    assert done.stats["succeeded"] == 3 and done.stats["failed"] == 3


async def test_launch_that_cannot_resolve_targets_leaves_nothing(engine, tenant):
    await engine.store.apply_resource(tenant.id, res("Workflow", "hello", {"steps": [{"id": "a", "type": "set", "values": {}}]}))
    await engine.store.apply_resource(tenant.id, res("Campaign", "repos", {
        "workflow": "hello", "targets": {"connection": "missing", "resource": "repository", "key": "{{ target.name }}"},
    }))
    with pytest.raises(InvalidRequest):
        await engine.campaigns.launch(tenant.id, "repos")
    assert list(await engine.store.list_campaigns(tenant.id)) == []
