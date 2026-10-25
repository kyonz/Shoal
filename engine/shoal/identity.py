"""Initiator attribution (§11.1 of the architecture).

Every run records who drove it (the *initiator*), separately from which
credential performs the work. ``Identity`` resources map one person across
systems so an actor seen in one place (an API key, a Jira webhook user, a
GitHub sender) resolves to the same person everywhere, including their GitHub
login and, when they linked it, their own GitHub OAuth token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shoal.core.models import Identity

if TYPE_CHECKING:
    from shoal.engine import Engine

HINT_FIELDS = ("github", "jira_account_id", "email", "slack_id")


class IdentityService:
    def __init__(self, engine: Engine):
        self.engine = engine

    async def _identities(self, tenant_id: str) -> list[Identity]:
        rows = await self.engine.store.list_resources(tenant_id, "Identity")
        return [Identity.model_validate(r.document) for r in rows]

    async def find(self, tenant_id: str, *, actor: str | None = None, **hints: Any) -> Identity | None:
        hints = {k: v for k, v in hints.items() if k in HINT_FIELDS and v}
        for identity in await self._identities(tenant_id):
            spec = identity.spec
            if actor and actor in spec.actors:
                return identity
            for key, value in hints.items():
                mine = getattr(spec, key, None)
                if mine and str(mine).lower() == str(value).lower():
                    return identity
        return None

    async def resolve(
        self, tenant_id: str, *, actor: str | None, source: str, display_name: str | None = None, **hints: Any
    ) -> dict[str, Any]:
        """Build an initiator record from an actor id and whatever hints the source provides."""
        identity = await self.find(tenant_id, actor=actor, **hints)
        initiator: dict[str, Any] = {"actor": actor, "source": source, "identity": None, "display_name": display_name}
        for key in HINT_FIELDS:
            initiator[key] = hints.get(key)
        if actor and actor.startswith("user:") and not initiator["email"] and "@" in actor:
            initiator["email"] = actor[5:]
        if identity is not None:
            spec = identity.spec
            initiator["identity"] = identity.metadata.name
            initiator["display_name"] = spec.display_name or initiator["display_name"] or identity.metadata.name
            for key in HINT_FIELDS:
                initiator[key] = getattr(spec, key, None) or initiator[key]
        initiator["display_name"] = initiator["display_name"] or initiator["github"] or initiator["email"] or actor
        return initiator

    async def github_token(self, tenant_id: str, initiator: dict[str, Any] | None) -> str | None:
        """The initiator's linked GitHub OAuth token, if any."""
        if not initiator or not initiator.get("identity"):
            return None
        resource = await self.engine.store.find_resource(tenant_id, "Identity", initiator["identity"])
        if resource is None or not resource.spec.github_token_secret:  # type: ignore[attr-defined]
            return None
        return await self.engine.secrets.get(tenant_id, resource.spec.github_token_secret)  # type: ignore[attr-defined]
