"""Declarative resources (agents, workflows, connections, ...), apply, secrets,
and connection / trigger operations."""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Query, Request, Response
from pydantic import ValidationError

from shoal.api.deps import Ctx, CtxDep
from shoal.api.schemas import (
    ActionCallIn,
    ApplyResult,
    DeliveryOut,
    QueryIn,
    ResourceIn,
    ResourceMetadataOut,
    ResourceOut,
    ResourceVersionOut,
    RollbackIn,
    SecretIn,
    SecretOut,
    TriggerTestIn,
    VersionDiffOut,
)
from shoal.core.diff import diff
from shoal.core.errors import InvalidRequest, NotFound
from shoal.core.ids import new_id
from shoal.core.models import KIND_BY_PLURAL, Connection, ResourceBase, Trigger, parse_resource
from shoal.core.scope import SHARED_NAMESPACE, TENANT_SCOPED_KINDS, use_namespace
from shoal.integrations.base import IntegrationEvent, describe_actions, describe_resources
from shoal.store import tables as t
from shoal.triggers import validate_cron

router = APIRouter(prefix="/v1")

NAMESPACED = {p: k for p, k in KIND_BY_PLURAL.items() if k not in TENANT_SCOPED_KINDS}
TENANT_SCOPED = {p: k for p, k in KIND_BY_PLURAL.items() if k in TENANT_SCOPED_KINDS}
# Tenant-scoped kinds use tenant permissions: identities:read|write, blueprints:read|publish
TENANT_VERBS = {"identities": {"read": "identities:read", "write": "identities:write", "delete": "identities:write"},
                "blueprints": {"read": "blueprints:read", "write": "blueprints:publish", "delete": "blueprints:publish"},
                "gateways": {"read": "gateways:read", "write": "gateways:manage", "delete": "gateways:manage"},
                "model-policies": {"read": "model-policies:read", "write": "model-policies:manage",
                                   "delete": "model-policies:manage"}}


async def to_out(ctx: Ctx, row: t.Resource) -> ResourceOut:
    doc = row.document
    meta = doc.get("metadata", {})
    status: dict[str, Any] = {}
    if row.kind == "Connection":
        with use_namespace(row.namespace):
            status["webhook_url"] = await ctx.engine.integrations.webhook_url(ctx.tenant_id, row.name)
    elif row.kind == "Trigger" and doc["spec"].get("event") == "schedule" and doc["spec"].get("schedule"):
        with use_namespace(row.namespace):
            state = await ctx.engine.store.get_trigger_state(ctx.tenant_id, row.name)
        status["next_fire_at"] = state.next_fire_at.isoformat() if state.next_fire_at else None
        status["last_fired_at"] = state.last_fired_at.isoformat() if state.last_fired_at else None
    return ResourceOut(
        kind=row.kind,
        metadata=ResourceMetadataOut(
            name=row.name, namespace=row.namespace, description=meta.get("description"), labels=meta.get("labels") or {},
            annotations=row.annotations or {},
            version=row.version, created_at=row.created_at, updated_at=row.updated_at, updated_by=row.updated_by,
        ),
        spec=row.source if row.source is not None else doc["spec"],
        status=status,
    )


def build_resource(kind: str, name: str, body: ResourceIn) -> ResourceBase:
    document = {
        "apiVersion": "shoal.dev/v1",
        "kind": kind,
        "metadata": {"name": name, **body.metadata.model_dump(exclude_none=True)},
        "spec": strip_nulls(body.spec),
    }
    try:
        resource = parse_resource(document)
    except ValidationError as exc:
        raise InvalidRequest(f"invalid {kind}", details={"errors": _errors(exc)}) from exc
    validate_semantics(resource)
    return resource


def strip_nulls(value: Any) -> Any:
    """Drop null members (an omitted field and an explicit null mean the same)."""
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


