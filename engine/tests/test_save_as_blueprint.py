"""Saving a workflow as a blueprint: packaging, parameter detection, secret stripping, publishing and adoption."""

from __future__ import annotations

import json

from tests.test_api import _key_for, admin, api  # noqa: F401  (fixtures)

GH_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
TEXT_TOKEN = "sk-" + "ant-api03-abcdefghijklmnopqrstuvwxyz"

AGENT = {"spec": {"model": "claude-opus-5-5", "instructions": f"Fix bugs. Debug key: {TEXT_TOKEN}",
                  "skills": ["triage"], "env": {"API_TOKEN": GH_TOKEN, "MODE": "fix"}}}
SKILL = {"spec": {"description": "Triage a ticket", "instructions": "Read the ticket for acme/api first."}}
WORKFLOW = {
    "metadata": {"description": "Fix Jira bugs in acme/api"},
    "spec": {
        "inputs": {"issue_key": {"type": "string"}},
        "connections": {"jira": "jira-acme", "github": "github-acme"},
        "defaults": {"agent": "bugfixer", "sandbox": "big"},
        "steps": [
            {"id": "search", "type": "action", "action": "jira.search_issues",
             "with": {"jql": "project = ABC AND status = 'Ready for AI'"}},
            {"id": "fix", "type": "agent", "prompt": "Fix {{ inputs.issue_key }} in https://github.com/acme/api"},
            {"id": "notify", "type": "script", "run": "echo done", "env": {"HOOK": "{{ secrets.slack_webhook }}"}},
            {"id": "pr", "type": "action", "action": "github.create_pull_request",
             "with": {"repo": "acme/api", "head": "fix", "title": "Fix {{ inputs.issue_key }}"}},
        ],
    },
}
TRIGGER = {"spec": {"workflow": "jira-bugfix", "workflow_version": 1, "connection": "jira-acme", "event": "issue_updated",
                    "inputs": {"issue_key": "{{ event.issue.key }}"}}}
SAVE = "/v1/namespaces/team-a/workflows/jira-bugfix/save-as-blueprint"


async def _setup(admin):  # noqa: F811
    for ns in ("team-a", "team-b"):
        assert (await admin.put(f"/v1/namespaces/{ns}", json={})).status_code == 201
    for ns, name, integration in (("team-a", "jira-acme", "jira"), ("team-a", "github-acme", "github"),
                                  ("team-b", "jira-b", "jira"), ("team-b", "gh-b", "github")):
        config = {"base_url": "https://x.atlassian.net", "email": "bot@x.example"} if integration == "jira" else {}
        r = await admin.put(f"/v1/namespaces/{ns}/connections/{name}", json={"spec": {"integration": integration, "config": config}})
        assert r.status_code == 201, r.text
    for name, body in (("skills/triage", SKILL), ("agents/bugfixer", AGENT), ("workflows/jira-bugfix", WORKFLOW),
                       ("triggers/on-ready", TRIGGER),
                       ("workflows/other", {"spec": {"steps": [{"id": "x", "type": "set", "values": {}}]}})):
        r = await admin.put(f"/v1/namespaces/team-a/{name}", json=body)
        assert r.status_code == 201, r.text


