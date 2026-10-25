"""Nobody can hand out more than they hold, tenants stay apart, and deleted things leave nothing behind."""

from __future__ import annotations

import httpx

from tests.test_api import ADMIN, HELLO, admin, api  # noqa: F401 (fixtures)

SCIM = {"content-type": "application/scim+json"}


async def _sa(admin, name: str, role: str | None = None, namespace: str | None = None) -> dict:  # noqa: F811
    sa = await admin.put(f"/v1/service-accounts/{name}", json={})
    assert sa.status_code == 201, sa.text
    if role:
        r = await admin.post("/v1/role-bindings", json={"role": role, "subject_kind": "user", "subject_id": sa.json()["id"], "namespace": namespace})
        assert r.status_code == 201, r.text
    return sa.json()


async def _client(admin, name: str, permissions: list[str], **key) -> httpx.AsyncClient:  # noqa: F811
    """A client for a new service account holding a custom tenant-wide role with ``permissions``."""
    assert (await admin.put(f"/v1/roles/{name}-role", json={"permissions": permissions})).status_code == 201
    await _sa(admin, name, f"{name}-role")
    r = await admin.post("/v1/api-keys", json={"name": name, "service_account": name, **key})
    assert r.status_code == 201, r.text
    return httpx.AsyncClient(transport=admin._transport, base_url="http://shoal.test", headers={"Authorization": f"Bearer {r.json()['token']}"})


async def test_role_managers_cannot_grow_their_own_role(admin):  # noqa: F811
    async with await _client(admin, "rm", ["roles:manage", "users:read"]) as rm:
        r = await rm.put("/v1/roles/rm-role", json={"permissions": ["*"]})
        assert r.status_code == 403 and "you cannot give role 'rm-role'" in r.json()["detail"]
        assert (await rm.put("/v1/roles/readers", json={"permissions": ["users:read"]})).status_code == 201


async def test_user_managers_cannot_join_privileged_groups(admin):  # noqa: F811
    async with await _client(admin, "people", ["users:manage", "users:read"]) as people:
        me = (await people.get("/v1/me")).json()
        admins = (await admin.post("/v1/groups", json={"display_name": "admins"})).json()
        r = await admin.post("/v1/role-bindings", json={"role": "tenant-admin", "subject_kind": "group", "subject_id": admins["id"]})
        assert r.status_code == 201
        r = await people.put(f"/v1/groups/{admins['id']}/members", json={"members": [me["user_id"]]})
        assert r.status_code == 403
        assert (await people.patch(f"/v1/groups/{admins['id']}", json={"display_name": "okta-everyone"})).status_code == 403
        plain = (await people.post("/v1/groups", json={"display_name": "book-club"})).json()
        assert (await people.put(f"/v1/groups/{plain['id']}/members", json={"members": [me["user_id"]]})).status_code == 200


async def test_user_managers_cannot_manage_people_above_them(admin):  # noqa: F811
    boss = (await admin.post("/v1/users", json={"username": "boss", "email": "boss@acme.test"})).json()
    assert (await admin.post("/v1/role-bindings", json={"role": "tenant-admin", "subject_kind": "user", "subject_id": boss["id"]})).status_code == 201
    intern = (await admin.post("/v1/users", json={"username": "intern"})).json()
    async with await _client(admin, "hr", ["users:manage", "users:read"]) as hr:
        assert (await hr.patch(f"/v1/users/{boss['id']}", json={"active": False})).status_code == 403
        assert (await hr.patch(f"/v1/users/{boss['id']}", json={"email": "hr@evil.test"})).status_code == 403
        assert (await hr.delete(f"/v1/users/{boss['id']}")).status_code == 403
        assert (await hr.patch(f"/v1/users/{intern['id']}", json={"active": False})).status_code == 200
        me = (await hr.get("/v1/me")).json()
        r = await hr.patch(f"/v1/users/{me['user_id']}", json={"active": False})
        assert r.status_code == 422 and "yourself" in r.json()["detail"]


async def test_key_managers_cannot_borrow_a_service_accounts_roles(admin):  # noqa: F811
    await _sa(admin, "root", "tenant-admin")
    await _sa(admin, "reader", "viewer")
    async with await _client(admin, "keys", ["api-keys:manage", "*:read"]) as keys:
        r = await keys.post("/v1/api-keys", json={"name": "x", "service_account": "root"})
        assert r.status_code == 403 and "you cannot create keys for 'root'" in r.json()["detail"]
        assert (await keys.post("/v1/api-keys", json={"name": "x", "service_account": "reader"})).status_code == 201


