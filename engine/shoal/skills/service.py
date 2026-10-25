"""Browsing skill sources and importing skills from them into namespaces.

A ``SkillSource`` resource is tenant-wide (stored in ``shared``, managed with
``skill-sources:manage``); its secrets are read from ``shared``. Importing
writes a normal ``Skill`` resource into a namespace, labelled with where it
came from, so agents use it like any other skill. Re-importing (an upgrade)
is a new version of that resource: it can be previewed as a diff first and
rolled back through the usual resource versioning.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from shoal.core.diff import diff
from shoal.core.errors import Conflict, Forbidden, InvalidRequest, NotFound
from shoal.core.models import Skill, SkillSource
from shoal.core.scope import SHARED_NAMESPACE, use_namespace
from shoal.skills.base import SkillBundle, SkillPage, SkillSourceContext, SkillSourceError
from shoal.skills.base import SkillSource as SkillSourcePlugin

if TYPE_CHECKING:
    from shoal.engine import Engine

LABEL_SOURCE = "shoal.dev/skill-source"
LABEL_SKILL = "shoal.dev/skill-source-skill"
LABEL_VERSION = "shoal.dev/skill-version"
LABEL_REF = "shoal.dev/skill-ref"
IMPORT_LABELS = (LABEL_SOURCE, LABEL_SKILL, LABEL_VERSION, LABEL_REF)


def local_name(name: str) -> str:
    """A resource name for a skill called ``name`` (lowercase letters, digits and '-')."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63].strip("-")
    return slug or "skill"


