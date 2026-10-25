"""Blueprint adoption: render a catalogue blueprint into a namespace (architecture §10.1).

The catalogue lives inside the tenant. Teams publish ``Blueprint`` resources;
namespace owners adopt them. Adopting renders the templates with the
adopter's parameters, applies the resulting resources in the namespace
(labelled with the blueprint and the adoption) and records the adoption so it
can be upgraded or removed later.

Only expressions rooted at ``params``, ``prefix`` or ``blueprint`` are filled
in at adoption; every other ``{{ }}`` (``inputs``, ``steps``, ``event``, ...)
is left untouched for run time, except that ``secrets[params.x]`` inside one
becomes ``secrets["<the secret the adopter chose>"]``.

``save_from_workflow`` goes the other way: it packages a workflow (and what it
uses) as a blueprint, see ``packaging``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import select

from shoal.blueprints import packaging
from shoal.core import clock
from shoal.core.errors import Conflict, Forbidden, InvalidRequest, NotFound
from shoal.core.expressions import ExpressionError, evaluate_expression
from shoal.core.ids import new_id
from shoal.core.models import Blueprint, BlueprintSpec, ResourceBase, parse_resource
from shoal.core.scope import SHARED_NAMESPACE, TENANT_SCOPED_KINDS, use_namespace
from shoal.store import tables as t

ADOPTABLE = ("published", "featured")
TEMPLATE_ROOTS = ("params", "prefix", "blueprint")
EXPR = re.compile(r"\{\{\s*((?:(?!\}\}).)+?)\s*\}\}", re.DOTALL)
LABEL_BLUEPRINT = "shoal.dev/blueprint"
LABEL_VERSION = "shoal.dev/blueprint-version"
LABEL_ADOPTION = "shoal.dev/adoption"
LABEL_FROM_WORKFLOW = "shoal.dev/from-workflow"
LABEL_FROM_NAMESPACE = "shoal.dev/from-namespace"
SECRET_PARAM = re.compile(r"secrets\[\s*params\.([A-Za-z_][A-Za-z0-9_]*)\s*\]")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([^+]+))?(?:\+.*)?$")


def _is_template(expr: str) -> bool:
    root = re.split(r"[\s.\[|(]", expr.strip(), maxsplit=1)[0]
    return root in TEMPLATE_ROOTS


def render(value: Any, context: dict[str, Any]) -> Any:
    """Fill in adoption-time expressions, leaving run-time ones as written."""
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    if not isinstance(value, str) or "{{" not in value:
        return value
    whole = EXPR.fullmatch(value.strip())
    if whole and _is_template(whole.group(1)):
        return evaluate_expression(whole.group(1), context)

    def substitute(match: re.Match[str]) -> str:
        if not _is_template(match.group(1)):
            return _bind_secret_names(match.group(0), context)
        result = evaluate_expression(match.group(1), context)
        return "" if result is None else str(result)

    return EXPR.sub(substitute, value)


def _bind_secret_names(expression: str, context: dict[str, Any]) -> str:
    """``secrets[params.x]`` -> ``secrets["<value of x>"]``, leaving the rest for run time."""

    def name(m: re.Match[str]) -> str:
        value = (context.get("params") or {}).get(m.group(1))
        if not isinstance(value, str) or not value:
            raise ExpressionError(f"params.{m.group(1)} must name a secret")
        return f"secrets[{json.dumps(value)}]"

    return SECRET_PARAM.sub(name, expression)


def semver_key(version: str) -> tuple[int, int, int, int, str]:
    m = SEMVER.match(version)
    if not m:
        raise InvalidRequest(f"'{version}' is not a semantic version (1.2.3)")
    major, minor, patch, pre = m.groups()
    return int(major), int(minor), int(patch), 0 if pre else 1, pre or ""


def next_version(version: str) -> str:
    major, minor, _, _, _ = semver_key(version)
    return f"{major}.{minor + 1}.0"


class BlueprintService:
    def __init__(self, engine: Any):
        self.engine = engine

    # ------------------------------------------------------------------ lookups

    async def blueprint(self, tenant_id: str, name: str, version: str | None = None) -> Blueprint:
        """The current blueprint, or the revision whose spec.version is ``version``."""
        store = self.engine.store
        current = Blueprint.model_validate(
            (await store.get_resource_row(tenant_id, "Blueprint", name)).document
        )
        if version is None or current.spec.version == version:
            return current
        for revision in reversed(await store.resource_versions(tenant_id, "Blueprint", name)):
            doc = revision.document
            if doc.get("spec", {}).get("version") == version:
                return Blueprint.model_validate(doc)
        raise NotFound(f"blueprint '{name}' has no version {version}")

    async def adoptions(
        self, tenant_id: str, *, namespace: str | None = None, blueprint: str | None = None
    ) -> list[t.BlueprintAdoption]:
        q = select(t.BlueprintAdoption).where(t.BlueprintAdoption.tenant_id == tenant_id)
        if namespace:
            q = q.where(t.BlueprintAdoption.namespace == namespace)
        if blueprint:
            q = q.where(t.BlueprintAdoption.blueprint == blueprint)
        async with self.engine.store.session() as s:
            return list((await s.execute(q.order_by(t.BlueprintAdoption.created_at.desc()))).scalars().all())

    async def adoption(self, tenant_id: str, adoption_id: str) -> t.BlueprintAdoption:
        async with self.engine.store.session() as s:
            row = await s.get(t.BlueprintAdoption, adoption_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFound(f"adoption '{adoption_id}' not found")
        return row

    # ------------------------------------------------------------------ rendering

    async def check_params(
        self, tenant_id: str, namespace: str, spec: BlueprintSpec, params: dict[str, Any]
    ) -> dict[str, Any]:
        unknown = set(params) - set(spec.params)
        if unknown:
            raise InvalidRequest(f"unknown parameters: {sorted(unknown)}")
        values: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name, p in spec.params.items():
            value = params.get(name, p.default)
            if value is None:
                if p.required:
                    errors[name] = "required"
                continue
            if p.type == "number" and not isinstance(value, int | float):
                errors[name] = "must be a number"
            elif p.type == "boolean" and not isinstance(value, bool):
                errors[name] = "must be true or false"
            elif p.type in ("string", "connection", "secret", "choice") and not isinstance(value, str):
                errors[name] = "must be a string"
            elif p.type == "choice" and value not in p.options:
                errors[name] = f"must be one of {p.options}"
            elif p.type == "connection":
                with use_namespace(namespace):
                    connection = await self.engine.store.find_resource(
                        tenant_id, "Connection", value, namespace=namespace
                    )
                if connection is None:
                    errors[name] = f"no connection '{value}' is visible from namespace '{namespace}'"
                elif p.integration and connection.spec.integration != p.integration:  # type: ignore[attr-defined]
                    errors[name] = (
                        f"connection '{value}' is {connection.spec.integration}, not {p.integration}"  # type: ignore[attr-defined]
                    )
            elif p.type == "secret":
                if not any(
                    row.name == value
                    for row in await self.engine.store.list_secrets(tenant_id, namespace=namespace)
                ):
                    errors[name] = f"no secret '{value}' in namespace '{namespace}'"
            values[name] = value
        if errors:
            raise InvalidRequest("invalid blueprint parameters", details={"params": errors})
        return values

    def render_resources(
        self, blueprint: Blueprint, params: dict[str, Any], prefix: str
    ) -> list[tuple[ResourceBase, dict[str, Any]]]:
        context = {
            "params": params,
            "prefix": f"{prefix}-" if prefix else "",
            "blueprint": {"name": blueprint.metadata.name, "version": blueprint.spec.version},
        }
        out: list[tuple[ResourceBase, dict[str, Any]]] = []
        for i, template in enumerate(blueprint.spec.resources):
            try:
                doc = render(template, context)
            except ExpressionError as exc:
                raise InvalidRequest(f"resource {i}: {exc}") from exc
            if doc.get("kind") in TENANT_SCOPED_KINDS or doc.get("kind") == "Blueprint":
                raise InvalidRequest(f"resource {i}: blueprints cannot create {doc.get('kind')} resources")
            doc.setdefault("apiVersion", "shoal.dev/v1")
            doc.setdefault("metadata", {}).pop("namespace", None)
            try:
                out.append((parse_resource(doc), doc.get("spec") or {}))
            except (ValidationError, ValueError) as exc:
                raise InvalidRequest(
                    f"resource {i} ({doc.get('kind')} {doc['metadata'].get('name')}) is invalid: {exc}"
                ) from exc
        names = [(r.to_document()["kind"], r.metadata.name) for r, _ in out]
        if len(set(names)) != len(names):
            raise InvalidRequest(
                "the blueprint renders two resources with the same kind and name; use {{ prefix }} in names"
            )
        return out

    # ------------------------------------------------------------------ adopt / upgrade / remove

    async def adopt(
        self,
        tenant_id: str,
        namespace: str,
        name: str,
        params: dict[str, Any],
        *,
        actor: str,
        prefix: str = "",
        version: str | None = None,
        dry_run: bool = False,
        can_adopt_drafts: bool = False,
        adoption: t.BlueprintAdoption | None = None,
    ) -> dict[str, Any]:
        blueprint = await self.blueprint(tenant_id, name, version)
        spec = blueprint.spec
        if spec.status not in ADOPTABLE and not (spec.status == "draft" and can_adopt_drafts):
            raise Forbidden(f"blueprint '{name}' is {spec.status} and cannot be adopted")
        if spec.namespaces and namespace not in spec.namespaces:
            raise Forbidden(f"blueprint '{name}' is not available to namespace '{namespace}'")
        values = await self.check_params(tenant_id, namespace, spec, params)
        rendered = self.render_resources(blueprint, values, prefix)
        adoption_id = adoption.id if adoption else new_id("adp")

        plan: list[dict[str, Any]] = []
        store = self.engine.store
        for resource, _ in rendered:
            kind = resource.to_document()["kind"]
            existing = await store.find_resource(tenant_id, kind, resource.metadata.name, namespace=namespace)
            owner = existing.metadata.labels.get(LABEL_ADOPTION) if existing and existing.metadata else None
            if (
                existing is not None
                and await self._in_namespace(tenant_id, kind, resource.metadata.name, namespace)
                and owner != adoption_id
            ):
                raise Conflict(
                    f"{kind} '{resource.metadata.name}' already exists in '{namespace}'"
                    + (f" (from adoption {owner})" if owner else "")
                    + "; adopt with a different prefix"
                )
            plan.append({"kind": kind, "name": resource.metadata.name, "plural": resource.plural})
        if dry_run:
            return {
                "blueprint": name,
                "version": spec.version,
                "namespace": namespace,
                "resources": plan,
                "dry_run": True,
            }

        labels = {LABEL_BLUEPRINT: name, LABEL_VERSION: spec.version, LABEL_ADOPTION: adoption_id}
        applied = []
        for resource, source in rendered:
            resource.metadata.labels = {**resource.metadata.labels, **labels}
            row, outcome = await store.apply_resource(
                tenant_id, resource, actor=actor, namespace=namespace, source=source
            )
            applied.append({"kind": row.kind, "name": row.name, "outcome": outcome, "version": row.version})

        previous = {(r["kind"], r["name"]) for r in (adoption.resources if adoption else [])}
        removed = previous - {(r["kind"], r["name"]) for r in applied}
        for kind, rname in removed:  # dropped by the new version
            if await self._in_namespace(tenant_id, kind, rname, namespace):
                await store.delete_resource(tenant_id, kind, rname, namespace=namespace)

        now = clock.now()
        async with store.session() as s:
            row = await s.get(t.BlueprintAdoption, adoption_id)
            if row is None:
                row = t.BlueprintAdoption(
                    id=adoption_id, tenant_id=tenant_id, namespace=namespace, blueprint=name, created_by=actor
                )
                s.add(row)
            row.blueprint_version = spec.version
            row.prefix = prefix
            row.params = values
            row.resources = [{"kind": r["kind"], "name": r["name"]} for r in applied]
            row.updated_at = now
        result = await self.adoption(tenant_id, adoption_id)
        return {
            "adoption": result,
            "resources": applied,
            "removed": [{"kind": k, "name": n} for k, n in sorted(removed)],
        }

    async def upgrade(
        self,
        tenant_id: str,
        adoption_id: str,
        *,
        actor: str,
        version: str | None,
        params: dict[str, Any] | None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        adoption = await self.adoption(tenant_id, adoption_id)
        merged = {**adoption.params, **(params or {})}
        return await self.adopt(
            tenant_id,
            adoption.namespace,
            adoption.blueprint,
            merged,
            actor=actor,
            prefix=adoption.prefix,
            version=version,
            dry_run=dry_run,
            adoption=adoption,
        )

    async def remove(self, tenant_id: str, adoption_id: str) -> list[dict[str, str]]:
        adoption = await self.adoption(tenant_id, adoption_id)
        removed = []
        for ref in adoption.resources:
            existing = await self.engine.store.find_resource(
                tenant_id, ref["kind"], ref["name"], namespace=adoption.namespace
            )
            if (
                existing
                and existing.metadata.labels.get(LABEL_ADOPTION) == adoption_id
                and await self._in_namespace(tenant_id, ref["kind"], ref["name"], adoption.namespace)
            ):
                await self.engine.store.delete_resource(
                    tenant_id, ref["kind"], ref["name"], namespace=adoption.namespace
                )
                removed.append(ref)
        async with self.engine.store.session() as s:
            row = await s.get(t.BlueprintAdoption, adoption_id)
            if row is not None:
                await s.delete(row)
        return removed

    # ------------------------------------------------------------------ save a workflow as a blueprint

    async def package_workflow(
        self,
        tenant_id: str,
        namespace: str,
        workflow: str,
        *,
        blueprint: str | None = None,
        target: Literal["new", "existing"] = "new",
        version: str | None = None,
        details: dict[str, Any] | None = None,
        status: str | None = None,
        include: set[tuple[str, str]] | None = None,
        choices: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Package ``workflow`` as a new blueprint or a new version of an existing one, without writing.

        ``details`` holds title, summary, readme, author, icon, tags, categories and namespaces; for a new
        version, what is left out is kept from the current version. Returns the blueprint document, the
        package (candidates, findings, warnings) and what exists; ``publish_package`` writes it.
        """
        store = self.engine.store
        name = blueprint or workflow
        with use_namespace(namespace):
            candidates, warnings = await packaging.collect(store, tenant_id, namespace, workflow)
            connections: dict[str, tuple[str, str]] = {}
            for row in [
                *await store.list_resources(tenant_id, "Connection", namespace=namespace),
                *await store.list_resources(tenant_id, "Connection", namespace=SHARED_NAMESPACE),
            ]:
                connections.setdefault(row.name, (row.document["spec"]["integration"], row.namespace))
        pkg = packaging.package(
            candidates, include=include, choices=choices or {}, connections=connections, warnings=warnings
        )

        existing = await store.find_resource(tenant_id, "Blueprint", name)
        current: BlueprintSpec | None = existing.spec if existing is not None else None  # type: ignore[attr-defined]
        if target == "new" and current is not None:
            pkg.warnings.insert(0, f"blueprint '{name}' already exists; publish this as a new version of it instead")
        if target == "existing" and current is None:
            raise NotFound(f"blueprint '{name}' not found")
        base = current if target == "existing" else None
        if version is None:
            version = next_version(base.version) if base else "0.1.0"
        if base and semver_key(version) <= semver_key(base.version):
            raise InvalidRequest(f"version {version} must be newer than the current {base.version}")

        details = {k: v for k, v in (details or {}).items() if v is not None}
        root = next(c for c in candidates if c.root)
        spec: dict[str, Any] = {
            "title": details.get("title") or (base.title if base else _title(workflow)),
            "summary": details.get("summary") or (base.summary if base else (
                root.document["metadata"].get("description") or root.document["spec"].get("description")
                or f"Workflow {workflow}, packaged for other teams to adopt."
            )),
            "readme": details["readme"] if "readme" in details else (base.readme if base else _readme(pkg, namespace, workflow)),
            "version": version,
            "status": status or (base.status if base else "draft"),
        }
        for key in ("author", "icon"):
            value = details.get(key) or (getattr(base, key) if base else None)
            if value:
                spec[key] = value
        for key in ("tags", "categories", "namespaces"):
            value = details[key] if key in details else (getattr(base, key) if base else [])
            if value:
                spec[key] = value
        if pkg.params:
            spec["params"] = pkg.params
        spec["resources"] = pkg.resources

        document = {
            "apiVersion": "shoal.dev/v1",
            "kind": "Blueprint",
            "metadata": {
                "name": name,
                **({"description": existing.metadata.description} if existing and existing.metadata.description else {}),
                "labels": {
                    **(existing.metadata.labels if existing else {}),
                    LABEL_FROM_WORKFLOW: workflow,
                    LABEL_FROM_NAMESPACE: namespace,
                },
            },
            "spec": spec,
        }
        try:
            resource = Blueprint.model_validate(document)
        except ValidationError as exc:
            raise InvalidRequest(f"the packaged blueprint is invalid: {exc}") from exc
        # it must render back into valid resources, with the original values in every parameter
        sample = {f.param: f.default if f.default is not None else (f.value or "secret")
                  for f in pkg.findings if f.enabled}
        self.render_resources(resource, sample, "")

        return {
            "package": pkg,
            "blueprint": document,
            "resource": resource,
            "target": target,
            "source": f"{namespace}/{workflow}",
            "exists": current is not None,
            "previous_version": current.version if current else None,
        }

    async def publish_package(self, tenant_id: str, packaged: dict[str, Any], *, actor: str) -> tuple[t.Resource, str]:
        """Write a blueprint from ``package_workflow``; returns (row, created | updated | unchanged)."""
        name = packaged["blueprint"]["metadata"]["name"]
        if packaged["target"] == "new" and packaged["exists"]:
            raise Conflict(f"blueprint '{name}' already exists; publish a new version of it instead")
        return await self.engine.store.apply_resource(
            tenant_id, packaged["resource"], actor=actor, namespace=SHARED_NAMESPACE,
            source=packaged["blueprint"]["spec"], message=f"Saved from workflow {packaged['source']}",
        )

    async def _in_namespace(self, tenant_id: str, kind: str, name: str, namespace: str) -> bool:
        try:
            await self.engine.store.get_resource_row(
                tenant_id, kind, name, namespace=namespace, fallback=False
            )
            return True
        except NotFound:
            return False


def _title(name: str) -> str:
    return name.replace("-", " ").capitalize()


def _readme(pkg: packaging.Package, namespace: str, workflow: str) -> str:
    lines = [f"Packaged from the workflow `{workflow}` in `{namespace}`.", "", "**Contents**", ""]
    lines += [f"- {c.kind} `{c.name}`" for c in pkg.candidates if c.included]
    if pkg.params:
        lines += ["", "**What you supply when adopting**", ""]
        lines += [f"- `{name}` ({p['type']}): {p.get('description') or ''}".rstrip(": ") for name, p in pkg.params.items()]
    return "\n".join(lines) + "\n"
