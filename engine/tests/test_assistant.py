"""The chat assistant: tool loop over the public API, approvals, permissions."""

from __future__ import annotations

import copy
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr

from shoal.agents.providers.base import ModelResponse
from shoal.agents.providers.scripted import set_script_function
from shoal.api.app import create_app

ADMIN = "test-admin-token"

HELLO_YAML = """\
apiVersion: shoal.dev/v1
kind: Workflow
metadata: {name: word-count}
spec:
  inputs: {text: {type: string}}
  outputs: {words: "{{ steps.count.output.words }}"}
  steps:
    - id: count
      type: script
      language: python
      inputs: {text: "{{ inputs.text }}"}
      outputs: {words: {type: integer}}
      code: |
        import shoal_step
        shoal_step.set_output("words", len(shoal_step.inputs["text"].split()))
"""


def tool(name: str, input: dict, id: str) -> dict:
    return {"type": "tool_use", "id": id, "name": name, "input": input}


class Script:
    """Plays model turns in order and records every request it saw."""

    def __init__(self, *turns: list[dict]):
        self.turns = list(turns)
        self.requests = []

    def __call__(self, request):
        self.requests.append(replace(request, messages=copy.deepcopy(request.messages)))
        content = self.turns.pop(0) if self.turns else [{"type": "text", "text": "(out of script)"}]
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return ModelResponse(content=content, stop_reason=stop, usage={"input_tokens": 10, "output_tokens": 5})

    def last_results(self) -> dict[str, dict]:
        last = self.requests[-1].messages[-1]["content"]
        return {b["tool_use_id"]: b for b in last if b.get("type") == "tool_result"}


