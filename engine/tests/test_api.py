from __future__ import annotations

import json

import httpx
import pytest
import yaml
from pydantic import SecretStr

from shoal.api.app import create_app
from shoal.core.scope import use_namespace

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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test") as client:
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
    assert body["apiVersion"] == "shoal.dev/v1" and body["kind"] == "Workflow"
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
apiVersion: shoal.dev/v1
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

    viewer = httpx.AsyncClient(transport=api._transport, base_url="http://shoal.test", headers={"Authorization": f"Bearer {token}"})
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
    body = {"issuer": "https://idp.example.com", "client_id": "shoal", "client_secret": "shh", "groups_claim": "groups"}
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
    assert card["url"] == "http://shoal.test/v1/namespaces/default/a2a/hello"
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
    viewer = httpx.AsyncClient(transport=api._transport, base_url="http://shoal.test", headers={"Authorization": f"Bearer {token}"})
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


GREETER = {
    "spec": {
        "title": "Greeter",
        "summary": "A workflow that greets",
        "version": "1.0.0",
        "status": "published",
        "params": {"greeting": {"default": "Hello"}, "team": {}},
        "resources": [
            {"kind": "Workflow", "metadata": {"name": "{{ prefix }}greet", "labels": {"team": "{{ params.team }}"}},
             "spec": {"inputs": {"name": {"type": "string"}},
                      "steps": [{"id": "g", "type": "set", "values": {"text": "{{ params.greeting }} {{ inputs.name }}"}}]}},
            {"kind": "Workflow", "metadata": {"name": "{{ prefix }}wave"},
             "spec": {"steps": [{"id": "w", "type": "set", "values": {"text": "wave"}}]}},
        ],
    }
}


async def test_blueprint_adoption_lifecycle(admin, api):
    assert (await admin.put("/v1/namespaces/team-a", json={})).status_code == 201
    assert (await admin.put("/v1/blueprints/greeter", json=GREETER)).status_code == 201
    adopt = "/v1/namespaces/team-a/blueprints/greeter/adopt"

    bad = await admin.post(adopt, json={"params": {}})
    assert bad.status_code == 422 and bad.json()["details"]["params"] == {"team": "required"}

    dry = await admin.post(adopt, json={"params": {"team": "a"}, "prefix": "x", "dry_run": True})
    assert dry.status_code == 200 and dry.json()["dry_run"] is True, dry.text
    assert [r["name"] for r in dry.json()["resources"]] == ["x-greet", "x-wave"]
    assert (await admin.get("/v1/namespaces/team-a/workflows/x-greet")).status_code == 404

    r = await admin.post(adopt, json={"params": {"team": "a"}, "prefix": "x"})
    assert r.status_code == 201, r.text
    adoption = r.json()["adoption"]
    assert adoption["blueprint_version"] == "1.0.0" and adoption["params"] == {"greeting": "Hello", "team": "a"}
    wf = (await admin.get("/v1/namespaces/team-a/workflows/x-greet")).json()
    assert wf["metadata"]["labels"]["team"] == "a" and wf["metadata"]["labels"]["shoal.dev/adoption"] == adoption["id"]
    # run-time expressions are left alone, adoption-time ones filled in
    assert wf["spec"]["steps"][0]["values"]["text"] == "Hello {{ inputs.name }}"

    clash = await admin.post(adopt, json={"params": {"team": "a"}, "prefix": "x"})
    assert clash.status_code == 409

    # v2 drops the wave workflow; upgrading the adoption removes it and keeps the params
    v2 = json.loads(json.dumps(GREETER))
    v2["spec"]["version"] = "2.0.0"
    v2["spec"]["resources"] = v2["spec"]["resources"][:1]
    assert (await admin.put("/v1/blueprints/greeter", json=v2)).status_code == 200
    url = f"/v1/namespaces/team-a/blueprint-adoptions/{adoption['id']}"
    up = await admin.put(url, json={"params": {"greeting": "Hi"}})
    assert up.status_code == 200, up.text
    assert up.json()["removed"] == [{"kind": "Workflow", "name": "x-wave"}]
    assert up.json()["adoption"]["blueprint_version"] == "2.0.0"
    assert (await admin.get("/v1/namespaces/team-a/workflows/x-wave")).status_code == 404
    wf = (await admin.get("/v1/namespaces/team-a/workflows/x-greet")).json()
    assert wf["spec"]["steps"][0]["values"]["text"] == "Hi {{ inputs.name }}"

    # an older version can still be adopted by its version number
    old = await admin.post(adopt, json={"params": {"team": "b"}, "prefix": "y", "version": "1.0.0"})
    assert old.status_code == 201 and len(old.json()["resources"]) == 2
    listed = (await admin.get("/v1/namespaces/team-a/blueprint-adoptions")).json()
    assert {a["prefix"] for a in listed} == {"x", "y"}

    # viewers can browse but not adopt; drafts and restricted blueprints cannot be adopted
    viewer = await _key_for(admin, "bp-viewer", "viewer", "team-a")
    r = await api.post(adopt, json={"params": {"team": "a"}, "prefix": "z"}, headers={"Authorization": f"Bearer {viewer}"})
    assert r.status_code == 403
    restricted = json.loads(json.dumps(GREETER))
    restricted["spec"]["namespaces"] = ["team-b"]
    await admin.put("/v1/blueprints/greeter", json=restricted)
    r = await admin.post(adopt, json={"params": {"team": "a"}, "prefix": "z"})
    assert r.status_code == 403

    gone = await admin.delete(url)
    assert gone.status_code == 200 and gone.json() == {"removed": [{"kind": "Workflow", "name": "x-greet"}], "kept": []}
    assert (await admin.get("/v1/namespaces/team-a/workflows/x-greet")).status_code == 404
    assert (await admin.get(url)).status_code == 404
    assert any(e["action"] == "blueprint.adopted" for e in (await admin.get("/v1/audit")).json()["items"])


