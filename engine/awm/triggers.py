"""Trigger evaluation: integration events and cron schedules start workflows or campaigns."""

from __future__ import annotations

import fnmatch
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from croniter import croniter

from awm.core import clock
from awm.core.expressions import ExpressionError, render, truthy
from awm.core.ids import new_id
from awm.core.models import RunStatus, Trigger
from awm.integrations.base import IntegrationEvent
from awm.store import tables as t

if TYPE_CHECKING:
    from awm.engine import Engine

log = logging.getLogger(__name__)


def next_fire(cron: str, tz: str, after: datetime) -> datetime:
    zone = ZoneInfo(tz)
    local = after.astimezone(zone)
    return croniter(cron, local).get_next(datetime).astimezone(ZoneInfo("UTC"))


def validate_cron(cron: str, tz: str) -> None:
    if not croniter.is_valid(cron):
        raise ValueError(f"invalid cron expression '{cron}'")
    ZoneInfo(tz)


class TriggerService:
    def __init__(self, engine: Engine):
        self.engine = engine

    async def _triggers(self, tenant_id: str) -> list[Trigger]:
        rows = await self.engine.store.list_resources(tenant_id, "Trigger")
        return [Trigger.model_validate(r.document) for r in rows]

    # ------------------------------------------------------------------ evaluation

    def evaluate(self, trigger: Trigger, event: IntegrationEvent) -> dict[str, Any]:
        """Dry-run a trigger against an event: does it match, and with which inputs?"""
        spec = trigger.spec
        ctx = {"event": event.data, "event_type": event.type, "trigger": {"name": trigger.metadata.name}}
        if not fnmatch.fnmatch(event.type, spec.event):
            return {"matched": False, "reason": f"event type '{event.type}' does not match '{spec.event}'"}
        try:
            if spec.filter and not truthy(spec.filter, ctx):
                return {"matched": False, "reason": "filter is false"}
            inputs = render(spec.inputs, ctx)
            dedupe = render(spec.dedupe_key, ctx) if spec.dedupe_key else None
        except ExpressionError as exc:
            return {"matched": False, "reason": f"expression error: {exc}", "error": True}
        return {"matched": True, "inputs": inputs, "dedupe_key": None if dedupe is None else str(dedupe)}

    async def dispatch_event(self, tenant_id: str, connection: str, event: IntegrationEvent) -> list[dict[str, Any]]:
        fired = []
        for trigger in await self._triggers(tenant_id):
            spec = trigger.spec
            if not spec.enabled or spec.event == "schedule" or spec.connection != connection:
                continue
            outcome = self.evaluate(trigger, event)
            if not outcome["matched"] and not outcome.get("error"):
                continue
            fired.append(await self._fire(tenant_id, trigger, event, outcome))
        return fired

    async def _fire(
        self, tenant_id: str, trigger: Trigger, event: IntegrationEvent, outcome: dict[str, Any]
    ) -> dict[str, Any]:
        name = trigger.metadata.name
        store = self.engine.store
        delivery = t.TriggerDelivery(
            id=new_id("dlv"),
            tenant_id=tenant_id,
            trigger_name=name,
            dedupe_key=outcome.get("dedupe_key"),
            event_type=event.type,
            status="received",
            detail={"event_id": event.id},
        )
        if outcome.get("error"):
            delivery.status = "error"
            delivery.detail["reason"] = outcome["reason"]
            await store.record_delivery(delivery)
            return {"trigger": name, "status": "error", "reason": outcome["reason"]}
        if not await store.record_delivery(delivery):
            return {"trigger": name, "status": "duplicate", "dedupe_key": delivery.dedupe_key}

        spec = trigger.spec
        if spec.overlap == "skip" and spec.workflow:
            active = await store.count_runs(
                tenant_id, statuses=[RunStatus.pending, RunStatus.running, RunStatus.waiting], trigger_name=name
            )
            if active:
                await store.update_delivery(delivery.id, status="skipped", detail={**delivery.detail, "reason": "previous run still active"})
                return {"trigger": name, "status": "skipped"}
        try:
            context = {"trigger": {"name": name, "event_type": event.type}, "event": event.data}
            if spec.workflow:
                run = await self.engine.executor.start_run(
                    tenant_id,
                    spec.workflow,
                    outcome["inputs"],
                    started_by=f"trigger:{name}",
                    trigger_name=name,
                    context=context,
                )
                await store.update_delivery(delivery.id, status="fired", run_id=run.id)
                return {"trigger": name, "status": "fired", "run_id": run.id}
            campaign = await self.engine.campaigns.launch(
                tenant_id, spec.campaign, started_by=f"trigger:{name}", extra_inputs=outcome["inputs"]
            )
            await store.update_delivery(delivery.id, status="fired", detail={**delivery.detail, "campaign_id": campaign.id})
            return {"trigger": name, "status": "fired", "campaign_id": campaign.id}
        except Exception as exc:
            log.warning("trigger %s failed to start: %s", name, exc)
            await store.update_delivery(delivery.id, status="error", detail={**delivery.detail, "reason": str(exc)})
            return {"trigger": name, "status": "error", "reason": str(exc)}

    async def fire_manual(self, tenant_id: str, trigger_name: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        resource = await self.engine.store.get_resource(tenant_id, "Trigger", trigger_name)
        assert isinstance(resource, Trigger)
        event = IntegrationEvent(
            type=resource.spec.event if "*" not in resource.spec.event else "manual",
            id=new_id("evt"),
            data=data or {},
        )
        outcome = self.evaluate(resource, event)
        if not outcome["matched"]:
            return {"trigger": trigger_name, "status": "filtered", "reason": outcome["reason"]}
        return await self._fire(tenant_id, resource, event, outcome)

    # ------------------------------------------------------------------ schedules

    async def tick_schedules(self) -> int:
        fired = 0
        now = clock.now()
        for row in await self.engine.store.list_resources_all_tenants("Trigger"):
            trigger = Trigger.model_validate(row.document)
            spec = trigger.spec
            if spec.event != "schedule" or not spec.schedule:
                continue
            state = await self.engine.store.get_trigger_state(row.tenant_id, trigger.metadata.name)
            if not spec.enabled:
                if state.next_fire_at is not None:
                    await self.engine.store.set_trigger_next(state.id, None)
                continue
            if state.next_fire_at is None:
                await self.engine.store.set_trigger_next(state.id, next_fire(spec.schedule, spec.timezone, now))
                continue
            if state.next_fire_at > now:
                continue
            upcoming = next_fire(spec.schedule, spec.timezone, now)
            if not await self.engine.store.claim_trigger_tick(state.id, state.next_fire_at, upcoming):
                continue  # another worker fired this tick
            event = IntegrationEvent(
                type="schedule",
                id=f"{trigger.metadata.name}:{state.next_fire_at.isoformat()}",
                data={"scheduled_at": state.next_fire_at.isoformat(), "fired_at": now.isoformat(), "next_at": upcoming.isoformat()},
            )
            outcome = self.evaluate(trigger, event)
            if outcome["matched"] or outcome.get("error"):
                outcome.setdefault("dedupe_key", event.id)
                if outcome.get("dedupe_key") is None:
                    outcome["dedupe_key"] = event.id
                await self._fire(row.tenant_id, trigger, event, outcome)
                fired += 1
        return fired

    async def schedule_preview(self, cron: str, tz: str, count: int = 5) -> list[str]:
        validate_cron(cron, tz)
        out, cursor = [], clock.now()
        for _ in range(count):
            cursor = next_fire(cron, tz, cursor)
            out.append(cursor.isoformat())
        return out