async def test_oidc_default_role_counts_as_a_grant(admin):  # noqa: F811
    body = {"issuer": "https://idp.example.com", "client_id": "shoal", "default_role": "tenant-admin"}
    async with await _client(admin, "sso", ["auth:manage", "*:read"]) as sso:
        assert (await sso.put("/v1/oidc-providers/okta", json=body)).status_code == 403
        assert (await sso.put("/v1/oidc-providers/okta", json={**body, "default_role": "viewer"})).status_code == 201


async def test_namespace_limited_keys_cannot_administer_the_tenant(admin):  # noqa: F811
    await _sa(admin, "ops", "tenant-admin")
    token = (await admin.post("/v1/api-keys", json={"name": "ns", "service_account": "ops", "namespaces": ["default"]})).json()["token"]
    async with httpx.AsyncClient(transport=admin._transport, base_url="http://shoal.test", headers={"Authorization": f"Bearer {token}"}) as ns:
        assert (await ns.get("/v1/users")).status_code == 200
        assert (await ns.put("/v1/namespaces/default/workflows/w", json=HELLO)).status_code == 201
        assert (await ns.post("/v1/users", json={"username": "mallory"})).status_code == 403
        assert (await ns.put("/v1/roles/x", json={"permissions": ["users:read"]})).status_code == 403


async def test_scim_cannot_reach_other_tenants_or_service_accounts(admin, api):  # noqa: F811
    other = await api.post("/v1/tenants", json={"slug": "globex", "name": "Globex"}, headers={"Authorization": f"Bearer {ADMIN}"})
    globex = {"Authorization": f"Bearer {other.json()['api_key']}"}
    stranger = (await api.post("/v1/users", json={"username": "hank"}, headers=globex)).json()

    scim_sa = await _sa(admin, "okta", "tenant-admin")
    token = (await admin.post("/v1/api-keys", json={"name": "scim", "service_account": "okta", "scopes": ["scim:provision"]})).json()["token"]
    h = {**SCIM, "Authorization": f"Bearer {token}"}
    group = {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"], "displayName": "eng"}
    r = await admin.post("/scim/v2/Groups", json={**group, "members": [{"value": stranger["id"]}]}, headers=h)
    assert r.status_code == 422 and stranger["id"] in r.json()["detail"]
    r = await admin.post("/scim/v2/Groups", json={**group, "members": [{"value": scim_sa["id"]}]}, headers=h)
    assert r.status_code == 422  # service accounts are not the IdP's
    gid = (await admin.post("/scim/v2/Groups", json=group, headers=h)).json()["id"]
    add = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], "Operations": [{"op": "add", "path": "members", "value": [{"value": stranger["id"]}]}]}
    assert (await admin.patch(f"/scim/v2/Groups/{gid}", json=add, headers=h)).status_code == 422
    assert (await admin.put(f"/scim/v2/Groups/{gid}", json={**group, "members": [{"display": "no value"}]}, headers=h)).status_code == 422
    assert (await admin.get(f"/scim/v2/Users/{scim_sa['id']}", headers=h)).status_code == 404
    assert (await admin.delete(f"/scim/v2/Users/{scim_sa['id']}", headers=h)).status_code == 404
    assert (await api.get(f"/v1/users/{stranger['id']}", headers=globex)).json()["groups"] == []


async def test_deleted_namespace_takes_its_members_and_secrets_along(admin, engine):  # noqa: F811
    assert (await admin.put("/v1/namespaces/team-x", json={})).status_code == 201
    user = (await admin.post("/v1/users", json={"username": "old-member"})).json()
    assert (await admin.post("/v1/role-bindings", json={"role": "editor", "subject_kind": "user", "subject_id": user["id"], "namespace": "team-x"})).status_code == 201
    assert (await admin.put("/v1/namespaces/team-x/secrets/token", json={"value": "old-team-secret"})).status_code == 201
    assert (await admin.delete("/v1/namespaces/team-x")).status_code == 204

    assert (await admin.put("/v1/namespaces/team-x", json={})).status_code == 201
    assert (await admin.get("/v1/namespaces/team-x/secrets")).json() == []
    members = (await admin.get("/v1/namespaces/team-x/members")).json()
    assert all(m["namespace"] is None for m in members)  # only tenant-wide bindings remain


async def test_namespace_used_by_an_oidc_default_cannot_be_deleted(admin):  # noqa: F811
    assert (await admin.put("/v1/namespaces/newcomers", json={})).status_code == 201
    body = {"issuer": "https://idp.example.com", "client_id": "shoal", "default_role": "viewer", "default_namespace": "newcomers"}
    assert (await admin.put("/v1/oidc-providers/okta", json=body)).status_code == 201
    r = await admin.delete("/v1/namespaces/newcomers")
    assert r.status_code == 409 and "okta" in r.json()["detail"]


