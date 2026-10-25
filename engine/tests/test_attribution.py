"""Initiator attribution and GitHub governance (architecture §11.1)."""

from __future__ import annotations

import httpx
import pytest

from awm.config import Settings
from awm.core import clock
from awm.core.models import parse_resource
from awm.engine import Engine
from awm.integrations.base import IntegrationEvent

from .fakes import FakeGitHub, FakeJira, Router

REPO = "acme/api"


def res(kind, name, spec):
    return parse_resource({"apiVersion": "awm.dev/v1", "kind": kind, "metadata": {"name": name}, "spec": spec})


@pytest.fixture
async def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    github = FakeGitHub()
    github.add_repo(REPO)
    engine = Engine(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/awm.db"),
        http_transport=httpx.MockTransport(Router(github, FakeJira())),
    )
    await engine.start()
    tenant = await engine.store.create_tenant("acme")
    secrets = engine.secrets.writable()
    await secrets.put(tenant.id, "app-token", "ghs_app")
    await secrets.put(tenant.id, "dana-gh", "gho_dana")
    await engine.store.apply_resource(
        tenant.id,
        res("Identity", "dana", {"display_name": "Dana", "email": "dana@acme.test", "github": "dana", "jira_account_id": "jira-123", "actors": ["user:dana@acme.test"], "github_token_secret": "dana-gh"}),
    )
    await engine.store.apply_resource(tenant.id, res("Identity", "sam", {"display_name": "Sam", "github": "sam"}))
    yield engine, tenant, github
    await engine.close()
    clock.reset()


async def connect(engine, tenant, **attribution):
    await engine.store.apply_resource(
        tenant.id,
        res("Connection", "gh", {"integration": "github", "config": {"attribution": attribution}, "secrets": {"token": "app-token"}}),
    )


async def initiator(engine, tenant, **hints):
    return await engine.identities.resolve(tenant.id, actor=hints.pop("actor", None), source="test", **hints)


async def call(engine, tenant, action, params, who):
    return await engine.integrations.call(tenant.id, f"github.{action}", params, connection="gh", initiator=who)


async def test_identity_resolution(world):
    engine, tenant, _ = world
    by_actor = await initiator(engine, tenant, actor="user:dana@acme.test")
    by_jira = await initiator(engine, tenant, jira_account_id="jira-123")
    assert by_actor["identity"] == by_jira["identity"] == "dana" and by_jira["github"] == "dana"
    stranger = await initiator(engine, tenant, actor="user:x@acme.test")
    assert stranger["identity"] is None and stranger["email"] == "x@acme.test"


async def test_app_mode_provenance_and_ownership(world):
    engine, tenant, github = world
    github.collaborators[(REPO, "dana")] = "read"
    await connect(engine, tenant, assign_initiator=True, request_initiator_review=True)
    dana = await initiator(engine, tenant, actor="user:dana@acme.test")
    pr = await call(engine, tenant, "create_pull_request", {"repo": REPO, "title": "Fix", "head": "fix", "body": "Body", "labels": ["bug"]}, dana)
    assert pr["provenance"] == {"mode": "app", "initiator": "dana", "initiator_permission": "read", "initiator_can_merge": False, "labels": ["awm:automated", "awm:initiator-cannot-merge"]}
    assert github.labels[(REPO, 1)] == ["bug", "awm:automated", "awm:initiator-cannot-merge"]
    assert github.pulls[REPO][1]["body"].endswith("Requested-by: @dana")
    assert github.assignees[(REPO, 1)] == ["dana"] and github.review_requests[(REPO, 1)] == ["dana"]
    audit = await engine.store.list_audit(tenant.id)
    assert audit[0].action == "github.pull_request.created" and audit[0].actor == "user:dana@acme.test"


async def test_qualified_approval_gates_merge(world):
    engine, tenant, github = world
    github.collaborators[(REPO, "dana")] = "read"
    github.collaborators[(REPO, "intern")] = "triage"
    await connect(engine, tenant)
    dana = await initiator(engine, tenant, actor="user:dana@acme.test")
    await call(engine, tenant, "create_pull_request", {"repo": REPO, "title": "Fix", "head": "fix"}, dana)

    github.approve(REPO, 1, user="intern")
    result = await call(engine, tenant, "merge_pull_request", {"repo": REPO, "number": 1}, dana)
    assert not result["merged"] and "approval from someone with write access" in result["reason"]
    assert result["governance"]["elevation"] == "awaiting_qualified_approval"

    github.approve(REPO, 1, user="reviewer")  # has write access
    result = await call(engine, tenant, "merge_pull_request", {"repo": REPO, "number": 1}, dana)
    assert result["merged"] and result["governance"]["qualified_approvers"] == ["reviewer"]
    assert "awm:merged-via-qualified-approval" in github.labels[(REPO, 1)]


