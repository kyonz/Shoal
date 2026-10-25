"""The ``shoal`` CLI against an in-process server (with an embedded worker)."""

from __future__ import annotations

import json
import time

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

from shoal.api.app import create_app
from shoal.cli import client as cli_client
from shoal.cli import config
from shoal.cli.main import app
from shoal.config import Settings

ADMIN = "cli-admin-token"
SERVER = "http://shoal.test"

HELLO = """
apiVersion: shoal.dev/v1
kind: Workflow
metadata: {name: hello, description: Says hello, labels: {team: platform}}
spec:
  inputs: {name: {type: string}}
  outputs: {greeting: "{{ steps.greet.output.text }}"}
  steps:
    - {id: greet, type: set, values: {text: "Hello {{ inputs.name }}"}}
"""

BROKEN = """
apiVersion: shoal.dev/v1
kind: Workflow
metadata: {name: broken}
spec:
  steps:
    - {id: boom, type: script, run: "exit 3"}
"""


@pytest.fixture
def server(tmp_path, monkeypatch):
    """An app on its own loop (TestClient's portal), with the CLI's HTTP routed to it."""
    monkeypatch.setenv("SHOAL_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    monkeypatch.setenv("SHOAL_CONFIG", str(tmp_path / "cli.yaml"))
    for var in ("SHOAL_SERVER", "SHOAL_TOKEN", "SHOAL_NAMESPACE", "SHOAL_TENANT", "SHOAL_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/shoal.db", public_url=SERVER,
        admin_token=SecretStr(ADMIN), poll_interval_seconds=0.05,
    )
    with TestClient(create_app(settings, run_worker=True), base_url=SERVER) as http:

        def make_http(base_url, headers, timeout):
            assert base_url == SERVER
            http.headers.clear()
            http.headers.update(headers)
            return _Unclosable(http)

        monkeypatch.setattr(cli_client, "make_http", make_http)
        yield http


class _Unclosable:
    """Share one TestClient across CLI invocations."""

    def __init__(self, inner):
        self._inner = inner

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def cli(server):
    runner = CliRunner()

    def invoke(*args, input=None):
        result = runner.invoke(app, ["--server", SERVER, *args], input=input)
        return result

    return invoke


@pytest.fixture
def tenant_key(cli):
    result = cli("--token", ADMIN, "tenants", "create", "acme", "-o", "json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["api_key"]


def test_login_saves_a_context(cli, tenant_key, tmp_path):
    bad = cli("login", SERVER, "--token", "nope")
    assert bad.exit_code == 1 and "401" in bad.output

    result = cli("login", SERVER, "--token", tenant_key, "--name", "local")
    assert result.exit_code == 0, result.output
    assert "sa:admin" in result.output
    saved = yaml.safe_load((tmp_path / "cli.yaml").read_text())
    assert saved["current-context"] == "local"
    assert saved["contexts"]["local"]["token"] == tenant_key
    assert (tmp_path / "cli.yaml").stat().st_mode & 0o077 == 0
    assert config.resolve().token == tenant_key

    who = cli("whoami", "-o", "json")
    assert who.exit_code == 0 and json.loads(who.stdout)["tenant"]["slug"] == "acme"
    assert cli("logout").exit_code == 0
    assert config.resolve().token is None


def test_apply_get_run_logs(cli, tenant_key, tmp_path):
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "hello.yaml").write_text(HELLO)
    auth = ("--token", tenant_key)

    dry = cli(*auth, "apply", "-f", str(tmp_path / "manifests"), "--dry-run")
    assert dry.exit_code == 0 and "created" in dry.output and "dry run" in dry.output
    applied = cli(*auth, "apply", "-f", str(tmp_path / "manifests"), "-o", "json")
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)[0]["outcome"] == "created"
    again = cli(*auth, "apply", "-f", "-", input=HELLO)
    assert "unchanged" in again.output

    listed = cli(*auth, "get", "workflows")
    assert listed.exit_code == 0 and "hello" in listed.output and "Says hello" in listed.output
    assert cli(*auth, "get", "wf", "-o", "name").stdout.strip() == "workflows/hello"
    exported = yaml.safe_load(cli(*auth, "get", "workflow", "hello", "-o", "yaml").stdout)
    assert exported["kind"] == "Workflow" and exported["metadata"]["labels"] == {"team": "platform"}
    assert cli(*auth, "get", "widgets").exit_code == 2

    ran = cli(*auth, "run", "hello", "-i", "name=Trevor", "--follow")
    assert ran.exit_code == 0, ran.output
    assert "Hello Trevor" in ran.output and "succeeded" in ran.output

    runs = json.loads(cli(*auth, "get", "runs", "-o", "json").stdout)
    run_id = runs["items"][0]["id"]
    detail = cli(*auth, "get", "run", run_id)
    assert detail.exit_code == 0 and "greet" in detail.output
    logs = cli(*auth, "logs", run_id)
    assert "step.succeeded" in logs.output and "run.succeeded" in logs.output
    streamed = cli(*auth, "logs", run_id, "-f", "-o", "json")
    assert streamed.exit_code == 0
    assert json.loads(streamed.stdout.splitlines()[-1])["type"] == "run.succeeded"

    missing = cli(*auth, "run", "hello", "--wait")
    assert missing.exit_code == 1  # a required input is missing: the API refuses the run


