"""Per-user credentials: a person's own token or OAuth grant for one connection.

Secrets live at four scopes (architecture §11.4): the deployment (operator keys),
the tenant (`shared`: central gateways, org-wide connections), a namespace (a
team's connection secrets), and the user (this module). A connection with
``credentials: per_user`` (or ``per_user_or_shared``) fills its ``user_fields``
from the credential of the person the call acts for: the run's initiator, mapped
to a Shoal user through identities. Values are encrypted with the tenant cipher,
only their owner can set or replace them, and nobody can read them back.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from shoal.core import clock
from shoal.core.errors import credential_key
from shoal.core.ids import new_id
from shoal.store import tables as t

if TYPE_CHECKING:
    from shoal.engine import Engine


class UserCredentials:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.store = engine.store

    async def owner_for(self, tenant_id: str, initiator: dict[str, Any] | None) -> str | None:
        """The Shoal user a call acts for, or None (service accounts, unknown people)."""
        if not initiator:
            return None
        actor = initiator.get("actor") or ""
        if actor.startswith("user:"):
            return actor
        if initiator.get("identity"):
            identity = await self.store.find_resource(tenant_id, "Identity", initiator["identity"])
            for candidate in getattr(getattr(identity, "spec", None), "actors", []) or []:
                if candidate.startswith("user:"):
                    return candidate
        return None

    async def put(
        self, tenant_id: str, owner: str, namespace: str, connection: str, values: dict[str, Any], *,
        kind: str = "token", expires_at: datetime | None = None,
    ) -> t.UserCredential:
        cipher = self.engine.secrets.cipher
        fields = sorted(k for k in values if not k.startswith("_"))
        async with self.store.session() as s:
            row = (await s.execute(self._query(tenant_id, owner, namespace, connection))).scalar_one_or_none()
            if row is None:
                row = t.UserCredential(id=new_id("ucred"), tenant_id=tenant_id, owner=owner, namespace=namespace, connection=connection)
                s.add(row)
            row.kind, row.fields, row.expires_at = kind, fields, expires_at
            row.ciphertext = cipher.encrypt(json.dumps(values))
            row.updated_at = clock.now()
        await self.store.wake_by_keys(tenant_id, [credential_key(tenant_id, owner, namespace, connection)])
        return row

    async def get(self, tenant_id: str, owner: str, namespace: str, connection: str) -> dict[str, Any] | None:
        row = await self.row(tenant_id, owner, namespace, connection)
        if row is None:
            return None
        return json.loads(self.engine.secrets.cipher.decrypt(row.ciphertext))

    async def row(self, tenant_id: str, owner: str, namespace: str, connection: str) -> t.UserCredential | None:
        async with self.store.session() as s:
            return (await s.execute(self._query(tenant_id, owner, namespace, connection))).scalar_one_or_none()

    async def list(self, tenant_id: str, owner: str | None = None) -> list[t.UserCredential]:
        q = select(t.UserCredential).where(t.UserCredential.tenant_id == tenant_id)
        if owner is not None:
            q = q.where(t.UserCredential.owner == owner)
        async with self.store.session() as s:
            return list((await s.execute(q.order_by(t.UserCredential.namespace, t.UserCredential.connection))).scalars().all())

    async def delete(self, tenant_id: str, owner: str, namespace: str, connection: str) -> bool:
        async with self.store.session() as s:
            result = await s.execute(
                delete(t.UserCredential).where(
                    t.UserCredential.tenant_id == tenant_id, t.UserCredential.owner == owner,
                    t.UserCredential.namespace == namespace, t.UserCredential.connection == connection,
                )
            )
        return bool(result.rowcount)

    @staticmethod
    def _query(tenant_id: str, owner: str, namespace: str, connection: str) -> Any:
        return select(t.UserCredential).where(
            t.UserCredential.tenant_id == tenant_id, t.UserCredential.owner == owner,
            t.UserCredential.namespace == namespace, t.UserCredential.connection == connection,
        )
