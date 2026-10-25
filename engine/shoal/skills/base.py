"""Skill sources: marketplaces that skills are imported from.

A skill source (``git``, ``registry``, or any ``shoal.skill_sources`` plugin)
answers three questions about the skills it offers:

* **list** / **search** - pages of :class:`SkillSummary` for the catalogue;
* **fetch** - one skill as a :class:`SkillBundle`: its ``SKILL.md``, the files
  next to it, and the version (and, where there is one, the exact ref) it came from.

The tenant configures a ``SkillSource`` resource (plugin + config + secrets);
importing a skill writes a normal ``Skill`` resource into a namespace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, get_args, get_origin

import httpx
import yaml
from pydantic import BaseModel, Field, SecretStr

if TYPE_CHECKING:
    from shoal.engine import Engine

MAX_SKILL_BYTES = 2 * 1024 * 1024  # SKILL.md plus files, per skill
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


class SkillSummary(BaseModel):
    """One skill as the catalogue lists it."""

    id: str = Field(description="Stable id within the source (a path, slug or registry key).")
    name: str
    description: str = ""
    version: str | None = None
    tags: list[str] = Field(default_factory=list)
    homepage: str | None = None


class SkillPage(BaseModel):
    items: list[SkillSummary]
    next_cursor: str | None = None


class SkillBundle(BaseModel):
    """Everything needed to install one version of a skill."""

    id: str
    name: str
    description: str = ""
    version: str = Field(description="What people call this version: a semver, a tag or a short commit.")
    ref: str | None = Field(None, description="The exact revision (e.g. a commit sha) the bundle was read from.")
    instructions: str = Field(description="SKILL.md body, without its frontmatter.")
    files: dict[str, str] = Field(default_factory=dict, description="Relative path -> text content.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="SKILL.md frontmatter.")
    skipped: list[str] = Field(default_factory=list, description="Files left out (binary or too large).")
    homepage: str | None = None


class SkillSourceError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class SkillSourceContext:
    """Everything a source needs to talk to one configured ``SkillSource``."""

    engine: Engine
    tenant_id: str
    name: str
    config: Any  # instance of SkillSource.config_model
    source: SkillSource

    def http(self, **kwargs: Any) -> httpx.AsyncClient:
        transport = getattr(self.engine, "http_transport", None)
        kwargs.setdefault("timeout", httpx.Timeout(30.0, connect=10.0))
        if transport is not None:
            kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)


class SkillSource:
    """Base class for skill source plugins (entry-point group ``shoal.skill_sources``)."""

    key: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str] = ""
    config_model: ClassVar[type[BaseModel]]

    async def list(self, ctx: SkillSourceContext, cursor: str | None = None, limit: int = 50) -> SkillPage:
        raise NotImplementedError

    async def search(self, ctx: SkillSourceContext, query: str, cursor: str | None = None, limit: int = 50) -> SkillPage:
        """Default: filter the full listing by name, id, description and tags."""
        needle = query.lower()
        matches: list[SkillSummary] = []
        page_cursor: str | None = None
        while True:
            page = await self.list(ctx, page_cursor, 200)
            matches += [s for s in page.items if _matches(s, needle)]
            page_cursor = page.next_cursor
            if not page_cursor:
                break
        start = int(cursor or 0)
        end = start + limit
        return SkillPage(items=matches[start:end], next_cursor=str(end) if end < len(matches) else None)

    async def fetch(self, ctx: SkillSourceContext, skill_id: str, version: str | None = None) -> SkillBundle:
        raise NotImplementedError

    async def test(self, ctx: SkillSourceContext) -> dict[str, Any]:
        page = await self.list(ctx, None, 1)
        return {"ok": True, "sample": [s.id for s in page.items]}

    @classmethod
    def secret_fields(cls) -> list[str]:
        out = []
        for name, info in cls.config_model.model_fields.items():
            ann = info.annotation
            if ann is SecretStr or (get_origin(ann) is not None and SecretStr in get_args(ann)):
                out.append(name)
        return out

    @classmethod
    def catalog(cls) -> dict[str, Any]:
        return {
            "key": cls.key,
            "title": cls.title,
            "description": cls.description,
            "config_schema": cls.config_model.model_json_schema(),
            "secret_fields": cls.secret_fields(),
        }


def _matches(summary: SkillSummary, needle: str) -> bool:
    hay = " ".join([summary.id, summary.name, summary.description, *summary.tags]).lower()
    return all(word in hay for word in needle.split())


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Split a SKILL.md into (frontmatter, body). A file without frontmatter is all body."""
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillSourceError(f"SKILL.md frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillSourceError("SKILL.md frontmatter must be a mapping")
    return meta, text[match.end():].strip()


def skill_version(meta: dict[str, Any]) -> str | None:
    """``version`` from the frontmatter, or ``metadata.version`` (the Agent Skills spelling)."""
    nested = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    for value in (meta.get("version"), nested.get("version")):
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def skill_tags(meta: dict[str, Any]) -> list[str]:
    nested = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    tags = meta.get("tags") or nested.get("tags")
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t) for t in tags] if isinstance(tags, list) else []


def raise_for_status(response: httpx.Response, service: str) -> None:
    if response.is_success:
        return
    retryable = response.status_code in (408, 425, 429) or response.status_code >= 500
    raise SkillSourceError(
        f"{service} {response.request.method} {response.request.url.path} failed: {response.status_code} {response.text[:300]}",
        status=response.status_code,
        retryable=retryable,
    )
