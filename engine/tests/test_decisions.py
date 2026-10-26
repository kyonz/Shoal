"""Decision providers, decide/grade steps, model routing, trigger triage and tool guards."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from shoal.agents.providers.base import ModelRequest, ModelResponse, ProviderUnavailable, ToolDef
from shoal.agents.providers.openai_compat import OpenAICompatibleProvider, to_openai_messages
from shoal.agents.providers.scripted import set_script_function
from shoal.core.models import DecisionQuestion, parse_resource
from shoal.decisions.base import DecisionProvider, DecisionRequest, DecisionResult, answer_from_probabilities
from shoal.decisions.jev import JevProvider


def res(kind: str, name: str, spec: dict[str, Any]):
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": kind, "metadata": {"name": name}, "spec": spec})


class FakeDecider(DecisionProvider):
    """Answers with probabilities computed by a function of the request."""

    name = "fake"

    def __init__(self, fn):
        self.fn = fn
        self.requests: list[DecisionRequest] = []

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        self.requests.append(request)
        probs = self.fn(request)
        answers = {k: answer_from_probabilities(q, probs[k]) for k, q in request.questions.items()}
        return DecisionResult(answers=answers, provider="fake", usage={"cost_usd": 0.0001})


async def run_wf(engine, tenant, name, inputs=None):
    run = await engine.executor.start_run(tenant.id, name, inputs or {})
    await engine.run_until_idle()
    return await engine.store.get_run(tenant.id, run.id)


async def events(engine, tenant, run_id):
    return await engine.store.list_events(tenant.id, run_id)


# --------------------------------------------------------------------------- decide / grade


async def test_decide_step_with_llm_provider(engine, tenant):
    def decider(request: ModelRequest) -> ModelResponse:
        assert request.tools[0].name == "answer" and "<state>" in request.messages[0]["content"]
        answer = {
            "kind": {"probabilities": {"bug": 0.9, "feature": 0.1}},
            "repro": {"probability_true": 0.8},
        }
        return ModelResponse(
            content=[{"type": "tool_use", "id": "t1", "name": "answer", "input": answer}],
            stop_reason="tool_use",
            usage={"input_tokens": 50, "output_tokens": 10, "cost_usd": 0.00002},
        )

    set_script_function("decider", decider)
    await engine.store.apply_resource(
        tenant.id,
        res(
            "Workflow",
            "triage",
            {
                "inputs": {"text": {"type": "string"}},
                "steps": [
                    {
                        "id": "classify",
                        "type": "decide",
                        "provider": "llm",
                        "provider_config": {"gateway": "scripted", "model": "tiny", "function": "decider"},
                        "state": "{{ inputs.text }}",
                        "questions": {
                            "kind": {"type": "choice", "instructions": "What is it?", "options": {"bug": "A defect", "feature": "A request"}},
                            "repro": {"type": "boolean", "instructions": "It has reproduction steps."},
                        },
                    },
                    {"id": "fix", "type": "set", "if": "{{ steps.classify.output.kind.value == 'bug' and steps.classify.output.repro.value }}", "values": {"go": True}},
                ],
            },
        ),
    )
    run = await run_wf(engine, tenant, "triage", {"text": "Crash on save. Steps: click save."})
    assert run.status == "succeeded", run.error
    out = (await engine.store.get_step(run.id, "classify")).output
    assert out["kind"]["value"] == "bug" and out["kind"]["confidence"] == 0.9
    assert out["repro"]["score"] == 0.8 and out["low_confidence"] == []
    assert (await engine.store.get_step(run.id, "fix")).output == {"go": True}
    assert "decision.made" in [e.type for e in await events(engine, tenant, run.id)]


async def test_low_confidence_decision_waits_for_review(engine, tenant):
    engine.registry.register("decision_providers", "fake", FakeDecider(lambda r: {"kind": {"a": 0.55, "b": 0.45}}))
    await engine.store.apply_resource(
        tenant.id,
        res(
            "Workflow",
            "unsure",
            {
                "steps": [
                    {
                        "id": "pick",
                        "type": "decide",
                        "provider": "fake",
                        "state": "something",
                        "questions": {"kind": {"type": "choice", "instructions": "Pick", "options": {"a": "A", "b": "B"}}},
                        "min_confidence": 0.8,
                        "on_low_confidence": "approval",
                    }
                ]
            },
        ),
    )
    run = await run_wf(engine, tenant, "unsure")
    assert run.status == "waiting"
    await engine.executor.submit_approval(tenant.id, run.id, "pick", approved=True, by="trevor")
    await engine.run_until_idle()
    run = await engine.store.get_run(tenant.id, run.id)
    assert run.status == "succeeded", run.error
    out = (await engine.store.get_step(run.id, "pick")).output
    assert out["low_confidence"] == ["kind"] and out["reviewed"]["by"] == "trevor"
    assert len(engine.registry.get("decision_providers", "fake").requests) == 1  # not re-asked on resume


async def test_grade_step_threshold(engine, tenant):
    engine.registry.register(
        "decision_providers", "fake", FakeDecider(lambda r: {"grade": {"poor": 0.6, "ok": 0.3, "great": 0.1}})
    )
    await engine.store.apply_resource(
        tenant.id,
        res(
            "Workflow",
            "graded",
            {
                "steps": [
                    {
                        "id": "quality",
                        "type": "grade",
                        "provider": "fake",
                        "subject": "a PR description",
                        "instructions": "How good is this PR?",
                        "levels": ["poor", "ok", "great"],
                        "threshold": 0.5,
                        "on_fail": "fail",
                    }
                ]
            },
        ),
    )
    run = await run_wf(engine, tenant, "graded")
    assert run.status == "failed"
    step = await engine.store.get_step(run.id, "quality")
    assert step.error["level"] == "poor" and step.error["score"] == 0.25


async def test_jev_provider_maps_wire_format(engine, tenant):
    calls: list[dict[str, Any]] = []

    class FakeClient:
        def system_one(self, **kwargs):
            calls.append(kwargs)
            return {
                "answers": {
                    "route": {"choice": "hard", "probabilities": {"easy": 0.2, "hard": 0.8}, "confidence": 0.7},
                    "level": {"score": 2, "confidence": 0.9},
                    "risky": {"noul": 0.15},
                },
                "usage": {"input_tokens": 1000, "output_tokens": 3},
            }

    engine.registry.register("decision_providers", "jev", JevProvider(client_factory=lambda cfg: FakeClient()))
    questions = {
        "route": DecisionQuestion(type="choice", instructions="Difficulty", options={"easy": "E", "hard": "H"}),
        "level": DecisionQuestion(type="score", instructions="Quality", levels=["bad", "fine", "good"]),
        "risky": DecisionQuestion(type="boolean", instructions="Risky"),
    }
    result = await engine.decisions.decide(tenant.id, {"pr": 1}, questions, provider="jev")
    assert calls[0]["questions"] == {
        "route": {"type": "choice", "instructions": "Difficulty", "criteria": {"easy": "E", "hard": "H"}},
        "level": {"type": "score", "instructions": "Quality", "criteria": ["bad", "fine", "good"]},
        "risky": {"type": "noul", "instructions": "Risky"},
    }
    a = result.answers
    assert (a["route"].value, a["route"].confidence) == ("hard", 0.7)
    assert (a["level"].value, a["level"].score, a["level"].confidence) == ("good", 1.0, 0.9)
    assert a["risky"].value is False and a["risky"].score == 0.15
    assert result.usage["cost_usd"] == round(1000 * 0.042 / 1e6, 8)


async def test_jev_provider_against_the_real_sdk(engine, tenant):
    """The installed typesafe-sdk, its HTTP layer stubbed: request shape and response parsing end to end."""
    httpx2 = pytest.importorskip("httpx2")
    typesafe_sdk = pytest.importorskip("typesafe_sdk")
    sent: list[dict[str, Any]] = []

    def api(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json={"model": "jev-1", "usage": {"input_tokens": 120, "output_tokens": 3}, "answers": {
            "kind": {"type": "choice", "choice": "bug", "confidence": 0.9, "probabilities": {"bug": 0.9, "feature": 0.1}},
            "sev": {"type": "score", "score": 1.7, "confidence": 0.8, "legend": {"0": "low", "1": "mid", "2": "high"},
                    "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8}},
            "repro": {"type": "noul", "noul": 0.8}}})

    def client(config):
        return typesafe_sdk.TypeSafeClient(api_key=config["api_key"], transport=httpx2.MockTransport(api))

    engine.registry.register("decision_providers", "jev", JevProvider(client_factory=client))
    await engine.secrets.writable().put(tenant.id, "jev-key", "ts-key")
    questions = {
        "kind": DecisionQuestion(type="choice", instructions="What is it?", options={"bug": "A defect", "feature": "A request"}),
        "sev": DecisionQuestion(type="score", instructions="How bad?", levels=["low", "mid", "high"]),
        "repro": DecisionQuestion(type="boolean", instructions="Has steps."),
    }
    result = await engine.decisions.decide(tenant.id, "Crash on save", questions, provider="jev",
                                           config={"api_key_secret": "jev-key"})
    assert sent[0]["questions"]["sev"] == {"type": "score", "instructions": "How bad?", "criteria": ["low", "mid", "high"]}
    a = result.answers
    assert (a["kind"].value, a["kind"].confidence) == ("bug", 0.9)
    assert a["sev"].value == "high" and a["sev"].probabilities == {"low": 0.1, "mid": 0.1, "high": 0.8}
    assert a["repro"].value is True and result.usage["decision_input_tokens"] == 120


# --------------------------------------------------------------------------- model routing


def _finisher(outputs: dict[str, Any]):
    def fn(request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            content=[{"type": "tool_use", "id": "f", "name": "finish", "input": {**outputs, "summary": request.model}}],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001},
            model=request.model,
        )

    return fn


async def _agent_workflow(engine, tenant, model: str):
    await engine.store.apply_resource(
        tenant.id, res("Agent", "coder", {"provider": "anthropic", "model": model, "tools": []})
    )
    await engine.store.apply_resource(
        tenant.id,
        res("Workflow", "code", {"steps": [{"id": "work", "type": "agent", "agent": "coder", "prompt": "Refactor the parser"}]}),
    )


async def test_model_policy_fallback_and_tenant_deny_list(engine, tenant):
    def down(request: ModelRequest) -> ModelResponse:
        raise ProviderUnavailable("overloaded")

    set_script_function("down", down)
    set_script_function("ok", _finisher({}))
    for name, fn in (("primary", "down"), ("backup", "ok")):
        await engine.store.apply_resource(tenant.id, res("Gateway", name, {"type": "scripted", "config": {"function": fn}}))
    await engine.store.apply_resource(tenant.id, res("ModelPolicy", "default", {"deny_models": ["forbidden-*"], "rules": [{"name": "any", "candidates": [{"model": "x"}]}]}))
    await engine.store.apply_resource(
        tenant.id,
        res(
            "ModelPolicy",
            "coding",
            {
                "rules": [
                    {"name": "other", "match": {"agents": ["reviewer"]}, "candidates": [{"model": "never", "gateway": "backup"}]},
                    {
                        "name": "coders",
                        "match": {"agents": ["cod*"]},
                        "candidates": [
                            {"model": "forbidden-big", "gateway": "backup"},
                            {"model": "big", "gateway": "primary"},
                            {"model": "medium", "gateway": "backup"},
                        ],
                    },
                ]
            },
        ),
    )
    await _agent_workflow(engine, tenant, "policy:coding")
    run = await run_wf(engine, tenant, "code")
    assert run.status == "succeeded", run.error
    assert (await engine.store.get_step(run.id, "work")).output["summary"] == "medium"
    evs = {e.type: e.data for e in await events(engine, tenant, run.id)}
    assert [c["model"] for c in evs["model.routed"]["candidates"]] == ["big", "medium"]
    assert evs["model.routed"]["candidates"][0]["rule"] == "coding/coders"
    assert evs["model.fallback"]["model"] == "big"

    await _agent_workflow(engine, tenant, "forbidden-model")
    run = await run_wf(engine, tenant, "code")
    assert run.status == "failed" and "not permitted" in run.error["message"]


async def test_model_policy_keeps_agent_settings_and_skips_broken_gateways(engine, tenant):
    seen: list[ModelRequest] = []

    def ok(request: ModelRequest) -> ModelResponse:
        seen.append(request)
        return _finisher({})(request)

    set_script_function("ok", ok)
    await engine.store.apply_resource(tenant.id, res("Gateway", "nokey", {"type": "scripted", "api_key_secret": "missing-key"}))
    await engine.store.apply_resource(tenant.id, res("Gateway", "gw", {"type": "scripted", "config": {"function": "ok"}}))
    await engine.store.apply_resource(tenant.id, res("ModelPolicy", "coding", {"rules": [{"name": "all", "candidates": [
        {"model": "big", "gateway": "nokey"}, {"model": "medium", "gateway": "gw"}]}]}))
    await engine.store.apply_resource(tenant.id, res("Agent", "coder", {
        "provider": "anthropic", "model": "policy:coding", "tools": [], "provider_config": {"temperature": 0.2, "effort": "high"}}))
    await engine.store.apply_resource(
        tenant.id, res("Workflow", "code", {"steps": [{"id": "work", "type": "agent", "agent": "coder", "prompt": "Go"}]}))
    run = await run_wf(engine, tenant, "code")
    assert run.status == "succeeded", run.error
    assert seen[0].model == "medium" and seen[0].config["temperature"] == 0.2 and seen[0].config["effort"] == "high"
    fallback = next(e.data for e in await events(engine, tenant, run.id) if e.type == "model.fallback")
    assert fallback["gateway"] == "nokey" and "missing-key" in fallback["reason"]

    await engine.store.apply_resource(tenant.id, res("ModelPolicy", "coding", {"rules": [{"name": "all", "candidates": [
        {"model": "big", "gateway": "nokey"}]}]}))
    run = await run_wf(engine, tenant, "code")
    assert run.status == "failed" and "no usable candidate" in run.error["message"] and "missing-key" in run.error["message"]


async def test_llm_decisions_respect_the_tenant_model_policy(engine, tenant):
    await engine.store.apply_resource(tenant.id, res("ModelPolicy", "default", {"deny_models": ["tiny"], "rules": [{"name": "any", "candidates": [{"model": "x"}]}]}))
    await engine.store.apply_resource(tenant.id, res("Workflow", "triage", {"steps": [{
        "id": "classify", "type": "decide", "provider": "llm", "provider_config": {"gateway": "scripted", "model": "tiny"},
        "state": "x", "questions": {"repro": {"type": "boolean", "instructions": "Has steps."}}}]}))
    run = await run_wf(engine, tenant, "triage")
    assert run.status == "failed" and "not permitted" in run.error["message"]


async def test_difficulty_router_picks_rule(engine, tenant):
    engine.registry.register("decision_providers", "fake", FakeDecider(lambda r: {"difficulty": {"easy": 0.1, "hard": 0.9}}))
    set_script_function("ok", _finisher({}))
    await engine.store.apply_resource(tenant.id, res("Gateway", "gw", {"type": "scripted", "config": {"function": "ok"}}))
    await engine.store.apply_resource(
        tenant.id,
        res(
            "ModelPolicy",
            "auto",
            {
                "decision_provider": "fake",
                "difficulty_router": {"type": "choice", "instructions": "How hard is the task?", "options": {"easy": "Small", "hard": "Large"}},
                "rules": [
                    {"name": "easy", "match": {"difficulty": ["easy"]}, "candidates": [{"model": "small", "gateway": "gw"}]},
                    {"name": "hard", "match": {"difficulty": ["hard"]}, "candidates": [{"model": "large", "gateway": "gw", "effort": "max"}]},
                ],
            },
        ),
    )
    await _agent_workflow(engine, tenant, "policy:auto")
    run = await run_wf(engine, tenant, "code")
    assert run.status == "succeeded", run.error
    assert (await engine.store.get_step(run.id, "work")).output["summary"] == "large"
    routed = next(e.data for e in await events(engine, tenant, run.id) if e.type == "model.routed")
    assert routed["difficulty"]["value"] == "hard"
    assert engine.registry.get("decision_providers", "fake").requests[0].state["task"] == "Refactor the parser"


async def test_weighted_selection_is_deterministic():
    from shoal.core.models import ModelRule
    from shoal.llm.router import ModelRouter

    rule = ModelRule.model_validate(
        {"name": "ab", "strategy": "weighted", "candidates": [{"model": "a", "weight": 1}, {"model": "b", "weight": 1}]}
    )
    picks = {ModelRouter.order(rule, f"run{i}")[0].model for i in range(40)}
    assert picks == {"a", "b"}
    assert ModelRouter.order(rule, "same")[0].model == ModelRouter.order(rule, "same")[0].model


# --------------------------------------------------------------------------- triage and guards


async def test_trigger_triage_filters_events(engine, tenant):
    def judge(request: DecisionRequest):
        p = 0.9 if "crash" in json.dumps(request.state) else 0.1
        return {"automatable": {"true": p}}

    engine.registry.register("decision_providers", "fake", FakeDecider(judge))
    await engine.store.apply_resource(tenant.id, res("Workflow", "fixit", {"steps": [{"id": "s", "type": "set", "values": {"t": "{{ trigger.triage.automatable.score }}"}}]}))
    await engine.store.apply_resource(
        tenant.id,
        res(
            "Trigger",
            "smart",
            {
                "workflow": "fixit",
                "connection": "hooks",
                "event": "received",
                "triage": {
                    "provider": "fake",
                    "state": "{{ event.title }}",
                    "questions": {"automatable": {"type": "boolean", "instructions": "An agent could fix this alone."}},
                    "when": "{{ answers.automatable.score >= 0.7 }}",
                },
            },
        ),
    )
    skipped = await engine.triggers.fire_manual(tenant.id, "smart", {"title": "Please redesign the logo"})
    assert skipped["status"] == "triaged_out"
    fired = await engine.triggers.fire_manual(tenant.id, "smart", {"title": "crash on save"})
    assert fired["status"] == "fired"
    await engine.run_until_idle()
    assert (await engine.store.get_step(fired["run_id"], "s")).output == {"t": 0.9}
    deliveries = await engine.store.list_deliveries(tenant.id, "smart")
    assert sorted(d.status for d in deliveries) == ["fired", "triaged_out"]


async def test_tool_guard_blocks_risky_calls(engine, tenant):
    def judge(request: DecisionRequest):
        cmd = json.dumps(request.state["pending_tool_call"])
        risky = 0.95 if "rm -rf" in cmd else 0.05
        return {"irreversible": {"true": risky}, "intent_mismatch": {"true": 0.1}}

    engine.registry.register("decision_providers", "fake", FakeDecider(judge))
    await engine.store.apply_resource(
        tenant.id,
        res(
            "Agent",
            "careful",
            {
                "provider": "scripted",
                "provider_config": {
                    "script": [
                        {"tool_calls": [{"name": "shell", "input": {"command": "rm -rf /workspace"}}]},
                        {"tool_calls": [{"name": "shell", "input": {"command": "echo safe"}}]},
                    ],
                    "final_outputs": {"summary": "done"},
                },
                "guard": {"provider": "fake"},
            },
        ),
    )
    await engine.store.apply_resource(
        tenant.id, res("Workflow", "guarded", {"steps": [{"id": "a", "type": "agent", "agent": "careful", "prompt": "tidy up"}]})
    )
    run = await run_wf(engine, tenant, "guarded")
    assert run.status == "succeeded", run.error
    evs = await events(engine, tenant, run.id)
    blocked = [e.data for e in evs if e.type == "agent.tool_blocked"]
    assert len(blocked) == 1 and blocked[0]["checks"] == ["irreversible"]
    results = [e.data for e in evs if e.type == "agent.tool_result"]
    assert results[0]["is_error"] and "Blocked" in results[0]["output"]
    assert not results[1]["is_error"] and "safe" in results[1]["output"]


# --------------------------------------------------------------------------- openai-compatible gateway


async def test_openai_compatible_provider_translation():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "Let me look"}, {"type": "tool_use", "id": "c1", "name": "shell", "input": {"command": "ls"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "a.txt"}]},
    ]
    wire = to_openai_messages("sys", messages)
    assert wire[0] == {"role": "system", "content": "sys"}
    assert wire[2]["tool_calls"][0]["function"] == {"name": "shell", "arguments": '{"command": "ls"}'}
    assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": "a.txt"}

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "llama",
                "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "finish", "arguments": '{"summary": "ok"}'}}]}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
            },
        )

    provider = OpenAICompatibleProvider()
    provider.transport = httpx.MockTransport(handler)
    response = await provider.complete(
        ModelRequest(
            model="llama",
            system="sys",
            messages=messages,
            tools=[ToolDef("finish", "done", {"type": "object"})],
            config={"base_url": "https://openrouter.test/api/v1", "api_key": "k", "pricing": [1.0, 2.0]},
        )
    )
    assert seen["auth"] == "Bearer k" and seen["body"]["tools"][0]["function"]["name"] == "finish"
    assert response.stop_reason == "tool_use" and response.tool_uses[0]["input"] == {"summary": "ok"}
    assert response.usage["cost_usd"] == 0.0012
