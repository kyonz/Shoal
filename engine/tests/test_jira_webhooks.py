"""Jira webhooks that Shoal registers itself (`Trigger.spec.webhook`), against the MockJira of the test kit.

The journey: a ticket lands in the board's "Ready for AI" column -> Jira delivers to the webhook Shoal
registered for the trigger -> the bugfix workflow runs -> it writes back to the ticket.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import httpx
import jwt
import pytest

from shoal.core import clock
from shoal.core.loader import load_path, load_text
from shoal.core.models import Agent
from shoal.integrations.jira import JiraIntegration, connect_jwt
from shoal.sdk.testing import IntegrationHarness, MockJira, engine_harness, jql_matches

from .fakes import FakeGitHub, comment_text

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
APP_SECRET = "oauth-client-secret-0123456789abcdef"


def router(jira: MockJira, github: FakeGitHub):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return github.handle(request)
        return jira(request)

    return handle


def trigger(name: str = "ready-for-ai", *, webhook: dict | None = None, enabled: bool = True, connection: str = "jira", **spec) -> dict:
    return {
        "apiVersion": "shoal.dev/v1", "kind": "Trigger", "metadata": {"name": name},
        "spec": {
            "workflow": "note", "connection": connection, "event": "issue_updated", "enabled": enabled,
            "inputs": {"key": "{{ event.issue_key }}"}, "dedupe_key": "{{ event.issue_key }}:{{ event.timestamp }}",
            **({"webhook": webhook} if webhook is not None else {}), **spec,
        },
    }


NOTE_WORKFLOW = {
    "apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": "note"},
    "spec": {
        "inputs": {"key": {"type": "string"}},
        "connections": {"jira": "jira"},
        "steps": [{"id": "say", "type": "action", "action": "jira.add_comment", "with": {"key": "{{ inputs.key }}", "body": "Shoal saw it"}}],
    },
}


@pytest.fixture
def jira():
    mock = MockJira(app_secret=APP_SECRET)
    mock.add_issue("BUG-7", "Crash on login", "Login crashes when the email has a plus sign.", fields={"customfield_10050": "acme/api"})
    mock.add_board(42, {"To Do": ["To Do"], "Ready for AI": ["Ready for AI"], "Doing": ["In Progress", "In Review"], "Done": ["Done"]})
    return mock


@pytest.fixture
async def world(jira, tmp_path, monkeypatch):
    monkeypatch.setenv("SHOAL_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    github = FakeGitHub()
    async with engine_harness(http=router(jira, github), directory=tmp_path) as w:
        w.github = github
        yield w


async def connect_admin(world, name: str = "jira") -> None:
    await world.connect(name, "jira", config={"base_url": "https://acme.atlassian.net", "email": "bot@acme.example"},
                        secrets={"api_token": "t0ken"})


async def connect_oauth(world, name: str = "jira") -> None:
    await world.connect(name, "jira", config={"base_url": "https://acme.atlassian.net", "cloud_id": "cloud-1"},
                        secrets={"oauth_token": "access-token", "oauth_client_secret": APP_SECRET})


async def registration(world, name: str = "ready-for-ai"):
    return await world.engine.webhooks.get(world.tenant_id, "default", name)


# ----------------------------------------------------------------------------- the journey


async def test_journey_column_to_bugfix_to_writeback(world, jira):
    """Ticket lands in column "Ready for AI" -> bugfix workflow -> PR + comment + transition on the ticket."""
    world.github.add_repo("acme/api")
    await world.secret("jira-api-token", "t0ken")
    await world.secret("github-token", "ghp_x")
    await world.secret("github-webhook-secret", "gh")
    await world.secret("jira-webhook-secret", "unused")
    for resource in [r for d in ("shared", "jira-bugfix", "jira-webhooks") for r in load_path(EXAMPLES / d)]:
        if isinstance(resource, Agent):
            resource.spec.provider = "scripted"
            resource.spec.provider_config = {"final_outputs": {
                "tests_passed": True, "branch": "fix/bug-7", "root_cause": "Email normalisation stripped '+'.",
                "test_summary": "212 passed, 0 failed",
            }}
        if resource.metadata.name == "jira-ready-for-ai":
            continue  # the hand-configured variant of the same trigger
        await world.apply(json.dumps(resource.to_document()))

    # Shoal registered an admin webhook (API-token connection) for the column's statuses, scoped by the trigger's JQL.
    [hook] = jira.webhooks.values()
    reg = await registration(world, "jira-ready-for-ai-registered")
    assert reg.status == "active", reg.error
    assert hook.kind == "admin" and hook.events == ["jira:issue_updated"]
    assert hook.jql == "(project = BUG AND issuetype = Bug) AND status in (10001)"
    assert hook.url == f"{await world.engine.integrations.webhook_url(world.tenant_id, 'jira-acme')}/{reg.id}"
    assert hook.secret and hook.name == "Shoal default/jira-ready-for-ai-registered"

    jira.move("BUG-7", "Ready for AI")
    [delivery] = await world.flush(jira)
    assert delivery["triggers"][0]["status"] == "fired", delivery
    await world.engine.run_until_idle()
    await world.flush(jira)  # the workflow's own transitions are delivered too, and must not fire again
    await world.engine.run_until_idle()

    runs = await world.runs()
    assert len(runs) == 1 and runs[0].status == "succeeded", runs[0].error
    assert world.github.pulls["acme/api"][1]["title"] == "BUG-7: Crash on login"
    [(key, comment)] = jira.comments
    assert key == "BUG-7" and "https://github.com/acme/api/pull/1" in comment_text(comment)
    assert jira.issues["BUG-7"]["fields"]["status"]["name"] == "In Review"
    assert (await registration(world, "jira-ready-for-ai-registered")).last_received_at is not None


# ----------------------------------------------------------------------------- admin webhooks


async def test_admin_webhook_lifecycle_follows_the_trigger(world, jira):
    await connect_admin(world)
    await world.apply(NOTE_WORKFLOW, trigger(webhook={"statuses": ["Ready for AI"]}))
    [hook] = jira.webhooks.values()
    assert hook.jql == 'status in ("Ready for AI")'

    # Changing the trigger updates the same Jira webhook in place.
    await world.apply(trigger(webhook={"board": 42, "column": "Doing", "jql": "project = BUG"}))
    [same] = jira.webhooks.values()
    assert same.id == hook.id and same.jql == "(project = BUG) AND status in (3, 10002)"
    assert ("PUT", f"/rest/webhooks/1.0/webhook/{hook.id}") in jira.calls

    # Disabling removes it; enabling registers it again; deleting the trigger removes it for good.
    await world.apply(trigger(webhook={"statuses": ["Ready for AI"]}, enabled=False))
    assert jira.webhooks == {} and await registration(world) is None
    await world.apply(trigger(webhook={"statuses": ["Ready for AI"]}))
    assert len(jira.webhooks) == 1
    await world.engine.store.delete_resource(world.tenant_id, "Trigger", "ready-for-ai", namespace="default")
    await world.engine.webhooks.reconcile(force=True)
    assert jira.webhooks == {} and await registration(world) is None


async def test_webhook_deleted_in_jira_is_recreated(world, jira):
    await connect_admin(world)
    await world.apply(NOTE_WORKFLOW, trigger(webhook={"statuses": ["Ready for AI"]}))
    jira.webhooks.clear()  # someone deleted it in Jira's admin screens
    await world.engine.webhooks.sync(world.tenant_id, "default", "ready-for-ai", force=True)
    assert len(jira.webhooks) == 1 and (await registration(world)).status == "active"


async def test_status_filter_and_signatures(world, jira):
    await connect_admin(world)
    jira.add_issue("BUG-8", "Other", status="To Do")
    # The webhook has no JQL status clause here (events only), so Shoal itself checks the status move.
    await world.apply(NOTE_WORKFLOW, trigger(webhook={"statuses": ["Ready for AI"], "jql": "project = BUG"}))
    hook = next(iter(jira.webhooks.values()))
    hook.jql = "project = BUG"  # as if Jira sent every BUG update

    jira.move("BUG-8", "In Progress")
    assert [r["triggers"] for r in await world.flush(jira)] == [[]]
    jira.move("BUG-8", "Ready for AI")
    [fired] = await world.flush(jira)
    assert fired["triggers"][0]["status"] == "fired"

    [delivery] = jira.move("BUG-7", "Ready for AI")
    jira.take()
    tampered = delivery.body.replace(b"BUG-7", b"BUG-9")
    with pytest.raises(Exception, match="signature"):
        await world.deliver(delivery.url, delivery.headers, tampered)
    with pytest.raises(Exception, match="signature"):
        await world.deliver(delivery.url, {}, delivery.body)
    with pytest.raises(Exception, match="unknown webhook"):
        await world.deliver(delivery.url.rsplit("/", 1)[0] + "/whr_nope", delivery.headers, delivery.body)


async def test_registered_trigger_ignores_connection_url_and_vice_versa(world, jira):
    await world.connect("jira", "jira", config={"base_url": "https://acme.atlassian.net", "email": "bot@acme.example"},
                        secrets={"api_token": "t0ken", "webhook_secret": "conn-secret"})
    await world.apply(NOTE_WORKFLOW, trigger(webhook={"statuses": ["Ready for AI"]}), trigger("manual-hook"))
    [delivery] = jira.move("BUG-7", "Ready for AI")
    jira.take()
    # Through the registered URL only the registered trigger fires ...
    result = await world.deliver(delivery.url, delivery.headers, delivery.body)
    assert [t["trigger"] for t in result["triggers"]] == ["ready-for-ai"]
    # ... and through the connection's own URL only the hand-configured one does.
    url = await world.engine.integrations.webhook_url(world.tenant_id, "jira")
    result = await world.deliver(url, {"X-Shoal-Token": "conn-secret"}, delivery.body)
    assert [t["trigger"] for t in result["triggers"]] == ["manual-hook"]


async def test_connection_url_rejects_unsigned_deliveries_without_a_secret(world, jira):
    await connect_admin(world)  # no webhook_secret
    url = await world.engine.integrations.webhook_url(world.tenant_id, "jira")
    with pytest.raises(Exception, match="signature"):
        await world.deliver(url, {}, {"webhookEvent": "jira:issue_updated", "issue": {"key": "BUG-7"}})


async def test_registration_errors_are_recorded_and_retried(world, jira):
    await connect_admin(world)
    jira.admin_enabled = False
    await world.apply(NOTE_WORKFLOW, trigger(webhook={"statuses": ["Ready for AI"]}))
    reg = await registration(world)
    assert reg.status == "error" and "administrator" in reg.error and reg.next_attempt_at > clock.now()

    jira.admin_enabled = True
    await world.engine.webhooks.reconcile(force=True)  # still backing off
    assert (await registration(world)).status == "error"
    clock.advance(timedelta(minutes=5))
    await world.engine.webhooks.reconcile(force=True)
    assert (await registration(world)).status == "active" and len(jira.webhooks) == 1


async def test_unknown_column_is_an_error(world, jira):
    await connect_admin(world)
    await world.apply(NOTE_WORKFLOW, trigger(webhook={"board": 42, "column": "Nope"}))
    reg = await registration(world)
    assert reg.status == "error" and "no column 'Nope'" in reg.error and "Ready for AI" in reg.error


# ----------------------------------------------------------------------------- dynamic webhooks (Cloud apps)


async def test_dynamic_webhook_oauth_register_deliver_refresh(world, jira):
    await connect_oauth(world)
    await world.apply(NOTE_WORKFLOW, trigger(webhook={"board": 42, "column": "Ready for AI", "jql": "project = BUG"}))
    [hook] = jira.webhooks.values()
    reg = await registration(world)
    assert reg.status == "active", reg.error
    assert hook.kind == "dynamic" and hook.fields == ["status"] and hook.jql == "(project = BUG) AND status in (10001)"
    assert reg.state["webhook_ids"] == [int(hook.id)]
    assert timedelta(days=29) < reg.expires_at - clock.now() <= timedelta(days=30)
    assert all(m != "POST" or p != "/rest/webhooks/1.0/webhook" for m, p in jira.calls)

    # A summary edit is not sent at all (fieldIdsFilter); a move into the column is, signed with a JWT.
    assert jira.update("BUG-7", summary="Crash on login (again)") == []
    jira.move("BUG-7", "Ready for AI")
    [fired] = await world.flush(jira)
    assert fired["triggers"][0]["status"] == "fired"

    [delivery] = jira.move("BUG-7", "Ready for AI")
    jira.take()
    forged = jwt.encode({"exp": 9999999999}, "not-the-secret-0123456789abcdef0123", algorithm="HS256")
    with pytest.raises(Exception, match="signature"):
        await world.deliver(delivery.url, {**delivery.headers, "Authorization": f"Bearer {forged}"}, delivery.body)
    other = json.dumps({**delivery.json, "matchedWebhookIds": [1]}).encode()
    with pytest.raises(Exception, match="signature"):
        await world.deliver(delivery.url, delivery.headers, other)

    # Before the 30 days are up, the background reconcile refreshes it.
    clock.advance(timedelta(days=24))
    await world.engine.webhooks.reconcile(force=True)
    assert ("PUT", "/rest/api/3/webhook/refresh") in jira.calls
    reg = await registration(world)
    assert reg.expires_at - clock.now() > timedelta(days=7)

    # A changed trigger replaces the webhook (dynamic webhooks have no update): new first, then the old one goes.
    await world.apply(trigger(webhook={"statuses": ["In Review"], "jql": "project = BUG"}))
    [new] = jira.webhooks.values()
    assert new.id != hook.id and new.jql == '(project = BUG) AND status in ("In Review")'


async def test_dynamic_webhook_needs_jql(world, jira):
    await connect_oauth(world)
    await world.apply(NOTE_WORKFLOW, trigger(webhook={}))
    reg = await registration(world)
    assert reg.status == "error" and "JQL" in reg.error


async def test_expired_dynamic_webhook_is_registered_again(world, jira):
    await connect_oauth(world)
    await world.apply(NOTE_WORKFLOW, trigger(webhook={"statuses": ["Ready for AI"]}))
    old = next(iter(jira.webhooks))
    del jira.webhooks[old]  # Jira dropped it
    clock.advance(timedelta(days=25))
    await world.engine.webhooks.reconcile(force=True)
    [new] = jira.webhooks
    assert new != old and (await registration(world)).status == "active"


async def test_per_user_oauth_registers_as_the_trigger_author(world, jira):
    """With each person's own OAuth credential, the webhook is registered (and refreshed) as whoever saved the trigger."""
    await world.secret("jira-app-secret", APP_SECRET)
    await world.apply({
        "apiVersion": "shoal.dev/v1", "kind": "Connection", "metadata": {"name": "jira"},
        "spec": {"integration": "jira", "config": {"base_url": "https://acme.atlassian.net", "cloud_id": "cloud-1"},
                 "secrets": {"oauth_client_secret": "jira-app-secret"}, "credentials": "per_user", "user_fields": ["oauth_token"]},
    })
    await world.engine.user_credentials.put(world.tenant_id, "user:dana@acme.example", "default", "jira", {"oauth_token": "dana-token"})
    await world.apply(NOTE_WORKFLOW)
    [resource] = load_text(json.dumps(trigger(webhook={"statuses": ["Ready for AI"]})))
    await world.engine.store.apply_resource(world.tenant_id, resource, actor="user:dana@acme.example")
    await world.engine.webhooks.sync(world.tenant_id, "default", "ready-for-ai")
    reg = await registration(world)
    assert reg.status == "active", reg.error
    assert reg.registered_by == "user:dana@acme.example"
    [hook] = jira.webhooks.values()
    assert hook.kind == "dynamic"
    assert ("POST", "/rest/api/3/webhook") in jira.calls


