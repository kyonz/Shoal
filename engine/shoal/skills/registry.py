"""Skills from an HTTP registry speaking ``shoal.skill-source/v1``.

The protocol is three read-only calls, authenticated with an optional bearer token::

    GET {url}/skills?q=&cursor=&limit=    -> {"items": [SkillSummary], "next_cursor": str | null}
    GET {url}/skills/{id}?version=        -> SkillBundle
    GET {url}/manifest                    -> {"protocol": "shoal.skill-source/v1", "title": ...}

Any marketplace can implement it directly, and :mod:`shoal.sdk.skill_server`
serves any :class:`~shoal.skills.base.SkillSource` plugin over it, so a source
can run as its own service (its own release cadence, network zone and
credentials) and be added to Shoal as a ``registry`` source.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field, SecretStr

from shoal.skills.base import (
    SkillBundle,
    SkillPage,
    SkillSource,
    SkillSourceContext,
    SkillSourceError,
    raise_for_status,
)

PROTOCOL = "shoal.skill-source/v1"


class RegistryConfig(BaseModel):
    url: str = Field(description="Registry base URL.")
    token: SecretStr | None = Field(None, description="Bearer token, if the registry needs one.")
    headers: dict[str, str] = Field(default_factory=dict, description="Extra non-secret headers.")


class RegistrySkillSource(SkillSource):
    key = "registry"
    title = "Skill registry (HTTP)"
    description = f"A marketplace or remote skill source service speaking {PROTOCOL}."
    config_model = RegistryConfig

    def _client(self, ctx: SkillSourceContext) -> Any:
        config: RegistryConfig = ctx.config
        if not config.url.startswith(("https://", "http://")):
            raise SkillSourceError("registry url must be http(s)", status=400)
        headers = {"Accept": "application/json", **config.headers}
        if config.token is not None:
            headers["Authorization"] = f"Bearer {config.token.get_secret_value()}"
        return ctx.http(base_url=config.url.rstrip("/"), headers=headers)

    async def list(self, ctx: SkillSourceContext, cursor: str | None = None, limit: int = 50) -> SkillPage:
        return await self._page(ctx, {"cursor": cursor, "limit": limit})

    async def search(self, ctx: SkillSourceContext, query: str, cursor: str | None = None, limit: int = 50) -> SkillPage:
        return await self._page(ctx, {"q": query, "cursor": cursor, "limit": limit})

    async def _page(self, ctx: SkillSourceContext, params: dict[str, Any]) -> SkillPage:
        async with self._client(ctx) as http:
            r = await http.get("/skills", params={k: v for k, v in params.items() if v is not None})
        raise_for_status(r, "skill registry")
        return SkillPage.model_validate(r.json())

    async def fetch(self, ctx: SkillSourceContext, skill_id: str, version: str | None = None) -> SkillBundle:
        async with self._client(ctx) as http:
            r = await http.get(f"/skills/{quote(skill_id, safe='/')}", params={"version": version} if version else None)
        raise_for_status(r, "skill registry")
        return SkillBundle.model_validate(r.json())

    async def test(self, ctx: SkillSourceContext) -> dict[str, Any]:
        async with self._client(ctx) as http:
            r = await http.get("/manifest")
        raise_for_status(r, "skill registry")
        manifest = r.json()
        if manifest.get("protocol") != PROTOCOL:
            return {"ok": False, "error": f"registry speaks {manifest.get('protocol')!r}, not {PROTOCOL}"}
        return {"ok": True, "manifest": manifest}