def test_failed_run_exits_nonzero(cli, tenant_key):
    auth = ("--token", tenant_key)
    assert cli(*auth, "apply", "-f", "-", input=BROKEN).exit_code == 0
    result = cli(*auth, "run", "broken", "--wait")
    assert result.exit_code == 1
    assert "failed" in result.output


GATED = """
apiVersion: shoal.dev/v1
kind: Workflow
metadata: {name: gated}
spec:
  steps:
    - {id: review, type: approval, message: "Ship it?"}
    - {id: done, type: set, needs: [review], if: "{{ steps.review.output.approved }}", values: {ok: true}}
"""


def _wait_for(cli, auth, run_id, *statuses):
    for _ in range(200):
        run = json.loads(cli(*auth, "get", "run", run_id, "-o", "json").stdout)
        if run["status"] in statuses:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} stayed {run['status']}")


def test_approve_and_reject(cli, tenant_key):
    auth = ("--token", tenant_key)
    assert cli(*auth, "apply", "-f", "-", input=GATED).exit_code == 0

    approved_id = json.loads(cli(*auth, "run", "gated", "-o", "json").stdout)["id"]
    _wait_for(cli, auth, approved_id, "waiting")
    result = cli(*auth, "approve", approved_id, "-m", "lgtm")
    assert result.exit_code == 0, result.output
    assert "step review approved" in result.output
    assert _wait_for(cli, auth, approved_id, "succeeded", "failed")["status"] == "succeeded"
    assert cli(*auth, "approve", approved_id).exit_code == 2  # nothing left to approve

    rejected_id = json.loads(cli(*auth, "run", "gated", "-o", "json").stdout)["id"]
    _wait_for(cli, auth, rejected_id, "waiting")
    result = cli(*auth, "approve", rejected_id, "review", "--reject")
    assert result.exit_code == 0, result.output
    assert "step review rejected" in result.output
    run = _wait_for(cli, auth, rejected_id, "succeeded", "failed")
    steps = {s["step_id"]: s for s in run["steps"]}
    assert steps["review"]["output"]["approved"] is False
    assert steps["done"]["status"] == "skipped"


FLEET = """
apiVersion: shoal.dev/v1
kind: Campaign
metadata: {name: greet-all}
spec:
  workflow: hello
  targets: {items: [{id: ada}, {id: grace}], key: "{{ target.id }}"}
  inputs: {name: "{{ target.id }}"}
---
apiVersion: shoal.dev/v1
kind: Trigger
metadata: {name: by-hand}
spec:
  workflow: hello
  event: schedule
  schedule: "0 0 1 1 *"
  inputs: {name: "{{ event.who }}"}
---
apiVersion: shoal.dev/v1
kind: Blueprint
metadata: {name: greeter}
spec:
  title: Greeter
  summary: Greets
  status: published
  params: {greeting: {default: Hello}}
  resources:
    - kind: Workflow
      metadata: {name: "{{ prefix }}greet"}
      spec: {steps: [{id: g, type: set, values: {text: "{{ params.greeting }}"}}]}
"""


