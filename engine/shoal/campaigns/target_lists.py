"""Target lists: named, reusable sets of targets (repositories, projects, ...) for campaigns and triggers.

A list is static items, a saved integration query, or both: the listed targets looked up
through the query, so each carries the integration's details. Campaigns use one with
``targets: {list: <name>}``; trigger expressions see the keys of the lists they name as
``lists.<name>`` (or ``lists['<name>']``).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from shoal.core.errors import NotFound
from shoal.core.expressions import render
from shoal.core.models import TargetList, TargetListSpec
from shoal.core.scope import current_namespace

if TYPE_CHECKING:
    from shoal.engine import Engine

# How long trigger evaluation reuses a resolved list (a query list calls the integration).
CACHE_SECONDS = 60.0
_REFERENCE = re.compile(r"\blists(?:\.([A-Za-z0-9_]+)|\[\s*['\"]([A-Za-z0-9][A-Za-z0-9_.-]*)['\"]\s*\])")


@dataclass
class Resolved:
    targets: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> target, in list order
    missing: list[str] = field(default_factory=list)  # listed keys the lookup query did not return


def referenced_lists(value: Any) -> list[str]:
    """Names of target lists an expression (or a structure of them) refers to."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return sorted({a or b for a, b in _REFERENCE.findall(text)})


def _keyed(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        k = str(render(key, {"target": item}))
        if k and k not in out:
            out[k] = item
    return out


class TargetLists:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._cache: dict[tuple[str, str, str, int], tuple[float, Resolved]] = {}

    async def resolve_spec(self, tenant_id: str, spec: TargetListSpec) -> Resolved:
        queried: dict[str, dict[str, Any]] | None = None
        if spec.connection and spec.resource:
            items = await self.engine.integrations.query_all(tenant_id, spec.connection, spec.resource, spec.filter)
            queried = _keyed(items, spec.key)
        if spec.items is None:
            return Resolved(targets=queried or {})
        listed = _keyed(spec.items, spec.key)
        if queried is None:
            return Resolved(targets=listed)
        found = {k: {**queried[k], **item} for k, item in listed.items() if k in queried}
        return Resolved(targets=found, missing=[k for k in listed if k not in queried])

    async def resolve(self, tenant_id: str, name: str, *, cached: bool = False) -> Resolved:
        """Resolve the named list in the current namespace (or the shared one)."""
        row = await self.engine.store.get_resource_row(tenant_id, "TargetList", name)
        cache_key = (tenant_id, row.namespace, name, row.version)
        if cached and (hit := self._cache.get(cache_key)) and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        resolved = await self.resolve_spec(tenant_id, TargetList.model_validate(row.document).spec)
        self._cache[cache_key] = (time.monotonic(), resolved)
        if len(self._cache) > 1000:
            self._cache.clear()
        return resolved

    async def keys(self, tenant_id: str, names: list[str]) -> dict[str, list[str]]:
        """The keys of each named list, for trigger expressions. Raises NotFound naming a missing list."""
        out: dict[str, list[str]] = {}
        for name in names:
            try:
                out[name] = list((await self.resolve(tenant_id, name, cached=True)).targets)
            except NotFound as exc:
                raise NotFound(f"target list '{name}' not found in namespace '{current_namespace()}'") from exc
        return out
