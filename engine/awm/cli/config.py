"""The CLI's saved servers and credentials (``~/.config/awm/config.yaml``).

A *context* names a server, a token and defaults, like a kubeconfig context::

    current-context: prod
    contexts:
      prod:
        server: https://awm.example.com
        token: awm_...
        namespace: default

Flags win over ``AWM_SERVER`` / ``AWM_TOKEN`` / ``AWM_NAMESPACE`` /
``AWM_TENANT``, which win over the current context.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SERVER = "http://localhost:8080"


def config_path() -> Path:
    if explicit := os.environ.get("AWM_CONFIG"):
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "awm" / "config.yaml"


def load() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {"contexts": {}}
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("contexts", {})
    return data


def save(data: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The file holds tokens: create it private rather than chmod after writing.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        yaml.safe_dump(data, fh, sort_keys=False)
    return path


@dataclass
class Target:
    """Where and as whom a command talks to the API."""

    server: str
    token: str | None
    namespace: str
    tenant: str | None
    context: str | None


def resolve(
    *,
    server: str | None = None,
    token: str | None = None,
    namespace: str | None = None,
    tenant: str | None = None,
    context: str | None = None,
) -> Target:
    data = load()
    name = context or os.environ.get("AWM_CONTEXT") or data.get("current-context")
    saved: dict[str, Any] = data["contexts"].get(name, {}) if name else {}
    return Target(
        server=(server or os.environ.get("AWM_SERVER") or saved.get("server") or DEFAULT_SERVER).rstrip("/"),
        token=token or os.environ.get("AWM_TOKEN") or saved.get("token"),
        namespace=namespace or os.environ.get("AWM_NAMESPACE") or saved.get("namespace") or "default",
        tenant=tenant or os.environ.get("AWM_TENANT") or saved.get("tenant"),
        context=name if saved else None,
    )
