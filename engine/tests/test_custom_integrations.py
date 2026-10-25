"""Integrations an organisation registers itself, and organisation vs namespace connections."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from shoal.api.app import create_app
from shoal.config import Settings
from shoal.engine import Engine
from shoal.sdk.server import create_app as create_service

from .test_sdk import SERVICE_TOKEN, HostRouter, TicketsIntegration, tickets_api

ADMIN = "test-admin-token"


class OrgOnlyTickets(TicketsIntegration):
    key = "org-tickets"
    title = "Org-only tickets"
    connection_scopes = ("org",)


@pytest.fixture
async def world(tmp_path, monkeypatch):
    """An API client for tenant acme, with a tickets service at https://svc.test."""
    monkeypatch.setenv("SHOAL_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    provider = tickets_api()
    service = httpx.ASGITransport(app=create_service(TicketsIntegration, token=SERVICE_TOKEN, http_transport=provider.transport))
    transport = HostRouter({"svc.test": service, "tickets.example": provider.transport})
    engine = Engine(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/shoal.db", public_url="http://shoal.test",
                             admin_token=SecretStr(ADMIN)), http_transport=transport)
    engine.registry.register("integrations", "org-tickets", OrgOnlyTickets)
    await engine.start()
    app = create_app(engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test") as client:
        r = await client.post("/v1/tenants", json={"slug": "acme", "name": "Acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
        assert r.status_code == 201, r.text
        client.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        for ns in ("payments", "search"):
            assert (await client.put(f"/v1/namespaces/{ns}", json={})).status_code in (200, 201)
        client.engine = engine  # type: ignore[attr-defined]
        yield client
    await engine.close()


async def secret(api, ns: str, name: str, value: str) -> None:
    r = await api.put(f"/v1/namespaces/{ns}/secrets/{name}", json={"value": value})
    assert r.status_code in (200, 201), r.text


async def register_tickets(api, name: str = "tickets", **spec) -> httpx.Response:
    await secret(api, "shared", "tickets-service-token", SERVICE_TOKEN)
    body = {"spec": {"source": "remote", "url": "https://svc.test", "token_secret": "tickets-service-token", **spec}}
    return await api.put(f"/v1/custom-integrations/{name}", json=body)


async def connect(api, ns: str, name: str, integration: str, **spec) -> httpx.Response:
    await secret(api, ns, f"{name}-api-token", "t0k")
    body = {"spec": {"integration": integration, "secrets": {"api_token": f"{name}-api-token"}, **spec}}
    return await api.put(f"/v1/namespaces/{ns}/connections/{name}", json=body)


async def test_registering_a_remote_service_pulls_its_manifest(world):
    r = await register_tickets(world, "acme-tickets", title="Acme tickets")
    assert r.status_code == 201, r.text
    status = r.json()["status"]
    assert status["ready"] and status["actions"] == 1 and status["secret_fields"] == ["api_token", "webhook_secret"]
    assert status["connection_scopes"] == ["org", "namespace"]

    # It shows in the tenant's catalogue under its registered name, with the service's settings schema.
    catalog = {c["key"]: c for c in (await world.get("/v1/integrations")).json()}
    entry = catalog["acme-tickets"]
    assert entry["source"] == "custom" and entry["title"] == "Acme tickets"
    assert "api_token" in entry["connection_schema"]["properties"]
    assert entry["actions"][0]["name"] == "open_ticket"
    assert (await world.get("/v1/integrations/acme-tickets")).json()["key"] == "acme-tickets"

    # Connecting works like a built-in: actions go through the service to the provider.
    assert (await connect(world, "payments", "tix", "acme-tickets")).status_code == 201
    r = await world.post("/v1/namespaces/payments/connections/tix/actions/open_ticket", json={"params": {"title": "Hi"}})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "T-1"

    # Registrations belong to one tenant.
    other = await world.post("/v1/tenants", json={"slug": "globex"}, headers={"Authorization": f"Bearer {ADMIN}"})
    keys = {c["key"] for c in (await world.get("/v1/integrations", headers={"Authorization": f"Bearer {other.json()['api_key']}"})).json()}
    assert "acme-tickets" not in keys


async def test_registration_checks(world):
    r = await world.put("/v1/custom-integrations/github", json={"spec": {"source": "remote", "url": "https://svc.test"}})
    assert r.status_code == 422 and "already an installed integration" in r.text
    r = await world.put("/v1/custom-integrations/plain", json={"spec": {"source": "remote", "url": "http://svc.example"}})
    assert r.status_code == 422 and "not https" in r.text
    r = await world.put("/v1/custom-integrations/nope", json={"spec": {"source": "package", "package": "no-such-package"}})
    assert r.status_code == 422 and "no installed integration" in r.text
    r = await world.put("/v1/custom-integrations/mod", json={"spec": {"source": "package", "package": "tests.test_sdk:TicketsIntegration"}})
    assert r.status_code == 422 and "turned off" in r.text
    r = await world.put("/v1/custom-integrations/half", json={"spec": {"source": "remote"}})
    assert r.status_code == 422 or r.status_code == 400

    # An unreachable service is stored, with the error in its status, until a refresh succeeds.
    r = await world.put("/v1/custom-integrations/later", json={"spec": {"source": "remote", "url": "https://svc.test"}})
    assert r.status_code == 201 and not r.json()["status"]["ready"] and "401" in r.json()["status"]["error"]
    assert "later" not in {c["key"] for c in (await world.get("/v1/integrations")).json()}
    r = await world.post("/v1/custom-integrations/later/refresh")
    assert r.status_code == 200 and r.json()["ready"] is False


async def test_registering_an_installed_package(world):
    world.engine.settings.integration_allow_imports = True
    r = await world.put("/v1/custom-integrations/py-tickets", json={"spec": {
        "source": "package", "package": "tests.test_sdk:TicketsIntegration", "connection_scopes": ["namespace"],
    }})
    assert r.status_code == 201, r.text
    assert r.json()["status"]["connection_scopes"] == ["namespace"]
    # By plugin name too (the operator installed it), keeping its key.
    r = await world.put("/v1/custom-integrations/org-tickets", json={"spec": {"source": "package", "package": "org-tickets", "namespaces": ["payments"]}})
    assert r.status_code == 201, r.text

    # The registration narrowed it to namespace connections.
    r = await connect(world, "shared", "py-org", "py-tickets")
    assert r.status_code == 422 and "organisation level" in r.text
    assert (await connect(world, "payments", "py", "py-tickets")).status_code == 201

    # Limited to payments: search neither sees it nor can connect it.
    search = {c["key"] for c in (await world.get("/v1/integrations", params={"namespace": "search"})).json()}
    payments = {c["key"] for c in (await world.get("/v1/integrations", params={"namespace": "payments"})).json()}
    assert "py-tickets" in search and "org-tickets" not in search and "org-tickets" in payments


async def test_integrations_declare_where_connections_live(world):
    catalog = {c["key"]: c for c in (await world.get("/v1/integrations")).json()}
    assert catalog["github"]["connection_scopes"] == ["org", "namespace"]
    assert catalog["org-tickets"]["connection_scopes"] == ["org"]
    r = await connect(world, "payments", "team", "org-tickets")
    assert r.status_code == 422 and "namespace level" in r.text and "admin console" in r.text
    assert (await connect(world, "shared", "org", "org-tickets")).status_code == 201
    r = await connect(world, "payments", "team-gh", "github", namespaces=["search"])
    assert r.status_code == 422 and "only applies to organisation connections" in r.text

    # apply reports the same problems.
    r = await world.post("/v1/apply?namespace=payments", content=(
        "apiVersion: shoal.dev/v1\nkind: Connection\nmetadata: {name: team}\nspec: {integration: org-tickets}\n"
    ))
    assert r.status_code == 422 and "namespace level" in r.text


async def test_org_connections_can_be_limited_to_some_namespaces(world):
    assert (await register_tickets(world)).status_code == 201
    assert (await connect(world, "shared", "org-tix", "tickets", namespaces=["payments"])).status_code == 201
    assert (await connect(world, "shared", "everyone", "tickets")).status_code == 201

    names = lambda r: sorted(c["metadata"]["name"] for c in r.json())  # noqa: E731
    assert names(await world.get("/v1/namespaces/payments/org-connections")) == ["everyone", "org-tix"]
    assert names(await world.get("/v1/namespaces/search/org-connections")) == ["everyone"]

    call = {"params": {"title": "Hi"}}
    r = await world.post("/v1/namespaces/payments/connections/org-tix/actions/open_ticket", json=call)
    assert r.status_code == 200, r.text
    r = await world.post("/v1/namespaces/search/connections/org-tix/actions/open_ticket", json=call)
    assert r.status_code == 404

    # Picking "the only tickets connection" ignores the ones not shared with the namespace.
    engine, tenant = world.engine, (await world.get("/v1/me")).json()["tenant"]["id"]
    from shoal.core.scope import use_namespace

    with use_namespace("search"):
        assert await engine.integrations.resolve_name(tenant, "tickets", None) == "everyone"