async def test_deny_elevation_and_owner_merges(world):
    engine, tenant, github = world
    github.collaborators[(REPO, "sam")] = "write"
    await connect(engine, tenant, elevation="deny")
    dana = await initiator(engine, tenant, github="dana")  # no access at all
    await call(engine, tenant, "create_pull_request", {"repo": REPO, "title": "A", "head": "a"}, dana)
    github.approve(REPO, 1)
    result = await call(engine, tenant, "merge_pull_request", {"repo": REPO, "number": 1}, dana)
    assert not result["merged"] and "forbids merging" in result["reason"]

    sam = await initiator(engine, tenant, github="sam")
    await call(engine, tenant, "create_pull_request", {"repo": REPO, "title": "B", "head": "b"}, sam)
    github.approve(REPO, 2)
    result = await call(engine, tenant, "merge_pull_request", {"repo": REPO, "number": 2}, sam)
    assert result["merged"] and result["governance"]["elevation"] == "not_needed"


async def test_agent_assisted_merge_check_and_required_labels(world):
    engine, tenant, github = world
    github.collaborators[(REPO, "sam")] = "admin"
    await connect(engine, tenant, require_merge_check=True, required_labels=["qa-ok"])
    sam = await initiator(engine, tenant, github="sam")
    await call(engine, tenant, "create_pull_request", {"repo": REPO, "title": "A", "head": "a"}, sam)
    github.approve(REPO, 1)
    check = await call(engine, tenant, "can_merge", {"repo": REPO, "number": 1}, sam)
    assert check["reasons"] == ["merge check (awm/merge-check) is missing", "missing required labels: qa-ok"]
    await call(engine, tenant, "set_merge_check", {"repo": REPO, "number": 1, "verdict": "pass", "summary": "Diff matches policy"}, sam)
    github.labels[(REPO, 1)].append("qa-ok")
    result = await call(engine, tenant, "merge_pull_request", {"repo": REPO, "number": 1}, sam)
    assert result["merged"] and result["governance"]["merge_check"] == "success"


async def test_user_mode_uses_initiator_token(world):
    engine, tenant, github = world
    seen: list[str] = []
    original = github.handle

    def spy(request):
        seen.append(request.headers["authorization"])
        return original(request)

    github.handle = spy  # type: ignore[method-assign]
    await connect(engine, tenant, mode="user")
    dana = await initiator(engine, tenant, github="dana")
    pr = await call(engine, tenant, "create_pull_request", {"repo": REPO, "title": "A", "head": "a"}, dana)
    assert pr["provenance"]["mode"] == "user" and set(seen) == {"Bearer gho_dana"}

    sam = await initiator(engine, tenant, github="sam")  # no linked token
    with pytest.raises(Exception, match="has not linked a GitHub account"):
        await call(engine, tenant, "create_pull_request", {"repo": REPO, "title": "B", "head": "b"}, sam)


async def test_webhook_actor_becomes_run_initiator(world):
    engine, tenant, _ = world
    await engine.store.apply_resource(tenant.id, res("Workflow", "noop", {"steps": [{"id": "who", "type": "set", "values": {"login": "${{ run.initiator.github }}"}}]}))
    await engine.store.apply_resource(tenant.id, res("Workflow", "outer", {"steps": [{"id": "child", "type": "workflow", "workflow": "noop"}]}))
    await engine.store.apply_resource(tenant.id, res("Trigger", "on-jira", {"workflow": "outer", "connection": "jira", "event": "issue_updated"}))
    event = IntegrationEvent(type="issue_updated", id="e1", data={}, actor={"jira_account_id": "jira-123", "display_name": "Dana D"})
    result = await engine.integrations.inject_event(tenant.id, "jira", event)
    run_id = result["triggers"][0]["run_id"]
    await engine.run_until_idle()
    run = await engine.store.get_run(tenant.id, run_id)
    assert run.initiator["identity"] == "dana" and run.initiator["source"] == "trigger"
    child_id = (await engine.store.get_step(run_id, "child")).output["run_id"]
    assert (await engine.store.get_step(child_id, "who")).output == {"login": "dana"}  # inherited
