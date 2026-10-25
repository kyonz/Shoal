"""Many tenants on one instance: instance sign-in, accounts in several tenants, switching, instance admins."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from shoal.api.app import create_app
from shoal.auth.sessions import verify_session
from shoal.config import InstanceOIDC, Settings
from shoal.core import clock
from shoal.core.errors import Forbidden
from shoal.engine import Engine
from tests.test_tenancy import FakeIdP

WF = {"spec": {"steps": [{"id": "s", "type": "set", "values": {"ok": True}}]}}


@pytest.fixture
async def world(tmp_path):
    idp = FakeIdP()
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/shoal.db", public_url="https://shoal.test",
                        instance_oidc=InstanceOIDC(issuer=idp.issuer, client_id="shoal-client", client_secret="shh"),
                        instance_admins=["root@corp.test"])
    engine = Engine(settings, http_transport=httpx.MockTransport(idp.handle))
    await engine.start()
    app = create_app(engine=engine)
    clients = []

    def client(session: str) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://shoal.test",
                              headers={"Authorization": f"Bearer {session}"})
        clients.append(c)
        return c

    yield engine, idp, client
    for c in clients:
        await c.aclose()
    await engine.close()
    clock.reset()


async def instance_sign_in(engine, idp, claims) -> dict:
    url = await engine.oidc.begin(None, "instance", "https://shoal.test/v1/auth/callback", redirect_to="/")
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    idp.codes["c1"] = {"nonce": query["nonce"], **claims}
    return await engine.oidc.complete(query["state"], "c1", "https://shoal.test/v1/auth/callback")


async def test_people_switch_between_their_tenants(world):
    engine, idp, client = world
    root = await instance_sign_in(engine, idp, {"sub": "root", "email": "root@corp.test", "email_verified": True, "name": "Root"})
    claims = verify_session(engine.settings, root["session"])
    assert "tid" not in claims and claims["acc"]  # instance admin with no tenant of their own yet
    admin = client(root["session"])
    me = (await admin.get("/v1/me")).json()
    assert me["instance_role"] == "instance-admin" and me["tenant"] is None

    for slug, name in (("red", "Red team"), ("blue", "Blue team")):
        r = await admin.post("/v1/tenants", json={"slug": slug, "name": name, "admin_email": "dana@corp.test"})
        assert r.status_code == 201, r.text
    assert (await admin.post("/v1/tenants", json={"slug": "green"})).status_code == 201
    r = await admin.post("/v1/tenants/blue/members", json={"email": "erin@corp.test", "role": "viewer"})
    assert r.status_code == 201 and r.json()["roles"] == ["viewer"] and r.json()["linked"] is False

    # Dana signs in once and lands in her first tenant; both of hers are in the switcher.
    dana_login = await instance_sign_in(engine, idp, {"sub": "dana", "email": "dana@corp.test", "email_verified": True, "name": "Dana"})
    dana = client(dana_login["session"])
    mine = (await dana.get("/v1/me/tenants")).json()
    assert [(t["slug"], t["member"], t["current"]) for t in mine] == [("blue", True, True), ("red", True, False)]
    assert (await dana.get("/v1/instance/tenants")).status_code == 403

    r = await dana.post("/v1/me/tenant", json={"tenant": "red"})
    assert r.status_code == 200 and r.json()["tenant"]["slug"] == "red" and "shoal_session" in r.headers.get("set-cookie", "")
    in_red = client(r.json()["session"])
    assert (await in_red.get("/v1/me")).json()["tenant"]["slug"] == "red"
    assert (await in_red.put("/v1/namespaces/default/workflows/hello", json=WF)).status_code == 201  # tenant-admin there
    assert (await dana.post("/v1/me/tenant", json={"tenant": "green"})).status_code == 403  # not a member

    # The instance admin sees every tenant and can step into one without being a member (audited as their account).
    summaries = {t["slug"]: t for t in (await admin.get("/v1/instance/tenants")).json()}
    assert summaries["red"]["users"] == 1 and summaries["blue"]["users"] == 2 and not summaries["red"]["federated"]
    r = await admin.post("/v1/me/tenant", json={"tenant": "red"})
    support = client(r.json()["session"])
    assert (await support.get("/v1/me")).json()["actor"] == "account:root@corp.test"
    assert [w["metadata"]["name"] for w in (await support.get("/v1/namespaces/default/workflows")).json()] == ["hello"]
    switcher = {t["slug"]: t["member"] for t in (await admin.get("/v1/me/tenants")).json()}
    assert switcher == {"red": False, "blue": False, "green": False}

    # Suspending a tenant locks its members out and stops new runs; the instance admin keeps access.
    r = await admin.patch("/v1/tenants/red", json={"status": "suspended", "description": "Offboarding"})
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    assert (await in_red.get("/v1/namespaces/default/workflows")).status_code == 403
    assert (await dana.post("/v1/me/tenant", json={"tenant": "red"})).status_code == 403
    assert (await support.get("/v1/namespaces/default/workflows")).status_code == 200
    red = await engine.store.get_tenant_by_slug("red")
    with pytest.raises(Forbidden, match="suspended"):
        await engine.executor.start_run(red.id, "hello", {})
    assert [t["slug"] for t in (await dana.get("/v1/me/tenants")).json()] == ["blue", "red"]  # still listed, marked suspended
    await admin.patch("/v1/tenants/red", json={"status": "active"})
    assert (await in_red.get("/v1/namespaces/default/workflows")).status_code == 200

    actions = [e["action"] for e in (await admin.get("/v1/instance/audit")).json()]
    assert {"tenant.created", "tenant.suspended", "tenant.resumed", "auth.login"} <= set(actions)

    # Signing out ends the session itself, also one held by an account without a tenant user.
    assert (await support.post("/v1/auth/logout")).status_code == 204
    assert (await support.get("/v1/me")).status_code == 401
    assert (await admin.get("/v1/me")).status_code == 200  # other sessions of the same account carry on
    assert (await in_red.post("/v1/auth/logout")).status_code == 204
    assert (await in_red.get("/v1/me")).status_code == 401
    assert (await dana.get("/v1/me")).status_code == 200
    logouts = await engine.store.list_audit(red.id, action="auth.logout")
    assert sorted(e.actor for e in logouts) == ["account:root@corp.test", "user:dana@corp.test"]


async def test_instance_roles_and_their_limits(world):
    engine, idp, client = world
    admin = client((await instance_sign_in(engine, idp, {"sub": "root", "email": "root@corp.test", "email_verified": True}))["session"])
    await admin.post("/v1/tenants", json={"slug": "red", "admin_email": "dana@corp.test"})
    await admin.post("/v1/tenants", json={"slug": "blue"})
    dana_login = await instance_sign_in(engine, idp, {"sub": "dana", "email": "dana@corp.test", "email_verified": True})
    dana_account = verify_session(engine.settings, dana_login["session"])["acc"]

    accounts = {a["email"]: a for a in (await admin.get("/v1/instance/accounts")).json()}
    assert accounts["root@corp.test"]["instance_role"] == "instance-admin" and accounts["dana@corp.test"]["instance_role"] is None
    detail = (await admin.get(f"/v1/instance/accounts/{dana_account}")).json()
    assert [(m["tenant"], m["roles"]) for m in detail["tenants"]] == [("red", ["tenant-admin"])]

    # Auditors read everything, everywhere, and change nothing.
    r = await admin.patch(f"/v1/instance/accounts/{dana_account}", json={"instance_role": "instance-auditor"})
    assert r.status_code == 200 and r.json()["instance_role"] == "instance-auditor"
    dana = client(dana_login["session"])
    assert {t["slug"] for t in (await dana.get("/v1/instance/tenants")).json()} == {"red", "blue"}
    assert (await dana.patch("/v1/tenants/blue", json={"name": "x"})).status_code == 403
    blue = client((await dana.post("/v1/me/tenant", json={"tenant": "blue"})).json()["session"])
    assert (await blue.get("/v1/namespaces/default/workflows")).status_code == 200
    assert (await blue.put("/v1/namespaces/default/workflows/nope", json=WF)).status_code == 403

    # Nobody changes their own instance role, and the last instance admin cannot be removed.
    root_id = accounts["root@corp.test"]["id"]
    assert (await admin.patch(f"/v1/instance/accounts/{root_id}", json={"instance_role": None})).status_code == 403
    await admin.patch(f"/v1/instance/accounts/{dana_account}", json={"instance_role": "instance-admin"})
    dana_admin = client((await dana.post("/v1/me/tenant", json={"tenant": None})).json()["session"])
    assert (await dana_admin.patch(f"/v1/instance/accounts/{root_id}", json={"active": False})).status_code == 200
    assert (await admin.get("/v1/me")).status_code == 401  # deactivated everywhere


async def test_only_the_instance_sign_in_vouches_for_emails(world):
    engine, idp, client = world
    admin = client((await instance_sign_in(engine, idp, {"sub": "root", "email": "root@corp.test", "email_verified": True}))["session"])
    await admin.post("/v1/tenants", json={"slug": "red", "admin_email": "dana@corp.test"})
    with pytest.raises(Forbidden, match="not a member"):  # unverified email: no linking by email
        await instance_sign_in(engine, idp, {"sub": "mallory", "email": "dana@corp.test", "email_verified": False})

    # A tenant's own identity provider cannot make anyone an instance admin or reach other tenants by email.
    red = await engine.store.get_tenant_by_slug("red")
    await engine.store.create_tenant("evil")
    evil = await engine.store.get_tenant_by_slug("evil")
    await engine.oidc.configure(evil.id, "own", issuer="https://idp.example.com", client_id="shoal-client", auto_provision=True)
    url = await engine.oidc.begin(evil.id, "own", "https://shoal.test/cb")
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    idp.codes["c1"] = {"nonce": query["nonce"], "sub": "eve", "email": "root@corp.test", "email_verified": True}
    result = await engine.oidc.complete(query["state"], "c1", "https://shoal.test/cb")
    eve = client(result["session"])
    me = (await eve.get("/v1/me")).json()
    assert me["instance_role"] is None and me["tenant"]["slug"] == "evil"
    assert [t["slug"] for t in (await eve.get("/v1/me/tenants")).json()] == ["evil"]
    assert (await eve.post("/v1/me/tenant", json={"tenant": "red"})).status_code == 403
    assert await engine.directory.find_user(red.id, email="root@corp.test") is None

    # API keys belong to one tenant and cannot switch.
    key = (await admin.post("/v1/tenants", json={"slug": "keyed"})).json()["api_key"]
    keyed = client(key)
    assert [t["slug"] for t in (await keyed.get("/v1/me/tenants")).json()] == ["keyed"]
    r = await keyed.post("/v1/me/tenant", json={"tenant": "red"})
    assert r.status_code == 422 and "API keys" in r.json()["detail"]
