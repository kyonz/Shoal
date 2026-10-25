"""Secret providers.

Workflows and connections reference secrets by name; the value is resolved at
the last moment through an ordered chain of providers (``db`` then ``env`` by
default). Providers are plugins (``awm.secrets`` entry point).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from typing import TYPE_CHECKING, Protocol

from cryptography.fernet import Fernet, InvalidToken

if TYPE_CHECKING:
    from awm.engine import Engine


class SecretProvider(Protocol):
    name: str
    writable: bool

    async def get(self, tenant_id: str, name: str) -> str | None: ...


class Cipher:
    """Fernet (AES-128-CBC + HMAC) keyed from ``AWM_SECRET_KEY``."""

    def __init__(self, secret_key: str):
        digest = hashlib.sha256(b"awm-secrets:" + secret_key.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("secret could not be decrypted (wrong AWM_SECRET_KEY?)") from exc


class DatabaseSecretProvider:
    """Secrets stored encrypted in the AWM database, managed through the API."""

    name = "db"
    writable = True

    def setup(self, engine: Engine) -> None:
        self.store = engine.store
        self.cipher = Cipher(engine.settings.secret_key.get_secret_value())

    async def get(self, tenant_id: str, name: str) -> str | None:
        token = await self.store.get_secret(tenant_id, name)
        return None if token is None else self.cipher.decrypt(token)

    async def put(self, tenant_id: str, name: str, value: str) -> None:
        await self.store.put_secret(tenant_id, name, self.cipher.encrypt(value))

    async def delete(self, tenant_id: str, name: str) -> None:
        await self.store.delete_secret(tenant_id, name)


def _env_name(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", part).upper()


class EnvSecretProvider:
    """Read-only secrets from the environment.

    Looks up ``AWM_SECRET_<TENANT>__<NAME>`` and then ``AWM_SECRET_<NAME>``
    (shared by all tenants; intended for single-tenant installs).
    """

    name = "env"
    writable = False

    def setup(self, engine: Engine) -> None:
        self.store = engine.store

    async def get(self, tenant_id: str, name: str) -> str | None:
        tenant = await self.store.get_tenant(tenant_id)
        return os.environ.get(f"AWM_SECRET_{_env_name(tenant.slug)}__{_env_name(name)}") or os.environ.get(
            f"AWM_SECRET_{_env_name(name)}"
        )


class SecretManager:
    def __init__(self, engine: Engine, order: list[str] | None = None):
        self.engine = engine
        self.order = order or ["db", "env"]
        self.cipher = Cipher(engine.settings.secret_key.get_secret_value())  # for platform credentials (e.g. federation links)

    def _providers(self) -> list[SecretProvider]:
        return [self.engine.plugin("secrets", name) for name in self.order]

    async def get(self, tenant_id: str, name: str) -> str | None:
        for provider in self._providers():
            value = await provider.get(tenant_id, name)
            if value is not None:
                return value
        return None

    async def resolve(self, tenant_id: str, names: set[str] | list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in names:
            value = await self.get(tenant_id, name)
            if value is not None:
                out[name] = value
        return out

    def writable(self) -> DatabaseSecretProvider:
        for provider in self._providers():
            if provider.writable:
                return provider  # type: ignore[return-value]
        raise RuntimeError("no writable secret provider configured")


class Redactor:
    """Replaces known secret values in text and nested structures."""

    def __init__(self, values: list[str] | None = None):
        self.values = sorted({v for v in (values or []) if v and len(v) >= 4}, key=len, reverse=True)

    def add(self, *values: str) -> None:
        self.values = sorted({*self.values, *(v for v in values if v and len(v) >= 4)}, key=len, reverse=True)

    def __call__(self, value):  # type: ignore[no-untyped-def]
        if not self.values:
            return value
        if isinstance(value, str):
            for secret in self.values:
                value = value.replace(secret, "***")
            return value
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v) for v in value]
        return value
