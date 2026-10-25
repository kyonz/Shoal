"""Signals: data sent into a running workflow, taken by ``wait`` steps with ``signal:``.

A signal comes from the API (``POST /v1/runs/{id}/signals/{name}``), from the run's
callback URL (``/v1/hooks/runs/{id}/{token}/signals/{name}``, for systems that only
know a URL), or from an integration event whose wake key a waiting step names.
Named signals are kept until a wait step takes them, so a reply that arrives before
the workflow reaches its wait is not lost. Each signal is taken by one step.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from shoal.core import clock
from shoal.core.errors import Conflict, InvalidRequest
from shoal.core.ids import new_id
from shoal.core.models import RunStatus
from shoal.integrations.base import IntegrationEvent
from shoal.store import tables as t

if TYPE_CHECKING:
    from shoal.engine import Engine

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
MAX_PAYLOAD = 256 * 1024


def signal_key(run_id: str, name: str) -> str:
    return f"signal:{run_id}:{name}"


def event_key(key: str) -> str:
    return f"event:{key}"


class Signals:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.store = engine.store

    # ------------------------------------------------------------------ callback URLs

    def token(self, tenant_id: str, run_id: str) -> str:
        key = self.engine.settings.secret_key.get_secret_value().encode()
        digest = hmac.new(key, f"signal:{tenant_id}:{run_id}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest[:24]).decode()

    def verify(self, run: t.Run, token: str) -> bool:
        return hmac.compare_digest(self.token(run.tenant_id, run.id), token)

    def callback_url(self, run: t.Run) -> str:
        """Base URL; append ``/<signal name>``. Anyone holding it can signal this run."""
        base = self.engine.settings.public_url.rstrip("/")
        return f"{base}/v1/hooks/runs/{run.id}/{self.token(run.tenant_id, run.id)}/signals"

    # ------------------------------------------------------------------ sending

    async def send(
        self, run: t.Run, name: str, payload: Any, *, source: str, sender: str = ""
    ) -> t.RunSignal:
        if not NAME.match(name):
            raise InvalidRequest("signal names are letters, digits, '_', '.', ':' and '-', up to 100 characters")
        if RunStatus(run.status).terminal:
            raise Conflict(f"run '{run.id}' has finished ({run.status})")
        return await self._add(run, name, payload, source=source, sender=sender, wake=[signal_key(run.id, name)])

    async def deliver_event(self, tenant_id: str, event: IntegrationEvent) -> list[str]:
        """Hand an integration event to steps waiting on one of its wake keys; returns woken run ids."""
        keys = [event_key(k) for k in event.wake_keys]
        if not keys:
            return []
        async with self.store.session() as s:
            waiting = (
                await s.execute(
                    select(t.WaitSubscription.run_id, t.WaitSubscription.key).where(
                        t.WaitSubscription.tenant_id == tenant_id, t.WaitSubscription.key.in_(keys)
                    )
                )
            ).all()
        payload = {"type": event.type, "id": event.id, "data": event.data, "actor": event.actor}
        woken: list[str] = []
        for run_id, key in sorted(set(waiting)):
            run = await self.store.get_run(tenant_id, run_id)
            await self._add(run, key, payload, source="event", sender=event.type, wake=[])
            woken.append(run_id)
        if woken:
            await self.store.wake_by_keys(tenant_id, keys)
        return woken

    async def _add(self, run: t.Run, name: str, payload: Any, *, source: str, sender: str, wake: list[str]) -> t.RunSignal:
        try:
            size = len(json.dumps(payload, default=str))
        except (TypeError, ValueError) as exc:
            raise InvalidRequest("signal payload must be JSON") from exc
        if size > MAX_PAYLOAD:
            raise InvalidRequest(f"signal payload is larger than {MAX_PAYLOAD // 1024} KiB")
        row = t.RunSignal(
            id=new_id("sig"), tenant_id=run.tenant_id, run_id=run.id, name=name,
            data={"payload": payload}, source=source, sender=sender[:200],
        )
        async with self.store.session() as s:
            s.add(row)
        await self.engine.events.emit(run, "signal.received", {"signal": row.id, "name": name, "source": source, "sender": row.sender})
        if wake:
            await self.store.wake_by_keys(run.tenant_id, wake)
        return row

    # ------------------------------------------------------------------ taking

    async def take(
        self, run_id: str, name: str, step_id: str, match: Callable[[Any], bool] | None = None
    ) -> t.RunSignal | None:
        """The oldest queued signal with this name (and matching ``match``), marked as taken by ``step_id``."""
        async with self.store.session() as s:
            queued = (
                await s.execute(
                    select(t.RunSignal)
                    .where(t.RunSignal.run_id == run_id, t.RunSignal.name == name, t.RunSignal.consumed_by.is_(None))
                    .order_by(t.RunSignal.created_at, t.RunSignal.id)
                )
            ).scalars().all()
        for row in queued:
            if match is not None and not match(row.data.get("payload")):
                continue
            async with self.store.session() as s:
                claimed = await s.execute(
                    update(t.RunSignal)
                    .where(t.RunSignal.id == row.id, t.RunSignal.consumed_by.is_(None))
                    .values(consumed_by=step_id, consumed_at=clock.now())
                )
            if claimed.rowcount:
                return row
        return None

    async def list(self, run_id: str) -> list[t.RunSignal]:
        async with self.store.session() as s:
            q = select(t.RunSignal).where(t.RunSignal.run_id == run_id).order_by(t.RunSignal.created_at, t.RunSignal.id)
            return list((await s.execute(q)).scalars().all())
