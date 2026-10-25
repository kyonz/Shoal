"""The blueprints that ship with Shoal: every new tenant's catalogue has them, and each one adopts cleanly."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from shoal.api.app import create_app
from shoal.blueprints import defaults

ADMIN = "test-admin-token"
BUNDLED = {
    "aws-account-report", "github-pr-review", "github-uplift", "jira-bugfix", "production-quality-eval",
    "slack-bot", "slack-release-approval", "slack-release-check",
}


@pytest.fixture
async def api(engine):
    engine.settings.admin_token = SecretStr(ADMIN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine=engine)), base_url="http://shoal.test") as client:
        yield client


async def _tenant(api, slug: str = "acme") -> httpx.AsyncClient:
    r = await api.post("/v1/tenants", json={"slug": slug}, headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 201, r.text
    api.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
    return api


@pytest.fixture
async def admin(api):
    return await _tenant(api)


async def _catalogue(client) -> dict[str, dict]:
    return {b["metadata"]["name"]: b for b in (await client.get("/v1/blueprints")).json()}


def test_bundled_blueprints_load():
    assert {bp.metadata.name for bp in defaults.bundled()} == BUNDLED
    for bp in defaults.bundled():
        assert bp.spec.status in ("published", "featured") and bp.spec.author == "Shoal"
        assert bp.metadata.labels[defaults.LABEL_BUNDLED] == "true"
        assert bp.spec.readme and bp.spec.categories


async def test_new_tenants_get_the_bundled_blueprints(admin):
    catalogue = await _catalogue(admin)
    assert set(catalogue) == BUNDLED
    assert catalogue["github-pr-review"]["spec"]["status"] == "featured"
    assert catalogue["github-pr-review"]["metadata"]["labels"][defaults.LABEL_BUNDLED] == "true"
    assert (await admin.get("/v1/audit", params={"action": "tenant.created"})).status_code == 200


async def test_the_setting_turns_them_off(api, engine):
    engine.settings.default_blueprints = False
    client = await _tenant(api)
    assert await _catalogue(client) == {}
    r = await client.post("/v1/blueprints/defaults", json={"dry_run": True})
    assert {x["outcome"] for x in r.json()} == {"created"}
    assert await _catalogue(client) == {}
    r = await client.post("/v1/blueprints/defaults", json={})
    assert r.status_code == 200, r.text
    assert set(await _catalogue(client)) == BUNDLED


async def test_installing_again_keeps_the_tenants_own_and_upgrades_old_copies(admin):
    r = await admin.post("/v1/blueprints/defaults", json={})
    assert {x["outcome"] for x in r.json()} == {"unchanged"}

    # The tenant replaced one with its own blueprint (no bundled label): never touched.
    own = (await admin.get("/v1/blueprints/slack-bot")).json()
    own["metadata"]["labels"] = {}
    own["spec"]["title"] = "Our Slack bot"
    assert (await admin.put("/v1/blueprints/slack-bot", json={"metadata": own["metadata"], "spec": own["spec"]})).status_code == 200

    # An older bundled copy that the tenant deprecated is upgraded and stays deprecated.
    old = (await admin.get("/v1/blueprints/jira-bugfix")).json()
    old["spec"].update(version="0.9.0", status="deprecated", title="Old")
    assert (await admin.put("/v1/blueprints/jira-bugfix", json={"metadata": old["metadata"], "spec": old["spec"]})).status_code == 200

    # A deleted one comes back.
    assert (await admin.delete("/v1/blueprints/aws-account-report")).status_code == 204

    preview = {x["name"]: x["outcome"] for x in (await admin.post("/v1/blueprints/defaults", json={"dry_run": True})).json()}
    assert preview["slack-bot"] == "kept" and preview["jira-bugfix"] == "updated" and preview["aws-account-report"] == "created"
    assert (await admin.get("/v1/blueprints/jira-bugfix")).json()["spec"]["version"] == "0.9.0"

    result = {x["name"]: x["outcome"] for x in (await admin.post("/v1/blueprints/defaults", json={})).json()}
    assert result == preview
    catalogue = await _catalogue(admin)
    assert catalogue["slack-bot"]["spec"]["title"] == "Our Slack bot"
    jira = catalogue["jira-bugfix"]["spec"]
    assert jira["version"] == "1.0.0" and jira["status"] == "deprecated" and jira["title"] == "Jira bugfix with writeback"
    assert "aws-account-report" in catalogue
    audit = (await admin.get("/v1/audit", params={"action": "blueprint.defaults_installed"})).json()["items"]
    assert sorted(audit[0]["data"]["installed"]) == ["aws-account-report", "jira-bugfix"]


async def test_installing_needs_publish_and_feature(admin):
    sa = await admin.put("/v1/service-accounts/dev", json={})
    await admin.post("/v1/role-bindings", json={"role": "editor", "subject_kind": "user", "subject_id": sa.json()["id"], "namespace": "default"})
    token = (await admin.post("/v1/api-keys", json={"name": "k", "service_account": "dev"})).json()["token"]
    r = await admin.post("/v1/blueprints/defaults", json={}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_every_bundled_blueprint_adopts_cleanly(admin):
    for name, integration, config in (
        ("gh", "github", {}), ("jira", "jira", {"base_url": "https://acme.atlassian.net", "email": "bot@acme.example"}),
        ("slack", "slack", {}),
    ):
        r = await admin.put(f"/v1/namespaces/default/connections/{name}", json={"spec": {"integration": integration, "config": config}})
        assert r.status_code == 201, r.text
    assert (await admin.put("/v1/namespaces/default/secrets/anthropic-api-key", json={"value": "k"})).status_code == 201
    hello = {"spec": {"steps": [{"id": "a", "type": "set", "values": {"x": 1}}]}}
    assert (await admin.put("/v1/namespaces/default/workflows/support-triage", json=hello)).status_code == 201

    params = {
        "jira-bugfix": {"jira": "jira", "github": "gh"},
        "github-uplift": {"github": "gh", "org": "acme"},
        "github-pr-review": {"github": "gh"},
        "slack-bot": {"slack": "slack", "channel": "#help"},
        "slack-release-check": {"slack": "slack"},
        "slack-release-approval": {"slack": "slack", "approvers": "group:sre"},
        "aws-account-report": {"role_arn": "arn:aws:iam::123456789012:role/shoal-report", "slack": "slack"},
        "production-quality-eval": {"workflow": "support-triage", "platform": "braintrust"},
    }
    assert set(params) == BUNDLED
    for name, values in params.items():
        r = await admin.post(f"/v1/namespaces/default/blueprints/{name}/adopt", json={"params": values, "prefix": "team"})
        assert r.status_code == 201, f"{name}: {r.text}"
        assert all(res["name"].startswith("team-") for res in r.json()["resources"])

    wf = (await admin.get("/v1/namespaces/default/workflows/team-jira-bugfix")).json()
    assert wf["spec"]["connections"] == {"jira": "jira", "github": "gh"} and wf["spec"]["defaults"]["agent"] == "team-bugfixer"
    campaign = (await admin.get("/v1/namespaces/default/campaigns/team-coverage-uplift")).json()
    assert campaign["spec"]["targets"]["filter"]["org"] == "acme" and campaign["spec"]["policy"]["budget_usd"] == 500

    review = (await admin.get("/v1/namespaces/default/triggers/team-pr-review")).json()["spec"]
    assert review["connection"] == "gh" and review["workflow"] == "team-pr-review"
    assert review["inputs"] == {"repo": "{{ event.repo }}", "number": "{{ event.number }}"}  # run time, untouched

    gate = (await admin.get("/v1/namespaces/default/workflows/team-release-approval")).json()["spec"]
    approve = next(s for s in gate["steps"] if s["id"] == "approve")
    assert approve["approvers"] == ["group:sre"] and approve["notify"][0]["to"] == "#deploys"

    identity = (await admin.get("/v1/namespaces/default/cloud-identities/team-aws-report")).json()["spec"]
    assert identity["aws"]["role_arn"] == "arn:aws:iam::123456789012:role/shoal-report"
    assert identity["aws"]["policy_arns"] == ["arn:aws:iam::aws:policy/ReadOnlyAccess"]
    assert identity["allow"] == {"workflows": ["team-aws-account-report"], "agents": ["team-aws-auditor"]}
    auditor = (await admin.get("/v1/namespaces/default/agents/team-aws-auditor")).json()["spec"]
    assert auditor["cloud_identities"] == ["team-aws-report"]
    schedule = (await admin.get("/v1/namespaces/default/triggers/team-aws-account-report")).json()["spec"]
    assert schedule["event"] == "schedule" and schedule["schedule"] == "0 9 * * 1"

    ev = (await admin.get("/v1/namespaces/default/evals/team-support-triage-quality")).json()["spec"]
    assert ev["workflow"] == "support-triage" and ev["sinks"] == [{"platform": "braintrust"}]
    assert ev["gate"]["fail_under"] == 0.8 and ev["from_runs"]["limit"] == 50


async def test_bad_parameters_are_reported(admin):
    r = await admin.post("/v1/namespaces/default/blueprints/aws-account-report/adopt", json={"params": {"role_arn": "not-an-arn", "slack": "nope"}})
    assert r.status_code == 422, r.text
    assert "slack" in r.json()["details"]["params"]
