"""Missing configuration is a typed problem that names what is missing and where to fix it."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from shoal.api.app import create_app
from shoal.core.errors import MissingConfiguration

ADMIN = "setup-admin"


@pytest.fixture
async def admin(engine, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    engine.settings.admin_token = SecretStr(ADMIN)
    engine.settings.assistant_gateway = "anthropic"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine=engine)), base_url="http://shoal.test") as c:
        r = await c.post("/v1/tenants", json={"slug": "acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
        c.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        yield c


async def test_assistant_without_a_gateway_points_at_the_gateway_admin(admin):
    info = (await admin.get("/v1/assistant")).json()
    assert info["enabled"] and info["ready"] is False
    problem = info["problem"]
    assert problem["code"] == "missing_configuration"
    assert problem["missing"] == {"kind": "Gateway", "name": "anthropic"}
    assert problem["fix"] == {"href": "/admin/gateways/new?name=anthropic", "label": "Add the anthropic gateway",
                              "permission": "gateways:manage", "namespace": None}
    assert "ANTHROPIC_API_KEY" in problem["detail"]

    r = await admin.post("/v1/assistant/chat", json={"message": "hi"})
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "missing_configuration" and body["type"].endswith("/missing_configuration")
    assert body["details"]["fix"]["href"] == "/admin/gateways/new?name=anthropic"


async def test_unknown_gateway_and_missing_gateway_secret(engine, admin):
    engine.settings.assistant_gateway = "corp-llm"
    problem = (await admin.get("/v1/assistant")).json()["problem"]
    assert problem["missing"] == {"kind": "Gateway", "name": "corp-llm"} and "no gateway named" in problem["detail"]

    await admin.put("/v1/gateways/corp-llm", json={"spec": {"type": "scripted", "api_key_secret": "corp-key"}})
    problem = (await admin.get("/v1/assistant")).json()["problem"]
    assert problem["missing"] == {"kind": "Secret", "name": "corp-key", "namespace": "shared"}
    assert problem["fix"] == {"href": "/ns/shared/secrets", "label": "Set the secret", "permission": "secrets:write",
                              "namespace": "shared"}

    await admin.put("/v1/namespaces/shared/secrets/corp-key", json={"value": "sk-test"})
    info = (await admin.get("/v1/assistant")).json()
    assert info["ready"] is True and info["problem"] is None


async def test_setup_status_lists_what_is_missing(admin):
    status = (await admin.get("/v1/setup")).json()
    checks = {c["id"]: c for c in status["checks"]}
    assert status["ready"] is False
    assert checks["models"]["ready"] is False and checks["models"]["problem"]["fix"]["href"] == "/admin/gateways/new"
    assert checks["assistant"]["ready"] is False
    assert checks["sandbox"]["ready"] is True
    anthropic = next(g for g in status["gateways"] if g["name"] == "anthropic")
    assert anthropic == {**anthropic, "managed": False, "ready": False}

    await admin.put("/v1/gateways/team", json={"spec": {"type": "scripted"}})
    status = (await admin.get("/v1/setup")).json()
    assert {c["id"]: c["ready"] for c in status["checks"]}["models"] is True
    assert next(g for g in status["gateways"] if g["name"] == "team") == {"name": "team", "managed": True, "ready": True,
                                                                          "problem": None}


async def test_a_step_that_hits_missing_configuration_says_how_to_fix_it(engine, admin):
    await admin.put("/v1/namespaces/default/agents/writer", json={"spec": {"provider": "nowhere", "model": "m"}})
    await admin.put("/v1/namespaces/default/workflows/write", json={"spec": {"steps": [
        {"id": "write", "type": "agent", "agent": "writer", "prompt": "write hi"}]}})
    run_id = (await admin.post("/v1/namespaces/default/workflows/write/runs", json={"inputs": {}})).json()["id"]
    await engine.run_until_idle()
    run = (await admin.get(f"/v1/runs/{run_id}")).json()
    assert run["status"] == "failed"
    step = run["steps"][0]
    assert step["attempt"] == 1 and step["error"]["retryable"] is False  # retrying will not help
    for error in (step["error"], run["error"]):
        assert error["code"] == "missing_configuration"
        assert error["missing"] == {"kind": "Gateway", "name": "nowhere"}
        assert error["fix"]["href"] == "/admin/gateways/new?name=nowhere"


async def test_missing_agent_and_connection(engine, admin):
    assert (await admin.put("/v1/namespaces/team", json={})).status_code in (200, 201)
    await admin.put("/v1/namespaces/team/workflows/x", json={"spec": {"steps": [
        {"id": "a", "type": "agent", "agent": "ghost", "prompt": "hi"}]}})
    run_id = (await admin.post("/v1/namespaces/team/workflows/x/runs", json={"inputs": {}})).json()["id"]
    await engine.run_until_idle()
    error = (await admin.get(f"/v1/runs/{run_id}")).json()["error"]
    assert error["missing"] == {"kind": "Agent", "name": "ghost"}
    assert error["fix"] == {"href": "/ns/team/agents/new?name=ghost", "label": "Add the ghost agent",
                            "permission": "agents:write", "namespace": "team"}

    tenant = (await engine.store.get_tenant_by_slug("acme")).id
    with pytest.raises(MissingConfiguration) as exc:
        await engine.integrations.resolve_name(tenant, "github", None)
    assert exc.value.details["fix"]["href"] == "/ns/default/connections/new?integration=github"
    with pytest.raises(MissingConfiguration) as exc:
        await engine.integrations.connection(tenant, "nope")
    assert exc.value.details["missing"] == {"kind": "Connection", "name": "nope"}


def test_cli_prints_the_fix(capsys):
    import typer

    from shoal.cli.client import APIError
    from shoal.cli.main import fail

    body = {"code": "missing_configuration", "detail": "no gateway named 'anthropic' is set up", "details": {
        "fix": {"href": "/admin/gateways/new?name=anthropic", "label": "Add the anthropic gateway", "permission": "gateways:manage"}}}
    with pytest.raises(typer.Exit):
        fail(APIError(409, body["detail"], body, server="https://shoal.example"))
    out = capsys.readouterr().err
    assert "fix: Add the anthropic gateway (needs gateways:manage): https://shoal.example/admin/gateways/new?name=anthropic" in out
