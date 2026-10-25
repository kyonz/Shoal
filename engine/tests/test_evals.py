"""Evals: graders, scores per workflow version, comparison and gates, variants, online evals,
and the Braintrust / Langfuse eval sinks."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from shoal.agents.providers.base import ModelRequest, ModelResponse
from shoal.agents.providers.scripted import set_script_function
from shoal.api.app import create_app
from shoal.core.models import parse_resource
from shoal.decisions.base import DecisionProvider, DecisionRequest, DecisionResult, answer_from_probabilities
from shoal.evals.schema import validate


def res(kind: str, name: str, spec: dict[str, Any]):
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": kind, "metadata": {"name": name}, "spec": spec})


def classifier(expr: str) -> dict[str, Any]:
    return {
        "inputs": {"text": {"type": "string"}},
        "outputs": {"label": "{{ steps.classify.output.label }}", "text": "{{ inputs.text }}"},
        "steps": [{"id": "classify", "type": "set", "values": {"label": expr}}],
    }


V1 = classifier("{{ 'bug' if 'crash' in inputs.text else 'feature' }}")
V2 = classifier("{{ 'bug' if ('crash' in inputs.text or 'error' in inputs.text) else 'feature' }}")
V3 = classifier("{{ 'feature' }}")

CASES = [
    {"name": "crash", "inputs": {"text": "app crash on start"}, "expect": {"label": "bug"}},
    {"name": "error", "inputs": {"text": "error 500 on save"}, "expect": {"label": "bug"}},
    {"name": "dark-mode", "inputs": {"text": "please add dark mode"}, "expect": {"label": "feature"}},
]

EVAL = {
    "workflow": "triage",
    "cases": CASES,
    "graders": [
        {"name": "label", "check": "outputs.label == expect.label", "weight": 3},
        {"name": "shape", "type": "json_schema", "schema": {
            "type": "object", "required": ["label"],
            "properties": {"label": {"type": "string", "enum": ["bug", "feature"]}},
        }},
    ],
    "threshold": 0.9,
}


async def apply(engine, tenant, kind, name, spec):
    row, _ = await engine.store.apply_resource(tenant.id, res(kind, name, spec))
    return row


async def run_eval(engine, tenant, name="triage-eval", **kwargs):
    row = await engine.evals.start(tenant.id, name, started_by="user:dana", **kwargs)
    await engine.run_until_idle()
    return await engine.evals.get(tenant.id, row.id)


# --------------------------------------------------------------------------- graders and runner


async def test_eval_run_grades_every_case(engine, tenant):
    await apply(engine, tenant, "Workflow", "triage", V1)
    await apply(engine, tenant, "Eval", "triage-eval", EVAL)
    row = await run_eval(engine, tenant)

    assert row.status == "completed"
    assert row.workflow_version == 1 and row.eval_version == 1
    assert row.variants[0]["versions"] == {"Workflow/triage": 1}
    results = {r.case_name: r for r in await engine.evals.results(row.id)}
    assert results["crash"].passed and results["dark-mode"].passed
    error = results["error"]
    assert not error.passed and error.scores["label"]["score"] == 0.0 and error.scores["shape"]["score"] == 1.0
    assert error.score == 0.25  # (0 * 3 + 1 * 1) / 4
    assert error.run_id and error.outputs == {"label": "feature", "text": "error 500 on save"}
    summary = row.summary["variants"]["default"]
    assert summary["cases"] == 3 and summary["pass_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert summary["graders"] == {"label": pytest.approx(0.6667, abs=1e-3), "shape": 1.0}
    # eval runs are labelled and attributed; completion is audited
    run = await engine.store.get_run(tenant.id, error.run_id)
    assert run.labels["eval_run"] == row.id and run.context["eval"]["case"] == "error"
    assert any(a.action == "eval.completed" and a.data["eval_run"] == row.id for a in await engine.store.list_audit(tenant.id))


async def test_failed_runs_and_bad_inputs_score_zero(engine, tenant):
    await apply(engine, tenant, "Workflow", "triage", V1)
    await apply(engine, tenant, "Eval", "triage-eval", {
        **EVAL, "cases": [{"name": "missing-input", "inputs": {}}, CASES[0]],
        "graders": [{"name": "ok", "check": "run.status == 'succeeded'"}],
    })
    row = await run_eval(engine, tenant)
    results = {r.case_name: r for r in await engine.evals.results(row.id)}
    assert results["missing-input"].status == "error" and results["missing-input"].score == 0.0
    assert "required" in results["missing-input"].error["message"]
    assert results["crash"].score == 1.0
    assert row.summary["score"] == 0.5


async def test_llm_judge_and_decision_graders(engine, tenant):
    seen: list[ModelRequest] = []

    def judge(request: ModelRequest) -> ModelResponse:
        seen.append(request)
        level = "good" if "bug" in request.messages[0]["content"].split("<output>")[1] else "poor"
        return ModelResponse(
            content=[{"type": "tool_use", "id": "v1", "name": "verdict", "input": {"reasoning": "Looks right.", "level": level}}],
            stop_reason="tool_use", usage={"cost_usd": 0.001},
        )

    class Decider(DecisionProvider):
        name = "fake"

        async def decide(self, request: DecisionRequest) -> DecisionResult:
            q = request.questions["grade"]
            return DecisionResult({"grade": answer_from_probabilities(q, {"true": 0.8})}, provider="fake", usage={"cost_usd": 0.0002})

    set_script_function("judge", judge)
    engine.registry.register("decision_providers", "fake", Decider())
    await apply(engine, tenant, "Workflow", "triage", V1)
    await apply(engine, tenant, "Eval", "triage-eval", {
        "workflow": "triage",
        "cases": CASES[:1],
        "decision_provider": "fake",
        "graders": [
            {"name": "judge", "type": "llm_judge", "rubric": "Is the label right?", "levels": ["poor", "ok", "good"],
             "gateway": "scripted", "model": "judge-1", "config": {"function": "judge"}},
            {"name": "plausible", "type": "decision", "question": {"type": "boolean", "instructions": "The label is plausible."}},
        ],
        "threshold": 0.5,
    })
    row = await run_eval(engine, tenant)
    [result] = await engine.evals.results(row.id)
    assert result.scores["judge"]["score"] == 1.0
    assert result.scores["judge"]["detail"] == {"level": "good", "reasoning": "Looks right.", "judge": "scripted:judge-1"}
    assert result.scores["plausible"]["score"] == 0.8
    assert result.score == 0.9 and result.passed
    assert result.cost_usd == pytest.approx(0.0012)
    assert seen[0].model == "judge-1" and "Is the label right?" in seen[0].messages[0]["content"]


async def test_broken_grader_scores_zero_without_losing_the_case(engine, tenant):
    await apply(engine, tenant, "Workflow", "triage", V1)
    await apply(engine, tenant, "Eval", "triage-eval", {
        **EVAL, "cases": CASES[:1],
        "graders": [{"name": "bad", "check": "outputs.label |nosuchfilter"}, {"name": "good", "check": "true"}],
    })
    row = await run_eval(engine, tenant)
    [result] = await engine.evals.results(row.id)
    assert result.scores["bad"]["score"] == 0.0 and "nosuchfilter" in result.scores["bad"]["error"]
    assert result.scores["good"]["score"] == 1.0 and result.score == 0.5


def test_eval_spec_validation():
    with pytest.raises(ValidationError, match="needs 'cases'"):
        res("Eval", "e", {"workflow": "w", "graders": [{"name": "g", "check": "true"}]})
    with pytest.raises(ValidationError, match="need 'check'"):
        res("Eval", "e", {"workflow": "w", "cases": [{"name": "a"}], "graders": [{"name": "g"}]})
    with pytest.raises(ValidationError, match="need a 'rubric'"):
        res("Eval", "e", {"workflow": "w", "cases": [{"name": "a"}], "graders": [{"name": "g", "type": "llm_judge"}]})
    with pytest.raises(ValidationError, match="unique"):
        res("Eval", "e", {"workflow": "w", "cases": [{"name": "a"}, {"name": "a"}], "graders": [{"name": "g", "check": "1"}]})


def test_json_schema_subset():
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer", "minimum": 1}, "b": {
        "type": "array", "items": {"type": "string", "pattern": "^x"}}}, "additionalProperties": False}
    assert validate({"a": 2, "b": ["xy"]}, schema) == []
    errors = validate({"a": 0, "b": ["y"], "c": 1}, schema)
    assert any("below" in e for e in errors) and any("does not match" in e for e in errors) and any("not allowed" in e for e in errors)
    assert validate({"b": []}, schema) == ["$: 'a' is required"]
    assert validate(1, {"anyOf": [{"type": "string"}, {"type": "integer"}]}) == []


# --------------------------------------------------------------------------- versions, comparison, gates


async def test_versions_are_compared_and_gated(engine, tenant):
    await apply(engine, tenant, "Workflow", "triage", V1)
    await apply(engine, tenant, "Eval", "triage-eval", {**EVAL, "gate": {"fail_under": 0.9, "max_regression": 0.05}})
    v1 = await run_eval(engine, tenant, baseline="none")
    assert v1.gate["passed"] is False and "below 0.9" in v1.gate["reasons"][0]

    await apply(engine, tenant, "Workflow", "triage", V2)  # uplift: catches "error" too
    v2 = await run_eval(engine, tenant, baseline="previous")
    assert v2.workflow_version == 2 and v2.baseline_id == v1.id
    diff = v2.summary["baseline_diff"]["default"]
    assert diff["improvements"] == ["error"] and diff["regressions"] == []
    assert diff["baseline"]["workflow_version"] == 1 and diff["candidate"]["workflow_version"] == 2
    assert diff["delta"] == pytest.approx(0.25, abs=1e-3)
    assert v2.gate["passed"] is True

    await apply(engine, tenant, "Workflow", "triage", V3)  # regression
    v3 = await run_eval(engine, tenant, baseline="version:2")
    assert v3.baseline_id == v2.id
    diff = v3.summary["baseline_diff"]["default"]
    assert diff["regressions"] == ["crash", "error"]
    assert v3.gate["passed"] is False and any("dropped" in r for r in v3.gate["reasons"])

    # an older version can still be evaluated, and history is kept per version
    again = await run_eval(engine, tenant, version=2, baseline="none")
    assert again.workflow_version == 2 and again.summary["score"] == 1.0
    history = await engine.evals.history(tenant.id, "default", "triage-eval")
    assert [h["workflow_version"] for h in history] == [3, 2, 1]
    assert history[1]["runs"] == 2 and history[1]["latest"]["eval_run_id"] == again.id
    assert history[0]["latest"]["gate_passed"] is False

    compared = await engine.evals.compare_runs(tenant.id, v1.id, v2.id)
    assert compared["variants"]["default"]["improvements"] == ["error"]


async def test_variants_compare_versions_in_one_run(engine, tenant):
    await apply(engine, tenant, "Workflow", "triage", V1)
    await apply(engine, tenant, "Workflow", "triage", V2)
    await apply(engine, tenant, "Eval", "triage-eval", {
        **EVAL, "variants": [{"name": "control", "version": 1}, {"name": "candidate", "version": 2}],
        "gate": {"max_regression": 0.0},
    })
    row = await run_eval(engine, tenant)
    assert [(v["name"], v["workflow_version"]) for v in row.variants] == [("control", 1), ("candidate", 2)]
    results = await engine.evals.results(row.id)
    assert len(results) == 6
    assert {r.workflow_version for r in results if r.variant == "candidate"} == {2}
    assert row.summary["variants"]["control"]["pass_rate"] < row.summary["variants"]["candidate"]["pass_rate"] == 1.0
    assert row.summary["variant_diff"]["candidate"]["improvements"] == ["error"]
    assert row.gate["passed"] is True


async def test_online_eval_grades_past_runs(engine, tenant):
    await apply(engine, tenant, "Workflow", "triage", V1)
    for case in CASES:
        await engine.executor.start_run(tenant.id, "triage", case["inputs"])
    await engine.run_until_idle()
    await apply(engine, tenant, "Eval", "prod-triage", {
        "workflow": "triage",
        "from_runs": {"limit": 10},
        "graders": [{"name": "is-bug-when-crash", "check": "('crash' not in inputs.text) or outputs.label == 'bug'"}],
    })
    row = await run_eval(engine, tenant, name="prod-triage")
    results = await engine.evals.results(row.id)
    assert row.mode == "runs" and len(results) == 3 and all(r.passed for r in results)
    runs = await engine.store.list_runs(tenant.id, workflow="triage", limit=50)
    assert len(runs) == 3  # graded in place: no new runs


# --------------------------------------------------------------------------- sinks


class FakeBraintrust:
    def __init__(self):
        self.experiments: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.auth: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.auth.add(request.headers.get("authorization", ""))
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/project":
            return httpx.Response(200, json={"id": "proj_1", "name": body["name"]})
        if path == "/v1/experiment":
            self.experiments.append(body)
            return httpx.Response(200, json={"id": "exp_1", "name": body["name"]})
        if path == "/v1/experiment/exp_1/insert":
            self.events += body["events"]
            return httpx.Response(200, json={"row_ids": [e["id"] for e in body["events"]]})
        if path == "/v1/project_logs/proj_1/insert":
            self.logs += body["events"]
            return httpx.Response(200, json={"row_ids": []})
        if path == "/v1/dataset":
            assert request.url.params["dataset_name"] == "golden"
            return httpx.Response(200, json={"objects": [{"id": "ds_1"}]})
        if path == "/v1/dataset/ds_1/fetch":
            if body.get("cursor"):
                return httpx.Response(200, json={"events": [], "cursor": None})
            return httpx.Response(200, json={"cursor": "c1", "events": [
                {"id": "row1", "input": {"text": "crash when saving"}, "expected": {"label": "bug"}, "metadata": {"name": "bt-crash"}},
                {"id": "row2", "input": {"text": "dark theme please"}, "expected": "feature", "tags": ["ui"]},
            ]})
        return httpx.Response(404, json={"error": path})


async def test_braintrust_dataset_import_and_experiment_export(engine, tenant):
    bt = FakeBraintrust()
    engine.http_transport = httpx.MockTransport(bt.handle)
    await engine.secrets.writable().put(tenant.id, "bt-key", "sk-bt")
    config = {"project": "triage", "api_key_secret": "bt-key", "org": "acme"}
    await apply(engine, tenant, "Workflow", "triage", V1)
    await apply(engine, tenant, "Eval", "triage-eval", {
        **EVAL, "cases": [],
        "dataset": {"sink": "braintrust", "name": "golden", "config": config},
        "graders": [{"name": "label", "check": "outputs.label == (expect.label or expect.output)"}],
        "sinks": [{"sink": "braintrust", "config": config}],
    })
    row = await run_eval(engine, tenant)
    results = {r.case_name: r for r in await engine.evals.results(row.id)}
    assert set(results) == {"bt-crash", "row2"}
    assert results["row2"].case["expect"] == {"output": "feature"}  # scalar values are wrapped
    assert results["bt-crash"].case["metadata"]["braintrust_id"] == "row1"

    assert bt.auth == {"Bearer sk-bt"}
    assert row.exports["braintrust"]["status"] == "exported"
    assert row.exports["braintrust"]["url"].startswith("https://www.braintrust.dev/app/acme/p/triage/experiments/")
    assert bt.experiments[0]["metadata"]["shoal_eval_run"] == row.id
    roots = [e for e in bt.events if e["id"] == e["root_span_id"]]
    spans = [e for e in bt.events if e["id"] != e["root_span_id"]]
    assert len(roots) == 2 and len(spans) == 2
    crash = next(e for e in roots if e["metadata"]["case"] == "bt-crash")
    assert crash["scores"] == {"label": 1.0} and crash["expected"] == {"label": "bug"}
    assert crash["output"]["label"] == "bug" and crash["metadata"]["workflow_version"] == 1
    assert spans[0]["span_parents"] == [spans[0]["root_span_id"]] and spans[0]["span_attributes"]["name"] == "classify"

    # production runs export to the project's logs
    run_id = results["bt-crash"].run_id
    await engine.evals.export_run(tenant.id, run_id, "braintrust", config)
    assert bt.logs[0]["id"] == run_id and bt.logs[0]["input"] == {"text": "crash when saving"}


async def test_failed_export_does_not_fail_the_eval(engine, tenant):
    engine.http_transport = httpx.MockTransport(lambda r: httpx.Response(500, json={"error": "down"}))
    await apply(engine, tenant, "Workflow", "triage", V1)
    await apply(engine, tenant, "Eval", "triage-eval", {
        **EVAL, "sinks": [{"sink": "braintrust", "config": {"project": "p", "api_key": "inline"}},
                          {"sink": "langfuse", "config": {"secret_key_secret": "missing"}}],
    })
    row = await run_eval(engine, tenant)
    assert row.status == "completed"
    assert row.exports["braintrust"]["status"] == "failed" and "500" in row.exports["braintrust"]["error"]
    assert row.exports["langfuse"] == {**row.exports["langfuse"], "status": "failed"}
    assert "missing" in row.exports["langfuse"]["error"]


class FakeLangfuse:
    def __init__(self):
        self.batches: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self.auth: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.auth.add(request.headers.get("authorization", ""))
        path = request.url.path
        if path == "/api/public/ingestion":
            batch = json.loads(request.content)["batch"]
            self.batches += batch
            return httpx.Response(207, json={"successes": [{"id": e["id"], "status": 201} for e in batch], "errors": []})
        if path == "/api/public/dataset-run-items":
            self.links.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "dri"})
        if path == "/api/public/dataset-items":
            assert request.url.params["datasetName"] == "triage-golden"
            page = int(request.url.params["page"])
            items = {
                1: [{"id": "item-1", "input": {"text": "crash on login"}, "expectedOutput": {"label": "bug"}, "metadata": {"name": "login"}}],
                2: [{"id": "item-2", "input": {"text": "old"}, "status": "ARCHIVED"},
                    {"id": "item-3", "input": {"text": "add export"}, "expectedOutput": {"label": "feature"}}],
            }[page]
            return httpx.Response(200, json={"data": items, "meta": {"page": page, "totalPages": 2}})
        return httpx.Response(404)


ADMIN = "test-admin-token"


@pytest.fixture
async def admin(engine):
    engine.settings.admin_token = SecretStr(ADMIN)
    app = create_app(engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test") as client:
        r = await client.post("/v1/tenants", json={"slug": "acme", "name": "Acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
        assert r.status_code == 201, r.text
        client.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        yield client


async def test_eval_api_with_langfuse(engine, admin):
    lf = FakeLangfuse()
    engine.http_transport = httpx.MockTransport(lf.handle)
    assert (await admin.put("/v1/namespaces/default/secrets/lf-secret", json={"value": "sk-lf"})).status_code in (200, 201, 204)
    lf_config = {"public_key": "pk-lf", "secret_key_secret": "lf-secret", "project_id": "p1"}

    sinks = (await admin.get("/v1/eval-sinks")).json()
    assert {"braintrust", "langfuse"} <= {s["name"] for s in sinks}

    assert (await admin.put("/v1/namespaces/default/workflows/triage", json={"spec": V1})).status_code == 201
    eval_spec = {**EVAL, "sinks": [{"sink": "langfuse", "config": lf_config}]}
    assert (await admin.put("/v1/namespaces/default/evals/triage-eval", json={"spec": eval_spec})).status_code == 201

    # import a Langfuse dataset into the eval: a new, audited Eval version
    r = await admin.post("/v1/namespaces/default/evals/triage-eval/import",
                         json={"sink": "langfuse", "dataset": "triage-golden", "config": lf_config})
    assert r.status_code == 200, r.text
    assert r.json()["metadata"]["version"] == 2
    names = [c["name"] for c in r.json()["spec"]["cases"]]
    assert names == ["crash", "error", "dark-mode", "login", "item-3"]

    r = await admin.post("/v1/namespaces/default/evals/triage-eval/runs", json={"labels": {"ci": "true"}})
    assert r.status_code == 201, r.text
    eval_run_id = r.json()["id"]
    # (a background worker, as with Temporal, may already have finished the cases)
    assert r.json()["eval_version"] == 2 and r.json()["status"] in ("running", "completed")
    await engine.run_until_idle()

    detail = (await admin.get(f"/v1/eval-runs/{eval_run_id}")).json()
    assert detail["status"] == "completed" and len(detail["results"]) == 5
    assert detail["summary"]["variants"]["default"]["cases"] == 5
    assert detail["exports"]["langfuse"]["url"] == f"https://cloud.langfuse.com/project/p1/sessions/{eval_run_id}"
    assert lf.auth == {"Basic " + base64.b64encode(b"pk-lf:sk-lf").decode()}
    traces = [e for e in lf.batches if e["type"] == "trace-create"]
    scores = [e for e in lf.batches if e["type"] == "score-create"]
    assert len(traces) == 5 and len(scores) == 10
    assert all(t["body"]["sessionId"] == eval_run_id for t in traces)
    assert [link["datasetItemId"] for link in lf.links] == ["item-1", "item-3"]

    page = (await admin.get("/v1/namespaces/default/eval-runs", params={"eval": "triage-eval", "limit": 1})).json()
    assert [i["id"] for i in page["items"]] == [eval_run_id] and page["next_cursor"] is None

    # a second run against the first; compare, scores per version
    r = await admin.post("/v1/namespaces/default/evals/triage-eval/runs", json={"baseline": "previous", "fail_under": 0.5})
    second = r.json()["id"]
    await engine.run_until_idle()
    detail = (await admin.get(f"/v1/eval-runs/{second}")).json()
    assert detail["baseline_id"] == eval_run_id and detail["gate"]["passed"] is True
    assert detail["summary"]["baseline_diff"]["default"]["delta"] == 0.0
    compared = (await admin.get(f"/v1/eval-runs/{second}/compare", params={"baseline": eval_run_id})).json()
    assert compared["variants"]["default"]["shared_cases"] == 5
    scores = (await admin.get("/v1/namespaces/default/evals/triage-eval/scores")).json()
    assert scores["versions"][0]["workflow_version"] == 1 and scores["versions"][0]["runs"] == 2

    results = (await admin.get(f"/v1/eval-runs/{second}/results")).json()
    r = await admin.post(f"/v1/runs/{results[0]['run_id']}/export", json={"sink": "langfuse", "config": lf_config})
    assert r.status_code == 200 and r.json()["ids"] == {"trace_id": results[0]["run_id"]}

    entries = (await admin.get("/v1/audit")).json()["items"]
    audit = [a["action"] for a in entries]
    imported = next(a for a in entries if a["action"] == "eval.dataset_imported")
    assert imported["outcome"] == "success" and imported["namespace"] == "default"
    assert imported["data"]["result"] == "updated"
    assert {"eval.dataset_imported", "eval.run_started", "eval.completed", "run.exported"} <= set(audit)


async def test_eval_api_permissions(engine, admin):
    assert (await admin.put("/v1/namespaces/default/workflows/triage", json={"spec": V1})).status_code == 201
    assert (await admin.put("/v1/namespaces/default/evals/triage-eval", json={"spec": EVAL})).status_code == 201
    sa = await admin.put("/v1/service-accounts/watcher", json={})
    await admin.post("/v1/role-bindings", json={"role": "viewer", "subject_kind": "user", "subject_id": sa.json()["id"], "namespace": "default"})
    token = (await admin.post("/v1/api-keys", json={"name": "k", "service_account": "watcher"})).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    r = await admin.post("/v1/namespaces/default/evals/triage-eval/runs", headers=headers)
    assert r.status_code == 403
    r = await admin.post("/v1/namespaces/default/evals/triage-eval/runs")
    assert r.status_code == 201
    assert (await admin.get(f"/v1/eval-runs/{r.json()['id']}", headers=headers)).status_code == 200
    assert (await admin.post("/v1/namespaces/default/evals/nope/runs")).status_code == 404
    r = await admin.post("/v1/namespaces/default/evals/triage-eval/runs", json={"baseline": "version:9"})
    assert r.status_code == 404 and "baseline" in r.json()["detail"]
