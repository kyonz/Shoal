"""The two acceptance journeys, end to end, against fake GitHub and Jira APIs.

J1: a campaign uplifts every admin repo in an org, waits (max 7 days) for review,
    merges what was approved, and nudges what was not.
J2: a Jira webhook (issue moved to "Ready for AI") triggers a bugfix workflow
    that opens a PR and writes back to the ticket.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from awm.config import Settings
from awm.core import clock
from awm.core.loader import load_path
from awm.core.models import Agent
from awm.engine import Engine

from .fakes import FakeGitHub, FakeJira, Router, comment_text

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
GH_WEBHOOK_SECRET = "gh-hook-secret"
JIRA_WEBHOOK_SECRET = "jira-hook-secret"


@pytest.fixture
async def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    github, jira = FakeGitHub(), FakeJira()
    engine = Engine(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/awm.db", public_url="https://awm.example"),
        http_transport=httpx.MockTransport(Router(github, jira)),
    )
    await engine.start()
    tenant = await engine.store.create_tenant("acme")
    secrets = engine.secrets.writable()
    for name, value in {
        "github-token": "ghp_faketoken123",
        "github-webhook-secret": GH_WEBHOOK_SECRET,
        "jira-api-token": "jira-token-xyz",
        "jira-webhook-secret": JIRA_WEBHOOK_SECRET,
        "anthropic-api-key": "sk-ant-unused",
    }.items():
        await secrets.put(tenant.id, name, value)
    yield engine, tenant, github, jira
    await engine.close()
    clock.reset()


async def apply_examples(engine: Engine, tenant_id: str, *dirs: str, agent_outputs: dict[str, dict]) -> None:
    for resource in [r for d in ("shared", *dirs) for r in load_path(EXAMPLES / d)]:
        if isinstance(resource, Agent):
            # Swap the real model for a deterministic script that "does the work".
            resource.spec.provider = "scripted"
            resource.spec.provider_config = {"final_outputs": agent_outputs[resource.metadata.name]}
        await engine.store.apply_resource(tenant_id, resource)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_examples_are_valid():
    resources = load_path(EXAMPLES)
    kinds = sorted({r.to_document()["kind"] for r in resources})
    assert kinds == ["Agent", "Campaign", "Connection", "SandboxProfile", "Skill", "Trigger", "Workflow"]


async def test_journey_github_org_uplift_campaign(world):
    engine, tenant, github, _ = world
    github.add_repo("acme/api")
    github.add_repo("acme/web")
    github.add_repo("acme/legacy", archived=True)
    github.add_repo("acme/docs", admin=False)
    await apply_examples(
        engine,
        tenant.id,
        "github-uplift",
        agent_outputs={
            "uplift": {
                "changed": True,
                "branch": "awm/uplift-coverage",
                "metric_before": 71.5,
                "metric_after": 100,
                "pr_body": "Adds tests to reach 100% coverage.",
                "summary": "Added 42 tests.",
            }
        },
    )

    campaign = await engine.campaigns.launch(tenant.id, "coverage-uplift", started_by="user:trevor")
    targets = {t.key: t for t in await engine.store.list_targets(tenant.id, campaign.id)}
    assert sorted(targets) == ["acme/api", "acme/web"]  # archived + read-only repos excluded

    await engine.run_until_idle()
    assert sorted(github.pulls) == ["acme/api", "acme/web"]
    for target in await engine.store.list_targets(tenant.id, campaign.id):
        run = await engine.store.get_run(tenant.id, target.run_id)
        assert run.status == "waiting", run.error
        assert (await engine.store.get_step(run.id, "await_review")).status == "waiting"

    # A reviewer approves acme/api; GitHub sends a signed webhook which wakes that run early.
    github.approve("acme/api", 1)
    url = await engine.integrations.webhook_url(tenant.id, "github-acme")
    body = json.dumps(
        {"action": "submitted", "review": {"state": "approved"}, "pull_request": {"number": 1}, "repository": {"full_name": "acme/api"}, "sender": {"login": "reviewer"}}
    ).encode()
    result = await engine.integrations.handle_webhook(
        url.rsplit("/", 1)[-1],
        {"X-GitHub-Event": "pull_request_review", "X-Hub-Signature-256": sign(GH_WEBHOOK_SECRET, body), "X-GitHub-Delivery": "d1"},
        body,
    )
    assert len(result["woken_runs"]) == 1
    await engine.run_until_idle()
    assert github.merges == [("acme/api", 1, "squash")]

    # acme/web never gets a review: after 7 days the wait times out and the PR is nudged, not merged.
    clock.advance(timedelta(days=7, minutes=1))
    await engine.run_until_idle()
    assert github.merges == [("acme/api", 1, "squash")]
    assert any(c["repo"] == "acme/web" and "No review after 7 days" in c["body"] for c in github.comments)

    campaign = await engine.store.get_campaign(tenant.id, campaign.id)
    assert campaign.status == "completed"
    assert campaign.stats["succeeded"] == 2
    outputs = {t.key: t.result["outputs"] for t in await engine.store.list_targets(tenant.id, campaign.id)}
    assert outputs["acme/api"]["merged"] is True
    assert outputs["acme/web"]["merged"] is False


async def test_bad_webhook_signature_is_rejected(world):
    engine, tenant, _, _ = world
    await apply_examples(engine, tenant.id, "github-uplift", agent_outputs={"uplift": {"changed": False}})
    url = await engine.integrations.webhook_url(tenant.id, "github-acme")
    with pytest.raises(Exception, match="signature"):
        await engine.integrations.handle_webhook(
            url.rsplit("/", 1)[-1], {"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=bad"}, b"{}"
        )


def jira_issue_updated(key: str, to: str, ts: int) -> bytes:
    return json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "timestamp": ts,
            "issue": {"key": key, "fields": {"summary": "Crash on login", "status": {"name": to}, "customfield_10050": "acme/api"}},
            "changelog": {"items": [{"field": "status", "fromString": "To Do", "toString": to}]},
            "user": {"displayName": "Dana"},
        }
    ).encode()


async def test_journey_jira_webhook_bugfix_writeback(world):
    engine, tenant, github, jira = world
    github.add_repo("acme/api")
    jira.add_issue("BUG-7", "Crash on login", "Login crashes when the email has a plus sign.")
    await apply_examples(
        engine,
        tenant.id,
        "jira-bugfix",
        agent_outputs={
            "bugfixer": {
                "tests_passed": True,
                "branch": "fix/bug-7",
                "root_cause": "Email normalisation stripped '+' before lookup.",
                "test_summary": "212 passed, 0 failed",
                "summary": "Fixed normalisation and added a regression test.",
            }
        },
    )
    webhook_id = (await engine.integrations.webhook_url(tenant.id, "jira-acme")).rsplit("/", 1)[-1]
    body = jira_issue_updated("BUG-7", "Ready for AI", 1700000000)
    result = await engine.integrations.handle_webhook(webhook_id, {"X-Hub-Signature": sign(JIRA_WEBHOOK_SECRET, body)}, body)
    assert result["triggers"][0]["status"] == "fired"
    run_id = result["triggers"][0]["run_id"]

    # The same delivery again is de-duplicated.
    again = await engine.integrations.handle_webhook(webhook_id, {"X-Hub-Signature": sign(JIRA_WEBHOOK_SECRET, body)}, body)
    assert again["triggers"][0]["status"] == "duplicate"

    # A move to some other column does not fire.
    other = jira_issue_updated("BUG-7", "Done", 1700000001)
    assert (await engine.integrations.handle_webhook(webhook_id, {"X-AWM-Token": JIRA_WEBHOOK_SECRET}, other))["triggers"] == []

    await engine.run_until_idle()
    run = await engine.store.get_run(tenant.id, run_id)
    assert run.status == "succeeded", run.error
    assert run.outputs == {"pr_url": "https://github.com/acme/api/pull/1", "tests_passed": True}
    assert github.pulls["acme/api"][1]["title"] == "BUG-7: Crash on login"
    [(key, comment)] = jira.comments
    text = comment_text(comment)
    assert key == "BUG-7" and "https://github.com/acme/api/pull/1" in text and "212 passed" in text
    assert jira.issues["BUG-7"]["fields"]["status"]["name"] == "In Review"


async def test_journey_jira_bugfix_reports_failure(world):
    engine, tenant, github, jira = world
    github.add_repo("acme/api")
    jira.add_issue("BUG-8", "Flaky export", "Export sometimes times out.")
    await apply_examples(
        engine,
        tenant.id,
        "jira-bugfix",
        agent_outputs={
            "bugfixer": {"tests_passed": False, "branch": "fix/bug-8", "root_cause": "unknown", "test_summary": "3 failed", "summary": "Could not reproduce reliably."}
        },
    )
    fired = await engine.triggers.fire_manual(
        tenant.id,
        "jira-ready-for-ai",
        json.loads(jira_issue_updated("BUG-8", "Ready for AI", 1700000100)) | {"issue_key": "BUG-8", "status_change": {"from": "To Do", "to": "Ready for AI"}},
    )
    await engine.run_until_idle()
    run = await engine.store.get_run(tenant.id, fired["run_id"])
    assert run.status == "succeeded"
    assert github.pulls == {}
    [(_, comment)] = jira.comments
    assert "could not produce a passing fix" in comment_text(comment)
    assert "Could not reproduce reliably." in comment_text(comment)
