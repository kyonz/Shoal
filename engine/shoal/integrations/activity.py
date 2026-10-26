"""A connection's usage history: what was delivered to its webhook, and which runs, steps and people used it.

Recording never fails the work it describes: a write that fails is logged and dropped.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from shoal.store import tables as t

if TYPE_CHECKING:
    from shoal.engine import Engine

log = logging.getLogger(__name__)

ACTIVITY_HEADER = "x-shoal-activity-id"  # carries a queued delivery's entry to the worker; stripped from senders
MAX_TRIGGERS = 50
MAX_RUNS = 50


def delivery_result(result: dict[str, Any]) -> dict[str, Any]:
    """What a processed delivery did: the triggers it fired (and their runs) and the waiting runs it woke."""
    out: dict[str, Any] = {
        "events": result.get("events", 0),
        "triggers": [
            {k: v for k, v in f.items() if k in ("trigger", "namespace", "status", "run_id", "campaign_id", "reason")}
            for f in (result.get("triggers") or [])[:MAX_TRIGGERS]
        ],
        "woken_runs": list(result.get("woken_runs") or [])[:MAX_RUNS],
    }
    if result.get("interactions"):
        out["interactions"] = len(result["interactions"])
    return out


class ConnectionActivityLog:
    def __init__(self, engine: Engine):
        self.engine = engine

    async def _add(self, row: t.ConnectionActivity) -> int | None:
        try:
            return await self.engine.store.add_connection_activity(row)
        except Exception as exc:
            log.warning("could not record activity for connection %s: %s", row.connection_name, exc)
            return None

    async def _update(self, activity_id: int, **values: Any) -> None:
        try:
            await self.engine.store.update_connection_activity(activity_id, **values)
        except Exception as exc:
            log.warning("could not update connection activity %s: %s", activity_id, exc)

    @staticmethod
    def _request(headers: dict[str, str], body: bytes) -> dict[str, Any]:
        return {
            "bytes": len(body),
            "user_agent": (headers.get("user-agent") or "")[:200] or None,
            "content_type": (headers.get("content-type") or "")[:100] or None,
        }

    async def delivery(
        self, endpoint: t.WebhookEndpoint, headers: dict[str, str], body: bytes, *, status: str,
        reason: str | None = None, auth: str | None = None, trigger: str | None = None,
        events: list[Any] | None = None, result: dict[str, Any] | None = None,
    ) -> int | None:
        """Record one delivery to a connection's webhook (``trigger``: the registered webhook's trigger)."""
        data: dict[str, Any] = {"request": self._request(headers, body)}
        if auth:
            data["auth"] = auth
        if trigger:
            data["registered_for"] = trigger
        if events is not None:
            data["event_types"] = sorted({e.type for e in events})[:20]
        if result is not None:
            data.update(delivery_result(result))
        return await self._add(t.ConnectionActivity(
            tenant_id=endpoint.tenant_id, namespace=endpoint.namespace, connection_name=endpoint.connection_name,
            kind="delivery", status=status, reason=(reason or None) and reason[:500], data=data,
        ))

    async def delivered(self, activity_id: int, *, events: list[Any], result: dict[str, Any]) -> None:
        """A queued delivery the worker processed."""
        await self._finish(activity_id, status="accepted", extra={
            "event_types": sorted({e.type for e in events})[:20], **delivery_result(result),
        })

    async def failed(self, activity_id: int, reason: str) -> None:
        """A queued delivery the worker gave up on."""
        await self._finish(activity_id, status="error", reason=reason[:500])

    async def _finish(self, activity_id: int, *, extra: dict[str, Any] | None = None, **values: Any) -> None:
        try:
            async with self.engine.store.session() as s:
                row = await s.get(t.ConnectionActivity, activity_id)
                data = dict(row.data or {}) if row is not None else {}
        except Exception as exc:
            log.warning("could not read connection activity %s: %s", activity_id, exc)
            return
        await self._update(activity_id, data={**data, **(extra or {})}, **values)

    async def use(
        self, tenant_id: str, namespace: str, connection: str, run_id: str, step_id: str | None, action: str | None
    ) -> None:
        """A run step used the connection (an action, an agent tool, sandbox credentials)."""
        try:
            await self.engine.store.record_connection_use(tenant_id, namespace, connection, run_id, step_id, action)
        except Exception as exc:
            log.warning("could not record use of connection %s by run %s: %s", connection, run_id, exc)

    async def call(
        self, tenant_id: str, namespace: str, connection: str, *, kind: str, actor: str | None, ok: bool,
        action: str | None = None, reason: str | None = None,
    ) -> None:
        """Someone used the connection directly: an action called from the API, or a connection test."""
        await self._add(t.ConnectionActivity(
            tenant_id=tenant_id, namespace=namespace, connection_name=connection, kind=kind,
            status="ok" if ok else "error", reason=(reason or None) and reason[:500], actor=actor,
            data={"action": action} if action else {},
        ))