@pytest.fixture
async def api(engine):
    engine.settings.admin_token = SecretStr(ADMIN)
    engine.settings.assistant_gateway = "assistant-test"
    app = create_app(engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test") as client:
        r = await client.post("/v1/tenants", json={"slug": "acme", "name": "Acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
        client.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        r = await client.put("/v1/gateways/assistant-test",
                             json={"spec": {"type": "scripted", "config": {"function": "assistant-test"}}})
        assert r.status_code in (200, 201), r.text
        yield client


def play(*turns: list[dict]) -> Script:
    script = Script(*turns)
    set_script_function("assistant-test", script)
    return script


async def test_reads_run_and_changes_wait_for_approval(api):
    script = play(
        [{"type": "text", "text": "Let me check."},
         tool("api_get", {"path": "/v1/namespaces/default/workflows"}, "t1"),
         tool("apply_resources", {"yaml": HELLO_YAML, "summary": "Create workflow word-count"}, "t2")],
        [{"type": "text", "text": "Created **word-count**."}],
    )
    r = await api.post("/v1/assistant/chat", json={"message": "Make a workflow that counts words", "page": "/workflows"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [p["id"] for p in out["pending"]] == ["t2"]
    pending = out["pending"][0]
    assert pending["summary"] == "Create workflow word-count" and pending["namespace"] == "default"
    assert pending["plan"] == [{"kind": "Workflow", "name": "word-count", "namespace": "default", "outcome": "created",
                                "version": None, "error": None}]
    assert pending["kinds"] == [{"kind": "Workflow", "name": "word-count"}]
    # the read was answered; nothing was applied yet
    assert out["messages"][-1]["role"] == "user" and out["messages"][-1]["content"][0]["tool_use_id"] == "t1"
    assert (await api.get("/v1/namespaces/default/workflows/word-count")).status_code == 404
    assert "namespace `default`" in script.requests[0].system and "/workflows" in script.requests[0].system

    r = await api.post("/v1/assistant/chat", json={"messages": out["messages"], "decisions": {"t2": {"approve": True}}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["pending"] == [] and out["messages"][-1]["content"][0]["text"] == "Created **word-count**."
    results = script.last_results()
    assert set(results) == {"t1", "t2"}  # both results in the one user turn after the tool calls
    assert '"outcome": "created"' in results["t2"]["content"] and "is_error" not in results["t2"]

    # the workflow it made runs deterministically
    r = await api.post("/v1/namespaces/default/workflows/word-count/runs", json={"inputs": {"text": "one two three"}})
    assert r.status_code == 201, r.text
    audit = (await api.get("/v1/audit", params={"action": "workflow.created"})).json()
    assert any(e["target"] == "default/word-count" for e in audit.get("items", audit))


async def test_invalid_yaml_goes_back_to_the_model(api):
    script = play(
        [tool("apply_resources", {"yaml": "apiVersion: shoal.dev/v1\nkind: Workflow\nmetadata: {name: bad}\nspec: {steps: [{id: s, type: script}]}\n",
                                  "summary": "Create bad"}, "t1")],
        [tool("apply_resources", {"yaml": HELLO_YAML, "summary": "Create word-count"}, "t2")],
    )
    out = (await api.post("/v1/assistant/chat", json={"message": "go"})).json()
    assert [p["id"] for p in out["pending"]] == ["t2"]
    first_result = script.requests[1].messages[-1]["content"][0]
    assert first_result["is_error"] and "Validation failed" in first_result["content"] and "exactly one of" in first_result["content"]


async def test_declined_and_superseded_actions(api):
    script = play(
        [tool("api_call", {"method": "DELETE", "path": "/v1/namespaces/default/workflows/x", "summary": "Delete x"}, "t1")],
        [{"type": "text", "text": "OK, I won't."}],
        [tool("api_call", {"method": "POST", "path": "/v1/namespaces/default/workflows/x/runs", "summary": "Run x"}, "t2")],
        [{"type": "text", "text": "Sure, something else."}],
    )
    out = (await api.post("/v1/assistant/chat", json={"message": "delete x"})).json()
    assert out["pending"][0]["method"] == "DELETE"
    # a pending action needs a decision or a new message
    r = await api.post("/v1/assistant/chat", json={"messages": out["messages"]})
    assert r.status_code == 422
    out = (await api.post("/v1/assistant/chat", json={"messages": out["messages"],
                                                       "decisions": {"t1": {"approve": False, "note": "keep it"}}})).json()
    result = script.last_results()["t1"]
    assert result["is_error"] and "declined" in result["content"] and "keep it" in result["content"]

    out = (await api.post("/v1/assistant/chat", json={"messages": out["messages"], "message": "run x"})).json()
    assert out["pending"][0]["id"] == "t2"
    out = (await api.post("/v1/assistant/chat", json={"messages": out["messages"], "message": "never mind"})).json()
    last_user = script.requests[-1].messages[-1]["content"]
    assert last_user[0]["tool_use_id"] == "t2" and last_user[0]["is_error"]
    assert last_user[-1] == {"type": "text", "text": "never mind"}


async def test_calls_use_the_callers_permissions(api):
    sa = await api.put("/v1/service-accounts/viewer", json={})
    await api.post("/v1/role-bindings", json={"role": "viewer", "subject_kind": "user", "subject_id": sa.json()["id"],
                                               "namespace": "default"})
    token = (await api.post("/v1/api-keys", json={"name": "k", "service_account": "viewer"})).json()["token"]
    viewer = {"Authorization": f"Bearer {token}"}
    script = play(
        [tool("api_call", {"method": "PUT", "path": "/v1/namespaces/default/secrets/x", "body": {"value": "abcd"},
                           "summary": "Set secret"}, "t1"),
         tool("api_get", {"path": "/v1/assistant"}, "t2")],
        [{"type": "text", "text": "No permission."}],
    )
    out = (await api.post("/v1/assistant/chat", json={"message": "set it"}, headers=viewer)).json()
    blocked = out["messages"][-1]["content"][0]
    assert blocked["tool_use_id"] == "t2" and blocked["is_error"] and "not available" in blocked["content"]
    r = await api.post("/v1/assistant/chat", json={"messages": out["messages"], "decisions": {"t1": {"approve": True}}},
                       headers=viewer)
    assert r.status_code == 200, r.text
    assert '"status": 403' in script.last_results()["t1"]["content"]
    audit = (await api.get("/v1/audit")).json()
    denied = next(e for e in audit["items"] if e["action"] == "access.denied")
    assert denied["source"] == "assistant" and denied["actor"] == "sa:viewer" and denied["target"] == "secrets:write"
    chat = next(e for e in audit["items"] if e["target"] == "/v1/assistant/chat")
    assert chat["request_id"] == denied["request_id"]  # the inner call carries the chat's request id


async def test_auto_approve_and_endpoint_discovery(api):
    script = play(
        [tool("list_endpoints", {"tag": "triggers"}, "t1"),
         tool("describe_endpoint", {"method": "POST", "path": "/v1/namespaces/{namespace}/triggers/{name}/fire"}, "t2"),
         tool("apply_resources", {"yaml": HELLO_YAML, "summary": "Create"}, "t3")],
        [{"type": "text", "text": "Done."}],
    )
    out = (await api.post("/v1/assistant/chat", json={"message": "go", "auto_approve": True})).json()
    assert out["pending"] == [] and out["usage"]["input_tokens"] == 20
    results = script.last_results()
    assert "POST /v1/namespaces/{namespace}/triggers/{name}/fire" in results["t1"]["content"]
    assert "/v1/assistant" not in results["t1"]["content"]
    assert "parameters" in results["t2"]["content"]
    assert (await api.get("/v1/namespaces/default/workflows/word-count")).status_code == 200


async def test_policy_and_disable(api, engine):
    play([{"type": "text", "text": "hi"}])
    assert (await api.get("/v1/assistant")).json()["model"] == engine.settings.assistant_model
    await api.put("/v1/model-policies/default", json={"spec": {
        "allow_models": ["claude-haiku-*"], "rules": [{"name": "all", "candidates": [{"model": "claude-haiku-5-5"}]}]}})
    r = await api.post("/v1/assistant/chat", json={"message": "hi"})
    assert r.status_code == 403
    engine.settings.assistant_enabled = False
    r = await api.post("/v1/assistant/chat", json={"message": "hi"})
    assert r.status_code == 503 and r.json()["code"] == "assistant_unavailable"


@pytest.mark.parametrize("path", ["/v1/assistant", "/v1/%61ssistant/chat", "/v1/%2561ssistant", "/v1/auth/logout",
                                  "/v1//assistant", "/v1/namespaces/../assistant", "/scim/v2/Users", "v1/runs"])
def test_blocked_paths_cannot_be_disguised(path):
    from shoal.assistant.chat import _check_path

    with pytest.raises(ValueError):
        _check_path(path)
    assert _check_path("/v1/namespaces/default/workflows") == "/v1/namespaces/default/workflows"
