"""The built-in plugins pass the same contracts third-party plugins are asked to pass."""

from __future__ import annotations

import pytest

from shoal.agents.providers.scripted import ScriptedProvider
from shoal.decisions.jev import JevProvider
from shoal.evals.sinks.braintrust import BraintrustSink
from shoal.evals.sinks.langfuse import LangfuseSink
from shoal.sandbox.local import LocalBackend
from shoal.sdk.plugins import PLUGIN_GROUPS
from shoal.sdk.testing import (
    check_decision_provider,
    check_entry_point,
    check_eval_sink,
    check_model_provider,
    check_sandbox_backend,
    check_secret_provider,
)

BUILT_INS = [
    ("integrations", "github"), ("integrations", "jira"), ("integrations", "webhook"), ("integrations", "remote"),
    ("integrations", "mcp"), ("providers", "anthropic"), ("providers", "openai-compatible"), ("providers", "scripted"),
    ("sandboxes", "local"), ("sandboxes", "docker"), ("sandboxes", "kubernetes"), ("sandboxes", "e2b"),
    ("sandboxes", "space"), ("sandboxes", "remote"), ("secrets", "env"), ("secrets", "db"),
    ("eval_sinks", "braintrust"), ("eval_sinks", "langfuse"), ("decision_providers", "llm"), ("decision_providers", "jev"),
    ("agent_runtimes", "tool-loop"), ("agent_runtimes", "cli"), ("agent_runtimes", "a2a"), ("credentials", "aws"),
]


@pytest.mark.parametrize(("group", "name"), BUILT_INS)
def test_built_ins_are_entry_points_of_the_right_kind(group, name):
    check_entry_point(group, name)


def test_every_group_is_documented():
    from pathlib import Path

    guide = (Path(__file__).resolve().parents[2] / "docs" / "plugins.md").read_text()
    for group in PLUGIN_GROUPS:
        assert f"shoal.{group}" in guide, f"docs/plugins.md does not cover shoal.{group}"


async def test_local_sandbox_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOAL_LOCAL_SANDBOX_ROOT", str(tmp_path))
    await check_sandbox_backend(LocalBackend(), workdir="/workspace")


async def test_model_provider_contract():
    provider = ScriptedProvider()
    await check_model_provider(provider, model="scripted", config={"script": [{"text": "OK"}]}, check_tools=False)
    await check_model_provider(provider, model="scripted", config={
        "script": [{"text": "OK", "tool_calls": [{"name": "record", "input": {"word": "ok"}}]}]}, check_tools=False)


async def test_secret_provider_contracts(engine, tenant, monkeypatch):
    db = engine.plugin("secrets", "db")
    await db.put(tenant.id, "known", "value")
    await check_secret_provider(db, tenant.id, known={"known": "value"})
    monkeypatch.setenv("SHOAL_SECRET_ACME__TOKEN", "from-env")
    await check_secret_provider(engine.plugin("secrets", "env"), tenant.id, known={"token": "from-env"})


async def test_decision_provider_contract():
    class Client:
        async def system_one(self, **request):
            out = {}
            for key, q in request["questions"].items():
                if q["type"] == "choice":
                    out[key] = {"choice": next(iter(q["criteria"])), "confidence": 0.8}
                elif q["type"] == "score":
                    out[key] = {"score": 1, "confidence": 0.6}
                else:
                    out[key] = {"noul": 0.9}
            return {"answers": out, "usage": {}}

    await check_decision_provider(JevProvider(client_factory=lambda cfg: Client()))


def test_eval_sink_contracts():
    check_eval_sink(BraintrustSink())
    check_eval_sink(LangfuseSink())