async def test_personal_credentials_follow_renames_and_go_with_their_owner(engine, tenant):
    d, creds = engine.directory, engine.user_credentials
    alice = await d.create_user(tenant.id, "alice")
    group = await d.create_group(tenant.id, "eng")
    await d.add_group_members(tenant.id, group.id, [alice.id])
    await creds.put(tenant.id, "user:alice", "default", "github", {"token": "alice-token"})

    await d.update_user(tenant.id, alice.id, username="alice.l")
    assert await creds.get(tenant.id, "user:alice", "default", "github") is None
    assert await creds.get(tenant.id, "user:alice.l", "default", "github") == {"token": "alice-token"}

    await d.delete_user(tenant.id, alice.id)
    assert await creds.list(tenant.id) == []
    assert await d.group_members(group.id) == []
    newcomer = await d.create_user(tenant.id, "alice.l")
    assert await creds.get(tenant.id, f"user:{newcomer.username}", "default", "github") is None


async def test_signing_out_ends_the_session_token(engine, api):  # noqa: F811
    from shoal.auth.sessions import issue_session

    tenant = await engine.store.create_tenant("initech")
    user = await engine.directory.create_user(tenant.id, "peter")
    token = issue_session(engine.settings, tenant_id=tenant.id, user_id=user.id)
    h = {"Authorization": f"Bearer {token}"}
    assert (await api.get("/v1/me", headers=h)).status_code == 200
    assert (await api.post("/v1/auth/logout", headers=h)).status_code == 204
    r = await api.get("/v1/me", headers=h)
    assert r.status_code == 401 and "signed out" in r.json()["detail"]
    other = issue_session(engine.settings, tenant_id=tenant.id, user_id=user.id)  # a new sign-in still works
    assert (await api.get("/v1/me", headers={"Authorization": f"Bearer {other}"})).status_code == 200
    assert (await api.post("/v1/auth/logout")).status_code == 204  # no session: just clears the cookie
    logouts = await engine.store.list_audit(tenant.id, action="auth.logout")
    assert [e.actor for e in logouts] == ["user:peter"]


async def test_api_keys_respect_the_servers_longest_lifetime(admin, engine):  # noqa: F811
    from datetime import timedelta

    from shoal.core import clock

    engine.settings.api_key_max_days = 30
    r = await admin.post("/v1/api-keys", json={"name": "ci"})
    assert r.status_code == 201
    expires = r.json()["key"]["expires_at"]
    assert expires and expires[:10] == (clock.now() + timedelta(days=30)).date().isoformat()
    too_long = (clock.now() + timedelta(days=90)).isoformat()
    r = await admin.post("/v1/api-keys", json={"name": "ci", "expires_at": too_long})
    assert r.status_code == 422 and "30 days" in r.json()["detail"]
    soon = (clock.now() + timedelta(days=7)).replace(tzinfo=None).isoformat()
    assert (await admin.post("/v1/api-keys", json={"name": "ci", "expires_at": soon})).status_code == 201


async def test_audit_records_the_client_behind_trusted_proxies(admin, engine):  # noqa: F811
    from shoal.api.app import client_ip

    class Req:
        def __init__(self, peer, xff=None):
            self.client = type("C", (), {"host": peer})()
            self.headers = {"x-forwarded-for": xff} if xff else {}

    proxies = ["10.0.0.0/8"]
    assert client_ip(Req("10.0.0.5", "203.0.113.9, 10.1.1.1"), proxies) == "203.0.113.9"
    assert client_ip(Req("10.0.0.5", "6.6.6.6, 203.0.113.9"), proxies) == "203.0.113.9"  # spoofed left hops ignored
    assert client_ip(Req("198.51.100.7", "6.6.6.6"), proxies) == "198.51.100.7"  # untrusted peer: header ignored
    assert client_ip(Req("10.0.0.5", "6.6.6.6"), []) == "10.0.0.5"

    engine.settings.trusted_proxies = ["127.0.0.0/8"]  # the test client connects from 127.0.0.1
    assert (await admin.put("/v1/namespaces/proxied", json={}, headers={"x-forwarded-for": "203.0.113.9"})).status_code == 201
    entry = (await engine.store.list_audit((await engine.store.get_tenant_by_slug("acme")).id, action="namespace.created"))[0]
    assert entry.ip == "203.0.113.9"
