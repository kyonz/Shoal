from __future__ import annotations

from datetime import timedelta

from shoal.core import clock
from shoal.core.models import Agent, Workflow, parse_resource


def wf(doc: dict) -> Workflow:
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": "Workflow", **doc})


async def test_sequential_and_outputs(engine, tenant):
    await engine.store.apply_resource(
        tenant.id,
        wf(
            {
                "metadata": {"name": "hello"},
                "spec": {
                    "inputs": {"name": {"type": "string"}},
                    "outputs": {"greeting": "{{ steps.greet.output.text }}", "echo": "{{ steps.sh.output.stdout | trim }}"},
                    "steps": [
                        {"id": "greet", "type": "set", "values": {"text": "Hello {{ inputs.name }}"}},
                        {"id": "sh", "type": "script", "run": "echo '{{ steps.greet.output.text }}'"},
                    ],
                },
            }
        ),
    )
    run = await engine.executor.start_run(tenant.id, "hello", {"name": "Trevor"})
    await engine.run_until_idle()
    run = await engine.store.get_run(tenant.id, run.id)
    assert run.status == "succeeded", run.error
    assert run.outputs == {"greeting": "Hello Trevor", "echo": "Hello Trevor"}


async def test_if_skip_and_parallel_branches(engine, tenant):
    await engine.store.apply_resource(
        tenant.id,
        wf(
            {
                "metadata": {"name": "branches"},
                "spec": {
                    "inputs": {"flag": {"type": "boolean", "default": False}},
                    "steps": [
                        {"id": "a", "type": "set", "values": {"x": 1}},
                        {"id": "b", "type": "set", "needs": ["a"], "if": "{{ inputs.flag }}", "values": {"y": 2}},
                        {"id": "c", "type": "set", "needs": ["a"], "values": {"z": 3}},
                        {"id": "d", "type": "set", "needs": ["b", "c"], "values": {"b": "{{ steps.b.status }}"}},
                    ],
                },
            }
        ),
    )
    run = await engine.executor.start_run(tenant.id, "branches", {})
    await engine.run_until_idle()
    steps = {s.step_id: s for s in await engine.store.get_steps(run.id)}
    assert steps["b"].status == "skipped"
    assert steps["d"].output == {"b": "skipped"}


async def test_retry_then_fail(engine, tenant):
    await engine.store.apply_resource(
        tenant.id,
        wf(
            {
                "metadata": {"name": "flaky"},
                "spec": {
                    "steps": [
                        {"id": "boom", "type": "script", "run": "exit 3", "retry": {"max_attempts": 2, "backoff": "1s"}},
                    ]
                },
            }
        ),
    )
    run = await engine.executor.start_run(tenant.id, "flaky", {})
    await engine.run_until_idle()
    step = await engine.store.get_step(run.id, "boom")
    # script failures are not retryable by default (deterministic), so one attempt
    assert step.status == "failed" and step.attempt == 1
    assert (await engine.store.get_run(tenant.id, run.id)).status == "failed"


async def test_durable_wait_survives_and_resumes(engine, tenant):
    await engine.store.apply_resource(
        tenant.id,
        wf(
            {
                "metadata": {"name": "sleepy"},
                "spec": {"steps": [{"id": "nap", "type": "wait", "duration": "7d"}, {"id": "after", "type": "set", "values": {"ok": True}}]},
            }
        ),
    )
    run = await engine.executor.start_run(tenant.id, "sleepy", {})
    await engine.run_until_idle()
    run = await engine.store.get_run(tenant.id, run.id)
    assert run.status == "waiting"
    clock.advance(timedelta(days=7, seconds=1))
    await engine.run_until_idle()
    assert (await engine.store.get_run(tenant.id, run.id)).status == "succeeded"


async def test_approval_and_cancel(engine, tenant):
    await engine.store.apply_resource(
        tenant.id,
        wf(
            {
                "metadata": {"name": "gated"},
                "spec": {
                    "steps": [
                        {"id": "ok", "type": "approval", "message": "Ship it?"},
                        {"id": "done", "type": "set", "if": "{{ steps.ok.output.approved }}", "values": {"shipped": True}},
                    ]
                },
            }
        ),
    )
    run = await engine.executor.start_run(tenant.id, "gated", {})
    await engine.run_until_idle()
    step = await engine.store.get_step(run.id, "ok")
    assert step.status == "waiting"
    step.state = {**step.state, "decision": {"approved": True, "by": "trevor"}}
    await engine.store.save_step(step)
    await engine.store.wake_by_keys(tenant.id, [f"approval:{run.id}:ok"])
    await engine.run_until_idle()
    assert (await engine.store.get_step(run.id, "done")).output == {"shipped": True}

    run2 = await engine.executor.start_run(tenant.id, "gated", {})
    await engine.run_until_idle()
    await engine.executor.request_cancel(tenant.id, run2.id)
    await engine.run_until_idle()
    assert (await engine.store.get_run(tenant.id, run2.id)).status == "cancelled"


