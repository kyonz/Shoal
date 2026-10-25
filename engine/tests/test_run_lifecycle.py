"""Run lifecycle edges: failing parents and their children, approvers, schedule edits, overlap, campaign resume."""

from __future__ import annotations

import asyncio
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


async def test_rejection_stops_the_run_unless_on_reject_continue(engine, tenant):
    for name, extra in (("strict", {}), ("branching", {"on_reject": "continue"})):
        await engine.store.apply_resource(tenant.id, res("Workflow", name, {"steps": [
            {"id": "ok", "type": "approval", "message": "Ship it?", **extra},
            {"id": "ship", "type": "set", "values": {"shipped": True}},
        ]}))
    strict = await engine.executor.start_run(tenant.id, "strict", {})
    branching = await engine.executor.start_run(tenant.id, "branching", {})
    await engine.run_until_idle()
    for run in (strict, branching):
        await engine.executor.submit_approval(tenant.id, run.id, "ok", approved=False, by="dana", comment="not yet")
    await engine.run_until_idle()

    strict = await engine.store.get_run(tenant.id, strict.id)
    assert strict.status == "failed" and strict.error["message"] == "rejected by dana: not yet"
    steps = {s.step_id: s for s in await engine.store.get_steps(strict.id)}
    assert steps["ok"].error["message"] == "rejected by dana: not yet" and steps["ship"].status == "cancelled"
    assert (await engine.store.get_run(tenant.id, branching.id)).status == "succeeded"
    assert (await engine.store.get_step(branching.id, "ok")).output["approved"] is False

    await engine.store.apply_resource(tenant.id, res("Workflow", "expiring", {"steps": [
        {"id": "ok", "type": "approval", "message": "Ship it?", "max_wait": "1h"},
    ]}))
    expiring = await engine.executor.start_run(tenant.id, "expiring", {})
    await engine.run_until_idle()
    clock.advance(timedelta(hours=1, seconds=1))
    await engine.run_until_idle()
    assert (await engine.store.get_step(expiring.id, "ok")).error["message"].startswith("not approved within")


async def test_approval_decision_is_compare_and_set(engine, tenant):
    await engine.store.apply_resource(tenant.id, res("Workflow", "gated", {"steps": [{"id": "ok", "type": "approval", "message": "?"}]}))
    run = await engine.executor.start_run(tenant.id, "gated", {})
    await engine.run_until_idle()
    stale = await engine.store.get_step(run.id, "ok")
    await engine.executor.submit_approval(tenant.id, run.id, "ok", approved=True, by="dana")
    # a second decider who read the step before the first decision landed loses
    assert not await engine.store.update_step_if_unchanged(stale, state={**stale.state, "decision": {"approved": False}})
    assert (await engine.store.get_step(run.id, "ok")).state["decision"]["approved"] is True


async def test_poison_run_backs_off_then_fails(engine, tenant, monkeypatch):
    await engine.store.apply_resource(tenant.id, res("Workflow", "hello", {"steps": [{"id": "a", "type": "set", "values": {}}]}))
    run = await engine.executor.start_run(tenant.id, "hello", {})

    async def boom(run):
        raise RuntimeError("bug")

    monkeypatch.setattr(engine.executor, "_advance_run", boom)
    for _ in range(200):  # take the lease like a worker (with Temporal, its own passes fail too and count)
        row = await engine.store.get_run(tenant.id, run.id)
        if row.status == "failed":
            break
        if await engine.store.lease_run(run.id, "w1", timedelta(seconds=30)):
            await engine.executor.advance(run.id, "w1")
        else:
            await asyncio.sleep(0.05)
    row = await engine.store.get_run(tenant.id, run.id)
    assert row.status == "failed" and "10 times in a row" in row.error["message"]
    errors = [e.data for e in await engine.store.list_events(tenant.id, run.id) if e.type == "run.error"]
    assert [e["attempt"] for e in errors] == list(range(1, 10))
    assert [e["retry_in_seconds"] for e in errors] == [30, 60, 120, 240, 480, 960, 1920, 3600, 3600]


async def test_retry_reruns_only_failed_children(engine, tenant, tmp_path):
    mark = tmp_path / "failed-once"
    await engine.store.apply_resource(tenant.id, res("Workflow", "child", {
        "inputs": {"n": {"type": "integer"}},
        # n=2 fails the first time only
        "steps": [{"id": "s", "type": "script", "run": f"test {{{{ inputs.n }}}} -ne 2 || test -f {mark} || {{ touch {mark}; exit 1; }}"}],
    }))
    await engine.store.apply_resource(tenant.id, res("Workflow", "fan", {"steps": [{
        "id": "each", "type": "workflow", "workflow": "child", "for_each": "{{ [1, 2, 3] }}", "with": {"n": "{{ item }}"},
        "retry": {"max_attempts": 2, "backoff": "1s"},
    }]}))
    run = await engine.executor.start_run(tenant.id, "fan", {})
    for _ in range(20):
        await engine.run_until_idle()
        if (await engine.store.get_run(tenant.id, run.id)).status == "succeeded":
            break
        clock.advance(timedelta(seconds=2))
        await asyncio.sleep(0.1)  # with Temporal, wakes from finished children arrive asynchronously
    run = await engine.store.get_run(tenant.id, run.id)
    step = await engine.store.get_step(run.id, "each")
    children = await engine.store.list_runs(tenant.id, parent_run_id=run.id)
    assert run.status == "succeeded", step.error
    assert step.attempt == 2 and len(children) == 4  # 3 first time, only n=2 again


async def test_budget_counts_runs_still_in_flight(engine, tenant):
    await engine.store.apply_resource(tenant.id, res("Workflow", "slow", {"steps": [{"id": "w", "type": "wait", "duration": "1d"}]}))
    await engine.store.apply_resource(tenant.id, res("Campaign", "c", {
        "workflow": "slow", "targets": {"items": [{"name": f"t{i}"} for i in range(4)], "key": "{{ target.name }}"},
        "policy": {"concurrency": 2, "budget_usd": 1.0},
    }))
    row = await engine.campaigns.launch(tenant.id, "c")
    await engine.run_until_idle()
    for run in await engine.store.list_runs(tenant.id, campaign_id=row.id):
        await engine.store.update_run(run.id, usage={"cost_usd": 0.6})
    clock.advance(timedelta(seconds=31))
    await engine.run_until_idle()
    row = await engine.store.get_campaign(tenant.id, row.id)
    assert row.stats["cost_usd"] == 1.2 and row.status == "paused" and row.stats["pending"] == 2
