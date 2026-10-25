from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from awm.api.app import create_app

ADMIN = "test-admin-token"

HELLO = {
    "metadata": {"description": "Says hello", "labels": {"team": "platform"}},
    "spec": {
        "inputs": {"name": {"type": "string"}},
        "outputs": {"greeting": "{{ steps.greet.output.text }}"},
        "steps": [{"id": "greet", "type": "set", "values": {"text": "Hello {{ inputs.name }}"}}],
    },
}


@pytest.fixture
async def api(engine):
    engine.settings.admin_token = SecretStr(ADMIN)
    app = create_app(engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://awm.test") as client:
        yield client


@pytest.fixture
async def admin(api):
    """A client authenticated with a fresh tenant's bootstrap key."""
    r = await api.post("/v1/tenants", json={"slug": "acme", "name": "Acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 201, r.text
    api.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
    return api


async def _key_for(admin, name: str, role: str, namespace: str | None) -> str:
    """A key for a new service account holding ``role`` (tenant-wide when namespace is None)."""
    sa = await admin.put(f"/v1/service-accounts/{name}", json={})
    assert sa.status_code == 201, sa.text
    r = await admin.post("/v1/role-bindings", json={"role": role, "subject_kind": "user", "subject_id": sa.json()["id"], "namespace": namespace})
    assert r.status_code == 201, r.text
    r = await admin.post("/v1/api-keys", json={"name": "k", "service_account": name})
    assert r.status_code == 201, r.text
    return r.json()["token"]


async def test_auth_errors_are_problems(api):
    r = await api.get("/v1/me")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "unauthorized"
    r = await api.get("/v1/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_tenant_bootstrap_and_me(admin):
    me = (await admin.get("/v1/me")).json()
    assert me["actor"] == "sa:admin"
    assert me["tenant"]["slug"] == "acme"
    assert set(me["namespaces"]) >= {"default", "shared"}
    perms = (await admin.get("/v1/me/permissions")).json()
    assert "users:manage" in perms["tenant"]


async def test_resource_put_is_create_or_replace(admin):
    assert (await admin.put("/v1/namespaces/team-a", json={"display_name": "Team A"})).status_code == 201
    url = "/v1/namespaces/team-a/workflows/hello"
    r = await admin.put(url, json=HELLO)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["apiVersion"] == "awm.dev/v1" and body["kind"] == "Workflow"
    assert body["metadata"]["name"] == "hello" and body["metadata"]["namespace"] == "team-a"
    assert body["metadata"]["version"] == 1 and body["metadata"]["labels"] == {"team": "platform"}

    r = await admin.put(url, json=HELLO)  # unchanged: same version
    assert r.status_code == 200 and r.json()["metadata"]["version"] == 1
    changed = {**HELLO, "metadata": {"description": "Greets"}}
    r = await admin.put(url, json=changed)
    assert r.status_code == 200 and r.json()["metadata"]["version"] == 2

    got = (await admin.get(url)).json()
    assert got["spec"]["steps"][0]["id"] == "greet" and got["metadata"]["description"] == "Greets"
    assert len((await admin.get(f"{url}/versions")).json()) == 2
    listed = (await admin.get("/v1/namespaces/team-a/workflows")).json()
    assert [i["metadata"]["name"] for i in listed] == ["hello"]

    bad = await admin.put("/v1/namespaces/team-a/workflows/broken", json={"spec": {"steps": []}})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_request"

    assert (await admin.delete(url)).status_code == 204
    assert (await admin.get(url)).status_code == 404
    assert (await admin.get("/v1/namespaces/nope/workflows")).status_code == 404


async def test_apply_and_runs(admin):
    doc = """
apiVersion: awm.dev/v1
kind: Workflow
metadata: {name: hello}
spec:
  inputs: {name: {type: string}}
  outputs: {greeting: "{{ steps.greet.output.text }}"}
  steps:
    - {id: greet, type: set, values: {text: "Hello {{ inputs.name }}"}}
"""
    r = await admin.post("/v1/apply?namespace=default", content=doc, headers={"content-type": "application/yaml"})
    assert r.status_code == 200, r.text
    assert r.json()[0]["outcome"] == "created"

    r = await admin.post("/v1/namespaces/default/workflows/hello/runs", json={"inputs": {"name": "Trevor"}})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    assert r.json()["initiator"]["actor"] == "sa:admin"
    await admin._transport.app.state.engine.run_until_idle()  # type: ignore[attr-defined]

    run = (await admin.get(f"/v1/runs/{run_id}")).json()
    assert run["status"] == "succeeded" and run["outputs"] == {"greeting": "Hello Trevor"}
    assert [s["step_id"] for s in run["steps"]] == ["greet"]
    events = (await admin.get(f"/v1/runs/{run_id}/events")).json()
    assert events and events[-1]["type"].startswith("run.")
    page = (await admin.get("/v1/namespaces/default/runs")).json()
    assert page["items"][0]["id"] == run_id
    missing = await admin.post("/v1/namespaces/default/workflows/hello/runs", json={"inputs": {}})
    assert missing.status_code == 422


async def test_secrets_are_write_only(admin):
    r = await admin.put("/v1/namespaces/default/secrets/gh-token", json={"value": "s3cret"})
    assert r.status_code == 201, r.text
    assert "value" not in r.json()
    got = await admin.get("/v1/namespaces/default/secrets/gh-token")
    assert got.status_code == 200 and "s3cret" not in got.text
    assert [s["name"] for s in (await admin.get("/v1/namespaces/default/secrets")).json()] == ["gh-token"]
    assert (await admin.delete("/v1/namespaces/default/secrets/gh-token")).status_code == 204
    assert (await admin.get("/v1/namespaces/default/secrets/gh-token")).status_code == 404


async def test_rbac_namespace_viewer(admin, api):
    await admin.put("/v1/namespaces/team-a", json={})
    await admin.put("/v1/namespaces/team-b", json={})
    await admin.put("/v1/namespaces/team-a/workflows/hello", json=HELLO)
    token = await _key_for(admin, "val", "viewer", "team-a")

    viewer = httpx.AsyncClient(transport=api._transport, base_url="http://awm.test", headers={"Authorization": f"Bearer {token}"})
    async with viewer:
        assert (await viewer.get("/v1/namespaces/team-a/workflows/hello")).status_code == 200
        r = await viewer.put("/v1/namespaces/team-a/workflows/hello", json=HELLO)
        assert r.status_code == 403 and r.json()["code"] == "forbidden"
        assert (await viewer.get("/v1/namespaces/team-b/workflows")).status_code == 403
        assert (await viewer.get("/v1/users")).status_code == 403
        # anyone can mint keys for themselves, but never broader than the key they hold
        narrow = await viewer.post("/v1/api-keys", json={"name": "ci", "scopes": ["workflows:read"], "namespaces": ["team-a"]})
        assert narrow.status_code == 201
        viewer.headers["Authorization"] = f"Bearer {narrow.json()['token']}"
        assert (await viewer.post("/v1/api-keys", json={"name": "x"})).status_code == 403
        assert (await viewer.get("/v1/namespaces/team-a/runs")).status_code == 403


async def test_admin_roles_and_bindings(admin):
    r = await admin.put("/v1/roles/runner", json={"description": "Run things", "permissions": ["runs:create", "workflows:read"]})
    assert r.status_code == 201, r.text
    assert (await admin.put("/v1/roles/runner", json={"permissions": ["runs:*"]})).status_code == 200
    assert (await admin.put("/v1/roles/viewer", json={"permissions": ["runs:*"]})).status_code in (403, 409, 422)
    bad = await admin.put("/v1/roles/bogus", json={"permissions": ["nope:fly"]})
    assert bad.status_code == 422
    roles = {r["name"]: r for r in (await admin.get("/v1/roles")).json()}
    assert roles["tenant-admin"]["builtin"] and not roles["runner"]["builtin"]

    group = (await admin.post("/v1/groups", json={"display_name": "SRE"})).json()
    b = await admin.post("/v1/role-bindings", json={"role": "runner", "subject_kind": "group", "subject_id": group["id"], "namespace": "default"})
    assert b.status_code == 201
    members = (await admin.get("/v1/namespaces/default/members")).json()
    assert any(m["subject_name"] == "SRE" for m in members)
    assert (await admin.delete(f"/v1/role-bindings/{b.json()['id']}")).status_code == 204
    audit = (await admin.get("/v1/audit")).json()
    assert audit


async def test_oidc_provider_secret_is_write_only(admin):
    body = {"issuer": "https://idp.example.com", "client_id": "awm", "client_secret": "shh", "groups_claim": "groups"}
    r = await admin.put("/v1/oidc-providers/okta", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["has_client_secret"] is True and "shh" not in r.text
    providers = (await admin.get("/v1/auth/providers?tenant=acme")).json()
    assert providers[0]["login_url"].endswith("/v1/auth/login/acme/okta")


async def test_scim_users_and_groups(admin):
    token = await _key_for(admin, "okta", "tenant-admin", None)
    h = {"Authorization": f"Bearer {token}", "content-type": "application/scim+json"}
    user = {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": "ada@acme.io", "externalId": "00u1",
            "name": {"givenName": "Ada", "familyName": "Lovelace"}, "emails": [{"value": "ada@acme.io", "primary": True}], "active": True}
    r = await admin.post("/scim/v2/Users", json=user, headers=h)
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert (await admin.post("/scim/v2/Users", json=user, headers=h)).status_code == 409
    found = (await admin.get('/scim/v2/Users?filter=userName eq "ada@acme.io"', headers=h)).json()
    assert found["totalResults"] == 1

    g = await admin.post("/scim/v2/Groups", json={"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"], "displayName": "eng", "members": [{"value": uid}]}, headers=h)
    assert g.status_code == 201, g.text
    gid = g.json()["id"]
    patch = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], "Operations": [{"op": "remove", "path": f'members[value eq "{uid}"]'}]}
    assert (await admin.patch(f"/scim/v2/Groups/{gid}", json=patch, headers=h)).status_code == 200
    assert (await admin.get(f"/scim/v2/Groups/{gid}", headers=h)).json().get("members", []) == []

    deactivate = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], "Operations": [{"op": "replace", "value": {"active": False}}]}
    r = await admin.patch(f"/scim/v2/Users/{uid}", json=deactivate, headers=h)
    assert r.status_code == 200 and r.json()["active"] is False
    missing = await admin.get("/scim/v2/Users/nope", headers=h)
    assert missing.status_code == 404 and missing.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]


