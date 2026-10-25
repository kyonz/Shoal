"""Gateways and model policies are tenant-wide, admin-managed, and never hold clear-text credentials."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from shoal.api.app import create_app
from shoal.core.scope import use_namespace
from shoal.store import migrate
from shoal.store import tables as t

ADMIN = "models-admin"
GATEWAY = {"spec": {"type": "scripted", "api_key_secret": "llm-key", "headers": {"x-team": "platform"},
                    "secret_headers": {"x-portkey-api-key": "portkey-key"}}}


@pytest.fixture
async def admin(engine):
    engine.settings.admin_token = SecretStr(ADMIN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine=engine)), base_url="http://shoal.test") as c:
        r = await c.post("/v1/tenants", json={"slug": "acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
        c.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        yield c


async def _editor(admin) -> dict[str, str]:
    sa = (await admin.put("/v1/service-accounts/dev", json={})).json()
    await admin.post("/v1/role-bindings", json={"role": "editor", "subject_kind": "user", "subject_id": sa["id"], "namespace": None})
    token = (await admin.post("/v1/api-keys", json={"name": "k", "service_account": "dev"})).json()["token"]
    return {"Authorization": f"Bearer {token}"}


async def test_gateways_are_tenant_wide_and_admin_managed(admin):
    r = await admin.put("/v1/gateways/llm", json=GATEWAY)
    assert r.status_code == 201, r.text
    assert r.json()["metadata"].get("namespace", "shared") == "shared"
    assert (await admin.put("/v1/namespaces/default/gateways/llm", json=GATEWAY)).status_code in (404, 405)

    editor = await _editor(admin)
    assert (await admin.get("/v1/gateways/llm", headers=editor)).status_code == 200  # everyone can see what exists
    assert (await admin.put("/v1/gateways/mine", json=GATEWAY, headers=editor)).status_code == 403
    assert (await admin.put("/v1/model-policies/default", headers=editor, json={
        "spec": {"rules": [{"candidates": [{"model": "m", "gateway": "llm"}]}]}})).status_code == 403
    apply = "apiVersion: shoal.dev/v1\nkind: Gateway\nmetadata: {name: sneaky}\nspec: {type: scripted}\n"
    assert (await admin.post("/v1/apply", content=apply, headers=editor)).status_code == 403


async def test_clear_text_credentials_are_rejected(admin):
    bad_header = {"spec": {"type": "scripted", "headers": {"Authorization": "Bearer sk-live"}}}
    r = await admin.put("/v1/gateways/llm", json=bad_header)
    assert r.status_code == 422 and "secret_headers" in r.text
    r = await admin.put("/v1/gateways/llm", json={"spec": {"type": "scripted", "config": {"api_key": "sk-live"}}})
    assert r.status_code == 422
    for config in ({"api_key": "sk"}, {"base_url": "https://evil.example"}, {"headers": {"x": "y"}}):
        r = await admin.put("/v1/namespaces/default/agents/a", json={"spec": {"provider_config": config}})
        assert r.status_code == 422, config
    assert (await admin.put("/v1/namespaces/default/agents/a", json={"spec": {"provider_config": {"max_tokens": 9}}})).status_code == 201


async def test_gateway_secrets_come_from_shared(engine, admin):
    await admin.put("/v1/gateways/llm", json=GATEWAY)
    tenant = (await engine.store.get_tenant_by_slug("acme")).id
    with use_namespace("shared"):
        await engine.secrets.writable().put(tenant, "llm-key", "sk-shared")
        await engine.secrets.writable().put(tenant, "portkey-key", "pk-shared")
    with use_namespace("team-a"):  # a team's own secret with the same name is not used
        await engine.secrets.writable().put(tenant, "llm-key", "sk-team")
        target = await engine.models.gateway_target(tenant, "llm", "m")
    assert target.config["api_key"] == "sk-shared"
    assert target.config["headers"] == {"x-team": "platform", "x-portkey-api-key": "pk-shared"}


async def test_migration_moves_namespaced_gateways_to_shared(engine, admin):
    tenant = (await engine.store.get_tenant_by_slug("acme")).id
    await admin.put("/v1/gateways/llm", json=GATEWAY)
    async with engine.store.session() as s:
        for ns, rid in (("team-a", "res_a"), ("team-b", "res_b")):
            name = "llm" if ns == "team-a" else "other"
            s.add(t.Resource(id=rid, tenant_id=tenant, namespace=ns, kind="Gateway", name=name, version=1,
                             document={"kind": "Gateway", "metadata": {"name": name, "namespace": ns}, "spec": {"type": "scripted"}}))
    async with engine.store.engine.begin() as conn:
        await conn.run_sync(lambda c: migrate.stamp(c, "0002"))
        await conn.run_sync(lambda c: migrate.upgrade(c))
    names = {(r.namespace, r.name) for r in await engine.store.list_resources(tenant, "Gateway", all_namespaces=True)}
    assert names == {("shared", "llm"), ("shared", "team-a-llm"), ("shared", "other")}
