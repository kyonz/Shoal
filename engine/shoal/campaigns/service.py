"""Campaigns: fan a workflow out over many targets with concurrency, rate, budget and failure policies."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from shoal.core import clock
from shoal.core.errors import Conflict, InvalidRequest
from shoal.core.expressions import render
from shoal.core.ids import new_id
from shoal.core.models import Campaign, CampaignSpec, CampaignStatus, RunStatus, TargetStatus
from shoal.core.scope import current_namespace, use_namespace
from shoal.store import tables as t

if TYPE_CHECKING:
    from shoal.engine import Engine

log = logging.getLogger(__name__)

TICK_INTERVAL = timedelta(seconds=30)
_TERMINAL_TARGET = {TargetStatus.succeeded, TargetStatus.failed, TargetStatus.skipped, TargetStatus.cancelled}


class CampaignService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.store = engine.store

    # ------------------------------------------------------------------ lifecycle

    async def launch(
        self,
        tenant_id: str,
        campaign: str | CampaignSpec,
        *,
        name: str | None = None,
        started_by: str | None = None,
        extra_inputs: dict[str, Any] | None = None,
        start: bool = True,
        initiator: dict[str, Any] | None = None,
    ) -> t.CampaignRow:
        """Create a campaign instance from a Campaign resource (by name) or an inline spec."""
        if isinstance(campaign, str):
            resource = await self.store.get_resource(tenant_id, "Campaign", campaign)
            assert isinstance(resource, Campaign)
            spec, name = resource.spec, name or campaign
        else:
            spec, name = campaign, name or "adhoc"
        if extra_inputs:
            spec = spec.model_copy(update={"inputs": {**spec.inputs, **extra_inputs}})
        workflow = await self.store.get_resource_row(tenant_id, "Workflow", spec.workflow)  # must exist
        if spec.workflow_version is None:  # every target runs the version current at launch
            spec = spec.model_copy(update={"workflow_version": workflow.version})
        else:
            await self.store.get_resource_version(tenant_id, "Workflow", spec.workflow, spec.workflow_version)

        row = t.CampaignRow(
            id=new_id("cmp"),
            tenant_id=tenant_id,
            namespace=current_namespace(),
            name=name,
            spec=spec.model_dump(mode="json", by_alias=True, exclude_none=True),
            status=CampaignStatus.draft,
            stats={},
            created_by=started_by,
            initiator=initiator or await self.engine.identities.resolve(tenant_id, actor=started_by, source="campaign"),
        )
        await self.store.create_campaign(row)
        try:
            await self.plan(row)
        except Exception as exc:
            await self.store.update_campaign(row.id, status=CampaignStatus.cancelled, error={"message": str(exc)})
            raise InvalidRequest(f"could not resolve campaign targets: {exc}") from exc
        if start:
            await self.start(tenant_id, row.id)
        return await self.store.get_campaign(tenant_id, row.id)

    async def plan(self, row: t.CampaignRow) -> int:
        """Resolve and snapshot targets."""
        spec = CampaignSpec.model_validate(row.spec)
        sel = spec.targets
        if sel.items is not None:
            items = list(sel.items)
        else:
            items = await self.engine.integrations.query_all(row.tenant_id, sel.connection, sel.resource, sel.filter)
        if sel.limit:
            items = items[: sel.limit]
        targets = []
        seen: set[str] = set()
        for item in items:
            key = str(render(sel.key, {"target": item}))
            if not key or key in seen:
                continue
            seen.add(key)
            targets.append(
                t.CampaignTarget(id=new_id("tgt"), campaign_id=row.id, tenant_id=row.tenant_id, key=key, data=item)
            )
        added = await self.store.add_targets(targets)
        await self.refresh_stats(row.id)
        return added

    async def start(self, tenant_id: str, campaign_id: str) -> None:
        row = await self.store.get_campaign(tenant_id, campaign_id)
        if row.status not in (CampaignStatus.draft, CampaignStatus.paused):
            raise Conflict(f"campaign is {row.status}")
        await self.store.update_campaign(
            row.id, status=CampaignStatus.running, started_at=row.started_at or clock.now(), next_tick_at=clock.now()
        )

    async def pause(self, tenant_id: str, campaign_id: str, reason: str | None = None) -> None:
        row = await self.store.get_campaign(tenant_id, campaign_id)
        if row.status != CampaignStatus.running:
            raise Conflict(f"campaign is {row.status}")
        await self.store.update_campaign(row.id, status=CampaignStatus.paused, error={"message": reason} if reason else None)

    async def resume(self, tenant_id: str, campaign_id: str) -> None:
        await self.start(tenant_id, campaign_id)

    async def cancel(self, tenant_id: str, campaign_id: str) -> None:
        row = await self.store.get_campaign(tenant_id, campaign_id)
        if row.status in (CampaignStatus.completed, CampaignStatus.cancelled):
            raise Conflict(f"campaign is {row.status}")
        for target in await self.store.list_targets(tenant_id, row.id, status="pending,running"):
            if target.run_id:
                await self.engine.executor.request_cancel(tenant_id, target.run_id)
            await self.store.update_target(target.id, status=TargetStatus.cancelled, finished_at=clock.now())
        await self.store.update_campaign(row.id, status=CampaignStatus.cancelled, finished_at=clock.now())
        await self.refresh_stats(row.id)

    async def retry_failed(self, tenant_id: str, campaign_id: str) -> int:
        row = await self.store.get_campaign(tenant_id, campaign_id)
        count = await self.store.reset_targets(row.id, [TargetStatus.failed, TargetStatus.cancelled])
        if count and row.status in (CampaignStatus.completed, CampaignStatus.paused, CampaignStatus.cancelled):
            await self.store.update_campaign(
                row.id, status=CampaignStatus.running, finished_at=None, next_tick_at=clock.now(), error=None
            )
        await self.refresh_stats(row.id)
        return count

    async def add_targets(self, tenant_id: str, campaign_id: str, items: list[dict[str, Any]]) -> int:
        row = await self.store.get_campaign(tenant_id, campaign_id)
        spec = CampaignSpec.model_validate(row.spec)
        targets = [
            t.CampaignTarget(
                id=new_id("tgt"),
                campaign_id=row.id,
                tenant_id=tenant_id,
                key=str(render(spec.targets.key, {"target": item})),
                data=item,
            )
            for item in items
        ]
        added = await self.store.add_targets(targets)
        if added and row.status == CampaignStatus.completed:
            await self.store.update_campaign(row.id, status=CampaignStatus.running, finished_at=None)
        await self.store.update_campaign(row.id, next_tick_at=clock.now())
        return added

    # ------------------------------------------------------------------ controller

    async def refresh_stats(self, campaign_id: str) -> dict[str, Any]:
        counts = await self.store.target_counts(campaign_id)
        stats = {
            "total": sum(counts.values()),
            **{s.value: counts.get(s.value, 0) for s in TargetStatus},
            "cost_usd": round(await self.store.campaign_cost(campaign_id), 4),
        }
        await self.store.update_campaign(campaign_id, stats=stats)
        return stats

    async def tick_due(self) -> int:
        ticked = 0
        for row in await self.store.due_campaigns():
            if await self.store.claim_campaign(row.id, timedelta(seconds=60)):
                try:
                    with use_namespace(row.namespace):
                        await self.tick(row)
                    ticked += 1
                except Exception:
                    log.exception("campaign %s tick failed", row.id)
                finally:
                    await self.store.update_campaign(row.id, lease_expires_at=None)
        return ticked

    async def tick(self, row: t.CampaignRow) -> None:
        spec = CampaignSpec.model_validate(row.spec)
        policy = spec.policy
        stats = await self.refresh_stats(row.id)
        now = clock.now()

        if policy.max_failures and stats["failed"] >= policy.max_failures:
            await self.store.update_campaign(
                row.id, status=CampaignStatus.paused, error={"message": f"paused after {stats['failed']} failures"}
            )
            return
        if policy.budget_usd is not None and stats["cost_usd"] >= policy.budget_usd:
            await self.store.update_campaign(
                row.id, status=CampaignStatus.paused, error={"message": f"budget of ${policy.budget_usd} reached"}
            )
            return

        if stats["pending"] == 0 and stats["running"] == 0:
            await self.store.update_campaign(row.id, status=CampaignStatus.completed, finished_at=now, next_tick_at=None)
            return

        capacity = policy.concurrency - stats["running"]
        if policy.runs_per_hour:
            started = await self.store.runs_started_since(row.id, now - timedelta(hours=1))
            capacity = min(capacity, policy.runs_per_hour - started)
        if capacity > 0:
            for target in await self.store.list_targets(row.tenant_id, row.id, status="pending", limit=capacity):
                await self._start_target(row, spec, target)
        await self.refresh_stats(row.id)
        await self.store.update_campaign(row.id, next_tick_at=now + TICK_INTERVAL)

    async def _start_target(self, row: t.CampaignRow, spec: CampaignSpec, target: t.CampaignTarget) -> None:
        context = {
            "campaign": {"id": row.id, "name": row.name},
            "target": {"key": target.key, **target.data},
        }
        try:
            inputs = render(spec.inputs, {"target": context["target"], "campaign": context["campaign"]})
            run = await self.engine.executor.start_run(
                row.tenant_id,
                spec.workflow,
                inputs,
                version=spec.workflow_version,
                started_by=f"campaign:{row.id}",
                context=context,
                campaign_id=row.id,
                campaign_target_id=target.id,
                labels={"campaign": row.name, "target": target.key[:60]},
                initiator=row.initiator or None,
            )
        except Exception as exc:
            await self.store.update_target(
                target.id, status=TargetStatus.failed, result={"error": str(exc)}, finished_at=clock.now()
            )
            return
        await self.store.update_target(
            target.id, status=TargetStatus.running, run_id=run.id, attempts=target.attempts + 1, started_at=clock.now()
        )

    async def on_run_finished(self, run: t.Run) -> None:
        if not run.campaign_target_id or run.parent_run_id:
            return
        status = {
            RunStatus.succeeded: TargetStatus.succeeded,
            RunStatus.failed: TargetStatus.failed,
            RunStatus.cancelled: TargetStatus.cancelled,
        }[RunStatus(run.status)]
        await self.store.update_target(
            run.campaign_target_id,
            status=status,
            result={"outputs": run.outputs, "error": run.error},
            cost_usd=float((run.usage or {}).get("cost_usd", 0.0)),
            finished_at=clock.now(),
        )
        await self.store.update_campaign(run.campaign_id, next_tick_at=clock.now())