async def test_a2a_inbound(admin):
    await admin.put("/v1/namespaces/default/workflows/hello", json=HELLO)
    card = (await admin.get("/v1/namespaces/default/a2a/hello/.well-known/agent-card.json")).json()
    assert card["url"] == "http://awm.test/v1/namespaces/default/a2a/hello"
    assert card["skills"][0]["inputSchema"]["required"] == ["name"]

    send = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": "Ada"}]}}}
    r = (await admin.post("/v1/namespaces/default/a2a/hello", json=send)).json()
    task = r["result"]
    assert task["kind"] == "task" and task["status"]["state"] == "submitted"
    await admin._transport.app.state.engine.run_until_idle()  # type: ignore[attr-defined]

    got = (await admin.post("/v1/namespaces/default/a2a/hello", json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task["id"]}})).json()
    assert got["result"]["status"]["state"] == "completed"
    assert got["result"]["artifacts"][0]["parts"][0]["data"] == {"greeting": "Hello Ada"}
    unknown = (await admin.post("/v1/namespaces/default/a2a/hello", json={"jsonrpc": "2.0", "id": 3, "method": "nope"})).json()
    assert unknown["error"]["code"] == -32601


async def test_openapi_and_catalogues(admin):
    spec = (await admin.get("/v1/openapi.json")).json()
    assert "/v1/namespaces/{namespace}/workflows/{name}" in spec["paths"]
    assert (await admin.get("/healthz")).json() == {"status": "ok"}
    kinds = (await admin.get("/v1/schemas")).json()
    assert kinds
    assert any(i["key"] == "github" for i in (await admin.get("/v1/integrations")).json())


