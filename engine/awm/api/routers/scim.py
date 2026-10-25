"""SCIM 2.0 provisioning (RFC 7643 / 7644) for users and groups.

Identity providers (Okta, Entra ID, OneLogin, JumpCloud, ...) call these
endpoints with a tenant API key that holds ``scim:provision`` (create one for a
``scim`` service account). Deactivating a user revokes their API keys and
blocks sign-in; group membership drives role bindings on groups.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from awm.api.deps import Ctx, CtxDep
from awm.core.errors import AWMError, Conflict, InvalidRequest, NotFound
from awm.store import tables as t

router = APIRouter(prefix="/scim/v2", tags=["scim"])

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
MEDIA = "application/scim+json"


def scim(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, media_type=MEDIA)


def scim_error(exc: AWMError) -> JSONResponse:
    body = {"schemas": [ERROR_SCHEMA], "status": str(exc.status_code), "detail": exc.message}
    if isinstance(exc, Conflict):
        body["scimType"] = "uniqueness"
    elif isinstance(exc, InvalidRequest):
        body["scimType"] = "invalidValue"
    return scim(body, exc.status_code)


async def _auth(ctx: Ctx) -> None:
    await ctx.require("scim:provision")


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/scim/v2"


def _parse_filter(expr: str | None) -> tuple[str, str] | None:
    if not expr:
        return None
    match = re.fullmatch(r'\s*(\w+(?:\.\w+)?)\s+eq\s+"([^"]*)"\s*', expr)
    if not match:
        raise InvalidRequest(f"unsupported filter '{expr}' (only `attr eq \"value\"`)")
    return match.group(1), match.group(2)


# --------------------------------------------------------------------------- users


async def _user_doc(ctx: Ctx, request: Request, user: t.User) -> dict[str, Any]:
    groups = []
    for gid in await ctx.engine.directory.groups_of(user.id):
        group = await ctx.engine.directory.get_group(ctx.tenant_id, gid)
        groups.append({"value": gid, "display": group.display_name, "$ref": f"{_base(request)}/Groups/{gid}"})
    return {
        "schemas": [USER_SCHEMA],
        "id": user.id,
        "externalId": user.external_id,
        "userName": user.username,
        "displayName": user.display_name,
        "name": {"formatted": user.display_name},
        "emails": [{"value": user.email, "primary": True, "type": "work"}] if user.email else [],
        "active": user.active,
        "groups": groups,
        "meta": {
            "resourceType": "User",
            "created": user.created_at.isoformat(),
            "lastModified": user.updated_at.isoformat(),
            "location": f"{_base(request)}/Users/{user.id}",
        },
    }


def _user_values(doc: dict[str, Any]) -> dict[str, Any]:
    emails = doc.get("emails") or []
    primary = next((e for e in emails if e.get("primary")), emails[0] if emails else None)
    name = doc.get("name") or {}
    display = doc.get("displayName") or name.get("formatted") or " ".join(
        p for p in (name.get("givenName"), name.get("familyName")) if p
    )
    values: dict[str, Any] = {
        "email": primary.get("value") if primary else None,
        "display_name": display or doc.get("userName"),
        "external_id": doc.get("externalId"),
    }
    if "active" in doc:
        values["active"] = bool(doc["active"])
    return values


@router.get("/Users")
async def list_users(request: Request, ctx: CtxDep, filter: str | None = None, startIndex: int = 1, count: int = 100) -> Any:  # noqa: N803
    try:
        await _auth(ctx)
        directory = ctx.engine.directory
        parsed = _parse_filter(filter)
        if parsed:
            attr, value = parsed
            column = {"userName": "username", "externalId": "external_id", "emails.value": "email", "emails": "email"}.get(attr)
            if column is None:
                raise InvalidRequest(f"cannot filter on '{attr}'")
            user = await directory.find_user(ctx.tenant_id, **{column: value}, kind="user")
            users, total = ([user] if user else []), (1 if user else 0)
        else:
            users, total = await directory.list_users(ctx.tenant_id, kind="user", offset=max(startIndex - 1, 0), limit=min(count, 500))
        return scim({
            "schemas": [LIST_SCHEMA], "totalResults": total, "startIndex": startIndex, "itemsPerPage": len(users),
            "Resources": [await _user_doc(ctx, request, u) for u in users],
        })
    except AWMError as exc:
        return scim_error(exc)


@router.post("/Users")
async def create_user(request: Request, ctx: CtxDep) -> Any:
    try:
        await _auth(ctx)
        doc = await request.json()
        if not doc.get("userName"):
            raise InvalidRequest("userName is required")
        values = _user_values(doc)
        user = await ctx.engine.directory.create_user(
            ctx.tenant_id, doc["userName"], email=values["email"], display_name=values["display_name"],
            external_id=values["external_id"], active=values.get("active", True),
        )
        await ctx.audit("scim.user.created", user.username)
        return scim(await _user_doc(ctx, request, user), 201)
    except AWMError as exc:
        return scim_error(exc)


@router.get("/Users/{user_id}")
async def get_user(user_id: str, request: Request, ctx: CtxDep) -> Any:
    try:
        await _auth(ctx)
        return scim(await _user_doc(ctx, request, await ctx.engine.directory.get_user(ctx.tenant_id, user_id)))
    except AWMError as exc:
        return scim_error(exc)


@router.put("/Users/{user_id}")
async def replace_user(user_id: str, request: Request, ctx: CtxDep) -> Any:
    try:
        await _auth(ctx)
        doc = await request.json()
        values = _user_values(doc)
        if doc.get("userName"):
            values["username"] = doc["userName"]
        user = await ctx.engine.directory.update_user(ctx.tenant_id, user_id, **values)
        await ctx.audit("scim.user.replaced", user.username, active=user.active)
        return scim(await _user_doc(ctx, request, user))
    except AWMError as exc:
        return scim_error(exc)


@router.patch("/Users/{user_id}")
async def patch_user(user_id: str, request: Request, ctx: CtxDep) -> Any:
    try:
        await _auth(ctx)
        ops = (await request.json()).get("Operations") or []
        values: dict[str, Any] = {}
        for op in ops:
            if op.get("op", "").lower() not in ("replace", "add"):
                continue
            path, value = op.get("path"), op.get("value")
            items = value.items() if path is None and isinstance(value, dict) else [(path, value)]
            for key, val in items:
                key = (key or "").strip()
                if key == "active":
                    values["active"] = val if isinstance(val, bool) else str(val).lower() == "true"
                elif key == "userName":
                    values["username"] = val
                elif key in ("displayName", "name.formatted"):
                    values["display_name"] = val
                elif key.startswith("emails"):
                    values["email"] = val if isinstance(val, str) else (val[0].get("value") if val else None)
                elif key == "externalId":
                    values["external_id"] = val
        user = await ctx.engine.directory.update_user(ctx.tenant_id, user_id, **values)
        await ctx.audit("scim.user.patched", user.username, changes=list(values))
        return scim(await _user_doc(ctx, request, user))
    except AWMError as exc:
        return scim_error(exc)


@router.delete("/Users/{user_id}", status_code=204)
async def delete_user(user_id: str, ctx: CtxDep) -> Response:
    try:
        await _auth(ctx)
        await ctx.engine.directory.delete_user(ctx.tenant_id, user_id)
        await ctx.audit("scim.user.deleted", user_id)
        return Response(status_code=204)
    except AWMError as exc:
        return scim_error(exc)


# --------------------------------------------------------------------------- groups


async def _group_doc(ctx: Ctx, request: Request, group: t.Group) -> dict[str, Any]:
    members = []
    for uid in await ctx.engine.directory.group_members(group.id):
        try:
            user = await ctx.engine.directory.get_user(ctx.tenant_id, uid)
        except NotFound:
            continue
        members.append({"value": uid, "display": user.display_name, "$ref": f"{_base(request)}/Users/{uid}"})
    return {
        "schemas": [GROUP_SCHEMA],
        "id": group.id,
        "externalId": group.external_id,
        "displayName": group.display_name,
        "members": members,
        "meta": {
            "resourceType": "Group",
            "created": group.created_at.isoformat(),
            "lastModified": group.updated_at.isoformat(),
            "location": f"{_base(request)}/Groups/{group.id}",
        },
    }


@router.get("/Groups")
async def list_groups(request: Request, ctx: CtxDep, filter: str | None = None, startIndex: int = 1, count: int = 100) -> Any:  # noqa: N803
    try:
        await _auth(ctx)
        directory = ctx.engine.directory
        parsed = _parse_filter(filter)
        if parsed:
            attr, value = parsed
            if attr != "displayName":
                raise InvalidRequest(f"cannot filter on '{attr}'")
            group = await directory.find_group(ctx.tenant_id, value)
            groups, total = ([group] if group else []), (1 if group else 0)
        else:
            groups, total = await directory.list_groups(ctx.tenant_id, offset=max(startIndex - 1, 0), limit=min(count, 500))
        return scim({
            "schemas": [LIST_SCHEMA], "totalResults": total, "startIndex": startIndex, "itemsPerPage": len(groups),
            "Resources": [await _group_doc(ctx, request, g) for g in groups],
        })
    except AWMError as exc:
        return scim_error(exc)


@router.post("/Groups")
async def create_group(request: Request, ctx: CtxDep) -> Any:
    try:
        await _auth(ctx)
        doc = await request.json()
        directory = ctx.engine.directory
        group = await directory.create_group(ctx.tenant_id, doc["displayName"], external_id=doc.get("externalId"), source="scim")
        members = [m["value"] for m in doc.get("members") or []]
        if members:
            await directory.set_group_members(ctx.tenant_id, group.id, members)
        await ctx.audit("scim.group.created", group.display_name)
        return scim(await _group_doc(ctx, request, group), 201)
    except AWMError as exc:
        return scim_error(exc)
    except KeyError:
        return scim_error(InvalidRequest("displayName is required"))


@router.get("/Groups/{group_id}")
async def get_group(group_id: str, request: Request, ctx: CtxDep) -> Any:
    try:
        await _auth(ctx)
        return scim(await _group_doc(ctx, request, await ctx.engine.directory.get_group(ctx.tenant_id, group_id)))
    except AWMError as exc:
        return scim_error(exc)


@router.put("/Groups/{group_id}")
async def replace_group(group_id: str, request: Request, ctx: CtxDep) -> Any:
    try:
        await _auth(ctx)
        doc = await request.json()
        directory = ctx.engine.directory
        group = await directory.update_group(ctx.tenant_id, group_id, display_name=doc.get("displayName"), external_id=doc.get("externalId"))
        await directory.set_group_members(ctx.tenant_id, group_id, [m["value"] for m in doc.get("members") or []])
        return scim(await _group_doc(ctx, request, group))
    except AWMError as exc:
        return scim_error(exc)


@router.patch("/Groups/{group_id}")
async def patch_group(group_id: str, request: Request, ctx: CtxDep) -> Any:
    try:
        await _auth(ctx)
        directory = ctx.engine.directory
        group = await directory.get_group(ctx.tenant_id, group_id)
        for op in (await request.json()).get("Operations") or []:
            kind = op.get("op", "").lower()
            path = (op.get("path") or "").strip()
            value = op.get("value")
            if path.startswith("members"):
                match = re.search(r'value eq "([^"]+)"', path)
                ids = [match.group(1)] if match else [m["value"] for m in (value or [])]
                if kind == "add":
                    await directory.add_group_members(ctx.tenant_id, group_id, ids)
                elif kind == "remove":
                    if match or value:
                        await directory.remove_group_members(ctx.tenant_id, group_id, ids)
                    else:
                        await directory.set_group_members(ctx.tenant_id, group_id, [])
                elif kind == "replace":
                    await directory.set_group_members(ctx.tenant_id, group_id, ids)
            elif kind in ("replace", "add"):
                values = value if path == "" and isinstance(value, dict) else {path: value}
                if "displayName" in values:
                    group = await directory.update_group(ctx.tenant_id, group_id, display_name=values["displayName"])
                if "externalId" in values:
                    group = await directory.update_group(ctx.tenant_id, group_id, external_id=values["externalId"])
                if "members" in values:
                    await directory.set_group_members(ctx.tenant_id, group_id, [m["value"] for m in values["members"] or []])
        await ctx.audit("scim.group.patched", group.display_name)
        return scim(await _group_doc(ctx, request, await directory.get_group(ctx.tenant_id, group_id)))
    except AWMError as exc:
        return scim_error(exc)


@router.delete("/Groups/{group_id}", status_code=204)
async def delete_group(group_id: str, ctx: CtxDep) -> Response:
    try:
        await _auth(ctx)
        await ctx.engine.directory.delete_group(ctx.tenant_id, group_id)
        await ctx.audit("scim.group.deleted", group_id)
        return Response(status_code=204)
    except AWMError as exc:
        return scim_error(exc)


# --------------------------------------------------------------------------- discovery


@router.get("/ServiceProviderConfig")
async def service_provider_config() -> Any:
    return scim({
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 500},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {"type": "oauthbearertoken", "name": "Bearer token", "description": "A tenant API key with scim:provision", "primary": True}
        ],
    })


@router.get("/ResourceTypes")
async def resource_types() -> Any:
    return scim({
        "schemas": [LIST_SCHEMA],
        "totalResults": 2,
        "Resources": [
            {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": "User", "name": "User", "endpoint": "/Users", "schema": USER_SCHEMA},
            {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": "Group", "name": "Group", "endpoint": "/Groups", "schema": GROUP_SCHEMA},
        ],
    })


@router.get("/Schemas")
async def schemas() -> Any:
    return scim({
        "schemas": [LIST_SCHEMA],
        "totalResults": 2,
        "Resources": [
            {"id": USER_SCHEMA, "name": "User", "attributes": [{"name": n, "type": "string"} for n in ("userName", "displayName", "externalId")] + [{"name": "active", "type": "boolean"}, {"name": "emails", "type": "complex", "multiValued": True}]},
            {"id": GROUP_SCHEMA, "name": "Group", "attributes": [{"name": "displayName", "type": "string"}, {"name": "members", "type": "complex", "multiValued": True}]},
        ],
    })
