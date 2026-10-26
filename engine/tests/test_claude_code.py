"""The claude-code runtime runs Claude Code headless in the sandbox, signed in by a central gateway.

A fake ``claude`` on PATH stands in for the CLI: in print mode it records its
arguments and which credential it was given, then streams stream-json; for
``setup-token`` it plays the sign-in prompt.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import httpx
import pytest
from pydantic import SecretStr

from shoal.agents.claude_code import ClaudeCodeGatewayProvider, to_response, transcript
from shoal.agents.providers.base import ModelRequest
from shoal.api.app import create_app
from shoal.core.errors import StepError
from shoal.core.models import parse_resource
from shoal.core.scope import use_namespace

ADMIN = "cc-admin"
TOKEN = "sk-ant-oat01-" + "t" * 40

FAKE_CLAUDE = r'''#!PYTHON
import json, os, sys
if sys.argv[1:2] == ["setup-token"]:
    print("Browser didn't open? Use the url below to sign in")
    print("\x1b]8;;https://claude.com/cai/oauth/authorize?code=true&state=abc\x07https://claude.com/cai/oauth/authorize?code=true&state=abc\x1b]8;;\x07")
    print("Paste\x1b[8Gcode here if prompted >", flush=True)
    code = sys.stdin.readline().strip()
    print("Your token:\n" + "TOKEN" if code == "good#code" else "OAuth error: Invalid code. Please make sure the full code was copied", flush=True)
    sys.stdin.readline()
    sys.exit(0)
prompt = sys.stdin.read()
record = {"args": sys.argv[1:], "prompt": prompt,
          "api_key": os.environ.get("ANTHROPIC_API_KEY"), "oauth": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
          "base_url": os.environ.get("ANTHROPIC_BASE_URL"), "headers": os.environ.get("ANTHROPIC_CUSTOM_HEADERS")}
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps(record) + "\n")
def say(m): print(json.dumps(m), flush=True)
if sys.argv[sys.argv.index("--output-format") + 1] == "json":  # one model turn for the general gateway
    system = sys.argv[sys.argv.index("--system-prompt") + 1]
    if "<tool_result" in prompt:
        turn = {"text": "You have workflows: look.", "tool_calls": []}
    elif "## finish" in system:
        turn = {"text": "", "tool_calls": [{"name": "finish", "input": {"answer": "from the model gateway", "summary": "done"}}]}
    elif "## api_get" in system:
        turn = {"text": "Let me look.", "tool_calls": [{"name": "api_get", "input": {"path": "/v1/namespaces/default/workflows"}}]}
    else:
        turn = {"text": "hello", "tool_calls": []}
    say({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "total_cost_usd": 0.01,
         "usage": {"input_tokens": 50, "output_tokens": 5}, "result": "", "structured_output": turn})
    sys.exit(0)
say({"type": "system", "subtype": "init", "model": "claude-opus-5-5", "tools": ["Bash", "Edit"], "permissionMode": "bypassPermissions"})
say({"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking at the repo with " + str(os.environ.get("ANTHROPIC_API_KEY"))},
     {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}})
say({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "README.md"}]}})
if os.environ.get("SHOAL_FAKE_CLAUDE_FAIL"):
    say({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 3})
    sys.exit(1)
say({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "total_cost_usd": 0.42,
     "usage": {"input_tokens": 100, "output_tokens": 20}, "result": "done",
     "structured_output": {"answer": "README.md", "summary": "Listed the repo"}})
'''.replace("PYTHON", sys.executable).replace('"TOKEN"', repr(TOKEN))


def doc(kind: str, name: str, spec: dict) -> object:
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": kind, "metadata": {"name": name}, "spec": spec})


@pytest.fixture
async def world(engine, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    engine.claude_logins.cli = str(fake)
    engine.settings.claude_code_cli = str(fake)
    engine.settings.admin_token = SecretStr(ADMIN)
    app = create_app(engine=engine)
    clients: list[httpx.AsyncClient] = []

    def client(token: str) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test",
                              headers={"Authorization": f"Bearer {token}"})
        clients.append(c)
        return c

    r = await client(ADMIN).post("/v1/tenants", json={"slug": "acme"})
    admin = client(r.json()["api_key"])
    tenant = (await engine.store.get_tenant_by_slug("acme")).id
    with use_namespace("shared"):
        await engine.secrets.writable().put(tenant, "anthropic-key", "sk-ant-api-secret-value")
    await engine.store.apply_resource(tenant, doc("Agent", "coder", {
        "provider": "claude-code", "runtime": "claude-code", "model": "claude-opus-5-5", "instructions": "Be brief.",
        "runtime_config": {"cli": str(fake), "install": False, "poll_interval": 0.02, "allowed_tools": ["Bash(ls *)", "Read"]}}))
    await engine.store.apply_resource(tenant, doc("Workflow", "look", {"steps": [{
        "id": "look", "type": "agent", "agent": "coder", "prompt": "List the repo",
        "outputs": {"answer": {"type": "string"}}}]}))

    def calls() -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    yield engine, tenant, admin, client, calls
    for c in clients:
        await c.aclose()


async def _person(engine, tenant: str, admin, username: str) -> str:
    r = await admin.post("/v1/users", json={"username": username, "email": f"{username}@acme.test"})
    await admin.post("/v1/role-bindings", json={"role": "operator", "subject_kind": "user", "subject_id": r.json()["id"], "namespace": None})
    _, token = await engine.store.create_api_key(tenant, f"{username}-key", ["*"], owner_id=r.json()["id"])
    return token


async def _run(engine, tenant: str, actor: str) -> str:
    run = await engine.executor.start_run(tenant, "look", {}, started_by=actor)
    await engine.run_until_idle()
    return run.id


async def test_api_key_mode_streams_events_and_returns_outputs(world):
    engine, tenant, admin, _, calls = world
    r = await admin.put("/v1/gateways/claude-code", json={"spec": {
        "type": "claude-code", "api_key_secret": "anthropic-key", "base_url": "https://llm.acme.test",
        "headers": {"x-team": "platform"}}})
    assert r.status_code == 201, r.text

    run_id = await _run(engine, tenant, "sa:ci")  # API key mode serves service accounts and schedules too
    step = await engine.store.get_step(run_id, "look")
    assert step.status == "succeeded", step.error
    assert step.output == {"answer": "README.md", "summary": "Listed the repo"}
    assert step.usage["cost_usd"] == pytest.approx(0.42) and step.usage["input_tokens"] == 100

    [call] = calls()
    assert call["api_key"] == "sk-ant-api-secret-value" and call["oauth"] is None
    assert call["base_url"] == "https://llm.acme.test" and call["headers"] == "x-team: platform"
    args = call["args"]
    assert args[:2] == ["-p", "--output-format"] and "stream-json" in args and "--verbose" in args
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert args[args.index("--max-turns") + 1] == "60"
    assert args[args.index("--append-system-prompt") + 1] == "Be brief."
    assert args[args.index("--allowed-tools") + 1 : args.index("--allowed-tools") + 3] == ["Bash(ls *)", "Read"]
    schema = json.loads(args[args.index("--json-schema") + 1])
    assert set(schema["required"]) == {"answer", "summary"}
    assert "List the repo" in call["prompt"]

    events = await engine.store.list_events(tenant, run_id)
    types = [e.type for e in events]
    for expected in ("agent.claude_code.started", "agent.claude_code.init", "agent.claude_code.message",
                     "agent.claude_code.tool_results", "agent.claude_code.result"):
        assert expected in types
    message = next(e for e in events if e.type == "agent.claude_code.message")
    assert message.data["tool_calls"][0]["name"] == "Bash"
    assert "sk-ant-api-secret-value" not in json.dumps([e.data for e in events])  # the key is redacted


async def test_cli_failures_fail_the_step(world, monkeypatch):
    engine, tenant, admin, _, _ = world
    await admin.put("/v1/gateways/claude-code", json={"spec": {"type": "claude-code", "api_key_secret": "anthropic-key"}})
    monkeypatch.setenv("SHOAL_FAKE_CLAUDE_FAIL", "1")
    step = await engine.store.get_step(await _run(engine, tenant, "sa:ci"), "look")
    assert step.status == "failed" and "error_max_turns" in step.error["message"]


async def test_agents_must_name_a_claude_code_gateway(world):
    engine, tenant, admin, _, _ = world
    await admin.put("/v1/gateways/claude-code", json={"spec": {"type": "anthropic", "api_key_secret": "anthropic-key"}})
    step = await engine.store.get_step(await _run(engine, tenant, "sa:ci"), "look")
    assert step.status == "failed" and "type claude-code" in step.error["message"]


async def test_subscription_mode_uses_each_persons_own_plan(world):
    engine, tenant, admin, client, calls = world
    subscription = {"type": "claude-code", "config": {"mode": "subscription"}}
    await admin.put("/v1/gateways/claude-code", json={"spec": subscription})
    step = await engine.store.get_step(await _run(engine, tenant, "user:dana"), "look")
    assert step.status == "failed" and "acknowledge_subscription_terms" in step.error["message"]

    subscription["config"]["acknowledge_subscription_terms"] = True
    await admin.put("/v1/gateways/claude-code", json={"spec": subscription})
    run_id = await _run(engine, tenant, "user:dana")
    assert (await engine.store.get_step(run_id, "look")).status == "waiting"
    required = next(e for e in await engine.store.list_events(tenant, run_id) if e.type == "credential.required")
    assert required.data["connection"] == "claude-code" and "connection=shared/claude-code" in required.data["connect_url"]

    dana = client(await _person(engine, tenant, admin, "dana"))
    [listed] = (await dana.get("/v1/me/claude-code")).json()
    assert listed["mode"] == "subscription" and not listed["linked"] and listed["problem"] is None and "Anthropic" in listed["terms"]
    assert (await dana.put("/v1/me/claude-code/claude-code/token", json={"token": "not-a-claude-token-at-all"})).status_code == 422
    assert (await dana.put("/v1/me/claude-code/claude-code/token", json={"token": TOKEN})).status_code == 204
    await engine.run_until_idle()
    assert (await engine.store.get_step(run_id, "look")).status == "succeeded"
    [call] = calls()
    assert call["oauth"] == TOKEN and call["api_key"] is None
    assert step.usage.get("cost_usd") is None  # the plan pays, not an API key

    [listed] = (await dana.get("/v1/me/claude-code")).json()
    assert listed["linked"] and TOKEN not in json.dumps(listed)
    [cred] = (await admin.get("/v1/users/dana/credentials")).json()
    assert (cred["namespace"], cred["connection"], cred["kind"]) == ("shared", "claude-code", "claude-subscription")

    # Nobody else's runs use Dana's plan; service accounts cannot use subscription mode at all.
    step = await engine.store.get_step(await _run(engine, tenant, "sa:ci"), "look")
    assert step.status == "failed" and "service accounts" in step.error["message"]
    assert (await engine.store.get_step(await _run(engine, tenant, "user:erin"), "look")).status == "waiting"

    assert (await dana.delete("/v1/me/claude-code/claude-code")).status_code == 204
    assert not (await dana.get("/v1/me/claude-code")).json()[0]["linked"]


async def test_browser_sign_in_relays_the_cli_flow(world):
    engine, tenant, admin, client, _ = world
    await admin.put("/v1/gateways/claude-code", json={"spec": {
        "type": "claude-code", "config": {"mode": "subscription", "acknowledge_subscription_terms": True}}})
    dana = client(await _person(engine, tenant, admin, "dana"))

    r = await dana.post("/v1/me/claude-code/claude-code/login")
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://claude.com/cai/oauth/authorize?code=true&state=abc"
    r = await dana.post(f"/v1/me/claude-code/claude-code/login/{r.json()['login_id']}", json={"code": "bad#code"})
    assert r.status_code == 422 and "Invalid code" in r.text

    login = (await dana.post("/v1/me/claude-code/claude-code/login")).json()
    erin = client(await _person(engine, tenant, admin, "erin"))
    assert (await erin.post(f"/v1/me/claude-code/claude-code/login/{login['login_id']}", json={"code": "good#code"})).status_code == 404
    r = await dana.post(f"/v1/me/claude-code/claude-code/login/{login['login_id']}", json={"code": "good#code"})
    assert r.status_code == 204, r.text
    assert (await dana.get("/v1/me/claude-code")).json()[0]["linked"]
    assert (await engine.user_credentials.get(tenant, "user:dana", "shared", "claude-code")) == {"oauth_token": TOKEN}

    # API key gateways have nothing to link; service accounts have no subscription of their own.
    await admin.put("/v1/gateways/keyed", json={"spec": {"type": "claude-code", "api_key_secret": "anthropic-key"}})
    assert (await dana.post("/v1/me/claude-code/keyed/login")).status_code == 422
    assert (await admin.post("/v1/me/claude-code/claude-code/login")).status_code == 403


SUBSCRIPTION = {"type": "claude-code", "config": {"mode": "subscription", "acknowledge_subscription_terms": True}}


async def test_assistant_chats_on_the_persons_own_plan(world):
    engine, tenant, admin, client, calls = world
    await admin.put("/v1/gateways/claude-max", json={"spec": SUBSCRIPTION})
    engine.settings.assistant_gateway = "claude-max"
    dana = client(await _person(engine, tenant, admin, "dana"))

    r = await dana.post("/v1/assistant/chat", json={"message": "What workflows do I have?"})
    assert r.status_code == 503 and "has not linked" in r.text and "shared/claude-max" in r.text
    assert (await admin.post("/v1/assistant/chat", json={"message": "hi"})).status_code == 503  # service accounts: no plan

    assert (await dana.put("/v1/me/claude-code/claude-max/token", json={"token": TOKEN})).status_code == 204
    r = await dana.post("/v1/assistant/chat", json={"message": "What workflows do I have?"})
    assert r.status_code == 200, r.text
    assert r.json()["messages"][-1]["content"][0]["text"] == "You have workflows: look."
    first, second = calls()
    assert first["oauth"] == TOKEN and first["api_key"] is None
    args = first["args"]
    assert args[args.index("--tools") + 1] == "" and "--strict-mcp-config" in args
    assert args[args.index("--model") + 1] == engine.settings.assistant_model
    assert "What workflows do I have?" in first["prompt"] and '<tool_call id="' in second["prompt"]
    assert "look" in second["prompt"]  # the tool result reached the model


async def test_tool_loop_agents_use_the_gateway_as_a_model(world):
    engine, tenant, admin, client, calls = world
    await admin.put("/v1/gateways/claude-max", json={"spec": SUBSCRIPTION})
    await engine.store.apply_resource(tenant, doc("Agent", "coder", {"provider": "claude-max", "model": "sonnet", "tools": []}))
    run_id = await _run(engine, tenant, "user:dana")
    assert (await engine.store.get_step(run_id, "look")).status == "waiting"  # Dana's plan isn't linked yet
    dana = client(await _person(engine, tenant, admin, "dana"))
    await dana.put("/v1/me/claude-code/claude-max/token", json={"token": TOKEN})
    await engine.run_until_idle()
    step = await engine.store.get_step(run_id, "look")
    assert step.status == "succeeded", step.error
    assert step.output == {"answer": "from the model gateway", "summary": "done"}
    assert calls()[-1]["oauth"] == TOKEN and "cost_usd" not in step.usage

    # An API key gateway serves everyone, including service accounts.
    await admin.put("/v1/gateways/claude-max", json={"spec": {"type": "claude-code", "api_key_secret": "anthropic-key"}})
    step = await engine.store.get_step(await _run(engine, tenant, "sa:ci"), "look")
    assert step.status == "succeeded" and calls()[-1]["api_key"] == "sk-ant-api-secret-value"


async def test_model_calls_that_act_for_nobody_need_an_api_key():
    with pytest.raises(StepError, match="acts for nobody"):
        await ClaudeCodeGatewayProvider().complete(ModelRequest(model="m", system="", messages=[], tools=[]))


def test_transcript_and_turns_round_trip():
    text = transcript([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "api_get", "input": {"path": "/x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "ok"}], "is_error": True}]},
    ])
    assert '<tool_call id="t1" name="api_get">{"path": "/x"}</tool_call>' in text
    assert '<tool_result id="t1" is_error="true">ok</tool_result>' in text
    response = to_response({"structured_output": {"text": "x", "tool_calls": [{"name": "api_get", "input": {}}, {"name": "rm", "input": {}}]},
                            "usage": {"input_tokens": 3}, "total_cost_usd": 1.0}, ["api_get"], "subscription", "m")
    assert [b["type"] for b in response.content] == ["text", "tool_use"] and response.stop_reason == "tool_use"
    assert "cost_usd" not in response.usage
