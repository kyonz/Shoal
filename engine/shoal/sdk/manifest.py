"""Describe a Python integration as a remote-protocol manifest."""

from __future__ import annotations

from typing import Any

from shoal.integrations.base import Integration
from shoal.integrations.remote import PROTOCOL, RemoteManifest


def _overrides(cls: type[Integration], name: str) -> bool:
    return getattr(cls, name) is not getattr(Integration, name)


def manifest_data(cls: type[Integration], *, version: str | None = None) -> dict[str, Any]:
    catalog = cls.catalog()
    resources = {r.name: r for r in cls.resources.values()}
    return {
        **catalog,
        "protocol": PROTOCOL,
        "version": version or getattr(cls, "version", None),
        "resources": [{**r, "key_field": resources[r["name"]].key_field} for r in catalog["resources"]],
        "webhooks": _overrides(cls, "parse_webhook"),
        "test": _overrides(cls, "test_connection"),
    }


def manifest_for(integration: Integration | type[Integration], *, version: str | None = None) -> RemoteManifest:
    """The manifest an integration serves at ``GET /manifest`` (see :mod:`shoal.integrations.remote`)."""
    cls = integration if isinstance(integration, type) else type(integration)
    return RemoteManifest.model_validate(manifest_data(cls, version=version))