async def test_connect_app_signs_calls_and_checks_deliveries(world, jira):
    jira.connect_client_key = "jira-client-key"
    await world.connect("jira", "jira", config={"base_url": "https://acme.atlassian.net", "connect_app_key": "com.acme.shoal",
                                                "connect_client_key": "jira-client-key"},
                        secrets={"connect_shared_secret": APP_SECRET})
    await world.apply(NOTE_WORKFLOW, trigger(webhook={"statuses": ["Ready for AI"]}))
    assert (await registration(world)).status == "active"
    jira.move("BUG-7", "Ready for AI")
    [fired] = await world.flush(jira)
    assert fired["triggers"][0]["status"] == "fired"

    [delivery] = jira.move("BUG-7", "Ready for AI")
    jira.take()
    wrong_iss = jwt.encode({"iss": "someone-else", "exp": 9999999999}, APP_SECRET, algorithm="HS256")
    with pytest.raises(Exception, match="signature"):
        await world.deliver(delivery.url, {**delivery.headers, "Authorization": f"JWT {wrong_iss}"}, delivery.body)


def test_connect_jwt_query_string_hash():
    token = connect_jwt("app", "s3cret-0123456789abcdef0123456789ab", "get", "/rest/api/3/issue/BUG-1", {"fields": "summary,status", "b": ["2", "1"]})
    claims = jwt.decode(token, "s3cret-0123456789abcdef0123456789ab", algorithms=["HS256"])
    expected = hashlib.sha256(b"GET&/rest/api/3/issue/BUG-1&b=1,2&fields=summary%2Cstatus").hexdigest()
    assert claims["iss"] == "app" and claims["qsh"] == expected


