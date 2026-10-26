"""Blueprints that ship with Shoal (``library/*.yaml``).

New tenants get them in their catalogue when they are created (``SHOAL_DEFAULT_BLUEPRINTS``);
existing tenants install them with ``POST /v1/blueprints/defaults`` or ``shoal blueprints
install-defaults``. Installing never touches a blueprint the tenant wrote itself: a catalogue entry
with a bundled name is only updated while it still carries the ``shoal.dev/bundled`` label and an
older ``spec.version``, and its status (a tenant may have deprecated it) is kept.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from shoal.core.errors import NotFound
from shoal.core.models import Blueprint
from shoal.core.scope import SHARED_NAMESPACE

LIBRARY = Path(__file__).with_name("library")
LABEL_BUNDLED = "shoal.dev/bundled"


@cache
def _documents() -> tuple[dict[str, Any], ...]:
    docs = []
    for path in sorted(LIBRARY.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        Blueprint.model_validate(doc)  # a broken bundled blueprint is a packaging bug: fail loudly
        docs.append(doc)
    return tuple(docs)


def bundled() -> list[Blueprint]:
    """The bundled blueprints, labelled as such (fresh copies)."""
    out = []
    for doc in _documents():
        bp = Blueprint.model_validate(doc)
        bp.metadata.labels = {**bp.metadata.labels, LABEL_BUNDLED: "true"}
        out.append(bp)
    return out


def is_bundled(kind: str, document: dict[str, Any]) -> bool:
    """A catalogue entry that came from Shoal's bundle (the label goes when a tenant replaces it with its own)."""
    labels = (document.get("metadata") or {}).get("labels") or {}
    return kind == "Blueprint" and labels.get(LABEL_BUNDLED) == "true"


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.split(r"[-+]", version, maxsplit=1)[0].split("."))


def _older(current: str, bundled_version: str) -> bool:
    try:
        return _release(current) < _release(bundled_version)
    except ValueError:
        return False


async def install(engine: Any, tenant_id: str, *, actor: str, dry_run: bool = False) -> list[dict[str, str]]:
    """Add missing bundled blueprints to the tenant's catalogue and upgrade outdated bundled ones.

    Returns one entry per bundled blueprint: ``{name, version, outcome}`` where outcome is created,
    updated, unchanged (already at this version) or kept (the tenant's own blueprint has the name,
    or its copy is newer).
    """
    store = engine.store
    results = []
    for bp in bundled():
        name = bp.metadata.name
        try:
            row = await store.get_resource_row(tenant_id, "Blueprint", name)
        except NotFound:
            row = None
        if row is None:
            outcome = "created"
        else:
            current = Blueprint.model_validate(row.document)
            if not is_bundled("Blueprint", row.document):
                outcome = "kept"
            elif current.spec.version == bp.spec.version:
                outcome = "unchanged"
            elif _older(current.spec.version, bp.spec.version):
                outcome = "updated"
                bp.spec.status = current.spec.status
            else:
                outcome = "kept"
        if outcome in ("created", "updated") and not dry_run:
            await store.apply_resource(
                tenant_id, bp, actor=actor, namespace=SHARED_NAMESPACE,
                message=f"Shoal's bundled {name} {bp.spec.version}",
            )
        results.append({"name": name, "version": bp.spec.version, "outcome": outcome})
    return results
