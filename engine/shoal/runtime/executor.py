"""Durable workflow executor.

A run is a persisted state machine. ``advance`` loads a claimed run, executes
every ready step, checkpoints each result, and either finishes the run or
parks it (``waiting``) with the time it next needs attention. Workers hold a
lease while advancing; if a worker dies the lease expires and another worker
continues from the last checkpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from shoal.core import clock
from shoal.core.errors import Conflict, CredentialRequired, NotFound, StepError
from shoal.core.expressions import ExpressionError, referenced_secrets, render
from shoal.core.ids import new_id
from shoal.core.models import (
    RunStatus,
    StepBase,
    StepStatus,
    WorkflowSpec,
    validate_params,
)
from shoal.core.scope import current_namespace, use_namespace
from shoal.integrations.base import IntegrationError
from shoal.runtime.context import Completed, StepContext, Suspended
from shoal.secrets.providers import Redactor
from shoal.store import tables as t
from shoal.store.store import new_step_row

if TYPE_CHECKING:
    from shoal.engine import Engine

log = logging.getLogger(__name__)

# Waits longer than this park run-scoped sandboxes (suspend) to free compute.
SUSPEND_THRESHOLD = timedelta(minutes=10)
_DONE_OK = (StepStatus.succeeded, StepStatus.skipped)


def add_usage(total: dict[str, Any], extra: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(total or {})
    for key, value in (extra or {}).items():
        if isinstance(value, int | float):
            out[key] = round(out.get(key, 0) + value, 6)
    return out


class Executor:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.store = engine.store

    # ------------------------------------------------------------------ start

    async def start_run(
        self,
        tenant_id: str,
        workflow_name: str,
        inputs: dict[str, Any] | None = None,
        *,
        version: int | None = None,
        started_by: str | None = None,
        trigger_name: str | None = None,
        context: dict[str, Any] | None = None,
        campaign_id: str | None = None,
        campaign_target_id: str | None = None,
        parent_run_id: str | None = None,
        parent_step_id: str | None = None,
        labels: dict[str, str] | None = None,
        initiator: dict[str, Any] | None = None,
    ) -> t.Run:
        row = await self.store.get_resource_row(tenant_id, "Workflow", workflow_name)
        document = row.document
        if version is not None and version != row.version:
            document = await self.store.get_resource_version(tenant_id, "Workflow", workflow_name, version)
        spec = WorkflowSpec.model_validate(document["spec"])
        values = validate_params(spec.inputs, inputs)
        namespace = current_namespace()
        if parent_run_id:
            parent = await self.store.get_run(tenant_id, parent_run_id)
            namespace = parent.namespace
            initiator = initiator if initiator is not None else parent.initiator
        if initiator is None:
            initiator = await self.engine.identities.resolve(tenant_id, actor=started_by, source="api")
        now = clock.now()
        run = t.Run(
            id=new_id("run"),
            tenant_id=tenant_id,
            namespace=namespace,
            workflow_name=workflow_name,
            workflow_version=version or row.version,
            workflow=spec.model_dump(mode="json", by_alias=True, exclude_none=True),
            status=RunStatus.pending,
            inputs=values,
            context=context or {},
            state={},
            usage={},
            labels=labels or {},
            started_by=started_by,
            initiator=initiator,
            trigger_name=trigger_name,
            campaign_id=campaign_id,
            campaign_target_id=campaign_target_id,
            parent_run_id=parent_run_id,
            parent_step_id=parent_step_id,
            orchestrator=self.engine.orchestrator.name,
            next_wake_at=now,
            created_at=now,
            updated_at=now,
        )
        steps = [new_step_row(tenant_id, run.id, s.id, s.type) for s in spec.steps]
        await self.store.create_run(run, steps)
        await self.engine.events.emit(
            run,
            "run.created",
            {"workflow": workflow_name, "version": run.workflow_version, "started_by": started_by, "initiator": initiator},
        )
        await self.engine.orchestrator.submit(run)
        return run

    # ------------------------------------------------------------------ advance

    async def advance(self, run_id: str, worker_id: str) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(run_id, worker_id))
        try:
            await self._advance(run_id)
        except Exception:
            log.exception("error advancing run %s", run_id)
            # Back off so a poison run does not spin; the error is visible in events.
            with contextlib.suppress(Exception):
                run = await self.store.get_run(None, run_id)
                await self.engine.events.emit(run, "run.error", {"message": "internal error while advancing"})
                await self.store.update_run(run_id, next_wake_at=clock.now() + timedelta(seconds=30))
        finally:
            heartbeat.cancel()
            await self.store.release_lease(run_id, worker_id)

    async def _heartbeat(self, run_id: str, worker_id: str) -> None:
        lease = timedelta(seconds=self.engine.settings.lease_seconds)
        while True:
            await asyncio.sleep(lease.total_seconds() / 3)
            await self.store.extend_lease(run_id, worker_id, lease)

    async def _advance(self, run_id: str) -> None:
        try:
            run = await self.store.get_run(None, run_id)
        except NotFound:
            return
        if RunStatus(run.status).terminal:
            await self.store.update_run(run.id, next_wake_at=None)
            return
        with use_namespace(run.namespace):
            await self._advance_run(run)

    async def _advance_run(self, run: t.Run) -> None:
        spec = WorkflowSpec.model_validate(run.workflow)

        if run.cancel_requested:
            await self._cancel(run, spec)
            return

        now = clock.now()
        if run.status == RunStatus.pending:
            run.status = RunStatus.running
            run.started_at = now
            await self.store.update_run(run.id, status=run.status, started_at=now)
            await self.engine.events.emit(run, "run.started", {"inputs": run.inputs})

        if spec.timeout and run.started_at and now > run.started_at + spec.timeout:
            await self._finish(run, spec, RunStatus.failed, {"message": f"workflow timed out after {spec.timeout}"})
            return

        rows = {r.step_id: r for r in await self.store.get_steps(run.id)}
        for step in spec.steps:  # workflows edited mid-run never happen (snapshot), but be defensive
            if step.id not in rows:
                rows[step.id] = new_step_row(run.tenant_id, run.id, step.id, step.type)
                await self.store.save_step(rows[step.id])

        deps = spec.dependencies()
        secrets = await self.engine.secrets.resolve(run.tenant_id, referenced_secrets(run.workflow))
        redactor = Redactor(list(secrets.values()))
        semaphore = asyncio.Semaphore(16)
        resumed: set[str] = set()

        while True:
            now = clock.now()
            ready: list[StepBase] = []
            for step in spec.steps:
                row = rows[step.id]
                status = StepStatus(row.status)
                if status == StepStatus.pending:
                    if row.wake_at and row.wake_at > now:
                        continue
                    if all(StepStatus(rows[d].status).terminal for d in deps[step.id]):
                        ready.append(step)
                elif status in (StepStatus.waiting, StepStatus.running):
                    # running = interrupted by a crash; re-run the attempt.
                    if step.id in resumed or (row.wake_at is None and status == StepStatus.waiting):
                        continue
                    if row.wake_at is None or row.wake_at <= now:
                        resumed.add(step.id)
                        ready.append(step)
            if not ready:
                break

            async def guarded(step: StepBase) -> None:
                async with semaphore:
                    await self._execute(run, spec, step, rows, secrets, redactor)

            await asyncio.gather(*(guarded(s) for s in ready))
            failed = [
                s for s in spec.steps if rows[s.id].status == StepStatus.failed and s.on_error == "fail"
            ]
            if failed:
                first = rows[failed[0].id]
                await self._finish(
                    run,
                    spec,
                    RunStatus.failed,
                    {"message": f"step '{failed[0].id}' failed", "step": failed[0].id, **(first.error or {})},
                    rows=rows,
                )
                return
            # refresh the run (cancel requests, state written by handlers)
            fresh = await self.store.get_run(None, run.id)
            run.state, run.usage, run.cancel_requested = fresh.state, fresh.usage, fresh.cancel_requested
            if run.cancel_requested:
                await self._cancel(run, spec)
                return

        if all(StepStatus(r.status).terminal for r in rows.values()):
            await self._finish(run, spec, RunStatus.succeeded, None, rows=rows, secrets=secrets)
            return

        wakes = [
            r.wake_at
            for r in rows.values()
            if r.wake_at and r.status in (StepStatus.waiting, StepStatus.pending)
        ]
        next_wake = min(wakes) if wakes else None
        waiting = any(r.status == StepStatus.waiting for r in rows.values())
        status = RunStatus.waiting if waiting else RunStatus.running
        if spec.timeout and run.started_at:
            deadline = run.started_at + spec.timeout
            next_wake = min(next_wake, deadline) if next_wake else deadline
        await self.store.update_run(run.id, status=status, next_wake_at=next_wake)
        if status != run.status:
            await self.engine.events.emit(
                run, f"run.{status.value}", {"next_wake_at": next_wake.isoformat() if next_wake else None}
            )
        run.status = status
        if waiting and (next_wake is None or next_wake - clock.now() > SUSPEND_THRESHOLD):
            await self.engine.sandboxes.suspend_run(run)

    # ------------------------------------------------------------------ steps

    def expression_context(
        self, run: t.Run, rows: dict[str, t.StepRun], secrets: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "inputs": run.inputs,
            "steps": {
                sid: {"output": r.output or {}, "status": r.status, "error": r.error, "attempt": r.attempt}
                for sid, r in rows.items()
            },
            "run": {
                "id": run.id,
                "namespace": run.namespace,
                "workflow": run.workflow_name,
                "version": run.workflow_version,
                "started_by": run.started_by,
                "initiator": run.initiator or {},
                "labels": run.labels,
                "url": f"{self.engine.settings.public_url.rstrip('/')}/runs/{run.id}",
                "signal_url": self.engine.signals.callback_url(run),
            },
            "trigger": run.context.get("trigger", {}),
            "event": run.context.get("event", {}),
            "campaign": run.context.get("campaign", {}),
            "target": run.context.get("target", {}),
            "secrets": secrets,
        }

    async def _execute(
        self,
        run: t.Run,
        spec: WorkflowSpec,
        step: StepBase,
        rows: dict[str, t.StepRun],
        secrets: dict[str, str],
        redactor: Redactor,
    ) -> None:
        row = rows[step.id]
        ctx = StepContext(
            engine=self.engine,
            run=run,
            row=row,
            step=step,
            workflow=spec,
            expr=self.expression_context(run, rows, secrets),
            redactor=redactor,
        )
        events = self.engine.events

        if row.status == StepStatus.pending:
            try:
                should_run = ctx.truthy(step.if_) if step.if_ else True
            except ExpressionError as exc:
                await self._fail_step(ctx, str(exc), retryable=False)
                return
            if not should_run:
                row.status = StepStatus.skipped
                row.finished_at = clock.now()
                await self.store.save_step(row)
                await events.emit(run, "step.skipped", {"reason": "condition false", "if": step.if_}, step_id=step.id)
                return
            row.attempt += 1
            row.status = StepStatus.running
            row.started_at = row.started_at or clock.now()
            row.wake_at = None
            row.error = None
            await self.store.save_step(row)
            await events.emit(run, "step.started", {"type": step.type, "attempt": row.attempt}, step_id=step.id)
        else:
            row.status = StepStatus.running
            row.wake_at = None

        handler = self.engine.plugin("steps", step.type)
        try:
            if step.timeout is not None:
                async with asyncio.timeout(step.timeout.total_seconds()):
                    result = await handler.run(ctx)
            else:
                result = await handler.run(ctx)
        except CredentialRequired as exc:
            if exc.owner is None:
                await self._fail_step(ctx, exc.message, retryable=False, details=exc.details)
                return
            # Wait for the person to connect; saving the credential wakes the step, which then runs again.
            connect = f"{self.engine.settings.public_url.rstrip('/')}/settings/credentials?connection={exc.namespace}/{exc.connection}"
            await events.emit(run, "credential.required", {**exc.details, "message": exc.message, "connect_url": connect}, step_id=step.id)
            result = Suspended(wait_keys=[exc.wait_key], reason=exc.message)
        except StepError as exc:
            await self._fail_step(ctx, exc.message, retryable=exc.retryable, details=exc.details)
            return
        except IntegrationError as exc:
            await self._fail_step(ctx, str(exc), retryable=exc.retryable, details={"status": exc.status})
            return
        except TimeoutError:
            await self._fail_step(ctx, f"step timed out after {step.timeout}", retryable=True)
            return
        except (ExpressionError, ValidationError, ValueError, KeyError) as exc:
            await self._fail_step(ctx, f"{type(exc).__name__}: {exc}", retryable=False)
            return
        except Exception as exc:  # unexpected: retry per policy
            log.exception("step %s of run %s raised", step.id, run.id)
            await self._fail_step(ctx, f"{type(exc).__name__}: {exc}", retryable=True)
            return

        row.usage = add_usage(row.usage, result.usage)
        if result.usage:
            run.usage = add_usage(run.usage, result.usage)
            await self.store.update_run(run.id, usage=run.usage)

        if isinstance(result, Completed):
            row.status = StepStatus.succeeded
            row.output = redactor(result.output)
            row.finished_at = clock.now()
            row.wake_at = None
            await self.store.save_step(row)
            await self.store.clear_wait_keys(run.id, step.id)
            await events.emit(run, "step.succeeded", {"output": row.output}, step_id=step.id)
        elif isinstance(result, Suspended):
            first = not row.state.get("_suspended")
            row.state = {**row.state, "_suspended": True}
            row.status = StepStatus.waiting
            row.wake_at = result.wake_at
            await self.store.save_step(row)
            await self.store.set_wait_keys(run.tenant_id, run.id, step.id, result.wait_keys)
            if first:
                await events.emit(
                    run,
                    "step.waiting",
                    {
                        "reason": result.reason,
                        "wake_at": result.wake_at.isoformat() if result.wake_at else None,
                        "wait_keys": result.wait_keys,
                    },
                    step_id=step.id,
                )

    async def _fail_step(
        self, ctx: StepContext, message: str, *, retryable: bool, details: dict[str, Any] | None = None
    ) -> None:
        row, step = ctx.row, ctx.step
        message = ctx.redactor(message)
        error = {"message": message, "retryable": retryable, "attempt": row.attempt, **ctx.redactor(details or {})}
        row.error = error
        if retryable and row.attempt < step.retry.max_attempts:
            delay = step.retry.delay_for(row.attempt + 1)
            row.status = StepStatus.pending
            row.wake_at = clock.now() + delay
            await self.store.save_step(row)
            await ctx.emit("step.retrying", {"error": message, "attempt": row.attempt, "delay_seconds": delay.total_seconds()})
            return
        row.status = StepStatus.failed
        row.finished_at = clock.now()
        row.wake_at = None
        await self.store.save_step(row)
        await self.store.clear_wait_keys(ctx.run.id, step.id)
        await ctx.emit("step.failed", {"error": message, "attempt": row.attempt, "on_error": step.on_error})

    # ------------------------------------------------------------------ finish

    async def _cancel(self, run: t.Run, spec: WorkflowSpec) -> None:
        rows = await self.store.get_steps(run.id)
        for row in rows:
            if not StepStatus(row.status).terminal:
                row.status = StepStatus.cancelled
                row.finished_at = clock.now()
                row.wake_at = None
                await self.store.save_step(row)
        for child in await self.store.list_runs(run.tenant_id, parent_run_id=run.id, limit=1000):
            if not RunStatus(child.status).terminal:
                await self.request_cancel(run.tenant_id, child.id)
        await self._finish(run, spec, RunStatus.cancelled, {"message": "cancelled"})

    async def request_cancel(self, tenant_id: str, run_id: str) -> t.Run:
        run = await self.store.get_run(tenant_id, run_id)
        if not RunStatus(run.status).terminal:
            await self.store.update_run(run.id, cancel_requested=True)
            await self.store.wake_run(run.id)
        return run

    async def submit_approval(
        self, tenant_id: str, run_id: str, step_id: str, *, approved: bool, by: str | None, comment: str | None = None
    ) -> t.StepRun:
        """Record a human decision for a step waiting on approval and wake the run."""
        run = await self.store.get_run(tenant_id, run_id)
        row = await self.store.get_step(run.id, step_id)
        if row.status != StepStatus.waiting or not row.state.get("requested"):
            raise Conflict(f"step '{step_id}' is not waiting for approval")
        if row.state.get("decision"):
            raise Conflict(f"step '{step_id}' was already decided")
        decision = {"approved": approved, "by": by, "comment": comment, "at": clock.now().isoformat()}
        row.state = {**row.state, "decision": decision}
        await self.store.save_step(row)
        await self.engine.events.emit(run, "approval.decided", decision, step_id=step_id)
        await self.store.wake_by_keys(tenant_id, [f"approval:{run.id}:{step_id}"])
        await self.store.wake_run(run.id)
        return row

    async def _finish(
        self,
        run: t.Run,
        spec: WorkflowSpec,
        status: RunStatus,
        error: dict[str, Any] | None,
        *,
        rows: dict[str, t.StepRun] | None = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
        outputs: dict[str, Any] | None = None
        if status == RunStatus.succeeded and spec.outputs:
            rows = rows or {r.step_id: r for r in await self.store.get_steps(run.id)}
            try:
                outputs = render(spec.outputs, self.expression_context(run, rows, secrets or {}))
                outputs = Redactor(list((secrets or {}).values()))(outputs)
            except ExpressionError as exc:
                status, error = RunStatus.failed, {"message": f"failed to compute outputs: {exc}"}
        if status != RunStatus.succeeded and rows:
            # cancel steps that never got to run
            for row in rows.values():
                if row.status in (StepStatus.pending, StepStatus.waiting, StepStatus.running):
                    row.status = StepStatus.cancelled
                    row.finished_at = clock.now()
                    row.wake_at = None
                    await self.store.save_step(row)
        now = clock.now()
        run.status, run.outputs, run.error, run.finished_at = status, outputs, error, now
        await self.store.update_run(
            run.id, status=status, outputs=outputs, error=error, finished_at=now, next_wake_at=None
        )
        await self.store.clear_wait_keys(run.id)
        await self.engine.events.emit(run, f"run.{status.value}", {"outputs": outputs, "error": error})
        await self.engine.sandboxes.release_run(run)
        await self.engine.on_run_finished(run)