async def test_blueprint_upgrade_edge_cases(engine, admin, api):
    assert (await admin.put("/v1/namespaces/team-a", json={})).status_code == 201
    v1 = json.loads(json.dumps(GREETER))
    v1["spec"]["params"]["emoji"] = {"required": False}
    assert (await admin.put("/v1/blueprints/greeter", json=v1)).status_code == 201
    r = await admin.post("/v1/namespaces/team-a/blueprints/greeter/adopt", json={"params": {"team": "a", "emoji": "x"}})
    assert r.status_code == 201, r.text
    url = f"/v1/namespaces/team-a/blueprint-adoptions/{r.json()['adoption']['id']}"

    # v2 drops a parameter and a resource; the preview says what is updated and what goes
    v2 = json.loads(json.dumps(GREETER))
    v2["spec"].update(version="2.0.0", resources=GREETER["spec"]["resources"][:1])
    assert (await admin.put("/v1/blueprints/greeter", json=v2)).status_code == 200
    dry = await admin.put(url, json={"dry_run": True})
    assert dry.status_code == 200, dry.text
    assert [(x["name"], x["outcome"]) for x in dry.json()["resources"]] == [("greet", "updated")]
    assert dry.json()["removed"] == [{"kind": "Workflow", "name": "wave"}]
    assert (await admin.get("/v1/namespaces/team-a/workflows/wave")).status_code == 200
    up = await admin.put(url, json={})
    assert up.status_code == 200, up.text
    assert up.json()["adoption"]["params"] == {"greeting": "Hello", "team": "a"}
    assert (await admin.put(url, json={"params": {"emoji": "y"}, "dry_run": True})).status_code == 422  # still rejected when asked for

    # deprecated: no new adoptions, but existing ones can still change
    deprecated = json.loads(json.dumps(v2))
    deprecated["spec"]["status"] = "deprecated"
    assert (await admin.put("/v1/blueprints/greeter", json=deprecated)).status_code == 200
    assert (await admin.post("/v1/namespaces/team-a/blueprints/greeter/adopt", json={"params": {"team": "b"}, "prefix": "b"})).status_code == 403
    assert (await admin.put(url, json={"params": {"greeting": "Hi"}})).status_code == 200

    # a failed adoption leaves nothing behind, so it can be retried
    assert (await admin.put("/v1/blueprints/greeter", json=GREETER)).status_code == 200
    real_apply, calls = engine.store.apply_resource, []

    async def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("database went away")
        return await real_apply(*args, **kwargs)

    engine.store.apply_resource = flaky
    with pytest.raises(RuntimeError):
        await admin.post("/v1/namespaces/team-a/blueprints/greeter/adopt", json={"params": {"team": "c"}, "prefix": "c"})
    engine.store.apply_resource = real_apply
    assert (await admin.get("/v1/namespaces/team-a/workflows/c-greet")).status_code == 404
    r = await admin.post("/v1/namespaces/team-a/blueprints/greeter/adopt", json={"params": {"team": "c"}, "prefix": "c"})
    assert r.status_code == 201, r.text

    # removing reports what it left alone: a resource someone relabelled, and one already deleted
    wave = (await admin.get("/v1/namespaces/team-a/workflows/c-wave")).json()
    assert (await admin.put("/v1/namespaces/team-a/workflows/c-wave", json={"spec": wave["spec"]})).status_code == 200
    assert (await admin.delete("/v1/namespaces/team-a/workflows/c-greet")).status_code == 204
    gone = await admin.delete(f"/v1/namespaces/team-a/blueprint-adoptions/{r.json()['adoption']['id']}")
    assert gone.status_code == 200 and gone.json()["removed"] == []
    assert [(k["name"], k["reason"].split()[0]) for k in gone.json()["kept"]] == [("c-greet", "already"), ("c-wave", "no")]
    assert (await admin.get("/v1/namespaces/team-a/workflows/c-wave")).status_code == 200