def test_rerun_triggers_campaigns_and_blueprints(cli, tenant_key):
    auth = ("--token", tenant_key)
    assert cli(*auth, "apply", "-f", "-", input=HELLO).exit_code == 0
    applied = cli(*auth, "apply", "-f", "-", input=FLEET)
    assert applied.exit_code == 0, applied.output

    first = cli(*auth, "run", "hello", "-i", "name=Ada", "--wait", "-o", "json")
    run_id = json.loads(first.stdout)["id"]
    again = cli(*auth, "rerun", run_id, "--wait")
    assert again.exit_code == 0, again.output
    assert "rerun of" in again.output and "Hello Ada" in again.output

    fired = cli(*auth, "triggers", "fire", "by-hand", "-d", '{"who": "Grace"}', "-o", "json")
    assert fired.exit_code == 0, fired.output
    fired_run = json.loads(fired.stdout)["run_id"]
    assert _wait_for(cli, auth, fired_run, "succeeded", "failed")["outputs"] == {"greeting": "Hello Grace"}
    assert cli(*auth, "triggers", "fire", "by-hand", "-d", "[1]").exit_code == 2

    launched = cli(*auth, "campaigns", "launch", "greet-all", "--paused", "-o", "json")
    assert launched.exit_code == 0, launched.output
    campaign_id = json.loads(launched.stdout)["id"]
    assert campaign_id in cli(*auth, "campaigns", "list").output
    resumed = cli(*auth, "campaigns", "resume", campaign_id)
    assert resumed.exit_code == 0, resumed.output
    for _ in range(200):
        shown = json.loads(cli(*auth, "campaigns", "get", campaign_id, "-o", "json").stdout)
        if shown["status"] == "completed":
            break
        time.sleep(0.05)
    assert shown["stats"]["succeeded"] == 2
    assert "2/2" in cli(*auth, "campaigns", "get", campaign_id).output

    dry = cli(*auth, "blueprints", "adopt", "greeter", "--prefix", "cli", "--dry-run")
    assert dry.exit_code == 0 and "workflow/cli-greet" in dry.output and "dry run" in dry.output
    adopted = cli(*auth, "blueprints", "adopt", "greeter", "--prefix", "cli", "-p", "greeting=Hi")
    assert adopted.exit_code == 0, adopted.output
    assert "adoption" in adopted.output
    assert "cli-greet" in cli(*auth, "get", "workflows").output


def test_delete_names_the_kind(cli, tenant_key):
    auth = ("--token", tenant_key)
    assert cli(*auth, "apply", "-f", "-", input=HELLO).exit_code == 0
    result = cli(*auth, "delete", "wf", "hello")
    assert result.exit_code == 0 and "workflow/hello deleted" in result.output


def test_apply_reports_invalid_documents(cli, tenant_key):
    bad = HELLO.replace("type: set", "type: nope")
    result = cli("--token", tenant_key, "apply", "-f", "-", input=bad)
    assert result.exit_code == 1
    assert "workflow/hello" in result.output.lower()


def test_secrets(cli, tenant_key, tmp_path, monkeypatch):
    auth = ("--token", tenant_key)
    assert cli(*auth, "secrets", "set", "gh-token", "--value", "a", "--from-env", "X").exit_code == 2
    assert cli(*auth, "secrets", "set", "gh-token", input="s3cret\n").exit_code == 0
    monkeypatch.setenv("JIRA", "j")
    assert cli(*auth, "secrets", "set", "jira", "--from-env", "JIRA").exit_code == 0
    (tmp_path / "key").write_text("k\n")
    assert cli(*auth, "secrets", "set", "key", "--from-file", str(tmp_path / "key")).exit_code == 0

    names = cli(*auth, "secrets", "list", "-o", "name").stdout.split()
    assert sorted(names) == ["gh-token", "jira", "key"]
    shown = cli(*auth, "secrets", "get", "gh-token", "-o", "json")
    assert "s3cret" not in shown.stdout and json.loads(shown.stdout)["name"] == "gh-token"
    assert cli(*auth, "secrets", "delete", "jira").exit_code == 0
    assert cli(*auth, "get", "secrets", "-o", "name").stdout.split() == ["gh-token", "key"]


def test_admin_token_with_tenant_header(cli, tenant_key):
    result = cli("--token", ADMIN, "--tenant", "acme", "get", "namespaces", "-o", "name")
    assert result.exit_code == 0, result.output
    assert "default" in result.stdout.split()
    no_tenant = cli("--token", ADMIN, "get", "workflows")
    assert no_tenant.exit_code == 1 and "tenant" in no_tenant.output


def test_input_parsing():
    from shoal.cli.main import _parse_inputs

    assert _parse_inputs(["n=3", "flag=true", "s=hello", "obj={\"a\": 1}", "eq=a=b"], None) == {
        "n": 3, "flag": True, "s": "hello", "obj": {"a": 1}, "eq": "a=b",
    }


TRIAGE = """
apiVersion: shoal.dev/v1
kind: Workflow
metadata: {name: triage}
spec:
  inputs: {text: {type: string}}
  outputs: {label: "{{ steps.classify.output.label }}"}
  steps:
    - {id: classify, type: set, values: {label: "{{ 'bug' if 'crash' in inputs.text else 'feature' }}"}}
---
apiVersion: shoal.dev/v1
kind: Eval
metadata: {name: triage-eval}
spec:
  workflow: triage
  cases:
    - {name: crash, inputs: {text: app crash}, expect: {label: bug}}
    - {name: error, inputs: {text: error 500}, expect: {label: bug}}
  graders:
    - {name: label, check: "outputs.label == expect.label"}
"""