async def test_reads_return_the_spec_as_written(admin):
    """Declarative clients (Terraform) diff against what they wrote, so no server defaults leak in."""
    spec = {"provider": "anthropic", "model": "claude-opus-5-5", "instructions": "Fix bugs.", "limits": {"timeout": "90m", "budget_usd": 15}}
    r = await admin.put("/v1/namespaces/default/agents/fixer", json={"spec": spec})
    assert r.status_code == 201, r.text
    assert r.json()["spec"] == spec
    respelled = {**spec, "limits": {"timeout": "1h30m", "budget_usd": 15.0}, "skills": None}
    r = await admin.put("/v1/namespaces/default/agents/fixer", json={"spec": respelled})
    assert r.json()["metadata"]["version"] == 1  # same meaning: no new version
    assert r.json()["spec"]["limits"]["timeout"] == "1h30m" and "skills" not in r.json()["spec"]


async def test_annotations_are_kept_and_unversioned(admin):
    url = "/v1/namespaces/default/workflows/hello"
    layout = {"ui.shoal.dev/layout": '{"greet":[120,80]}'}
    r = await admin.put(url, json={**HELLO, "metadata": {**HELLO["metadata"], "annotations": layout}})
    assert r.json()["metadata"]["annotations"] == layout and r.json()["metadata"]["version"] == 1
    r = await admin.put(url, json=HELLO)  # a client that does not know about annotations
    assert r.json()["metadata"]["annotations"] == layout
    moved = {"ui.shoal.dev/layout": '{"greet":[300,80]}'}
    r = await admin.put(url, json={**HELLO, "metadata": {**HELLO["metadata"], "annotations": moved}})
    assert r.json()["metadata"]["annotations"] == moved and r.json()["metadata"]["version"] == 1