async def test_adoption_permissions_follow_rendered_kinds_and_removals(admin, api):
    assert (await admin.put("/v1/namespaces/team-a", json={})).status_code == 201
    perms = ["blueprints:read", "blueprints:adopt", "workflows:read", "workflows:write", "skills:read", "skills:write"]
    assert (await admin.put("/v1/roles/adopter", json={"permissions": perms})).status_code == 201
    headers = {"Authorization": f"Bearer {await _key_for(admin, 'adopter', 'adopter', 'team-a')}"}

    # the kind is a parameter: permissions are checked on what renders, not on the template
    helper = {"spec": {"title": "Helper", "summary": "A skill or an agent", "status": "published",
                       "params": {"kind": {"type": "choice", "options": ["Skill", "Agent"], "default": "Skill"}},
                       "resources": [{"kind": "{{ params.kind }}", "metadata": {"name": "{{ prefix }}helper"},
                                      "spec": {"description": "Helps", "instructions": "Help."}}]}}
    assert (await admin.put("/v1/blueprints/helper", json=helper)).status_code == 201
    adopt = "/v1/namespaces/team-a/blueprints/helper/adopt"
    r = await api.post(adopt, json={"params": {"kind": "Agent"}, "dry_run": True}, headers=headers)
    assert r.status_code == 403, r.text
    assert (await api.post(adopt, json={"params": {}}, headers=headers)).status_code == 201

    # an upgrade that drops a workflow needs workflows:delete
    assert (await admin.put("/v1/blueprints/greeter", json=GREETER)).status_code == 201
    r = await api.post("/v1/namespaces/team-a/blueprints/greeter/adopt", json={"params": {"team": "a"}}, headers=headers)
    assert r.status_code == 201, r.text
    url = f"/v1/namespaces/team-a/blueprint-adoptions/{r.json()['adoption']['id']}"
    v2 = json.loads(json.dumps(GREETER))
    v2["spec"].update(version="2.0.0", resources=GREETER["spec"]["resources"][:1])
    assert (await admin.put("/v1/blueprints/greeter", json=v2)).status_code == 200
    assert (await api.put(url, json={"dry_run": True}, headers=headers)).status_code == 403
    assert (await admin.put(url, json={})).status_code == 200


async def test_featuring_a_blueprint_needs_feature_on_every_write_path(admin, api):
    assert (await admin.put("/v1/roles/publisher", json={"permissions": ["blueprints:read", "blueprints:publish"]})).status_code == 201
    headers = {"Authorization": f"Bearer {await _key_for(admin, 'pub', 'publisher', None)}"}
    featured = json.loads(json.dumps(GREETER))
    featured["spec"]["status"] = "featured"
    assert (await api.put("/v1/blueprints/greeter", json=featured, headers=headers)).status_code == 403
    assert (await api.put("/v1/blueprints/greeter", json=GREETER, headers=headers)).status_code == 201
    doc = yaml.safe_dump({"apiVersion": "shoal.dev/v1", "kind": "Blueprint", "metadata": {"name": "greeter"}, **featured})
    assert (await api.post("/v1/apply", content=doc, headers=headers)).status_code == 403
    assert (await admin.put("/v1/blueprints/greeter", json=featured)).status_code == 200  # an admin features it (v2)
    assert (await admin.put("/v1/blueprints/greeter", json=GREETER)).status_code == 200  # and unfeatures it (v3)
    assert (await api.post("/v1/blueprints/greeter/rollback", json={"version": 2}, headers=headers)).status_code == 403
    assert (await api.post("/v1/blueprints/greeter/rollback", json={"version": 1}, headers=headers)).status_code == 200