# ----------------------------------------------------------------------------- Data Center


async def test_data_center_uses_admin_webhooks(world, jira):
    jira.dynamic_enabled = False
    await world.connect("jira", "jira", config={"base_url": "https://acme.atlassian.net", "api_version": "2"},
                        secrets={"personal_access_token": "pat"})
    await world.apply(NOTE_WORKFLOW, trigger(webhook={"statuses": ["Ready for AI"], "mode": "dynamic"}))
    reg = await registration(world)
    assert reg.status == "error" and "Cloud" in reg.error

    await world.apply(trigger(webhook={"statuses": ["Ready for AI"], "require_signature": False}))
    reg = await registration(world)
    assert reg.status == "active" and next(iter(jira.webhooks.values())).kind == "admin"
    # Older Data Center versions do not sign: with require_signature off, the unguessable URL is the check.
    hook = next(iter(jira.webhooks.values()))
    hook.secret = None
    jira.move("BUG-7", "Ready for AI")
    [fired] = await world.flush(jira)
    assert fired["triggers"][0]["status"] == "fired"


# ----------------------------------------------------------------------------- units


def test_mock_jql():
    issue = MockJira().add_issue("BUG-1", "x", status="Ready for AI", labels=["ai"])
    statuses = MockJira().statuses
    assert jql_matches('project = BUG AND status in ("Ready for AI", Done)', issue, statuses)
    assert jql_matches("(project = BUG) AND status in (10001) AND labels = ai", issue, statuses)
    assert not jql_matches("project != BUG", issue, statuses)
    with pytest.raises(AssertionError):
        jql_matches("project = BUG OR project = OPS", issue, statuses)


