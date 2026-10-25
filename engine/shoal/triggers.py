"""Trigger evaluation: integration events and cron schedules start workflows or campaigns."""

from __future__ import annotations

import fnmatch
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from croniter import croniter

from shoal.core import clock
from shoal.core.errors import NotFound
from shoal.core.expressions import ExpressionError, render, truthy
from shoal.core.ids import new_id
from shoal.core.models import RunStatus, Trigger
from shoal.core.scope import current_namespace, use_namespace
from shoal.integrations.base import IntegrationEvent
from shoal.store import tables as t

if TYPE_CHECKING:
    from shoal.engine import Engine

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

    async def _initiator(self, tenant_id: str, trigger: Trigger, event: IntegrationEvent) -> dict[str, Any]:
        """The person behind an event; for schedules and manual fires, whoever last changed the trigger."""
        if event.actor:
            hints = dict(event.actor)
            display = hints.pop("display_name", None)
            return await self.engine.identities.resolve(
                tenant_id, actor=f"trigger:{trigger.metadata.name}", source="trigger", display_name=display, **hints
            )
        row = await self.engine.store.get_resource_row(tenant_id, "Trigger", trigger.metadata.name)
        return await self.engine.identities.resolve(
            tenant_id, actor=row.updated_by or f"trigger:{trigger.metadata.name}", source="schedule" if trigger.spec.event == "schedule" else "trigger"
        )

    async def triage(self, tenant_id: str, trigger: Trigger, event: IntegrationEvent) -> dict[str, Any]:
        """Ask a decision model about the event; ``passed`` is the trigger's ``when`` expression."""
        spec = trigger.spec.triage
        assert spec is not None
        scope = {"event": event.data, "event_type": event.type, "trigger": {"name": trigger.metadata.name}}
        result = await self.engine.decisions.decide(
            tenant_id,
            render(spec.state, scope),
            spec.questions,
            provider=spec.provider,
            config=spec.provider_config,
        )
        answers = {k: a.to_dict() for k, a in result.answers.items()}
        return {"answers": answers, "passed": truthy(spec.when, {**scope, "answers": answers}), "provider": result.provider}

    async def dispatch_event(
        self, tenant_id: str, connection: str, event: IntegrationEvent, *, connection_namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Fire matching triggers in every namespace whose ``connection`` resolves to this one."""
        fired = []
        for row in await self.engine.store.list_resources(tenant_id, "Trigger", all_namespaces=True):
            trigger = Trigger.model_validate(row.document)
            spec = trigger.spec
            if not spec.enabled or spec.event == "schedule" or spec.connection != connection:
                continue
            with use_namespace(row.namespace):
                if connection_namespace is not None:
                    try:
                        conn_row = await self.engine.store.get_resource_row(tenant_id, "Connection", connection)
                    except NotFound:
                        conn_row = None
                    # The trigger's namespace must resolve its connection to the one that received the event
                    # (an organisation connection not shared with the namespace does not resolve).
                    if conn_row is None or conn_row.namespace != connection_namespace:
                        continue
                outcome = self.evaluate(trigger, event)
                if not outcome["matched"] and not outcome.get("error"):
                    continue
                fired.append({**await self._fire(tenant_id, trigger, event, outcome), "namespace": row.namespace})
        return fired

    async def _fire(
        self, tenant_id: str, trigger: Trigger, event: IntegrationEvent, outcome: dict[str, Any]
    ) -> dict[str, Any]:
        name = trigger.metadata.name
        store = self.engine.store
        delivery = t.TriggerDelivery(
            id=new_id("dlv"),
            tenant_id=tenant_id,
            namespace=current_namespace(),
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
        triage: dict[str, Any] | None = None
        if spec.triage is not None:
            try:
                triage = await self.triage(tenant_id, trigger, event)
            except Exception as exc:
                await store.update_delivery(delivery.id, status="error", detail={**delivery.detail, "reason": f"triage failed: {exc}"})
                return {"trigger": name, "status": "error", "reason": f"triage failed: {exc}"}
            delivery.detail = {**delivery.detail, "triage": triage}
            if not triage["passed"]:
                await store.update_delivery(delivery.id, status="triaged_out", detail=delivery.detail)
                return {"trigger": name, "status": "triaged_out", "triage": triage}
            await store.update_delivery(delivery.id, detail=delivery.detail)
        if spec.overlap == "skip" and spec.workflow:
            active = await store.count_runs(
                tenant_id, statuses=[RunStatus.pending, RunStatus.running, RunStatus.waiting], trigger_name=name
            )
            if active:
                await store.update_delivery(delivery.id, status="skipped", detail={**delivery.detail, "reason": "previous run still active"})
                return {"trigger": name, "status": "skipped"}
        try:
            context: dict[str, Any] = {"trigger": {"name": name, "event_type": event.type}, "event": event.data}
            if triage is not None:
                context["trigger"]["triage"] = triage["answers"]
            initiator = await self._initiator(tenant_id, trigger, event)
            if spec.workflow:
                run = await self.engine.executor.start_run(
                    tenant_id,
                    spec.workflow,
                    outcome["inputs"],
                    version=spec.workflow_version,
                    started_by=f"trigger:{name}",
                    trigger_name=name,
                    context=context,
                    initiator=initiator,
                )
                await store.update_delivery(delivery.id, status="fired", run_id=run.id)
                return {"trigger": name, "status": "fired", "run_id": run.id}
            campaign = await self.engine.campaigns.launch(
                tenant_id, spec.campaign, started_by=f"trigger:{name}", extra_inputs=outcome["inputs"], initiator=initiator
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
        for row in await self.engine.store.list_resources_all_tenants("Trigger"):
            with use_namespace(row.namespace):
                fired += await self._tick_schedule(row)
        return fired

    async def _tick_schedule(self, row: t.Resource) -> int:
        now = clock.now()
        trigger = Trigger.model_validate(row.document)
        spec = trigger.spec
        if spec.event != "schedule" or not spec.schedule:
            return 0
        state = await self.engine.store.get_trigger_state(row.tenant_id, trigger.metadata.name)
        if not spec.enabled:
            if state.next_fire_at is not None:
                await self.engine.store.set_trigger_next(state.id, None)
            return 0
        if state.next_fire_at is None:
            await self.engine.store.set_trigger_next(state.id, next_fire(spec.schedule, spec.timezone, now))
            return 0
        if state.next_fire_at > now:
            return 0
        upcoming = next_fire(spec.schedule, spec.timezone, now)
        if not await self.engine.store.claim_trigger_tick(state.id, state.next_fire_at, upcoming):
            return 0  # another worker fired this tick
        event = IntegrationEvent(
            type="schedule",
            id=f"{trigger.metadata.name}:{state.next_fire_at.isoformat()}",
            data={"scheduled_at": state.next_fire_at.isoformat(), "fired_at": now.isoformat(), "next_at": upcoming.isoformat()},
        )
        outcome = self.evaluate(trigger, event)
        if not (outcome["matched"] or outcome.get("error")):
            return 0
        if outcome.get("dedupe_key") is None:
            outcome["dedupe_key"] = event.id
        await self._fire(row.tenant_id, trigger, event, outcome)
        return 1

    async def schedule_preview(self, cron: str, tz: str, count: int = 5) -> list[str]:
        validate_cron(cron, tz)
        out, cursor = [], clock.now()
        for _ in range(count):
            cursor = next_fire(cron, tz, cursor)
            out.append(cursor.isoformat())
        return out
