"""Cloud identities (architecture §7.1).

A namespace registers a ``CloudIdentity`` (an AWS IAM role today) and grants
specific workflows and agents use of it. When a granted step runs, Shoal
exchanges a credential that starts from its own deployment for short-lived
credentials of that role, scoped to the step attempt:

- ``web_identity``: Shoal signs a per-step OIDC token (see
  ``shoal.auth.workload``) and the role trusts Shoal's issuer, pinned to this
  identity's subject. No cloud credential is needed or stored in Shoal.
- ``ambient``: Shoal uses the deployment's own cloud identity (IRSA, EKS Pod
  Identity, an instance role) and the role trusts that, pinned with an
  ExternalId unique to this identity. Operators opt in.

Sessions carry the initiator as source identity and the run as session tags,
every issue (and every refusal) is written to the run events and audit log,
and the credentials are redacted from logs and transcripts.

Providers are plugins (``shoal.credentials`` entry point) implementing
``CredentialProvider``, so GCP and Azure can follow the same shape.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from shoal.core.errors import NotFound, StepError
from shoal.core.models import CloudIdentitySpec

if TYPE_CHECKING:
    from shoal.engine import Engine
    from shoal.runtime.context import StepContext


@dataclass
class Attribution:
    """Who and what a session is for; providers turn it into tags, session names and claims."""

    tenant_id: str
    namespace: str
    workflow: str
    run_id: str
    step_id: str
    attempt: int
    agent: str | None = None
    initiator: str | None = None

    def tags(self) -> dict[str, str]:
        tags = {
            "shoal:tenant": self.tenant_id,
            "shoal:namespace": self.namespace,
            "shoal:workflow": self.workflow,
            "shoal:run": self.run_id,
            "shoal:step": self.step_id,
            "shoal:initiator": self.initiator or "none",
        }
        if self.agent:
            tags["shoal:agent"] = self.agent
        return tags


@dataclass
class IssueRequest:
    tenant_id: str
    namespace: str  # the identity's namespace (may be `shared`)
    name: str
    spec: CloudIdentitySpec
    attribution: Attribution | None = None

    @property
    def subject(self) -> str:
        """Stable, tenant-unique name of the identity: the OIDC ``sub`` and the AWS ExternalId."""
        return f"tenant:{self.tenant_id}:namespace:{self.namespace}:cloud-identity:{self.name}"


@dataclass
class CloudSession:
    env: dict[str, str]
    secrets: list[str]
    expires_at: datetime
    facts: dict[str, Any] = field(default_factory=dict)  # non-secret details for events and audit


class CredentialProvider(Protocol):
    name: str

    async def issue(self, request: IssueRequest) -> CloudSession: ...

    def trust_policy(self, request: IssueRequest) -> dict[str, Any]: ...


def _matches(patterns: list[str], candidates: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(c, p) for p in patterns for c in candidates)


def check_grant(spec: CloudIdentitySpec, identity_namespace: str, run_namespace: str, workflow: str,
                agent: str | None) -> str | None:
    """Why a step may not use the identity, or None when it may."""
    candidates = [f"{run_namespace}/{workflow}"]
    if identity_namespace == run_namespace:
        candidates.append(workflow)
    if not _matches(spec.allow.workflows, candidates):
        return f"workflow '{run_namespace}/{workflow}' is not in allow.workflows"
    if spec.allow.agents:
        if agent is None:
            return "only agent steps may use it (allow.agents is set)"
        if not _matches(spec.allow.agents, [agent]):
            return f"agent '{agent}' is not in allow.agents"
    return None


def initiator_label(initiator: dict[str, Any] | None) -> str | None:
    if not initiator:
        return None
    actor = initiator.get("actor") or ""
    return initiator.get("email") or initiator.get("github") or re.sub(r"^(user|sa|hub:user):", "", actor) or None


class CloudIdentities:
    def __init__(self, engine: Engine):
        self.engine = engine

    async def _load(self, tenant_id: str, name: str, namespace: str | None = None) -> tuple[str, CloudIdentitySpec]:
        try:
            row = await self.engine.store.get_resource_row(tenant_id, "CloudIdentity", name, namespace=namespace)
        except NotFound:
            raise StepError(f"cloud identity '{name}' not found") from None
        return row.namespace, CloudIdentitySpec.model_validate(row.document["spec"])

    def provider(self, name: str) -> CredentialProvider:
        return self.engine.plugin("credentials", name)

    async def trust_policy(self, tenant_id: str, namespace: str, name: str) -> dict[str, Any]:
        row = await self.engine.store.get_resource_row(tenant_id, "CloudIdentity", name, namespace=namespace)
        spec = CloudIdentitySpec.model_validate(row.document["spec"])
        request = IssueRequest(tenant_id, row.namespace, name, spec)
        ns = row.namespace
        return {"identity": f"{ns}/{name}", "provider": spec.provider, "trust": spec.trust,
                "subject": request.subject, **self.provider(spec.provider).trust_policy(request)}

    async def environment(self, ctx: StepContext, names: list[str], *, agent: str | None = None) -> dict[str, str]:
        """Issue a session for each identity and return the sandbox env; refusals fail the step."""
        env: dict[str, str] = {}
        owner: dict[str, str] = {}
        run = ctx.run
        for name in dict.fromkeys(names):
            ns, spec = await self._load(ctx.tenant_id, name)
            target = f"{ns}/{name}"
            reason = check_grant(spec, ns, run.namespace, run.workflow_name, agent)
            if reason:
                await self._audit(ctx, "cloud-identity.denied", target, ns, outcome="denied", reason=reason, agent=agent)
                await ctx.emit("cloud_identity.denied", {"identity": target, "reason": reason})
                raise StepError(f"cloud identity '{name}' may not be used here: {reason}")
            attribution = Attribution(
                tenant_id=ctx.tenant_id, namespace=run.namespace, workflow=run.workflow_name, run_id=run.id,
                step_id=ctx.step.id, attempt=ctx.attempt, agent=agent, initiator=initiator_label(run.initiator),
            )
            try:
                session = await self.provider(spec.provider).issue(IssueRequest(ctx.tenant_id, ns, name, spec, attribution))
            except StepError as exc:
                await self._audit(ctx, "cloud-identity.failed", target, ns, outcome="failure", reason=exc.message, agent=agent)
                await ctx.emit("cloud_identity.failed", {"identity": target, "error": exc.message})
                raise
            ctx.redactor.add(*session.secrets)
            for key in session.env:
                if key in owner:
                    raise StepError(f"cloud identities '{owner[key]}' and '{name}' both set {key}; use one per step")
                owner[key] = name
            env.update(session.env)
            facts = {"identity": target, "provider": spec.provider, "trust": spec.trust,
                     "expires_at": session.expires_at.isoformat(), **session.facts}
            await self._audit(ctx, "cloud-identity.issued", target, ns, agent=agent, **facts)
            await ctx.emit("cloud_identity.issued", facts)
        return env

    async def _audit(self, ctx: StepContext, action: str, target: str, namespace: str, *, outcome: str = "success",
                     **data: Any) -> None:
        await self.engine.store.audit(
            ctx.tenant_id, f"run:{ctx.run.id}", action, target, outcome=outcome, namespace=namespace,
            run_id=ctx.run.id, step_id=ctx.step.id, workflow=ctx.run.workflow_name, initiator=ctx.run.initiator or None,
            **data,
        )