async def test_connection_actions_are_listed_per_connection(admin):
    conn = {"spec": {"integration": "github", "config": {}, "secrets": {"token": "gh"}}}
    r = await admin.put("/v1/namespaces/default/connections/gh", json=conn)
    assert r.status_code == 201, r.text
    r = await admin.get("/v1/namespaces/default/connections/gh/actions")
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["integration"] == "github"
    names = {a["name"] for a in body["actions"]}
    catalogue = {a["name"] for a in (await admin.get("/v1/integrations/github")).json()["actions"]}
    assert names == catalogue and names


async def test_versions_diff_and_rollback(admin):
    url = "/v1/namespaces/default/workflows/hello"
    assert (await admin.put(url, json=HELLO)).status_code == 201
    v2 = json.loads(json.dumps(HELLO))
    v2["spec"]["steps"].insert(0, {"id": "first", "type": "set", "values": {}})
    v2["spec"]["steps"][1]["values"]["text"] = "Hi {{ inputs.name }}"
    r = await admin.put(url, json=v2, params={"message": "Friendlier greeting"})
    assert r.json()["metadata"]["version"] == 2

    history = (await admin.get(f"{url}/versions")).json()
    assert [(h["version"], h["message"]) for h in history] == [(2, "Friendlier greeting"), (1, None)]
    assert (await admin.get(f"{url}/versions/1")).json()["document"]["spec"]["steps"][0]["id"] == "greet"
    assert history[1]["source"] == HELLO["spec"]  # as written, without server defaults

    changes = (await admin.get(f"{url}/diff", params={"from": 1})).json()["changes"]
    assert {"op": "add", "path": "/spec/steps/[id=first]", "to": {"id": "first", "type": "set", "values": {}}} in changes
    assert {"op": "replace", "path": "/spec/steps/[id=greet]/values/text",
            "from": "Hello {{ inputs.name }}", "to": "Hi {{ inputs.name }}"} in changes

    preview = (await admin.post(f"{url}/rollback", json={"version": 1, "dry_run": True})).json()
    assert preview["to_version"] == 1 and preview["changes"]
    assert (await admin.get(url)).json()["metadata"]["version"] == 2

    r = await admin.post(f"{url}/rollback", json={"version": 1})
    assert r.status_code == 200, r.text
    assert r.json()["metadata"]["version"] == 3 and r.json()["spec"] == HELLO["spec"]
    assert (await admin.get(f"{url}/versions")).json()[0]["message"] == "Roll back to version 1"
    assert (await admin.get(f"{url}/diff", params={"from": 1, "to": 3})).json()["changes"] == []
    assert any(e["action"] == "workflow.rolled_back" for e in (await admin.get("/v1/audit")).json()["items"])


async def test_journey_blueprints_adopt_cleanly(admin):
    """The shipped journey blueprints (examples/blueprints) adopt into a namespace with real connections."""
    from pathlib import Path

    from shoal.core.loader import load_path

    for name, integration, config in (
        ("gh", "github", {}), ("jira", "jira", {"base_url": "https://acme.atlassian.net", "email": "bot@acme.example"}),
        ("slack", "slack", {}),
    ):
        r = await admin.put(f"/v1/namespaces/default/connections/{name}", json={"spec": {"integration": integration, "config": config}})
        assert r.status_code == 201, r.text
    assert (await admin.put("/v1/namespaces/default/secrets/anthropic-api-key", json={"value": "k"})).status_code == 201

    params = {"jira-bugfix": {"jira": "jira", "github": "gh"}, "github-uplift": {"github": "gh", "org": "acme"},
              "slack-bot": {"slack": "slack", "channel": "#help"}, "slack-release-check": {"slack": "slack"}}
    for blueprint in load_path(Path(__file__).parents[2] / "examples" / "blueprints"):
        doc = blueprint.to_document()
        name = doc["metadata"]["name"]
        assert (await admin.put(f"/v1/blueprints/{name}", json={"metadata": {"description": doc["metadata"].get("description")},
                                                                  "spec": doc["spec"]})).status_code == 201
        r = await admin.post(f"/v1/namespaces/default/blueprints/{name}/adopt", json={"params": params[name], "prefix": "team"})
        assert r.status_code == 201, r.text
        assert all(res["name"].startswith("team-") for res in r.json()["resources"])
    wf = (await admin.get("/v1/namespaces/default/workflows/team-jira-bugfix")).json()
    assert wf["spec"]["connections"] == {"jira": "jira", "github": "gh"} and wf["spec"]["defaults"]["agent"] == "team-bugfixer"
    campaign = (await admin.get("/v1/namespaces/default/campaigns/team-coverage-uplift")).json()
    assert campaign["spec"]["targets"]["filter"]["org"] == "acme" and campaign["spec"]["policy"]["budget_usd"] == 500