async def test_agent_step_with_scripted_provider(engine, tenant):
    await engine.store.apply_resource(
        tenant.id,
        parse_resource(
            {
                "apiVersion": "shoal.dev/v1",
                "kind": "Agent",
                "metadata": {"name": "writer"},
                "spec": {
                    "provider": "scripted",
                    "provider_config": {
                        "script": [
                            {"text": "Writing", "tool_calls": [{"name": "write_file", "input": {"path": "out.txt", "content": "hi"}}]},
                            {"tool_calls": [{"name": "shell", "input": {"command": "cat out.txt"}}]},
                        ],
                        "final_outputs": {"content": "{{ last_result.stdout }}", "summary": "wrote a file"},
                    },
                },
            }
        ),
    )
    await engine.store.apply_resource(
        tenant.id,
        wf(
            {
                "metadata": {"name": "agentic"},
                "spec": {
                    "steps": [
                        {"id": "write", "type": "agent", "agent": "writer", "prompt": "write hi", "outputs": {"content": {"type": "string"}}},
                        {"id": "check", "type": "script", "run": "cat out.txt"},
                    ]
                },
            }
        ),
    )
    run = await engine.executor.start_run(tenant.id, "agentic", {})
    await engine.run_until_idle()
    run = await engine.store.get_run(tenant.id, run.id)
    assert run.status == "succeeded", run.error
    steps = {s.step_id: s for s in await engine.store.get_steps(run.id)}
    assert steps["write"].output["content"] == "hi"
    assert steps["check"].output["stdout"] == "hi"  # same run-scoped workspace
    events = [e.type for e in await engine.store.list_events(tenant.id, run.id)]
    assert "agent.turn" in events and "agent.tool_result" in events
    assert isinstance(Agent, type)


def test_agent_step_rejects_reserved_outputs():
    import pytest
    from pydantic import ValidationError

    from shoal.core.models import AgentStep

    # `summary` is always the agent's own summary, so an output of that name could never be filled.
    with pytest.raises(ValidationError, match="reserved output names: \\['summary'\\]"):
        AgentStep(id="a", prompt="p", outputs={"summary": {"type": "string"}})
    assert AgentStep(id="a", prompt="p", outputs={"test_summary": {"type": "string"}}).outputs


async def test_child_workflow_fan_out(engine, tenant):
    await engine.store.apply_resource(
        tenant.id,
        wf({"metadata": {"name": "child"}, "spec": {"inputs": {"n": {"type": "integer"}}, "outputs": {"sq": "{{ inputs.n * inputs.n }}"}, "steps": [{"id": "s", "type": "set", "values": {}}]}}),
    )
    await engine.store.apply_resource(
        tenant.id,
        wf({"metadata": {"name": "parent"}, "spec": {"steps": [{"id": "fan", "type": "workflow", "workflow": "child", "for_each": "{{ [1, 2, 3] }}", "with": {"n": "{{ item }}"}, "concurrency": 2}]}}),
    )
    run = await engine.executor.start_run(tenant.id, "parent", {})
    await engine.run_until_idle()
    run = await engine.store.get_run(tenant.id, run.id)
    assert run.status == "succeeded", run.error
    step = await engine.store.get_step(run.id, "fan")
    assert [r["outputs"]["sq"] for r in step.output["results"]] == [1, 4, 9]


async def test_pinned_workflow_versions(engine, tenant):
    def child(label: str) -> Workflow:
        return wf({"metadata": {"name": "child"}, "spec": {"outputs": {"v": label}, "steps": [{"id": "s", "type": "set", "values": {}}]}})

    await engine.store.apply_resource(tenant.id, child("one"))
    await engine.store.apply_resource(tenant.id, child("two"))  # version 2 is current
    await engine.store.apply_resource(
        tenant.id,
        wf({"metadata": {"name": "parent"}, "spec": {
            "outputs": {"pinned": "{{ steps.old.output.v }}", "latest": "{{ steps.new.output.v }}"},
            "steps": [{"id": "old", "type": "workflow", "workflow": "child", "version": 1},
                      {"id": "new", "type": "workflow", "workflow": "child"}]}}),
    )
    run = await engine.executor.start_run(tenant.id, "parent", {})
    await engine.run_until_idle()
    run = await engine.store.get_run(tenant.id, run.id)
    assert run.status == "succeeded", run.error
    assert run.outputs == {"pinned": "one", "latest": "two"}

    # a campaign pins the version current at launch, so a mid-campaign change does not split it
    campaign = await engine.campaigns.launch(
        tenant.id, parse_resource({"apiVersion": "shoal.dev/v1", "kind": "Campaign", "metadata": {"name": "c"},
                                   "spec": {"workflow": "child", "targets": {"items": [{"id": "a"}, {"id": "b"}]}}}).spec,
        start=False,
    )
    assert campaign.spec["workflow_version"] == 2
    await engine.store.apply_resource(tenant.id, child("three"))
    await engine.campaigns.start(tenant.id, campaign.id)
    await engine.run_until_idle()
    runs = [r for r in await engine.store.list_runs(tenant.id) if r.campaign_id == campaign.id]
    assert len(runs) == 2 and {r.workflow_version for r in runs} == {2}