class SkillSourceService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def plugin(self, key: str) -> SkillSourcePlugin:
        return self.engine.plugin("skill_sources", key)

    def catalog(self) -> list[dict[str, Any]]:
        return [type(p).catalog() for p in self.engine.registry.available("skill_sources").values()]

    # ------------------------------------------------------------------ sources

    async def source(self, tenant_id: str, name: str) -> SkillSource:
        row = await self.engine.store.get_resource_row(tenant_id, "SkillSource", name)
        return SkillSource.model_validate(row.document)

    async def visible(self, tenant_id: str, namespace: str) -> list[SkillSource]:
        rows = await self.engine.store.list_resources(tenant_id, "SkillSource")
        sources = [SkillSource.model_validate(r.document) for r in rows]
        return [s for s in sources if s.spec.enabled and (not s.spec.namespaces or namespace in s.spec.namespaces)]

    async def _visible_source(self, tenant_id: str, namespace: str, name: str) -> SkillSource:
        source = await self.source(tenant_id, name)
        if source.spec.namespaces and namespace not in source.spec.namespaces:
            raise NotFound(f"SkillSource '{name}' not found")
        if not source.spec.enabled:
            raise Forbidden(f"skill source '{name}' is disabled")
        return source

    async def context(self, tenant_id: str, source: SkillSource) -> SkillSourceContext:
        plugin = self.plugin(source.spec.source)
        values: dict[str, Any] = dict(source.spec.config)
        with use_namespace(SHARED_NAMESPACE):  # sources are tenant-wide; so are their credentials
            for field, secret_name in source.spec.secrets.items():
                value = await self.engine.secrets.get(tenant_id, secret_name)
                if value is None:
                    raise InvalidRequest(
                        f"skill source '{source.metadata.name}': secret '{secret_name}' for '{field}' is not set in 'shared'"
                    )
                values[field] = value
        try:
            config = plugin.config_model.model_validate(values)
        except ValueError as exc:
            raise InvalidRequest(f"skill source '{source.metadata.name}' is misconfigured: {exc}") from exc
        return SkillSourceContext(engine=self.engine, tenant_id=tenant_id, name=source.metadata.name, config=config, source=plugin)

    async def test(self, tenant_id: str, name: str) -> dict[str, Any]:
        ctx = await self.context(tenant_id, await self.source(tenant_id, name))
        try:
            return await ctx.source.test(ctx)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------ browsing

    async def browse(
        self, tenant_id: str, namespace: str, name: str, *, query: str | None = None, cursor: str | None = None, limit: int = 50
    ) -> SkillPage:
        ctx = await self.context(tenant_id, await self._visible_source(tenant_id, namespace, name))
        if query:
            return await ctx.source.search(ctx, query, cursor, limit)
        return await ctx.source.list(ctx, cursor, limit)

    async def fetch(
        self, tenant_id: str, namespace: str, name: str, skill_id: str, version: str | None = None, *, refresh: bool = False
    ) -> SkillBundle:
        ctx = await self.context(tenant_id, await self._visible_source(tenant_id, namespace, name))
        ctx.refresh = refresh
        return await ctx.source.fetch(ctx, skill_id, version)

    async def installed(self, tenant_id: str, namespace: str, source: str, skill_id: str) -> list[dict[str, Any]]:
        """Skills in the namespace imported from this source skill."""
        rows = await self.engine.store.list_resources(
            tenant_id, "Skill", namespace=namespace, label={LABEL_SOURCE: source, LABEL_SKILL: skill_id}
        )
        return [_imported(r) for r in rows]

    # ------------------------------------------------------------------ import / upgrade

    async def import_skill(
        self,
        tenant_id: str,
        namespace: str,
        source: str,
        skill_id: str,
        *,
        actor: str,
        name: str | None = None,
        version: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        bundle = await self.fetch(tenant_id, namespace, source, skill_id, version)
        target = name or local_name(bundle.name)
        store = self.engine.store
        try:
            row = await store.get_resource_row(tenant_id, "Skill", target, namespace=namespace, fallback=False)
        except NotFound:
            row = None
        labels: dict[str, str] = {}
        if row is not None:
            labels = dict(row.document["metadata"].get("labels") or {})
            if (labels.get(LABEL_SOURCE), labels.get(LABEL_SKILL)) != (source, skill_id):
                origin = f" (imported from {labels[LABEL_SOURCE]}/{labels.get(LABEL_SKILL)})" if LABEL_SOURCE in labels else ""
                raise Conflict(f"skill '{target}' already exists in '{namespace}'{origin}; import it under another name")
        labels.update({LABEL_SOURCE: source, LABEL_SKILL: skill_id, LABEL_VERSION: bundle.version[:63]})
        if bundle.ref:
            labels[LABEL_REF] = bundle.ref
        else:
            labels.pop(LABEL_REF, None)
        description = bundle.description or bundle.name
        skill = Skill.model_validate({
            "metadata": {"name": target, "description": description, "labels": labels},
            "spec": _bundle_spec(bundle),
        })
        written = skill.to_document()
        after = {"metadata": {"description": description, "labels": labels}, "spec": written["spec"]}
        before = _as_written(row) if row is not None else {"metadata": {}, "spec": {}}
        changes = diff(before, after)
        summary = {
            "source": source,
            "skill_id": skill_id,
            "name": target,
            "namespace": namespace,
            "version": bundle.version,
            "ref": bundle.ref,
            "previous_version": (row.document["metadata"].get("labels") or {}).get(LABEL_VERSION) if row else None,
            "changes": changes,
            "skipped_files": bundle.skipped,
        }
        if dry_run:
            outcome = "created" if row is None else ("unchanged" if not changes else "updated")
            return {**summary, "outcome": outcome, "dry_run": True}
        message = f"Import {source}/{skill_id} {bundle.version}" + (f" ({bundle.ref[:12]})" if bundle.ref else "")
        new, outcome = await store.apply_resource(
            tenant_id, skill, actor=actor, namespace=namespace, source=written["spec"], message=message
        )
        return {**summary, "outcome": outcome, "resource_version": new.version, "dry_run": False}

    async def upgrade(
        self, tenant_id: str, namespace: str, name: str, *, actor: str, version: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Re-import an imported skill from its source (at ``version``, else the source's current one)."""
        row = await self.engine.store.get_resource_row(tenant_id, "Skill", name, namespace=namespace, fallback=False)
        labels = row.document["metadata"].get("labels") or {}
        if LABEL_SOURCE not in labels or LABEL_SKILL not in labels:
            raise InvalidRequest(f"skill '{name}' was not imported from a skill source")
        return await self.import_skill(
            tenant_id, namespace, labels[LABEL_SOURCE], labels[LABEL_SKILL],
            actor=actor, name=name, version=version, dry_run=dry_run,
        )

    async def imports(self, tenant_id: str, namespace: str, *, check: bool = False) -> list[dict[str, Any]]:
        """Imported skills in a namespace; with ``check``, ask each source for its current version."""
        rows = [
            r for r in await self.engine.store.list_resources(tenant_id, "Skill", namespace=namespace)
            if LABEL_SOURCE in (r.document["metadata"].get("labels") or {})
        ]
        out = [_imported(r) for r in rows]
        if not check:
            return out
        latest: dict[tuple[str, str], SkillBundle | str] = {}
        for item, row in zip(out, rows, strict=True):
            key = (item["source"], item["skill_id"])
            if key not in latest:
                try:
                    latest[key] = await self.fetch(tenant_id, namespace, *key, refresh=True)
                except (SkillSourceError, NotFound, Forbidden, InvalidRequest) as exc:
                    latest[key] = getattr(exc, "message", None) or str(exc)
            found = latest[key]
            if isinstance(found, str):
                item.update({"latest_version": None, "update_available": None, "check_error": found})
                continue
            item.update({"latest_version": found.version, "latest_ref": found.ref,
                         "update_available": _update_available(item, row, found)})
        return out


def _bundle_spec(bundle: SkillBundle) -> dict[str, Any]:
    description = bundle.description or bundle.name
    return {"description": description, "instructions": bundle.instructions, "files": bundle.files}


def _update_available(item: dict[str, Any], row: Any, found: SkillBundle) -> bool:
    """Whether re-importing would change the skill.

    A declared version (frontmatter, registry release) that moved is an update. A version that is
    just the source's commit moves with every commit to the repository, so there the skill only
    counts as updated when its own content changed.
    """
    commit_version = bool(found.ref) and found.ref.startswith(found.version)
    if not commit_version and item["version"] != found.version[:63]:
        return True
    imported = Skill.model_validate({"metadata": {"name": row.name}, "spec": _bundle_spec(found)}).to_document()["spec"]
    stored = row.source if row.source is not None else row.document.get("spec")
    return bool(diff({"spec": stored}, {"spec": imported}))


def _as_written(row: Any) -> dict[str, Any]:
    meta = row.document.get("metadata", {})
    return {
        "metadata": {"description": meta.get("description"), "labels": meta.get("labels") or {}},
        "spec": row.source if row.source is not None else row.document.get("spec"),
    }


def _imported(row: Any) -> dict[str, Any]:
    labels = row.document["metadata"].get("labels") or {}
    return {
        "name": row.name,
        "namespace": row.namespace,
        "source": labels.get(LABEL_SOURCE),
        "skill_id": labels.get(LABEL_SKILL),
        "version": labels.get(LABEL_VERSION),
        "ref": labels.get(LABEL_REF),
        "resource_version": row.version,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
    }