def test_eval_run_gates(cli, tenant_key):
    auth = ("--token", tenant_key)
    assert cli(*auth, "apply", "-f", "-", input=TRIAGE).exit_code == 0

    passing = cli(*auth, "eval", "run", "triage-eval", "--wait", "--poll", "0.05", "--fail-under", "0.4")
    assert passing.exit_code == 0, passing.output
    assert "gate passed" in passing.output and "50%" in passing.output

    failing = cli(*auth, "eval", "run", "triage-eval", "--wait", "--poll", "0.05", "--fail-under", "0.9", "-o", "json")
    assert failing.exit_code == 1
    row = json.loads(failing.stdout)
    assert row["gate"]["passed"] is False and row["gate"]["reasons"]
    shown = cli(*auth, "eval", "get", row["id"])
    assert shown.exit_code == 0 and "gate failed" in shown.output

    started = cli(*auth, "eval", "run", "triage-eval", "-o", "name")
    assert started.exit_code == 0 and started.stdout.strip()

    listed = cli(*auth, "eval", "list", "triage-eval")
    assert listed.exit_code == 0 and "triage-eval" in listed.output and "failed" in listed.output, listed.output
    names = cli(*auth, "eval", "list", "-o", "name").stdout.split()
    assert row["id"] in names and len(names) == 3
    compared = cli(*auth, "eval", "compare", row["id"], names[-1])
    assert compared.exit_code == 0 and "over 2 shared cases" in compared.output, compared.output
    scores = cli(*auth, "eval", "scores", "triage-eval")
    assert scores.exit_code == 0 and "v1" in scores.output and "0.500" in scores.output
    cancelled = cli(*auth, "eval", "cancel", row["id"])
    assert cancelled.exit_code == 1 and "completed" in cancelled.output  # finished runs cannot be cancelled


def test_integrations_new(tmp_path):
    runner = CliRunner()
    dest = tmp_path / "svc"
    result = runner.invoke(app, ["integrations", "new", "service-now", "--dest", str(dest), "--title", "Service Now"])
    assert result.exit_code == 0, result.output
    assert any(dest.rglob("*.py"))
    again = runner.invoke(app, ["integrations", "new", "service-now", "--dest", str(dest)])
    assert again.exit_code == 1 and "not empty" in again.output
    bad = runner.invoke(app, ["integrations", "new", "9lives", "--dest", str(tmp_path / "x")])
    assert bad.exit_code == 1


def test_skills_import_and_upgrade(cli, tenant_key, server, tmp_path):
    import subprocess

    repo = tmp_path / "skills"
    (repo / "pdf").mkdir(parents=True)
    (repo / "pdf" / "SKILL.md").write_text("---\nname: pdf\ndescription: PDFs.\nversion: 1.0.0\n---\nv1\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "v1"], check=True, env=env)
    engine = server.app.state.engine
    engine.settings.skill_source_allow_local = True
    engine.settings.skill_source_cache_dir = str(tmp_path / "cache")

    auth = ("--token", tenant_key)
    (tmp_path / "source.yaml").write_text(
        f"apiVersion: shoal.dev/v1\nkind: SkillSource\nmetadata: {{name: acme}}\n"
        f"spec: {{source: git, config: {{url: '{repo}', refresh_seconds: 0}}}}\n"
    )
    assert cli(*auth, "apply", "-f", str(tmp_path / "source.yaml")).exit_code == 0
    assert cli(*auth, "get", "skill-sources", "-o", "name").stdout.split() == ["skill-sources/acme"]
    browsed = cli(*auth, "skills", "browse", "acme", "-o", "name")
    assert browsed.exit_code == 0, browsed.output
    assert browsed.stdout.split() == ["pdf"]
    imported = cli(*auth, "skills", "import", "acme", "pdf")
    assert imported.exit_code == 0, imported.output
    assert "skill/pdf is created" in imported.stdout

    (repo / "pdf" / "SKILL.md").write_text("---\nname: pdf\ndescription: PDFs.\nversion: 1.1.0\n---\nv2\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "v2"], check=True, env=env)
    checked = json.loads(cli(*auth, "skills", "imports", "--check", "-o", "json").stdout)
    assert checked[0]["update_available"] is True
    upgraded = cli(*auth, "skills", "upgrade", "pdf", input="y\n")
    assert upgraded.exit_code == 0, upgraded.output
    assert "(was 1.0.0)" in upgraded.stdout and "/spec/instructions" in upgraded.stdout
    rolled = cli(*auth, "skills", "rollback", "pdf", "1")
    assert rolled.exit_code == 0 and "(1.0.0)" in rolled.stdout
