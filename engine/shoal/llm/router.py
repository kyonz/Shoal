"""LLM gateways and model routing (§7.3 of the architecture).

* A **gateway** is how a request reaches a model: a ``Gateway`` resource
  (provider plugin + base URL + credentials + headers) or, when no such
  resource exists, a provider plugin name used directly (``anthropic``).
* A **model policy** decides which model runs. Agents name a model
  (``claude-opus-5-5``) or a policy (``policy:coding``). Rules match on agent,
  workflow, run labels and (optionally) a difficulty decided by a decision
  model, and yield ordered candidates with fallback or weighted selection.
* A tenant ``ModelPolicy`` named ``default`` sets allow/deny lists for every
  model call in the tenant, direct or routed.
"""

from __future__ import annotations

import fnmatch
import random
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from shoal.agents.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderUnavailable
from shoal.core.errors import StepError
from shoal.core.models import (
    GATEWAY_ONLY,
    AgentSpec,
    ModelCandidate,
    ModelPolicySpec,
    ModelRule,
    credential_fields,
)
from shoal.core.scope import SHARED_NAMESPACE, use_namespace

if TYPE_CHECKING:
    from shoal.engine import Engine
    from shoal.runtime.context import StepContext
    from shoal.secrets.providers import Redactor

POLICY_PREFIX = "policy:"
DEFAULT_POLICY = "default"


@dataclass
class ModelTarget:
    gateway: str
    provider_name: str
    provider: ModelProvider
    model: str
    config: dict[str, Any] = field(default_factory=dict)
    rule: str | None = None

    def describe(self) -> dict[str, Any]:
        return {"gateway": self.gateway, "provider": self.provider_name, "model": self.model, "rule": self.rule}


def _strip_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for message in messages:
        content = message["content"]
        if message["role"] == "assistant" and isinstance(content, list):
            content = [b for b in content if b.get("type") not in ("thinking", "redacted_thinking")]
        out.append({**message, "content": content})
    return out


class ModelClient:
    """Calls ordered targets, falling back on outages, rate limits and refusals.

    The client is sticky: once a fallback target answers, later turns start
    there. Thinking blocks are dropped from history when the model changes,
    since they are only valid for the model that produced them.
    """

    def __init__(self, targets: list[ModelTarget], on_fallback: Any = None):
        if not targets:
            raise StepError("no model candidates")
        self.targets = targets
        self.index = 0
        self.on_fallback = on_fallback
        self._last_used: int | None = None

    @property
    def active(self) -> ModelTarget:
        return self.targets[self.index]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        errors: list[str] = []
        for index in range(self.index, len(self.targets)):
            target = self.targets[index]
            messages = request.messages
            if self._last_used is not None and self.targets[self._last_used].model != target.model:
                messages = _strip_thinking(messages)
            config = {**request.config, **target.config}
            try:
                response = await target.provider.complete(
                    replace(request, model=target.model, messages=messages, config=config)
                )
            except ProviderUnavailable as exc:
                errors.append(f"{target.gateway}/{target.model}: {exc}")
                await self._fallback(target, str(exc))
                continue
            if response.stop_reason == "refusal" and index + 1 < len(self.targets):
                errors.append(f"{target.gateway}/{target.model}: refusal")
                await self._fallback(target, "refusal")
                continue
            self.index = index
            self._last_used = index
            response.usage = {**response.usage, "model_calls": 1}
            return response
        raise StepError("all model candidates failed", retryable=True, details={"errors": errors})

    async def _fallback(self, target: ModelTarget, reason: str) -> None:
        if self.on_fallback is not None:
            await self.on_fallback(target, reason)


