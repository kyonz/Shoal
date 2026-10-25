"""Base classes for every kind of Shoal plugin, in one place.

Every extension point works the same way: subclass (or implement) the base
below, expose it with an entry point in the group named next to it, and check it
with the matching contract in :mod:`shoal.sdk.testing`. Integrations have their
own richer SDK (:mod:`shoal.sdk`); the rest are here::

    from shoal.sdk.plugins import SandboxBackend, Sandbox, SandboxSpec, ExecResult

    class FirecrackerBackend(SandboxBackend):
        name = "firecracker"
        async def create(self, spec: SandboxSpec) -> Sandbox: ...

    # pyproject.toml
    [project.entry-points."shoal.sandboxes"]
    firecracker = "shoal_firecracker:FirecrackerBackend"

Plugins that need the engine (secrets, HTTP transport for tests) define
``setup(self, engine)``; it is called once when the plugin is first used.
"""

from __future__ import annotations

from dataclasses import dataclass

from shoal.agents.providers.base import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderRefusal,
    ProviderUnavailable,
    ToolDef,
)
from shoal.agents.runtime import AgentInvocation, AgentResult, AgentRuntime
from shoal.cloud import CloudSession, CredentialProvider, IssueRequest
from shoal.decisions.base import Answer, DecisionProvider, DecisionRequest, DecisionResult
from shoal.evals.sinks.base import EvalExport, EvalSink, ExportResult, ScoredCase, Trace, TraceSpan
from shoal.integrations.base import Integration
from shoal.sandbox.base import ExecResult, Sandbox, SandboxBackend, SandboxSpec
from shoal.secrets.providers import SecretProvider
from shoal.skills.base import SkillBundle, SkillPage, SkillSource, SkillSourceContext, SkillSummary


@dataclass(frozen=True)
class PluginGroup:
    group: str  # entry-point group is "shoal.<group>"
    base: type
    summary: str
    remote: str | None = None  # how to run one out of process, when there is a way


PLUGIN_GROUPS: dict[str, PluginGroup] = {
    g.group: g
    for g in (
        PluginGroup("integrations", Integration, "Actions, resources and webhook events for an external system.",
                    "the remote integration protocol (any language), or MCP servers via the `mcp` integration"),
        PluginGroup("providers", ModelProvider, "Send model requests to an LLM API (used through Gateways).",
                    "any OpenAI-compatible endpoint via the `openai-compatible` provider"),
        PluginGroup("sandboxes", SandboxBackend, "Where agents and scripts run commands and keep files.",
                    "the `remote` sandbox backend's HTTP contract"),
        PluginGroup("secrets", SecretProvider, "Where secret values are read from (and written to, when writable)."),
        PluginGroup("eval_sinks", EvalSink, "Export eval results and traces to an eval platform; import datasets."),
        PluginGroup("decision_providers", DecisionProvider, "Answer typed questions (choice, score, boolean) about some state."),
        PluginGroup("agent_runtimes", AgentRuntime, "Run an agent: a tool loop, a CLI agent, or a remote agent.",
                    "any A2A agent via the `a2a` runtime"),
        PluginGroup("credentials", CredentialProvider, "Issue short-lived cloud credentials for a CloudIdentity."),
        PluginGroup("skill_sources", SkillSource, "Marketplaces skills are imported from: list, search and fetch SKILL.md bundles.",
                    "the `registry` source's shoal.skill-source/v1 protocol, which `shoal.sdk.skill_server` serves for any source"),
    )
}

__all__ = [
    "PLUGIN_GROUPS",
    "AgentInvocation",
    "AgentResult",
    "AgentRuntime",
    "Answer",
    "CloudSession",
    "CredentialProvider",
    "DecisionProvider",
    "DecisionRequest",
    "DecisionResult",
    "EvalExport",
    "EvalSink",
    "ExecResult",
    "ExportResult",
    "Integration",
    "IssueRequest",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "PluginGroup",
    "ProviderRefusal",
    "ProviderUnavailable",
    "Sandbox",
    "SandboxBackend",
    "SandboxSpec",
    "ScoredCase",
    "SecretProvider",
    "SkillBundle",
    "SkillPage",
    "SkillSource",
    "SkillSourceContext",
    "SkillSummary",
    "ToolDef",
    "Trace",
    "TraceSpan",
]