async def test_audit_covers_writes_denials_and_failures(admin, api):
    await admin.put("/v1/namespaces/team-a", json={})
    await admin.put("/v1/namespaces/team-a/workflows/hello", json=HELLO)
    await admin.put("/v1/namespaces/team-a/workflows/broken", json={"spec": {"steps": []}})  # rejected write
    token = await _key_for(admin, "val", "viewer", "team-a")
    viewer = httpx.AsyncClient(transport=api._transport, base_url="http://awm.test", headers={"Authorization": f"Bearer {token}"})
    async with viewer:
        r = await viewer.delete("/v1/namespaces/team-a/workflows/hello")
        assert r.status_code == 403 and r.headers["x-request-id"]

    page = (await admin.get("/v1/audit", params={"namespace": "team-a", "action": "workflow."})).json()
    assert [e["action"] for e in page["items"]] == ["workflow.created"]
    created = page["items"][0]
    assert created["actor"] == "sa:admin" and created["source"] == "api" and created["request_id"]

    denied = (await admin.get("/v1/audit", params={"outcome": "denied"})).json()["items"]
    assert denied[0]["actor"] == "sa:val"
    assert denied[0]["action"] == "access.denied" and denied[0]["target"] == "workflows:delete"

    failed = (await admin.get("/v1/audit", params={"outcome": "error"})).json()["items"]
    assert failed[0]["action"] == "api.put" and failed[0]["data"]["status"] == 422
    assert failed[0]["target"] == "/v1/namespaces/team-a/workflows/broken"

    first = (await admin.get("/v1/audit", params={"limit": 2})).json()
    assert first["next_cursor"]
    second = (await admin.get("/v1/audit", params={"limit": 2, "before": first["next_cursor"]})).json()
    assert {e["id"] for e in first["items"]}.isdisjoint({e["id"] for e in second["items"]})

    export = await admin.get("/v1/audit/export", params={"namespace": "team-a"})
    assert export.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in export.text.splitlines()]
    assert any(e["action"] == "workflow.created" for e in lines)