class ModelRouter:
    def __init__(self, engine: Engine):
        self.engine = engine

    # ------------------------------------------------------------------ gateways

    async def gateway_target(
        self,
        tenant_id: str,
        gateway: str,
        model: str,
        *,
        effort: str | None = None,
        overrides: dict[str, Any] | None = None,
        redactor: Redactor | None = None,
        rule: str | None = None,
    ) -> ModelTarget:
        resource = await self.engine.store.find_resource(tenant_id, "Gateway", gateway)
        config: dict[str, Any] = {}
        if resource is not None:
            spec = resource.spec  # type: ignore[attr-defined]
            provider_name = spec.type
            config.update(spec.config)
            if spec.base_url:
                config["base_url"] = spec.base_url
            headers = dict(spec.headers)
            with use_namespace(SHARED_NAMESPACE):  # gateways are tenant-wide; so are their secrets
                for header, secret in spec.secret_headers.items():
                    headers[header] = await self._secret(tenant_id, gateway, secret, redactor)
                if spec.api_key_secret:
                    config["api_key"] = await self._secret(tenant_id, gateway, spec.api_key_secret, redactor)
            if headers:
                config["headers"] = headers
        else:
            provider_name = gateway
        if provider_name not in self.engine.registry.names("providers"):
            raise StepError(f"unknown gateway or provider '{gateway}'")
        overrides = overrides or {}
        if bad := sorted({*credential_fields(overrides), *(set(overrides) & GATEWAY_ONLY)}):
            raise StepError(f"provider_config cannot set {bad}: endpoints and credentials are managed on Gateways")
        config.update(overrides)
        if effort:
            config["effort"] = effort
        provider = self.engine.plugin("providers", provider_name)
        return ModelTarget(gateway, provider_name, provider, model, config, rule)

    async def _secret(self, tenant_id: str, gateway: str, name: str, redactor: Redactor | None) -> str:
        value = await self.engine.secrets.get(tenant_id, name)
        if value is None:
            raise StepError(f"secret '{name}' (gateway '{gateway}') is not set in the 'shared' namespace")
        if redactor is not None:
            redactor.add(value)
        return value

    # ------------------------------------------------------------------ policies

    async def policy(self, tenant_id: str, name: str) -> ModelPolicySpec | None:
        resource = await self.engine.store.find_resource(tenant_id, "ModelPolicy", name)
        return resource.spec if resource is not None else None  # type: ignore[attr-defined]

    @staticmethod
    def permitted(model: str, *policies: ModelPolicySpec | None) -> bool:
        for policy in filter(None, policies):
            if policy.allow_models and not any(fnmatch.fnmatch(model, p) for p in policy.allow_models):
                return False
            if any(fnmatch.fnmatch(model, p) for p in policy.deny_models):
                return False
        return True

    @staticmethod
    def match(rule: ModelRule, *, agent: str, workflow: str, labels: dict[str, Any], difficulty: str | None) -> bool:
        m = rule.match
        if m.agents and not any(fnmatch.fnmatch(agent, p) for p in m.agents):
            return False
        if m.workflows and not any(fnmatch.fnmatch(workflow, p) for p in m.workflows):
            return False
        if any(str(labels.get(k)) != v for k, v in m.labels.items()):
            return False
        return not m.difficulty or (difficulty is not None and difficulty in m.difficulty)

    @staticmethod
    def order(rule: ModelRule, seed: str) -> list[ModelCandidate]:
        candidates = list(rule.candidates)
        if rule.strategy == "weighted" and len(candidates) > 1:
            chosen = random.Random(seed).choices(candidates, weights=[c.weight for c in candidates])[0]
            candidates.remove(chosen)
            candidates.insert(0, chosen)
        return candidates

    async def resolve(
        self, ctx: StepContext, agent_name: str, agent: AgentSpec, provider_config: dict[str, Any], prompt: str
    ) -> ModelClient:
        """Pick the model(s) for an agent invocation and return a client with fallbacks."""
        tenant = ctx.tenant_id
        default_policy = await self.policy(tenant, DEFAULT_POLICY)
        info: dict[str, Any] = {"agent": agent_name}
        targets: list[ModelTarget] = []

        if agent.model.startswith(POLICY_PREFIX):
            policy_name = agent.model[len(POLICY_PREFIX) :]
            policy = await self.policy(tenant, policy_name)
            if policy is None:
                raise StepError(f"model policy '{policy_name}' not found")
            difficulty = None
            if policy.difficulty_router is not None and any(r.match.difficulty for r in policy.rules):
                decision = await self.engine.decisions.decide(
                    tenant,
                    {"agent": agent_name, "task": prompt[:20000]},
                    {"difficulty": policy.difficulty_router},
                    provider=policy.decision_provider,
                    redactor=ctx.redactor,
                )
                difficulty = str(decision.answers["difficulty"].value)
                info["difficulty"] = decision.answers["difficulty"].to_dict()
            rule = next(
                (
                    r
                    for r in policy.rules
                    if self.match(r, agent=agent_name, workflow=ctx.run.workflow_name, labels=ctx.run.labels, difficulty=difficulty)
                ),
                None,
            )
            if rule is None:
                raise StepError(f"no rule in model policy '{policy_name}' matched", details=info)
            seed = f"{ctx.run.id}:{ctx.step.id}:{ctx.attempt}"
            effort = provider_config.get("effort")
            skipped: list[str] = []
            for candidate in self.order(rule, seed):
                if not self.permitted(candidate.model, default_policy, policy):
                    continue
                gateway = candidate.gateway or agent.provider
                try:
                    target = await self.gateway_target(
                        tenant,
                        gateway,
                        candidate.model,
                        effort=candidate.effort or effort,
                        overrides=provider_config,
                        redactor=ctx.redactor,
                        rule=f"{policy_name}/{rule.name}",
                    )
                except StepError as exc:  # a misconfigured candidate falls back like an unavailable one
                    skipped.append(f"{gateway}/{candidate.model}: {exc}")
                    await ctx.emit("model.fallback", {"gateway": gateway, "model": candidate.model,
                                                      "rule": f"{policy_name}/{rule.name}", "reason": str(exc)[:500]})
                    continue
                targets.append(target)
            info["policy"] = policy_name
            if skipped and not targets:
                raise StepError(f"no usable candidate in model policy '{policy_name}': {'; '.join(skipped)}", details=info)
        else:
            if self.permitted(agent.model, default_policy):
                targets.append(
                    await self.gateway_target(
                        tenant, agent.provider, agent.model, overrides=provider_config, redactor=ctx.redactor
                    )
                )
        if not targets:
            raise StepError(f"model '{agent.model}' is not permitted by the tenant's model policy", details=info)
        await ctx.emit("model.routed", {**info, "candidates": [t.describe() for t in targets]})

        async def on_fallback(target: ModelTarget, reason: str) -> None:
            await ctx.emit("model.fallback", {**target.describe(), "reason": reason[:500]})

        return ModelClient(targets, on_fallback)
