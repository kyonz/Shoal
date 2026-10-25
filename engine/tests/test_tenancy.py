"""Namespaces, RBAC and OIDC sign-in."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from shoal.auth.rbac import Principal
from shoal.auth.sessions import verify_session
from shoal.config import Settings
from shoal.core import clock
from shoal.core.errors import Forbidden, NotFound, Unauthorized
from shoal.core.models import parse_resource
from shoal.core.scope import use_namespace
from shoal.engine import Engine


def res(kind, name, spec, namespace=None):
    meta = {"name": name, **({"namespace": namespace} if namespace else {})}
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": kind, "metadata": meta, "spec": spec})


WF = {"steps": [{"id": "s", "type": "set", "values": {"ok": True}}]}


# --------------------------------------------------------------------------- namespaces


async def test_namespaces_isolate_resources_and_fall_back_to_shared(engine, tenant):
    store = engine.store
    assert {n.name for n in await store.list_namespaces(tenant.id)} == {"default", "shared"}
    await store.ensure_namespace(tenant.id, "payments")
    await store.apply_resource(tenant.id, res("Workflow", "deploy", WF, namespace="payments"))
    await store.apply_resource(tenant.id, res("Workflow", "deploy", {**WF, "description": "default one"}))
    await store.apply_resource(tenant.id, res("Skill", "house-style", {"description": "d", "instructions": "i"}, namespace="shared"))

    with use_namespace("payments"):
        assert (await store.get_resource(tenant.id, "Workflow", "deploy")).spec.description is None
        assert await store.find_resource(tenant.id, "Skill", "house-style") is not None  # shared fallback
        run = await engine.executor.start_run(tenant.id, "deploy", {})
    assert run.namespace == "payments"
    assert (await store.get_resource(tenant.id, "Workflow", "deploy")).spec.description == "default one"
    with use_namespace("other"):
        with pytest.raises(NotFound):
            await store.get_resource(tenant.id, "Workflow", "deploy")

    await engine.run_until_idle()  # the worker runs it inside its namespace
    assert (await store.get_run(tenant.id, run.id)).status == "succeeded"
    assert len(await store.list_resources(tenant.id, "Workflow", all_namespaces=True)) == 2
    with pytest.raises(Exception, match="still contains"):
        await store.delete_namespace(tenant.id, "payments")


async def test_secrets_do_not_leak_across_namespaces_but_shared_connections_work(engine, tenant):
    secrets = engine.secrets.writable()
    with use_namespace("shared"):
        await secrets.put(tenant.id, "hook-secret", "s3cret")
    await engine.store.apply_resource(
        tenant.id, res("Connection", "hooks", {"integration": "webhook", "secrets": {"secret": "hook-secret"}}, namespace="shared")
    )
    await engine.store.ensure_namespace(tenant.id, "team-a")
    with use_namespace("team-a"):
        assert await engine.secrets.get(tenant.id, "hook-secret") is None
        conn = await engine.integrations.connection(tenant.id, "hooks")  # resolves its secret in `shared`
        assert conn.config.secret.get_secret_value() == "s3cret"


async def test_shared_connection_events_fire_triggers_in_team_namespaces(engine, tenant):
    from shoal.integrations.base import IntegrationEvent

    await engine.store.apply_resource(tenant.id, res("Connection", "hooks", {"integration": "webhook"}, namespace="shared"))
    await engine.store.ensure_namespace(tenant.id, "team-a")
    await engine.store.apply_resource(tenant.id, res("Workflow", "w", WF, namespace="team-a"))
    await engine.store.apply_resource(tenant.id, res("Trigger", "t", {"workflow": "w", "connection": "hooks", "event": "received"}, namespace="team-a"))
    with use_namespace("shared"):
        result = await engine.integrations.inject_event(tenant.id, "hooks", IntegrationEvent(type="received", data={}))
    assert result["triggers"][0]["status"] == "fired" and result["triggers"][0]["namespace"] == "team-a"
    run = await engine.store.get_run(tenant.id, result["triggers"][0]["run_id"])
    assert run.namespace == "team-a"


async def test_org_connections_limited_to_namespaces_only_fire_their_triggers(engine, tenant):
    from shoal.integrations.base import IntegrationEvent

    spec = {"integration": "webhook", "namespaces": ["team-a"]}
    await engine.store.apply_resource(tenant.id, res("Connection", "hooks", spec, namespace="shared"))
    for ns in ("team-a", "team-b"):
        await engine.store.ensure_namespace(tenant.id, ns)
        await engine.store.apply_resource(tenant.id, res("Workflow", "w", WF, namespace=ns))
        await engine.store.apply_resource(tenant.id, res("Trigger", "t", {"workflow": "w", "connection": "hooks", "event": "received"}, namespace=ns))
    with use_namespace("shared"):
        result = await engine.integrations.inject_event(tenant.id, "hooks", IntegrationEvent(type="received", data={}))
    assert [t["namespace"] for t in result["triggers"]] == ["team-a"]


# --------------------------------------------------------------------------- RBAC


async def test_role_bindings_by_namespace_and_group(engine, tenant):
    d, authz = engine.directory, engine.authz
    await engine.store.ensure_namespace(tenant.id, "payments")
    alice = await d.create_user(tenant.id, "alice", email="alice@acme.test")
    bob = await d.create_user(tenant.id, "bob")
    sre = await d.create_group(tenant.id, "sre")
    await d.add_group_members(tenant.id, sre.id, [bob.id])

    await d.bind(tenant.id, "editor", "user", alice.id, namespace="payments")
    await d.bind(tenant.id, "viewer", "group", sre.id)  # tenant-wide
    await d.put_role(tenant.id, "approver", ["runs:approve", "runs:read"], "Can approve runs")
    await d.bind(tenant.id, "approver", "group", sre.id, namespace="payments")

    pa = Principal(tenant.id, "user:alice", user_id=alice.id)
    pb = Principal(tenant.id, "user:bob", user_id=bob.id)
    assert await authz.allowed(pa, "workflows:write", "payments")
    assert not await authz.allowed(pa, "workflows:write", "default")
    assert not await authz.allowed(pa, "users:manage")
    assert await authz.allowed(pb, "workflows:read", "default")  # tenant-wide viewer via group
    assert await authz.allowed(pb, "runs:approve", "payments") and not await authz.allowed(pb, "runs:approve", "default")
    with pytest.raises(Forbidden, match="lacks 'secrets:write' in namespace 'payments'"):
        await authz.authorize(pb, "secrets:write", "payments")
    assert await authz.visible_namespaces(pa) == ["payments"]
    summary = await authz.summary(pb)
    assert "runs:approve" in summary["namespaces"]["payments"] and "users:manage" not in summary["tenant"]


async def test_namespace_binding_never_grants_tenant_permissions(engine, tenant):
    d = engine.directory
    await engine.store.ensure_namespace(tenant.id, "sandbox")
    carol = await d.create_user(tenant.id, "carol")
    await d.bind(tenant.id, "tenant-admin", "user", carol.id, namespace="sandbox")
    pc = Principal(tenant.id, "user:carol", user_id=carol.id)
    assert await engine.authz.allowed(pc, "secrets:write", "sandbox")
    assert not await engine.authz.allowed(pc, "roles:manage")
    assert not await engine.authz.allowed(pc, "workflows:read", "default")


async def test_api_key_restrictions_and_deactivation(engine, tenant):
    d = engine.directory
    bot = await d.create_user(tenant.id, "ci-bot", kind="service")
    await d.bind(tenant.id, "editor", "user", bot.id)
    key, token = await engine.store.create_api_key(tenant.id, "ci", ["runs:*", "*:read"], owner_id=bot.id, namespaces=["default"])
    p = Principal(tenant.id, "sa:ci-bot", kind="service", user_id=bot.id, key_scopes=tuple(key.scopes), key_namespaces=tuple(key.namespaces))
    assert await engine.authz.allowed(p, "runs:create", "default")
    assert not await engine.authz.allowed(p, "workflows:write", "default")  # key narrower than the role
    assert not await engine.authz.allowed(p, "runs:create", "shared")
    with pytest.raises(Exception, match="built-in"):
        await d.put_role(tenant.id, "viewer", ["*"])
    with pytest.raises(Exception, match="unknown permissions"):
        await d.put_role(tenant.id, "weird", ["launch:missiles"])
    await d.update_user(tenant.id, bot.id, active=False)
    assert await engine.store.authenticate_api_key(token) is None


# --------------------------------------------------------------------------- OIDC


class FakeIdP:
    issuer = "https://idp.example.com"

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes: dict[str, dict] = {}
        self.token_requests: list[dict] = []

    def jwks(self):
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}

    def id_token(self, claims):
        now = int(clock.now().timestamp())
        body = {"iss": self.issuer, "aud": "shoal-client", "iat": now, "exp": now + 300, **claims}
        return jwt.encode(body, self.key, algorithm="RS256", headers={"kid": "k1"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token",
                "jwks_uri": f"{self.issuer}/jwks",
            })
        if path == "/jwks":
            return httpx.Response(200, json=self.jwks())
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            grant = self.codes.pop(form["code"], None)
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.id_token(grant), "access_token": "x", "token_type": "Bearer"})
        return httpx.Response(404)


@pytest.fixture
async def idp_world(tmp_path):
    idp = FakeIdP()
    engine = Engine(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/shoal.db"),
        http_transport=httpx.MockTransport(idp.handle),
    )
    await engine.start()
    tenant = await engine.store.create_tenant("acme")
    await engine.oidc.configure(
        tenant.id, "okta", issuer=idp.issuer + "/", client_id="shoal-client", client_secret="shh",
        groups_claim="groups", default_role="viewer", allowed_domains=["acme.test"],
    )
    yield engine, tenant, idp
    await engine.close()
    clock.reset()


async def sign_in(engine, tenant, idp, claims, redirect="https://shoal.test/auth/callback"):
    url = await engine.oidc.begin(tenant.id, "okta", redirect, redirect_to="/runs")
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert query["code_challenge_method"] == "S256" and query["client_id"] == "shoal-client"
    idp.codes["c1"] = {"nonce": query["nonce"], **claims}
    return await engine.oidc.complete(query["state"], "c1", redirect)


async def test_oidc_sign_in_provisions_user_and_syncs_groups(idp_world):
    engine, tenant, idp = idp_world
    result = await sign_in(engine, tenant, idp, {"sub": "00u1", "email": "dana@acme.test", "name": "Dana", "groups": ["platform"]})
    claims = verify_session(engine.settings, result["session"])
    assert claims["tid"] == tenant.id and result["redirect_to"] == "/runs"
    assert idp.token_requests[0]["client_secret"] == "shh" and len(idp.token_requests[0]["code_verifier"]) >= 43
    user = await engine.directory.get_user(tenant.id, result["user_id"])
    assert (user.display_name, user.oidc_subject) == ("Dana", "00u1")
    group = await engine.directory.find_group(tenant.id, "platform")
    assert await engine.directory.group_members(group.id) == [user.id]
    principal = Principal(tenant.id, "user:dana", user_id=user.id)
    assert await engine.authz.allowed(principal, "runs:read", "default")  # default role

    again = await sign_in(engine, tenant, idp, {"sub": "00u1", "email": "dana@acme.test", "groups": []})
    assert again["user_id"] == user.id
    assert await engine.directory.group_members(group.id) == []  # removed from the IdP group


async def test_oidc_rejects_bad_tokens(idp_world):
    engine, tenant, idp = idp_world
    with pytest.raises(Forbidden, match="domain"):
        await sign_in(engine, tenant, idp, {"sub": "x", "email": "eve@evil.test"})
    url = await engine.oidc.begin(tenant.id, "okta", "https://shoal.test/cb")
    state = parse_qs(urlparse(url).query)["state"][0]
    idp.codes["c1"] = {"sub": "x", "email": "eve@acme.test", "nonce": "wrong"}
    with pytest.raises(Unauthorized, match="nonce"):
        await engine.oidc.complete(state, "c1", "https://shoal.test/cb")
    with pytest.raises(Unauthorized, match="expired or was already used"):
        await engine.oidc.complete(state, "c1", "https://shoal.test/cb")