async def test_save_workflow_as_blueprint_and_adopt_it(admin, api):  # noqa: F811
    await _setup(admin)

    dry = await admin.post(SAVE, json={"dry_run": True})
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert {(r["kind"], r["name"], r["included"]) for r in body["resources"]} == {
        ("Workflow", "jira-bugfix", True), ("Agent", "bugfixer", True), ("Skill", "triage", True),
        ("Trigger", "on-ready", True),
    }
    findings = {f["id"]: f for f in body["findings"]}
    assert findings["connection:jira-acme"]["param"] == "jira" and findings["connection:jira-acme"]["integration"] == "jira"
    assert len(findings["connection:jira-acme"]["locations"]) == 2  # the workflow and the trigger
    assert findings["connection:github-acme"]["type"] == "connection"
    assert findings["repository:acme/api"]["default"] == "acme/api"
    assert findings["jira_project:ABC"]["param"] == "jira_project"
    assert findings["secret:slack_webhook"]["type"] == "secret"
    assert findings["model:claude-opus-5-5"]["default"] == "claude-opus-5-5"
    assert findings["sandbox:big"]["default"] == "big"
    credential = next(f for f in body["findings"] if f["category"] == "credential")
    assert credential["locked"] and credential["value"] == "" and credential["param"] == "api_token"
    assert body["stripped"] == ["Agent bugfixer · instructions"]
    assert any("no longer pins a workflow version" in w for w in body["warnings"])
    published = json.dumps(body["blueprint"])
    assert GH_TOKEN not in published and TEXT_TOKEN not in published
    assert (await admin.get("/v1/blueprints/jira-bugfix")).status_code == 404

    # a viewer can preview but not publish
    viewer = await _key_for(admin, "bp-viewer", "viewer", "team-a")
    as_viewer = {"Authorization": f"Bearer {viewer}"}
    assert (await api.post(SAVE, json={"dry_run": True}, headers=as_viewer)).status_code == 200
    assert (await api.post(SAVE, json={}, headers=as_viewer)).status_code == 403

    # credentials cannot be kept; names must be valid and unique
    r = await admin.post(SAVE, json={"dry_run": True, "params": {credential["id"]: {"enabled": False}}})
    assert r.json()["findings"][0]["enabled"] is True
    r = await admin.post(SAVE, json={"dry_run": True, "params": {"connection:github-acme": {"name": "jira"}}})
    assert r.status_code == 422

    choices = {"connection:github-acme": {"name": "gh", "description": "Where the PR goes"},
               "model:claude-opus-5-5": {"enabled": False}}
    r = await admin.post(SAVE, json={"title": "Jira bugfix", "summary": "Fix tickets", "tags": ["jira"], "params": choices,
                                     "include": [{"kind": "Agent", "name": "bugfixer"}, {"kind": "Skill", "name": "triage"},
                                                 {"kind": "Trigger", "name": "on-ready"}]})
    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "created"
    bp = (await admin.get("/v1/blueprints/jira-bugfix")).json()
    spec = bp["spec"]
    assert spec["status"] == "draft" and spec["version"] == "0.1.0" and spec["tags"] == ["jira"]
    assert bp["metadata"]["labels"] == {"shoal.dev/from-workflow": "jira-bugfix", "shoal.dev/from-namespace": "team-a"}
    assert set(spec["params"]) == {"api_token", "jira", "gh", "slack_webhook", "repo", "jira_project", "sandbox"}
    assert spec["params"]["gh"] == {"type": "connection", "integration": "github", "description": "Where the PR goes",
                                    "required": True}
    assert {r["metadata"]["name"] for r in spec["resources"]} == {
        "{{ prefix }}jira-bugfix", "{{ prefix }}bugfixer", "{{ prefix }}triage", "{{ prefix }}on-ready"}
    wf = next(r for r in spec["resources"] if r["kind"] == "Workflow")["spec"]
    assert wf["connections"] == {"jira": "{{ params.jira }}", "github": "{{ params.gh }}"}
    assert wf["defaults"] == {"agent": "{{ prefix }}bugfixer", "sandbox": "{{ params.sandbox }}"}
    assert wf["steps"][0]["with"]["jql"] == "project = {{ params.jira_project }} AND status = 'Ready for AI'"
    assert wf["steps"][1]["prompt"] == "Fix {{ inputs.issue_key }} in https://github.com/{{ params.repo }}"
    assert wf["steps"][2]["env"]["HOOK"] == "{{ secrets[params.slack_webhook] }}"
    assert wf["steps"][3]["with"]["repo"] == "{{ params.repo }}"
    agent = next(r for r in spec["resources"] if r["kind"] == "Agent")["spec"]
    assert agent["model"] == "claude-opus-5-5"  # left as is
    assert agent["env"] == {"API_TOKEN": "{{ secrets[params.api_token] }}", "MODE": "fix"}
    assert agent["skills"] == ["{{ prefix }}triage"] and "[redacted]" in agent["instructions"]
    trigger = next(r for r in spec["resources"] if r["kind"] == "Trigger")["spec"]
    assert trigger["workflow"] == "{{ prefix }}jira-bugfix" and "workflow_version" not in trigger
    assert any(e["action"] == "blueprint.saved_from_workflow" for e in (await admin.get("/v1/audit")).json()["items"])

    # another team adopts it with its own systems
    for name in ("slack-b", "api-token-b"):
        assert (await admin.put(f"/v1/namespaces/team-b/secrets/{name}", json={"value": "v"})).status_code == 201
    params = {"jira": "jira-b", "gh": "gh-b", "slack_webhook": "slack-b", "api_token": "api-token-b",
              "repo": "beta/web", "jira_project": "BETA"}
    r = await admin.post("/v1/namespaces/team-b/blueprints/jira-bugfix/adopt", json={"params": params, "prefix": "b"})
    assert r.status_code == 201, r.text
    adopted = (await admin.get("/v1/namespaces/team-b/workflows/b-jira-bugfix")).json()["spec"]
    assert adopted["connections"] == {"jira": "jira-b", "github": "gh-b"}
    assert adopted["defaults"] == {"agent": "b-bugfixer", "sandbox": "big"}
    assert adopted["steps"][0]["with"]["jql"] == "project = BETA AND status = 'Ready for AI'"
    assert adopted["steps"][2]["env"]["HOOK"] == '{{ secrets["slack-b"] }}'
    assert adopted["steps"][3]["with"]["repo"] == "beta/web"
    agent = (await admin.get("/v1/namespaces/team-b/agents/b-bugfixer")).json()["spec"]
    assert agent["env"]["API_TOKEN"] == '{{ secrets["api-token-b"] }}' and agent["skills"] == ["b-triage"]
    trigger = (await admin.get("/v1/namespaces/team-b/triggers/b-on-ready")).json()["spec"]
    assert trigger["workflow"] == "b-jira-bugfix" and trigger["connection"] == "jira-b"

    # saving again: a new blueprint clashes, a new version must be newer
    assert (await admin.post(SAVE, json={})).status_code == 409
    old = await admin.post(SAVE, json={"target": "existing", "version": "0.1.0"})
    assert old.status_code == 422
    r = await admin.post(SAVE, json={"target": "existing", "status": "published", "params": choices})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "updated" and r.json()["previous_version"] == "0.1.0"
    spec = (await admin.get("/v1/blueprints/jira-bugfix")).json()["spec"]
    assert spec["version"] == "0.2.0" and spec["status"] == "published" and spec["title"] == "Jira bugfix"
    assert (await admin.post("/v1/namespaces/team-a/workflows/other/save-as-blueprint",
                             json={"blueprint": "jira-bugfix", "target": "existing", "version": "1.0.0"})).status_code == 200
    assert (await admin.post("/v1/namespaces/team-a/workflows/nope/save-as-blueprint", json={"dry_run": True})).status_code == 404


async def test_excluded_resources_stay_references(admin):  # noqa: F811
    await _setup(admin)
    r = await admin.post(SAVE, json={"dry_run": True, "include": [], "params": {"connection:jira-acme": {"enabled": False}}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["metadata"]["name"] for x in body["blueprint"]["spec"]["resources"]] == ["{{ prefix }}jira-bugfix"]
    wf = body["blueprint"]["spec"]["resources"][0]["spec"]
    assert wf["defaults"]["agent"] == "bugfixer" and wf["connections"]["jira"] == "jira-acme"
    assert any("Agent 'bugfixer'" in w and "not included" in w for w in body["warnings"])
    assert any("connection 'jira-acme' stays hard-coded" in w for w in body["warnings"])
    bad = await admin.post(SAVE, json={"dry_run": True, "include": [{"kind": "Workflow", "name": "other"}]})
    assert bad.status_code == 422
