"""The ``awm`` CLI against an in-process server (with an embedded worker)."""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

from awm.api.app import create_app
from awm.cli import client as cli_client
from awm.cli import config
from awm.cli.main import app
from awm.config import Settings

ADMIN = "cli-admin-token"
SERVER = "http://awm.test"

HELLO = """
apiVersion: awm.dev/v1
kind: Workflow
metadata: {name: hello, description: Says hello, labels: {team: platform}}
spec:
  inputs: {name: {type: string}}
  outputs: {greeting: "{{ steps.greet.output.text }}"}
  steps:
    - {id: greet, type: set, values: {text: "Hello {{ inputs.name }}"}}
"""

BROKEN = """
apiVersion: awm.dev/v1
kind: Workflow
metadata: {name: broken}
spec:
  steps:
    - {id: boom, type: script, run: "exit 3"}
"""


@pytest.fixture
def server(tmp_path, monkeypatch):
    """An app on its own loop (TestClient's portal), with the CLI's HTTP routed to it."""
    monkeypatch.setenv("AWM_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    monkeypatch.setenv("AWM_CONFIG", str(tmp_path / "cli.yaml"))
    for var in ("AWM_SERVER", "AWM_TOKEN", "AWM_NAMESPACE", "AWM_TENANT", "AWM_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/awm.db", public_url=SERVER,
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
    from awm.cli.main import _parse_inputs

    assert _parse_inputs(["n=3", "flag=true", "s=hello", "obj={\"a\": 1}", "eq=a=b"], None) == {
        "n": 3, "flag": True, "s": "hello", "obj": {"a": 1}, "eq": "a=b",
    }
