"""Permission catalogue and built-in roles (architecture §11).

Permissions are ``resource:verb`` strings. Patterns may use ``*`` for either
part (``runs:*``, ``*:read``, ``*``). Tenant-level permissions are only ever
granted by tenant-wide role bindings; a namespace binding never confers them.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from shoal.core.models import KIND_BY_PLURAL

TENANT_PLURALS = ("identities", "blueprints", "gateways", "model-policies", "skill-sources", "eval-platforms", "custom-integrations")
RESOURCE_PLURALS = sorted(p for p in KIND_BY_PLURAL if p not in TENANT_PLURALS)

NAMESPACE_PERMISSIONS: dict[str, str] = {
    **{f"{p}:read": f"View {p}" for p in RESOURCE_PLURALS},
    **{f"{p}:write": f"Create and update {p}" for p in RESOURCE_PLURALS},
    **{f"{p}:delete": f"Delete {p}" for p in RESOURCE_PLURALS},
    "runs:read": "View runs, steps and events",
    "runs:create": "Start workflow runs",
    "runs:cancel": "Cancel runs",
    "runs:approve": "Approve or reject steps waiting for a human",
    "runs:signal": "Send signals (data) to running workflows",
    "campaigns:launch": "Launch campaign runs",
    "campaigns:manage": "Pause, resume, cancel and retry campaign runs",
    "connections:use": "Call integration actions and query resources through a connection",
    "triggers:fire": "Fire triggers manually and send test events",
    "secrets:read": "List secret names (values are never readable)",
    "secrets:write": "Set and delete secrets",
    "blueprints:adopt": "Adopt blueprints from the catalogue into the namespace",
    "skills:import": "Import skills from skill sources (marketplaces) into the namespace",
    "members:read": "View namespace members",
    "members:manage": "Add and remove namespace members",
    "evals:run": "Run evaluations",
}

TENANT_PERMISSIONS: dict[str, str] = {
    "tenant:manage": "Change tenant settings",
    "namespaces:create": "Create namespaces",
    "namespaces:delete": "Delete namespaces",
    "users:read": "View users and groups",
    "users:manage": "Create, update and deactivate users, groups and service accounts",
    "roles:manage": "Create custom roles and grant tenant-wide roles",
    "auth:manage": "Configure OIDC providers and SCIM provisioning",
    "identities:read": "View identities",
    "identities:write": "Manage identities used for attribution",
    "blueprints:read": "Browse the blueprint catalogue",
    "blueprints:publish": "Publish blueprints to the tenant catalogue",
    "blueprints:feature": "Feature blueprints in the catalogue",
    "gateways:read": "View LLM gateways",
    "gateways:manage": "Create, update and delete LLM gateways (tenant-wide)",
    "model-policies:read": "View model policies",
    "model-policies:manage": "Create, update and delete model policies (tenant-wide)",
    "skill-sources:read": "View the tenant's skill sources (marketplaces)",
    "skill-sources:manage": "Add, configure and remove skill sources (tenant-wide)",
    "custom-integrations:read": "View the integrations the organisation registered",
    "custom-integrations:manage": "Register, configure and remove custom integrations (tenant-wide)",
    "eval-platforms:read": "View the eval platforms (Braintrust, Langfuse, ...) connected for the tenant",
    "eval-platforms:manage": "Connect, configure and remove eval platforms (tenant-wide)",
    "audit:read": "Read the audit log",
    "api-keys:manage": "Manage every API key in the tenant",
    "scim:provision": "Provision users and groups over SCIM",
    "federation:manage": "Join this tenant to a federation hub and set what the hub may do",
}

ALL_PERMISSIONS: dict[str, str] = {**NAMESPACE_PERMISSIONS, **TENANT_PERMISSIONS}


@dataclass(frozen=True)
class RoleDef:
    name: str
    description: str
    permissions: tuple[str, ...]
    builtin: bool = True


READ_ALL = ("*:read",)
# Registering a cloud role and granting workflows use of it is for namespace admins: an editor
# who could do it could grant their own workflow access to the role.
ADMIN_ONLY_PLURALS = ("cloud-identities",)
EDITABLE_PLURALS = [p for p in RESOURCE_PLURALS if p not in ADMIN_ONLY_PLURALS]
WRITE_RESOURCES = tuple(f"{p}:write" for p in EDITABLE_PLURALS) + tuple(f"{p}:delete" for p in EDITABLE_PLURALS)
OPERATE = (
    "runs:create",
    "runs:cancel",
    "runs:approve",
    "runs:signal",
    "campaigns:launch",
    "campaigns:manage",
    "connections:use",
    "triggers:fire",
    "evals:run",
)

BUILTIN_ROLES: dict[str, RoleDef] = {
    r.name: r
    for r in (
        RoleDef("tenant-admin", "Full control of the tenant, its namespaces, users and settings.", ("*",)),
        RoleDef(
            "namespace-admin",
            "Full control inside a namespace, including its members and secrets.",
            tuple(NAMESPACE_PERMISSIONS),
        ),
        RoleDef(
            "editor",
            "Build and run: edit agents, workflows, connections and triggers, and operate runs.",
            (*READ_ALL, *WRITE_RESOURCES, *OPERATE, "secrets:write", "blueprints:adopt", "skills:import"),
        ),
        RoleDef("operator", "Run and supervise: start, approve and cancel runs and campaigns.", (*READ_ALL, *OPERATE)),
        RoleDef("viewer", "Read-only access.", READ_ALL),
    )
}


def matches(pattern: str, permission: str) -> bool:
    if pattern == "*" or pattern == permission:
        return True
    p_res, _, p_verb = pattern.partition(":")
    res, _, verb = permission.partition(":")
    return fnmatch.fnmatchcase(res, p_res or "*") and fnmatch.fnmatchcase(verb, p_verb or "*")


def granted(patterns: set[str] | list[str] | tuple[str, ...], permission: str) -> bool:
    return any(matches(p, permission) for p in patterns)


def expand(patterns: set[str] | list[str] | tuple[str, ...], *, tenant_scope: bool) -> set[str]:
    """Concrete permissions a set of patterns grants (for UIs and `GET /v1/me/permissions`)."""
    catalogue = ALL_PERMISSIONS if tenant_scope else NAMESPACE_PERMISSIONS
    return {perm for perm in catalogue if granted(patterns, perm)}