async def test_sandbox_profile_config_is_checked_against_the_backend(admin):
    r = await admin.put("/v1/namespaces/default/sandbox-profiles/k8s", json={"spec": {
        "backend": "kubernetes", "config": {"namespce": "sbx", "run_as_user": -1}}})
    assert r.status_code == 422, r.text
    assert "spec.config.namespce" in r.text and "spec.config.run_as_user" in r.text
    ok = {"backend": "kubernetes", "config": {"namespace": "shoal-{tenant}", "runtime_class": "gvisor"}}
    assert (await admin.put("/v1/namespaces/default/sandbox-profiles/k8s", json={"spec": ok})).status_code == 201

    doc = "apiVersion: shoal.dev/v1\nkind: SandboxProfile\nmetadata: {name: e2b}\nspec: {backend: e2b, config: {domian: x}}\n"
    r = await admin.post("/v1/apply", content=doc)
    assert r.status_code == 422 and "spec.config.domian" in r.text

    backends = {b["name"]: b for b in (await admin.get("/v1/sandbox-backends")).json()}
    assert "runtime_class" in backends["kubernetes"]["config_schema"]["properties"]
    assert backends["local"]["config_schema"]["properties"] == {}


async def test_approvers_over_the_api(admin, api):
    await admin.put("/v1/namespaces/default/workflows/gated", json={"spec": {"steps": [
        {"id": "ok", "type": "approval", "message": "Ship it?", "approvers": ["sa:ops"]},
    ]}})
    run_id = (await admin.post("/v1/namespaces/default/workflows/gated/runs", json={})).json()["id"]
    await api._transport.app.state.engine.run_until_idle()  # type: ignore[attr-defined]
    r = await admin.post(f"/v1/runs/{run_id}/steps/ok/approval", json={"approved": True})
    assert r.status_code == 403 and r.json()["details"]["approvers"] == ["sa:ops"]
    token = await _key_for(admin, "ops", "operator", "default")
    async with httpx.AsyncClient(transport=api._transport, base_url="http://shoal.test", headers={"Authorization": f"Bearer {token}"}) as ops:
        r = await ops.post(f"/v1/runs/{run_id}/steps/ok/approval", json={"approved": True})
        assert r.status_code == 200, r.text


async def test_rerun_keeps_the_trigger_context(admin, api):
    engine = api._transport.app.state.engine  # type: ignore[attr-defined]
    await admin.put("/v1/namespaces/default/workflows/echo", json={"spec": {
        "outputs": {"key": "{{ event.key }}"}, "steps": [{"id": "a", "type": "set", "values": {}}],
    }})
    await admin.put("/v1/namespaces/default/triggers/manual", json={"spec": {
        "workflow": "echo", "event": "schedule", "schedule": "0 0 * * *",
    }})
    tenant_id = (await admin.get("/v1/me")).json()["tenant"]["id"]
    with use_namespace("default"):
        fired = await engine.triggers.fire_manual(tenant_id, "manual", {"key": "PROJ-1"})
    await engine.run_until_idle()
    rerun = (await admin.post(f"/v1/runs/{fired['run_id']}/rerun")).json()
    await engine.run_until_idle()
    assert (await admin.get(f"/v1/runs/{rerun['id']}")).json()["outputs"] == {"key": "PROJ-1"}


async def test_tenant_wide_versions_can_be_read_one_at_a_time(admin):
    url = "/v1/identities/ada"
    assert (await admin.put(url, json={"spec": {"display_name": "Ada"}})).status_code == 201
    assert (await admin.put(url, json={"spec": {"display_name": "Ada Lovelace"}})).status_code == 200
    first = await admin.get(f"{url}/versions/1")
    assert first.status_code == 200, first.text
    assert first.json()["document"]["spec"]["display_name"] == "Ada"
    assert (await admin.get(f"{url}/versions/9")).status_code == 404