def _errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [{"loc": ".".join(str(p) for p in e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]


def validate_semantics(resource: ResourceBase) -> None:
    """Checks beyond the schema that a client should hear about at apply time."""
    if isinstance(resource, Trigger) and resource.spec.schedule:
        try:
            validate_cron(resource.spec.schedule, resource.spec.timezone)
        except Exception as exc:
            raise InvalidRequest(f"invalid schedule: {exc}") from exc


MessageQuery = Query(None, max_length=2000, description="Change note stored with the new version.")


async def _put(ctx: Ctx, kind: str, name: str, namespace: str, body: ResourceIn, response: Response,
               message: str | None = None) -> ResourceOut:
    resource = build_resource(kind, name, body)
    row, outcome = await ctx.engine.store.apply_resource(
        ctx.tenant_id, resource, actor=ctx.actor, namespace=namespace, source=strip_nulls(body.spec), message=message
    )
    if outcome != "unchanged":
        await ctx.audit(f"{kind.lower()}.{outcome}", f"{row.namespace}/{name}", namespace=row.namespace, version=row.version)
    if outcome == "created":
        response.status_code = 201
    return await to_out(ctx, row)


# --------------------------------------------------------------------------- namespaced resources
# One explicit set of routes per kind keeps the OpenAPI document (and the clients
# and Terraform provider generated from it) precise.


def _namespaced_routes(plural: str, kind: str) -> None:
    tag = [plural]
    base = f"/namespaces/{{namespace}}/{plural}"

    @router.get(base, tags=tag, response_model=list[ResourceOut], name=f"list_{plural}")
    async def list_all(namespace: str, ctx: CtxDep, label: list[str] = Query(default_factory=list)) -> Any:
        await ctx.namespace(namespace, f"{plural}:read")
        labels = dict(item.split("=", 1) for item in label if "=" in item)
        rows = await ctx.engine.store.list_resources(ctx.tenant_id, kind, namespace=namespace, label=labels or None)
        return [await to_out(ctx, r) for r in rows]

    @router.put(f"{base}/{{name}}", tags=tag, response_model=ResourceOut, name=f"put_{plural}",
                summary=f"Create or replace a {kind}", responses={201: {"description": "Created"}})
    async def put_one(namespace: str, name: str, body: ResourceIn, ctx: CtxDep, response: Response,
                      message: str | None = MessageQuery) -> Any:
        await ctx.namespace(namespace, f"{plural}:write")
        return await _put(ctx, kind, name, namespace, body, response, message)

    @router.get(f"{base}/{{name}}", tags=tag, response_model=ResourceOut, name=f"get_{plural}")
    async def get_one(namespace: str, name: str, ctx: CtxDep) -> Any:
        await ctx.namespace(namespace, f"{plural}:read")
        row = await ctx.engine.store.get_resource_row(ctx.tenant_id, kind, name, namespace=namespace, fallback=False)
        return await to_out(ctx, row)

    @router.delete(f"{base}/{{name}}", status_code=204, tags=tag, name=f"delete_{plural}")
    async def delete_one(namespace: str, name: str, ctx: CtxDep) -> None:
        await ctx.namespace(namespace, f"{plural}:delete")
        await ctx.engine.store.delete_resource(ctx.tenant_id, kind, name, namespace=namespace)
        await ctx.audit(f"{kind.lower()}.deleted", f"{namespace}/{name}", namespace=namespace)

    @router.get(f"{base}/{{name}}/versions", tags=tag, response_model=list[ResourceVersionOut], name=f"versions_{plural}")
    async def versions(namespace: str, name: str, ctx: CtxDep) -> Any:
        await ctx.namespace(namespace, f"{plural}:read")
        return await ctx.engine.store.resource_versions(ctx.tenant_id, kind, name, namespace=namespace)

    @router.get(f"{base}/{{name}}/versions/{{version}}", tags=tag, response_model=ResourceVersionOut, name=f"version_{plural}")
    async def version(namespace: str, name: str, version: int, ctx: CtxDep) -> Any:
        await ctx.namespace(namespace, f"{plural}:read")
        return await ctx.engine.store.get_resource_version_row(ctx.tenant_id, kind, name, version, namespace=namespace)

    @router.get(f"{base}/{{name}}/diff", tags=tag, response_model=VersionDiffOut, name=f"diff_{plural}",
                summary=f"What changed between two versions of a {kind}")
    async def diff_versions(namespace: str, name: str, ctx: CtxDep, from_: int = Query(alias="from", ge=1),
                            to: int | None = Query(None, ge=1, description="Defaults to the current version.")) -> Any:
        await ctx.namespace(namespace, f"{plural}:read")
        return await _diff(ctx, kind, name, namespace, from_, to)

    @router.post(f"{base}/{{name}}/rollback", tags=tag, response_model=ResourceOut | VersionDiffOut, name=f"rollback_{plural}",
                 summary=f"Restore an earlier version of a {kind} as a new version")
    async def rollback(namespace: str, name: str, body: RollbackIn, ctx: CtxDep) -> Any:
        await ctx.namespace(namespace, f"{plural}:write")
        return await _rollback(ctx, kind, name, namespace, body)


for _plural, _kind_name in NAMESPACED.items():
    _namespaced_routes(_plural, _kind_name)


# --------------------------------------------------------------------------- tenant-scoped resources


def _tenant_routes(plural: str, kind: str) -> None:
    verbs = TENANT_VERBS[plural]
    tag = [plural]

    @router.get(f"/{plural}", tags=tag, response_model=list[ResourceOut], name=f"list_{plural}")
    async def list_all(ctx: CtxDep) -> Any:
        await ctx.require(verbs["read"])
        return [await to_out(ctx, r) for r in await ctx.engine.store.list_resources(ctx.tenant_id, kind)]

    @router.put(f"/{plural}/{{name}}", tags=tag, response_model=ResourceOut, name=f"put_{plural}")
    async def put_one(name: str, body: ResourceIn, ctx: CtxDep, response: Response, message: str | None = MessageQuery) -> Any:
        await ctx.require(verbs["write"])
        if kind == "Blueprint" and (body.spec or {}).get("status") == "featured":
            current = await ctx.engine.store.find_resource(ctx.tenant_id, kind, name)
            if current is None or current.spec.status != "featured":  # type: ignore[attr-defined]
                await ctx.require("blueprints:feature")
        return await _put(ctx, kind, name, SHARED_NAMESPACE, body, response, message)

    @router.get(f"/{plural}/{{name}}", tags=tag, response_model=ResourceOut, name=f"get_{plural}")
    async def get_one(name: str, ctx: CtxDep) -> Any:
        await ctx.require(verbs["read"])
        return await to_out(ctx, await ctx.engine.store.get_resource_row(ctx.tenant_id, kind, name))

    @router.delete(f"/{plural}/{{name}}", status_code=204, tags=tag, name=f"delete_{plural}")
    async def delete_one(name: str, ctx: CtxDep) -> None:
        await ctx.require(verbs["delete"])
        await ctx.engine.store.delete_resource(ctx.tenant_id, kind, name)
        await ctx.audit(f"{kind.lower()}.deleted", name)


    @router.get(f"/{plural}/{{name}}/versions", tags=tag, response_model=list[ResourceVersionOut], name=f"versions_{plural}")
    async def versions(name: str, ctx: CtxDep) -> Any:
        await ctx.require(verbs["read"])
        return await ctx.engine.store.resource_versions(ctx.tenant_id, kind, name)

    @router.get(f"/{plural}/{{name}}/diff", tags=tag, response_model=VersionDiffOut, name=f"diff_{plural}")
    async def diff_versions(name: str, ctx: CtxDep, from_: int = Query(alias="from", ge=1), to: int | None = Query(None, ge=1)) -> Any:
        await ctx.require(verbs["read"])
        return await _diff(ctx, kind, name, SHARED_NAMESPACE, from_, to)

    @router.post(f"/{plural}/{{name}}/rollback", tags=tag, response_model=ResourceOut | VersionDiffOut, name=f"rollback_{plural}")
    async def rollback(name: str, body: RollbackIn, ctx: CtxDep) -> Any:
        await ctx.require(verbs["write"])
        return await _rollback(ctx, kind, name, SHARED_NAMESPACE, body)


for _plural, _kind_name in TENANT_SCOPED.items():
    _tenant_routes(_plural, _kind_name)


# --------------------------------------------------------------------------- versions


def _as_written(version: t.ResourceVersion) -> dict[str, Any]:
    """Diff what people wrote, not server defaults, when the version kept its source."""
    doc = version.document
    return {"metadata": doc.get("metadata", {}), "spec": version.source if version.source is not None else doc.get("spec")}


async def _diff(ctx: Ctx, kind: str, name: str, namespace: str, from_: int, to: int | None) -> VersionDiffOut:
    store = ctx.engine.store
    row = await store.get_resource_row(ctx.tenant_id, kind, name, namespace=namespace, fallback=False)
    to = to or row.version
    before = await store.get_resource_version_row(ctx.tenant_id, kind, name, from_, namespace=namespace)
    after = await store.get_resource_version_row(ctx.tenant_id, kind, name, to, namespace=namespace)
    return VersionDiffOut(kind=kind, name=name, from_version=from_, to_version=to, changes=diff(_as_written(before), _as_written(after)))


async def _rollback(ctx: Ctx, kind: str, name: str, namespace: str, body: RollbackIn) -> Any:
    store = ctx.engine.store
    row = await store.get_resource_row(ctx.tenant_id, kind, name, namespace=namespace, fallback=False)
    target = await store.get_resource_version_row(ctx.tenant_id, kind, name, body.version, namespace=namespace)
    if body.dry_run:
        current = await store.get_resource_version_row(ctx.tenant_id, kind, name, row.version, namespace=namespace)
        return VersionDiffOut(kind=kind, name=name, from_version=row.version, to_version=body.version,
                              changes=diff(_as_written(current), _as_written(target)))
    meta = target.document.get("metadata", {})
    restored = ResourceIn.model_validate(
        {"metadata": {"description": meta.get("description"), "labels": meta.get("labels") or {}},
         "spec": target.source if target.source is not None else target.document["spec"]}
    )
    resource = build_resource(kind, name, restored)
    new, outcome = await store.apply_resource(
        ctx.tenant_id, resource, actor=ctx.actor, namespace=namespace, source=strip_nulls(restored.spec),
        message=body.message or f"Roll back to version {body.version}",
    )
    if outcome != "unchanged":
        await ctx.audit(f"{kind.lower()}.rolled_back", f"{new.namespace}/{name}", namespace=new.namespace,
                        version=new.version, restored=body.version)
    return await to_out(ctx, new)


# --------------------------------------------------------------------------- apply


@router.post("/apply", tags=["resources"], response_model=list[ApplyResult], summary="Apply multi-document YAML or JSON")
async def apply(request: Request, ctx: CtxDep, namespace: str = "default", dry_run: bool = False,
                message: str | None = MessageQuery) -> Any:
    raw = (await request.body()).decode()
    try:
        docs = [d for d in yaml.safe_load_all(raw) if d]
    except yaml.YAMLError as exc:
        raise InvalidRequest(f"could not parse body: {exc}") from exc
    flat: list[dict[str, Any]] = []
    for doc in docs:
        items = doc.get("items", []) if isinstance(doc, dict) and doc.get("kind") == "List" else [doc]
        flat.extend(items if isinstance(items, list) else [])
    results: list[ApplyResult] = []
    parsed: list[tuple[ResourceBase, str, dict[str, Any]]] = []
    for doc in flat:
        kind = str(doc.get("kind")) if isinstance(doc, dict) else "?"
        name = str((doc.get("metadata") or {}).get("name", "?")) if isinstance(doc, dict) else "?"
        ns = SHARED_NAMESPACE if kind in TENANT_SCOPED_KINDS else ((doc.get("metadata") or {}).get("namespace") or namespace)
        try:
            resource = parse_resource({**doc, "spec": strip_nulls(doc.get("spec"))})
            validate_semantics(resource)
        except (ValidationError, ValueError, InvalidRequest) as exc:
            message = "; ".join(f"{e['loc']}: {e['msg']}" for e in _errors(exc)) if isinstance(exc, ValidationError) else str(exc)
            results.append(ApplyResult(kind=kind, name=name, namespace=ns, outcome="invalid", error=message))
            continue
        plural = resource.plural
        if kind in TENANT_SCOPED_KINDS:
            await ctx.require(TENANT_VERBS[plural]["write"])
        else:
            await ctx.namespace(ns, f"{plural}:write")
        parsed.append((resource, ns, strip_nulls(doc.get("spec") or {})))
    if any(r.outcome == "invalid" for r in results):
        raise InvalidRequest("some documents are invalid; nothing was applied", details={"results": [r.model_dump() for r in results]})
    for resource, ns, source in parsed:
        if dry_run:
            existing = await ctx.engine.store.find_resource(ctx.tenant_id, resource.to_document()["kind"], resource.metadata.name, namespace=ns)
            outcome = "unchanged" if existing and existing.to_document() == {**resource.to_document(), "metadata": existing.to_document()["metadata"]} else ("updated" if existing else "created")
            results.append(ApplyResult(kind=resource.to_document()["kind"], name=resource.metadata.name, namespace=ns, outcome=outcome))  # type: ignore[arg-type]
            continue
        row, outcome = await ctx.engine.store.apply_resource(
            ctx.tenant_id, resource, actor=ctx.actor, namespace=ns, source=source, message=message
        )
        if outcome != "unchanged":
            await ctx.audit(f"{row.kind.lower()}.{outcome}", f"{row.namespace}/{row.name}", namespace=row.namespace, version=row.version)
        results.append(ApplyResult(kind=row.kind, name=row.name, namespace=row.namespace, outcome=outcome, version=row.version))  # type: ignore[arg-type]
    return results


# --------------------------------------------------------------------------- secrets


@router.get("/namespaces/{namespace}/secrets", tags=["secrets"], response_model=list[SecretOut])
async def list_secrets(namespace: str, ctx: CtxDep) -> Any:
    await ctx.namespace(namespace, "secrets:read")
    return await ctx.engine.store.list_secrets(ctx.tenant_id, namespace=namespace)


@router.put("/namespaces/{namespace}/secrets/{name}", tags=["secrets"], response_model=SecretOut)
async def put_secret(namespace: str, name: str, body: SecretIn, ctx: CtxDep, response: Response) -> Any:
    await ctx.namespace(namespace, "secrets:write")
    if not any(row.name == name for row in await ctx.engine.store.list_secrets(ctx.tenant_id, namespace=namespace)):
        response.status_code = 201
    with use_namespace(namespace):
        await ctx.engine.secrets.writable().put(ctx.tenant_id, name, body.value)
    await ctx.audit("secret.set", f"{namespace}/{name}", namespace=namespace)
    for row in await ctx.engine.store.list_secrets(ctx.tenant_id, namespace=namespace):
        if row.name == name:
            return row
    raise NotFound(name)  # pragma: no cover


@router.get("/namespaces/{namespace}/secrets/{name}", tags=["secrets"], response_model=SecretOut, summary="Secret metadata (never the value)")
async def get_secret(namespace: str, name: str, ctx: CtxDep) -> Any:
    await ctx.namespace(namespace, "secrets:read")
    for row in await ctx.engine.store.list_secrets(ctx.tenant_id, namespace=namespace):
        if row.name == name:
            return row
    raise NotFound(f"secret '{name}' not found in namespace '{namespace}'")


@router.delete("/namespaces/{namespace}/secrets/{name}", status_code=204, tags=["secrets"])
async def delete_secret(namespace: str, name: str, ctx: CtxDep) -> None:
    await ctx.namespace(namespace, "secrets:write")
    await ctx.engine.store.delete_secret(ctx.tenant_id, name, namespace=namespace)
    await ctx.audit("secret.deleted", f"{namespace}/{name}", namespace=namespace)


# --------------------------------------------------------------------------- connections


async def _connection(ctx: Ctx, namespace: str, name: str) -> Connection:
    row = await ctx.engine.store.get_resource_row(ctx.tenant_id, "Connection", name, namespace=namespace)
    return Connection.model_validate(row.document)


@router.post("/namespaces/{namespace}/connections/{name}/test", tags=["connections"])
async def test_connection(namespace: str, name: str, ctx: CtxDep) -> dict[str, Any]:
    await ctx.namespace(namespace, "connections:use")
    with use_namespace(namespace):
        conn = await ctx.engine.integrations.connection(ctx.tenant_id, name)
        try:
            return await conn.integration.test_connection(conn)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


@router.get("/namespaces/{namespace}/connections/{name}/webhook", tags=["connections"])
async def connection_webhook(namespace: str, name: str, ctx: CtxDep) -> dict[str, Any]:
    await ctx.namespace(namespace, "connections:read")
    with use_namespace(namespace):
        url = await ctx.engine.integrations.webhook_url(ctx.tenant_id, name)
        conn = await _connection(ctx, namespace, name)
    integration = ctx.engine.integrations.get(conn.spec.integration)
    return {"url": url, "instructions": integration.webhook_setup_instructions(url)}


@router.get("/namespaces/{namespace}/connections/{name}/actions", tags=["connections"],
            summary="Actions and queryable resources this connection offers")
async def list_actions(namespace: str, name: str, ctx: CtxDep) -> dict[str, Any]:
    """The integration catalogue lists static actions; dynamic integrations (remote) only know theirs per connection."""
    await ctx.namespace(namespace, "connections:read")
    conn = await _connection(ctx, namespace, name)
    integration = ctx.engine.integrations.get(conn.spec.integration)
    actions, resources = integration.actions, integration.resources
    if not actions and not resources:  # dynamic: ask the connection (needs its secrets)
        with use_namespace(namespace):
            connection = await ctx.engine.integrations.connection(ctx.tenant_id, name)
            actions, resources = await integration.actions_for(connection), await integration.resources_for(connection)
    return {"integration": conn.spec.integration, "actions": describe_actions(actions), "resources": describe_resources(resources)}


@router.post("/namespaces/{namespace}/connections/{name}/actions/{action}", tags=["connections"])
async def call_action(namespace: str, name: str, action: str, body: ActionCallIn, ctx: CtxDep) -> dict[str, Any]:
    await ctx.namespace(namespace, "connections:use")
    conn = await _connection(ctx, namespace, name)
    integration = ctx.engine.integrations.get(conn.spec.integration)
    actions = integration.actions
    if not actions:  # dynamic integrations (remote) list their actions per connection
        with use_namespace(namespace):
            actions = await integration.actions_for(await ctx.engine.integrations.connection(ctx.tenant_id, name))
    definition = actions.get(action)
    if definition is None:
        raise NotFound(f"{conn.spec.integration} has no action '{action}'")
    initiator = await ctx.engine.identities.resolve(ctx.tenant_id, actor=ctx.actor, source="api", email=ctx.principal.email)
    with use_namespace(namespace):
        result = await ctx.engine.integrations.call(
            ctx.tenant_id, f"{conn.spec.integration}.{action}", body.params, connection=name, initiator=initiator
        )
    if definition.mutating:
        await ctx.audit("connection.action", f"{namespace}/{name}", action=action)
    return result


@router.post("/namespaces/{namespace}/connections/{name}/resources/{resource}/query", tags=["connections"])
async def query_resource(namespace: str, name: str, resource: str, body: QueryIn, ctx: CtxDep) -> dict[str, Any]:
    await ctx.namespace(namespace, "connections:use")
    with use_namespace(namespace):
        page = await ctx.engine.integrations.query(ctx.tenant_id, name, resource, body.filter, cursor=body.cursor, limit=body.limit)
    return page.model_dump()


# --------------------------------------------------------------------------- triggers


async def _trigger(ctx: Ctx, namespace: str, name: str) -> Trigger:
    row = await ctx.engine.store.get_resource_row(ctx.tenant_id, "Trigger", name, namespace=namespace, fallback=False)
    return Trigger.model_validate(row.document)


@router.post("/namespaces/{namespace}/triggers/{name}/test", tags=["triggers"], summary="Dry-run a trigger against an event")
async def test_trigger(namespace: str, name: str, body: TriggerTestIn, ctx: CtxDep) -> dict[str, Any]:
    await ctx.namespace(namespace, "triggers:read")
    trigger = await _trigger(ctx, namespace, name)
    event = IntegrationEvent(type=body.event_type or trigger.spec.event.replace("*", "test"), id=new_id("evt"), data=body.data)
    outcome = ctx.engine.triggers.evaluate(trigger, event)
    if outcome["matched"] and trigger.spec.triage is not None:
        with use_namespace(namespace):
            outcome["triage"] = await ctx.engine.triggers.triage(ctx.tenant_id, trigger, event)
        outcome["matched"] = outcome["triage"]["passed"]
    return outcome


@router.post("/namespaces/{namespace}/triggers/{name}/fire", tags=["triggers"], summary="Fire a trigger now with an optional payload")
async def fire_trigger(namespace: str, name: str, body: TriggerTestIn, ctx: CtxDep) -> dict[str, Any]:
    await ctx.namespace(namespace, "triggers:fire")
    with use_namespace(namespace):
        result = await ctx.engine.triggers.fire_manual(ctx.tenant_id, name, body.data)
    await ctx.audit("trigger.fired", f"{namespace}/{name}", result=result.get("status"))
    return result


@router.get("/namespaces/{namespace}/triggers/{name}/deliveries", tags=["triggers"], response_model=list[DeliveryOut])
async def trigger_deliveries(namespace: str, name: str, ctx: CtxDep, limit: int = Query(50, le=500)) -> Any:
    await ctx.namespace(namespace, "triggers:read")
    return await ctx.engine.store.list_deliveries(ctx.tenant_id, name, limit, namespace=namespace)


@router.get("/schedule-preview", tags=["triggers"], summary="Next fire times for a cron expression")
async def schedule_preview(ctx: CtxDep, cron: str, timezone: str = "UTC", count: int = Query(5, le=50)) -> dict[str, Any]:
    try:
        return {"cron": cron, "timezone": timezone, "next": await ctx.engine.triggers.schedule_preview(cron, timezone, count)}
    except Exception as exc:
        raise InvalidRequest(str(exc)) from exc