async def test_events_follow_the_trigger_event():
    harness = IntegrationHarness(JiraIntegration, config={"base_url": "https://x.atlassian.net", "email": "a", "api_token": "b"})
    integration = harness.integration
    config = JiraIntegration.webhook_model.model_validate({})
    assert integration._events("issue_*", config) == ["jira:issue_created", "jira:issue_updated", "jira:issue_deleted"]
    assert integration._events("comment_created", config) == ["comment_created"]
    with pytest.raises(Exception, match="no Jira webhook event"):
        integration._events("automation", config)
    with pytest.raises(ValueError, match="go together"):
        JiraIntegration.webhook_model.model_validate({"column": "Ready"})


# ----------------------------------------------------------------------------- the API


@pytest.fixture
async def api(engine, jira):
    from pydantic import SecretStr

    from shoal.api.app import create_app

    engine.http_transport = jira.transport
    engine.settings.admin_token = SecretStr("admin")
    app = create_app(engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test") as client:
        r = await client.post("/v1/tenants", json={"slug": "acme"}, headers={"Authorization": "Bearer admin"})
        client.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        yield client


async def test_api_registers_shows_and_removes_webhooks(api, jira, engine):
    ns = "/v1/namespaces/default"
    assert (await api.put(f"{ns}/secrets/jira-token", json={"value": "t0ken"})).status_code == 201
    r = await api.put(f"{ns}/connections/jira", json={"spec": {
        "integration": "jira", "config": {"base_url": "https://acme.atlassian.net", "email": "bot@acme.example"},
        "secrets": {"api_token": "jira-token"}}})
    assert r.status_code == 201, r.text
    assert (await api.put(f"{ns}/workflows/note", json={"spec": NOTE_WORKFLOW["spec"]})).status_code == 201

    # Bad settings are refused at save time, with the integration's own validation.
    spec = trigger(webhook={"board": 42})["spec"]
    r = await api.put(f"{ns}/triggers/ready-for-ai", json={"spec": spec})
    assert r.status_code == 422 and "go together" in r.text

    r = await api.post(f"{ns}/trigger-webhook-preview", json={"connection": "jira", "event": "issue_updated",
                                                             "webhook": {"statuses": ["Ready for AI"], "jql": "project = BUG"}})
    assert r.json() == {"integration": "jira", "events": ["jira:issue_updated"], "jql": '(project = BUG) AND status in ("Ready for AI")'}

    spec = trigger(webhook={"board": 42, "column": "Ready for AI"})["spec"]
    r = await api.put(f"{ns}/triggers/ready-for-ai", json={"spec": spec})
    assert r.status_code == 201, r.text
    summary = r.json()["status"]["webhook"]
    assert summary["status"] == "active" and summary["mode"] == "admin"
    webhook = (await api.get(f"{ns}/triggers/ready-for-ai/webhook")).json()
    assert webhook["id"] == summary["id"] and webhook["url"] == summary["url"]
    assert webhook["state"]["statuses"] == ["10001"] and webhook["desired"]["jql"] == "status in (10001)"
    [hook] = jira.webhooks.values()
    assert hook.url == webhook["url"] and webhook["url"].startswith("http://shoal.test/v1/hooks/wh_")

    [listed] = (await api.get(f"{ns}/connections/jira/webhooks")).json()
    assert listed["trigger"] == "ready-for-ai"

    # Jira delivers over HTTP to the registered URL; it is verified, queued, then handled by a worker.
    [delivery] = jira.move("BUG-7", "Ready for AI")
    path = delivery.url.replace("http://shoal.test", "")
    r = await api.post(path, content=delivery.body, headers=delivery.headers)
    assert r.status_code == 202 and r.json()["queued"] is True, r.text
    assert (await api.post(path, content=delivery.body, headers={})).status_code == 422
    assert await engine.integrations.process_queued_webhooks() == 1
    handled = await engine.store.get_webhook_delivery(r.json()["delivery"])
    assert handled.status == "done" and handled.result["triggers"][0]["status"] == "fired", handled.result
    # The registration's secret is not the connection's: its signature does not open the connection's URL.
    r = await api.post(path.rsplit("/", 1)[0], content=delivery.body,
                       headers={**delivery.headers, "x-shoal-registration-id": webhook["id"]})
    assert r.status_code == 422
    # Nor can a sender of the connection's URL route a delivery to a registration with Shoal's internal header.
    await api.put(f"{ns}/secrets/jira-hook", json={"value": "conn-secret"})
    r = await api.put(f"{ns}/connections/jira", json={"spec": {
        "integration": "jira", "config": {"base_url": "https://acme.atlassian.net", "email": "bot@acme.example"},
        "secrets": {"api_token": "jira-token", "webhook_secret": "jira-hook"}}})
    assert r.status_code == 200, r.text
    signature = "sha256=" + hmac.new(b"conn-secret", delivery.body, hashlib.sha256).hexdigest()
    r = await api.post(path.rsplit("/", 1)[0], content=delivery.body,
                       headers={"x-hub-signature": signature, "x-shoal-registration-id": webhook["id"]})
    assert r.status_code == 202, r.text
    assert await engine.integrations.process_queued_webhooks() == 1
    plain = await engine.store.get_webhook_delivery(r.json()["delivery"])
    assert plain.status == "done" and plain.result["triggers"] == [], plain.result

    # Re-sync after someone deleted it in Jira.
    jira.webhooks.clear()
    r = await api.post(f"{ns}/triggers/ready-for-ai/webhook/sync")
    assert r.json()["status"] == "active" and len(jira.webhooks) == 1

    # A trigger on a connection whose integration cannot register webhooks is refused.
    await api.put(f"{ns}/secrets/gh", json={"value": "x"})
    await api.put(f"{ns}/connections/gh", json={"spec": {"integration": "github", "secrets": {"token": "gh"}}})
    r = await api.put(f"{ns}/triggers/gh-push", json={"spec": {**spec, "connection": "gh", "event": "push"}})
    assert r.status_code == 422 and "cannot register webhooks" in r.text

    assert (await api.delete(f"{ns}/triggers/ready-for-ai")).status_code == 204
    assert jira.webhooks == {}
    assert (await api.get(f"{ns}/connections/jira/webhooks")).json() == []

    # The multi-document apply registers too.
    doc = trigger(webhook={"statuses": ["Ready for AI"]})
    r = await api.post("/v1/apply", content=json.dumps(doc))
    assert r.status_code == 200, r.text
    assert len(jira.webhooks) == 1
