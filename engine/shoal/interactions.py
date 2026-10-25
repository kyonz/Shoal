"""People answering Shoal from other tools: approval notifications and interactions.

An approval step with ``notify:`` asks through integrations that support it
(Slack posts a message with Approve / Reject buttons). When someone clicks, the
integration's webhook parses an event carrying an :class:`Interaction`; this
service resolves the person behind it (``Identity`` hints such as ``slack_id``
or ``email`` -> a directory user), checks they hold ``runs:approve`` (or
``runs:signal``) in the run's namespace (``submit_approval`` checks the step's ``approvers``),
and only then records the decision or sends the signal. Whatever decides the
step (a click, the UI, the API, a timeout), the notifications are updated with
the outcome.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from shoal.auth.rbac import Principal
from shoal.core.errors import Conflict, Forbidden, NotFound, ShoalError
from shoal.core.models import ApprovalStep
from shoal.core.scope import use_namespace
from shoal.integrations.base import ApprovalRequest, IntegrationError, IntegrationEvent, Interaction

if TYPE_CHECKING:
    from shoal.engine import Engine
    from shoal.runtime.context import StepContext
    from shoal.store import tables as t

log = logging.getLogger(__name__)


class InteractionService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def run_url(self, run: t.Run) -> str:
        return f"{self.engine.settings.public_url.rstrip('/')}/ns/{run.namespace}/runs/{run.id}"

    # ------------------------------------------------------------------ approval notifications

    async def notify_approval(self, ctx: StepContext, step: ApprovalStep, deadline: datetime | None) -> None:
        """Ask every ``notify`` target; each one sent is checkpointed, so a retry does not ask twice."""
        sent: list[dict[str, Any]] = ctx.state.setdefault("notifications", [])
        initiator = ctx.run.initiator or {}
        for index, target in enumerate(step.notify):
            if any(n["index"] == index for n in sent):
                continue
            with use_namespace(ctx.run.namespace):
                name = await self._connection_name(ctx, target.integration, target.connection)
                conn = await self.engine.integrations.connection(ctx.tenant_id, name, initiator=initiator, run_id=ctx.run.id)
            integration = conn.integration
            if not integration.supports_approvals:
                raise IntegrationError(f"connection '{name}' ({integration.key}) cannot ask for approvals")
            request = ApprovalRequest(
                run_id=ctx.run.id,
                step_id=step.id,
                workflow=ctx.run.workflow_name,
                namespace=ctx.run.namespace,
                message=ctx.state["message"],
                to=ctx.render(target.to) if target.to else None,
                options=ctx.render(target.with_),
                approvers=step.approvers,
                deadline=deadline.isoformat() if deadline else None,
                run_url=self.run_url(ctx.run),
                initiator=initiator.get("display_name"),
            )
            ref = await integration.request_approval(conn, request)
            sent.append({"index": index, "connection": name, "integration": integration.key,
                         "request": request.model_dump(mode="json"), "ref": ref})
            await ctx.checkpoint()
            await ctx.emit("approval.notified", {"connection": name, "integration": integration.key, "to": request.to})

    async def _connection_name(self, ctx: StepContext, integration: str | None, connection: str | None) -> str:
        if connection:
            return connection
        assert integration is not None
        return await self.engine.integrations.resolve_name(ctx.tenant_id, integration, ctx.connection_for(integration))

    async def approval_resolved(self, ctx: StepContext, outcome: dict[str, Any]) -> None:
        """Show the outcome on every notification (best effort: a failure here never fails the step)."""
        notifications = ctx.state.get("notifications") or []
        for note in notifications:
            if note.get("resolved"):
                continue
            try:
                with use_namespace(ctx.run.namespace):
                    conn = await self.engine.integrations.connection(
                        ctx.tenant_id, note["connection"], initiator=ctx.run.initiator, run_id=ctx.run.id
                    )
                request = ApprovalRequest.model_validate(note["request"])
                await conn.integration.approval_resolved(conn, request, note["ref"], outcome)
                note["resolved"] = True
            except Exception as exc:  # the decision stands even if the message cannot be updated
                log.warning("could not update approval notification on %s: %s", note.get("connection"), exc)
                note["resolved"], note["error"] = True, str(exc)[:500]

    # ------------------------------------------------------------------ who is acting

    async def person(self, tenant_id: str, hints: dict[str, Any], *, source: str) -> tuple[dict[str, Any], Principal | None]:
        """(initiator record, principal) for identity hints from an integration; no principal when unknown."""
        hints = dict(hints)
        display = hints.pop("display_name", None)
        initiator = await self.engine.identities.resolve(tenant_id, actor=None, source=source, display_name=display, **hints)
        user = None
        directory = self.engine.directory
        if initiator.get("identity"):
            identity = await self.engine.store.find_resource(tenant_id, "Identity", initiator["identity"])
            for actor in getattr(getattr(identity, "spec", None), "actors", []):
                if actor.startswith("user:"):
                    user = await directory.find_user(tenant_id, username=actor[5:])
                    if user is not None:
                        break
        if user is None and initiator.get("email"):
            user = await directory.find_user(tenant_id, email=initiator["email"], kind="user")
        if user is None or not user.active:
            return initiator, None
        initiator["display_name"] = initiator.get("display_name") or user.display_name
        principal = Principal(
            tenant_id=tenant_id, actor=f"user:{user.username}", kind=user.kind, user_id=user.id,
            display_name=user.display_name, email=user.email,
        )
        return initiator, principal

    # ------------------------------------------------------------------ approvals

    async def may_approve(self, principal: Principal, run: t.Run) -> str | None:
        """None when ``principal`` may decide approvals in the run's namespace; otherwise why not.

        The step's ``approvers`` list is checked by ``Executor.submit_approval`` (as for the UI and API).
        """
        if not await self.engine.authz.allowed(principal, "runs:approve", run.namespace):
            return f"{principal.display_name or principal.actor} lacks 'runs:approve' in namespace '{run.namespace}'"
        return None

    # ------------------------------------------------------------------ interactions

    async def handle(self, tenant_id: str, connection: str, integration: str, event: IntegrationEvent) -> dict[str, Any]:
        """Act on an interaction as the person who made it; returns the outcome (never raises for a refusal)."""
        interaction = event.interaction
        assert interaction is not None
        outcome: dict[str, Any] = {"kind": interaction.kind, "run_id": interaction.run_id, "ok": False}
        try:
            run = await self.engine.store.get_run(tenant_id, interaction.run_id)
        except NotFound:
            return {**outcome, "error": "that run no longer exists"}
        initiator, principal = await self.person(tenant_id, event.actor, source=integration)
        outcome["by"] = initiator.get("display_name")
        if principal is None:
            return {**outcome, "error": "Shoal does not know who you are: ask an admin to add your "
                    f"{integration} account to your Identity (or match your email to your Shoal user)"}
        try:
            if interaction.kind == "approval":
                return {**outcome, **await self._approve(run, connection, integration, interaction, principal)}
            return {**outcome, **await self._signal(run, integration, interaction, principal)}
        except ShoalError as exc:
            return {**outcome, "error": str(exc)}

    async def _approve(
        self, run: t.Run, connection: str, integration: str, interaction: Interaction, principal: Principal
    ) -> dict[str, Any]:
        step_id = interaction.step_id or ""
        row = await self.engine.store.get_step(run.id, step_id)
        if not any(n.get("connection") == connection for n in row.state.get("notifications") or []):
            return {"error": "this approval was not sent through this connection"}
        decision = row.state.get("decision")
        if decision:
            verdict = "approved" if decision["approved"] else "rejected"
            return {"error": f"already {verdict} by {decision.get('by') or 'someone else'}"}
        refusal = await self.may_approve(principal, run)
        if refusal is None:
            try:
                await self.engine.executor.submit_approval(
                    run.tenant_id, run.id, step_id, approved=bool(interaction.approved),
                    by=principal.display_name or principal.actor, comment=interaction.comment, decider=principal,
                )
            except Conflict as exc:
                return {"error": str(exc)}
            except Forbidden as exc:
                refusal = str(exc)
        if refusal is not None:
            await self.engine.store.audit(run.tenant_id, principal.actor, "access.denied", "runs:approve",
                                          outcome="denied", namespace=run.namespace, via=integration)
            return {"error": refusal}
        await self.engine.store.audit(run.tenant_id, principal.actor, "run.approval", f"{run.id}/{step_id}",
                                      namespace=run.namespace, approved=bool(interaction.approved), via=integration)
        return {"ok": True, "approved": bool(interaction.approved), "step_id": step_id}

    async def _signal(self, run: t.Run, integration: str, interaction: Interaction, principal: Principal) -> dict[str, Any]:
        if not await self.engine.authz.allowed(principal, "runs:signal", run.namespace):
            return {"error": f"{principal.display_name or principal.actor} lacks 'runs:signal' in namespace '{run.namespace}'"}
        row = await self.engine.signals.send(run, interaction.signal or "", interaction.payload, source=integration, sender=principal.actor)
        await self.engine.store.audit(run.tenant_id, principal.actor, "run.signal", f"{run.id}/{interaction.signal}",
                                      namespace=run.namespace, signal=row.id, via=integration)
        return {"ok": True, "signal": row.id, "name": interaction.signal}
