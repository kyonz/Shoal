"""Eval runs: start cases as workflow runs, grade them as they finish, compare and gate.

An eval run covers one or more *variants* (a workflow version plus input
overrides). Each case of each variant is one :class:`~shoal.store.tables.EvalResult`
holding the workflow run it started, every grader's score and the weighted
score. Results record the workflow version they ran, so scores are kept per
resource version and versions can be trended and compared.

Progress is driven by run completion (``on_run_finished``) and, as a safety
net, by the scheduler (``tick_due``); an eval run is advanced under a short
lease so two workers never grade or start the same case twice.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select, update

from shoal.core import clock
from shoal.core.errors import Conflict, InvalidRequest, NotFound
from shoal.core.ids import new_id
from shoal.core.models import (
    Eval,
    EvalCase,
    EvalPlatform,
    EvalSpec,
    EvalVariant,
    RunStatus,
    WorkflowSpec,
    endpoint_fields,
)
from shoal.core.scope import SHARED_NAMESPACE, current_namespace, use_namespace
from shoal.evals.compare import compare, evaluate_gate, summarize
from shoal.evals.graders import grade_case
from shoal.evals.sinks.base import (
    EvalExport,
    EvalSink,
    ExportResult,
    ScoredCase,
    Trace,
    TraceSpan,
    resolve_config,
)
from shoal.store import tables as t

if TYPE_CHECKING:
    from shoal.engine import Engine

log = logging.getLogger(__name__)

TICK_INTERVAL = timedelta(seconds=30)
LEASE = timedelta(seconds=120)
ACTIVE = ("pending", "running")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


class EvalService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.store = engine.store
        self._dirty: set[str] = set()

    # ------------------------------------------------------------------ plugins

    def sink(self, name: str) -> EvalSink:
        return self.engine.plugin("eval_sinks", name)

    async def sink_config(self, tenant_id: str, config: dict[str, Any]) -> dict[str, Any]:
        return await resolve_config(self.engine, tenant_id, config)

    async def target(self, tenant_id: str, sink: str | None, config: dict[str, Any],
                     platform: str | None = None) -> tuple[str, dict[str, Any]]:
        """The sink and its resolved settings: an EvalPlatform's (secrets from ``shared``) with ``config`` over them."""
        if not platform:
            if not sink:
                raise InvalidRequest("set `sink` or `platform`")
            return sink, await self.sink_config(tenant_id, config)
        row = await self.store.get_resource_row(tenant_id, "EvalPlatform", platform)
        spec = EvalPlatform.model_validate(row.document).spec
        if sink and sink != spec.sink:
            raise InvalidRequest(f"eval platform '{platform}' uses the {spec.sink} sink, not {sink}")
        if endpoints := endpoint_fields(config):  # the platform's credentials go only where the platform says
            raise InvalidRequest(f"{', '.join(endpoints)}: an eval platform's endpoints are set on the platform only")
        with use_namespace(SHARED_NAMESPACE):
            base = await self.sink_config(tenant_id, spec.config)
        return spec.sink, {**base, **await self.sink_config(tenant_id, config)}

    async def import_dataset(
        self, tenant_id: str, sink: str | None, dataset: str, config: dict[str, Any], *, limit: int | None = None,
        platform: str | None = None,
    ) -> list[EvalCase]:
        name, resolved = await self.target(tenant_id, sink, config, platform)
        cases = await self.sink(name).import_dataset(dataset, resolved, limit=limit)
        return _unique(cases)

    async def test_platform(self, tenant_id: str, platform: str) -> dict[str, Any]:
        try:
            name, resolved = await self.target(tenant_id, None, {}, platform)  # an unknown platform is raised
        except InvalidRequest as exc:  # e.g. a secret that is not set
            return {"ok": False, "detail": str(exc)}
        try:
            return {"sink": name, **await self.sink(name).test_connection(resolved)}
        except NotImplementedError:
            return {"sink": name, "ok": None, "detail": f"the {name} sink cannot check its settings"}
        except Exception as exc:  # noqa: BLE001 - reported to the admin, not raised
            return {"sink": name, "ok": False, "detail": str(exc)[:500]}

    async def platform_datasets(self, tenant_id: str, platform: str) -> list[str]:
        name, resolved = await self.target(tenant_id, None, {}, platform)
        try:
            return await self.sink(name).list_datasets(resolved)
        except NotImplementedError as exc:
            raise InvalidRequest(f"the {name} sink cannot list datasets") from exc

    # ------------------------------------------------------------------ start

    async def start(
        self,
        tenant_id: str,
        eval_name: str,
        *,
        variants: list[EvalVariant] | None = None,
        version: int | None = None,
        inputs: dict[str, Any] | None = None,
        baseline: str | None = None,
        fail_under: float | None = None,
        max_regression: float | None = None,
        cases: list[str] | None = None,
        started_by: str | None = None,
        initiator: dict[str, Any] | None = None,
        labels: dict[str, str] | None = None,
    ) -> t.EvalRun:
        """Start an eval run in the current namespace. ``cases`` limits the run to those case names."""
        row = await self.store.get_resource_row(tenant_id, "Eval", eval_name)
        resource = Eval.model_validate(row.document)
        spec = resource.spec
        if variants is None:
            variants = list(spec.variants) or [EvalVariant(name="default", version=version)]
        if inputs:
            variants = [v.model_copy(update={"inputs": {**v.inputs, **inputs}}) for v in variants]
        if not variants:
            raise InvalidRequest("an eval run needs at least one variant")
        if len({v.name for v in variants}) != len(variants):
            raise InvalidRequest("variant names must be unique")

        if spec.from_runs is not None:
            mode = "runs"
            sources = await self._past_runs(tenant_id, spec)
            resolved = [await self._variant(tenant_id, spec, EvalVariant(name="production"), None)]
        else:
            mode = "cases"
            todo = list(spec.cases)
            if spec.dataset is not None:
                ds = spec.dataset
                todo += await self.import_dataset(tenant_id, ds.sink, ds.name, ds.config, limit=ds.limit, platform=ds.platform)
            todo = _unique(todo)
            if cases:
                todo = [c for c in todo if c.name in set(cases)]
            if not todo:
                raise InvalidRequest("the eval has no cases to run")
            resolved = [await self._variant(tenant_id, spec, v, v.version) for v in variants]

        gate = spec.gate.model_dump()
        gate.update({k: v for k, v in {"fail_under": fail_under, "max_regression": max_regression}.items() if v is not None})
        if baseline is not None:
            gate["baseline"] = baseline
        now = clock.now()
        eval_run = t.EvalRun(
            id=new_id("evr"),
            tenant_id=tenant_id,
            namespace=current_namespace(),
            eval_name=eval_name,
            eval_version=row.version,
            spec=spec.model_dump(mode="json", by_alias=True, exclude_none=True),
            workflow_name=spec.workflow,
            workflow_version=resolved[0]["workflow_version"],
            variants=resolved,
            mode=mode,
            status="running",
            summary={},
            gate=gate,
            exports={},
            labels=labels or {},
            started_by=started_by,
            initiator=initiator or await self.engine.identities.resolve(tenant_id, actor=started_by, source="eval"),
            next_tick_at=now,
        )
        eval_run.baseline_id = await self._resolve_baseline(eval_run, gate.get("baseline"))
        results: list[t.EvalResult] = []
        if mode == "runs":
            for run in sources:
                results.append(self._result(eval_run, "production", _case_from_run(run), run.workflow_version, len(results), run.id))
        else:
            for variant in resolved:
                for case in todo:
                    results.append(
                        self._result(eval_run, variant["name"], case.model_dump(mode="json"), variant["workflow_version"], len(results))
                    )
        if not results:
            raise InvalidRequest("no runs matched 'from_runs'" if mode == "runs" else "the eval has no cases to run")
        async with self.store.session() as s:
            s.add(eval_run)
            await s.flush()
            s.add_all(results)
        await self.advance(tenant_id, eval_run.id)
        return await self.get(tenant_id, eval_run.id)

    def _result(
        self, eval_run: t.EvalRun, variant: str, case: dict[str, Any], version: int, position: int, run_id: str | None = None
    ) -> t.EvalResult:
        return t.EvalResult(
            id=new_id("evc"),
            eval_run_id=eval_run.id,
            tenant_id=eval_run.tenant_id,
            variant=variant,
            workflow_name=eval_run.workflow_name,
            workflow_version=version,
            case_name=case["name"],
            position=position,
            case=case,
            run_id=run_id,
            status="pending",
            scores={},
        )

    async def _variant(self, tenant_id: str, spec: EvalSpec, variant: EvalVariant, version: int | None) -> dict[str, Any]:
        """Pin the variant's workflow version and record the current versions of the agents and
        sub-workflows it uses. Only the workflow version is pinned: agents and sub-workflows resolve
        to their current version when each case starts, so ``versions`` documents what was live."""
        row = await self.store.get_resource_row(tenant_id, "Workflow", spec.workflow)
        document = row.document
        if version is not None and version != row.version:
            document = await self.store.get_resource_version(tenant_id, "Workflow", spec.workflow, version)
        wf_version = version or row.version
        versions = {f"Workflow/{spec.workflow}": wf_version}
        workflow = WorkflowSpec.model_validate(document["spec"])
        refs: set[tuple[str, str]] = set()
        if workflow.defaults.agent:
            refs.add(("Agent", workflow.defaults.agent))
        for step in workflow.steps:
            if step.type == "agent" and getattr(step, "agent", None):
                refs.add(("Agent", step.agent))  # type: ignore[attr-defined]
            elif step.type == "workflow":
                refs.add(("Workflow", step.workflow))  # type: ignore[attr-defined]
        for kind, name in sorted(refs):
            try:
                versions[f"{kind}/{name}"] = (await self.store.get_resource_row(tenant_id, kind, name)).version
            except NotFound:
                continue
        return {"name": variant.name, "workflow_version": wf_version, "inputs": variant.inputs, "versions": versions}

    async def _past_runs(self, tenant_id: str, spec: EvalSpec) -> Sequence[t.Run]:
        query = spec.from_runs
        assert query is not None
        q = select(t.Run).where(
            t.Run.tenant_id == tenant_id,
            t.Run.namespace == current_namespace(),
            t.Run.workflow_name == (query.workflow or spec.workflow),
            t.Run.status.in_(query.status),
            t.Run.parent_run_id.is_(None),
        )
        if query.since:
            q = q.where(t.Run.created_at >= clock.now() - query.since)
        if query.version:
            q = q.where(t.Run.workflow_version == query.version)
        async with self.store.session() as s:
            rows = (await s.execute(q.order_by(t.Run.created_at.desc()).limit(query.limit * 4))).scalars().all()
        rows = [r for r in rows if all(str((r.labels or {}).get(k)) == v for k, v in query.labels.items())]
        return [r for r in rows if not (r.labels or {}).get("eval_run")][: query.limit]

    async def _resolve_baseline(self, eval_run: t.EvalRun, baseline: str | None) -> str | None:
        if not baseline or baseline == "none":
            return None
        q = select(t.EvalRun.id).where(
            t.EvalRun.tenant_id == eval_run.tenant_id,
            t.EvalRun.namespace == eval_run.namespace,
            t.EvalRun.eval_name == eval_run.eval_name,
            t.EvalRun.status == "completed",
        )
        if baseline == "previous":
            pass
        elif baseline.startswith("version:") or baseline.startswith("v") and baseline[1:].isdigit():
            number = baseline.split(":", 1)[1] if ":" in baseline else baseline[1:]
            if not number.isdigit():
                raise InvalidRequest("baseline must be 'previous', 'version:N' or an eval run id")
            q = q.where(t.EvalRun.workflow_version == int(number))
        else:
            other = await self.get(eval_run.tenant_id, baseline)
            if other.eval_name != eval_run.eval_name:
                raise InvalidRequest(f"baseline {baseline} is a run of eval '{other.eval_name}'")
            return other.id
        async with self.store.session() as s:
            found = (await s.execute(q.order_by(t.EvalRun.created_at.desc()).limit(1))).scalar_one_or_none()
        if found is None and baseline == "previous":
            return None  # the first run of an eval has nothing to compare with
        if found is None:
            raise NotFound(f"no completed run of eval '{eval_run.eval_name}' matches baseline '{baseline}'")
        return found

    # ------------------------------------------------------------------ reads

    async def get(self, tenant_id: str, eval_run_id: str) -> t.EvalRun:
        async with self.store.session() as s:
            row = await s.get(t.EvalRun, eval_run_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFound(f"eval run '{eval_run_id}' not found")
        return row

    async def list_runs(
        self, tenant_id: str, namespace: str, *, eval_name: str | None = None, status: str | None = None,
        limit: int = 50, before: str | None = None,
    ) -> Sequence[t.EvalRun]:
        q = select(t.EvalRun).where(t.EvalRun.tenant_id == tenant_id, t.EvalRun.namespace == namespace)
        if eval_name:
            q = q.where(t.EvalRun.eval_name == eval_name)
        if status:
            q = q.where(t.EvalRun.status.in_(status.split(",")))
        if before:
            q = q.where(t.EvalRun.id < before)
        async with self.store.session() as s:
            return (await s.execute(q.order_by(t.EvalRun.id.desc()).limit(limit))).scalars().all()

    async def results(self, eval_run_id: str, *, variant: str | None = None) -> list[t.EvalResult]:
        q = select(t.EvalResult).where(t.EvalResult.eval_run_id == eval_run_id)
        if variant:
            q = q.where(t.EvalResult.variant == variant)
        async with self.store.session() as s:
            return list((await s.execute(q.order_by(t.EvalResult.position))).scalars().all())

    async def history(self, tenant_id: str, namespace: str, eval_name: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Scores per workflow version across completed runs of an eval (newest version first)."""
        runs = [r for r in await self.list_runs(tenant_id, namespace, eval_name=eval_name, status="completed", limit=500)]
        by_version: dict[int, dict[str, Any]] = {}
        for run in runs:  # newest first
            for name, v in (run.summary.get("variants") or {}).items():
                version = v.get("workflow_version")
                if version is None or v.get("score") is None:
                    continue
                entry = by_version.setdefault(version, {"workflow_version": version, "runs": 0, "latest": None, "scores": []})
                entry["runs"] += 1
                entry["scores"].append(v["score"])
                if entry["latest"] is None:
                    variant = next((x for x in run.variants if x["name"] == name), {})
                    entry["latest"] = {
                        "eval_run_id": run.id, "variant": name, "eval_version": run.eval_version, "score": v["score"],
                        "pass_rate": v.get("pass_rate"), "graders": v.get("graders"), "versions": variant.get("versions"),
                        "gate_passed": (run.gate or {}).get("passed"), "finished_at": _iso(run.finished_at),
                    }
        out = []
        for version in sorted(by_version, reverse=True)[:limit]:
            entry = by_version.pop(version)
            scores = entry.pop("scores")
            out.append({**entry, "mean_score": round(sum(scores) / len(scores), 4)})
        return out

    async def compare_runs(self, tenant_id: str, baseline_id: str, candidate_id: str) -> dict[str, Any]:
        baseline, candidate = await self.get(tenant_id, baseline_id), await self.get(tenant_id, candidate_id)
        return {
            "baseline_id": baseline.id,
            "candidate_id": candidate.id,
            "variants": self._diffs(await self.results(baseline.id), await self.results(candidate.id)),
        }

    @staticmethod
    def _diffs(baseline: list[t.EvalResult], candidate: list[t.EvalResult]) -> dict[str, Any]:
        """Per candidate variant: a diff against the same-named baseline variant, else the baseline's first."""
        base: dict[str, list[t.EvalResult]] = {}
        for r in baseline:
            base.setdefault(r.variant, []).append(r)
        cand: dict[str, list[t.EvalResult]] = {}
        for r in candidate:
            cand.setdefault(r.variant, []).append(r)
        first = next(iter(base.values()), [])
        return {name: compare(base.get(name, first), rows) for name, rows in cand.items() if base.get(name, first)}

    @staticmethod
    def _variant_diffs(results: list[t.EvalResult], order: list[str]) -> dict[str, Any]:
        """Every variant against the first (the control arm)."""
        if len(order) < 2:
            return {}
        control = [r for r in results if r.variant == order[0]]
        return {name: compare(control, [r for r in results if r.variant == name]) for name in order[1:]}

    # ------------------------------------------------------------------ progress

    async def on_run_finished(self, run: t.Run) -> None:
        info = (run.context or {}).get("eval")
        if not info or run.parent_run_id:
            return
        try:
            await self.advance(run.tenant_id, info["eval_run_id"])
        except Exception:
            log.exception("eval run %s: advancing after run %s failed", info.get("eval_run_id"), run.id)

    async def tick_due(self, limit: int = 20) -> int:
        now = clock.now()
        q = (
            select(t.EvalRun.tenant_id, t.EvalRun.id)
            .where(
                t.EvalRun.status == "running",
                or_(t.EvalRun.next_tick_at.is_(None), t.EvalRun.next_tick_at <= now),
                or_(t.EvalRun.lease_expires_at.is_(None), t.EvalRun.lease_expires_at < now),
            )
            .limit(limit)
        )
        async with self.store.session() as s:
            due = (await s.execute(q)).all()
        for tenant_id, eval_run_id in due:
            try:
                await self.advance(tenant_id, eval_run_id)
            except Exception:
                log.exception("eval run %s tick failed", eval_run_id)
        return len(due)

    async def _claim(self, eval_run_id: str) -> bool:
        now = clock.now()
        async with self.store.session() as s:
            result = await s.execute(
                update(t.EvalRun)
                .where(
                    t.EvalRun.id == eval_run_id,
                    t.EvalRun.status == "running",
                    or_(t.EvalRun.lease_expires_at.is_(None), t.EvalRun.lease_expires_at < now),
                )
                .values(lease_expires_at=now + LEASE)
            )
            return bool(result.rowcount)

    async def _renew(self, eval_run_id: str) -> None:
        await self._update(eval_run_id, lease_expires_at=clock.now() + LEASE)

    async def _update(self, eval_run_id: str, **values: Any) -> None:
        async with self.store.session() as s:
            await s.execute(update(t.EvalRun).where(t.EvalRun.id == eval_run_id).values(**values))

    async def _save(self, result: t.EvalResult) -> None:
        async with self.store.session() as s:
            await s.merge(result)

    async def advance(self, tenant_id: str, eval_run_id: str) -> None:
        """Grade finished cases, start pending ones and finish the run when nothing is left.

        A call that finds the run leased (runs finishing concurrently) marks it dirty, and the
        holder goes round again once it releases, so no completion is missed.
        """
        while True:
            if not await self._claim(eval_run_id):
                self._dirty.add(eval_run_id)
                await self._update(eval_run_id, next_tick_at=clock.now())  # other workers: the scheduler retries
                return
            self._dirty.discard(eval_run_id)
            try:
                row = await self.get(tenant_id, eval_run_id)
                with use_namespace(row.namespace):
                    await self._advance(row)
            finally:
                await self._update(eval_run_id, lease_expires_at=None)
            if eval_run_id not in self._dirty:
                return

    async def _advance(self, row: t.EvalRun) -> None:
        spec = EvalSpec.model_validate(row.spec)
        results = await self.results(row.id)
        variants = {v["name"]: v for v in row.variants}
        for result in results:
            if result.run_id and result.status in ACTIVE:
                run = await self.store.get_run(row.tenant_id, result.run_id)
                if RunStatus(run.status).terminal:
                    await self._grade(row, spec, result, run)
                    await self._renew(row.id)  # judges can be slow: keep the lease while grading
        running = sum(1 for r in results if r.status == "running")
        if row.mode == "cases":
            capacity = spec.concurrency - running
            for result in results:
                if capacity <= 0:
                    break
                if result.status == "pending" and not result.run_id:
                    await self._start_case(row, result, variants[result.variant])
                    await self._renew(row.id)
                    capacity -= 1
        if any(r.status in ACTIVE for r in results):
            await self._update(row.id, summary=summarize(results), next_tick_at=clock.now() + TICK_INTERVAL)
            return
        await self._finish(row, spec, results)

    async def _start_case(self, row: t.EvalRun, result: t.EvalResult, variant: dict[str, Any]) -> None:
        inputs = {**(result.case.get("inputs") or {}), **(variant.get("inputs") or {})}
        try:
            run = await self.engine.executor.start_run(
                row.tenant_id,
                row.workflow_name,
                inputs,
                version=result.workflow_version,
                started_by=f"eval:{row.id}",
                context={"eval": {"eval_run_id": row.id, "result_id": result.id, "eval": row.eval_name,
                                  "case": result.case_name, "variant": result.variant}},
                labels={"eval": row.eval_name, "eval_run": row.id, "variant": result.variant[:60]},
                initiator=row.initiator or None,
            )
        except Exception as exc:  # noqa: BLE001 - e.g. the case's inputs do not match the workflow
            result.status, result.score, result.passed = "error", 0.0, False
            result.error = {"message": str(exc)[:1000], "stage": "start"}
            result.finished_at = clock.now()
            await self._save(result)
            return
        result.run_id, result.status = run.id, "running"
        await self._save(result)

    async def _grade(self, row: t.EvalRun, spec: EvalSpec, result: t.EvalResult, run: t.Run) -> None:
        duration = (run.finished_at - run.started_at).total_seconds() * 1000 if run.started_at and run.finished_at else None
        case = result.case
        scope = {
            "outputs": run.outputs or {},
            "expect": case.get("expect") or {},
            "inputs": run.inputs,
            "case": case,
            "run": {"id": run.id, "status": run.status, "error": run.error, "usage": run.usage or {},
                    "duration_ms": duration, "workflow_version": run.workflow_version},
        }
        scores, score, grading_cost = await grade_case(self.engine, row.tenant_id, spec, scope)
        failed = run.status != "succeeded"
        require_success = spec.require_success if spec.require_success is not None else row.mode == "cases"
        if failed and require_success:
            score = 0.0  # graders can score an empty or partial output well; a failed run is not a pass
        result.outputs = run.outputs
        result.scores, result.score = scores, score
        result.passed = score >= spec.threshold
        result.error = {"message": f"workflow run {run.status}", "run_error": run.error, "stage": "run"} if failed else None
        result.status = "graded"
        result.cost_usd = round(float((run.usage or {}).get("cost_usd", 0.0)) + grading_cost, 6)
        result.duration_ms = duration
        result.finished_at = clock.now()
        await self._save(result)

    async def _finish(self, row: t.EvalRun, spec: EvalSpec, results: list[t.EvalResult]) -> None:
        summary = summarize(results)
        order = [v["name"] for v in row.variants]
        baseline_diff = self._diffs(await self.results(row.baseline_id), results) if row.baseline_id else {}
        variant_diff = self._variant_diffs(results, order)
        summary["baseline_diff"] = baseline_diff
        summary["variant_diff"] = variant_diff
        gate = {**row.gate, **evaluate_gate(row.gate, summary, baseline_diff or variant_diff)}
        await self._update(row.id, status="completed", summary=summary, gate=gate, finished_at=clock.now(), next_tick_at=None)
        row = await self.get(row.tenant_id, row.id)
        exports: dict[str, Any] = {}
        for ref in spec.sinks:
            exports[ref.platform or ref.sink] = await self._export_safely(row, ref.sink, ref.config, results, ref.platform)
        if exports:
            await self._update(row.id, exports=exports)
        await self.store.audit(
            row.tenant_id, f"eval:{row.id}", "eval.completed", f"{row.namespace}/{row.eval_name}", namespace=row.namespace,
            eval_run=row.id, score=summary.get("score"), gate_passed=gate["passed"], workflow_version=row.workflow_version,
        )

    async def cancel(self, tenant_id: str, eval_run_id: str) -> t.EvalRun:
        row = await self.get(tenant_id, eval_run_id)
        if row.status != "running":
            raise Conflict(f"eval run is {row.status}")
        for result in await self.results(row.id):
            if result.status in ACTIVE:
                if result.run_id and result.status == "running":
                    await self.engine.executor.request_cancel(tenant_id, result.run_id)
                result.status, result.finished_at = "cancelled", clock.now()
                await self._save(result)
        await self._update(row.id, status="cancelled", finished_at=clock.now(), next_tick_at=None,
                           summary=summarize(await self.results(row.id)))
        return await self.get(tenant_id, row.id)

    # ------------------------------------------------------------------ export

    async def trace(self, run: t.Run, *, name: str | None = None) -> Trace:
        steps = await self.store.get_steps(run.id)
        order = {s["id"]: i for i, s in enumerate(run.workflow.get("steps", []))}
        steps.sort(key=lambda s: order.get(s.step_id, 1_000))
        return Trace(
            id=run.id,
            name=name or run.workflow_name,
            input=run.inputs,
            output=run.outputs,
            status=run.status,
            started_at=_iso(run.started_at or run.created_at),
            finished_at=_iso(run.finished_at),
            error=run.error,
            usage=run.usage or {},
            metadata={"shoal_run_id": run.id, "namespace": run.namespace, "workflow": run.workflow_name,
                      "workflow_version": run.workflow_version, "labels": run.labels or {}},
            tags=[f"workflow:{run.workflow_name}", f"version:{run.workflow_version}"],
            spans=[
                TraceSpan(id=s.step_id, name=s.step_id, type=s.type, status=s.status, started_at=_iso(s.started_at),
                          finished_at=_iso(s.finished_at), output=s.output, error=s.error, usage=s.usage or {})
                for s in steps
                if s.status != "pending"
            ],
        )

    async def export(self, tenant_id: str, eval_run_id: str, sink: str | None, config: dict[str, Any],
                     platform: str | None = None) -> tuple[str, ExportResult]:
        row = await self.get(tenant_id, eval_run_id)
        if row.status == "running":
            raise Conflict("the eval run has not finished")
        name, resolved = await self.target(tenant_id, sink, config, platform)
        payload = await self._payload(row, await self.results(row.id))
        result = await self.sink(name).export_eval(payload, resolved)
        await self._update(row.id, exports={**row.exports, platform or name: {"status": "exported", "url": result.url,
                                                                              "ids": result.ids, "at": clock.now().isoformat()}})
        return name, result

    async def export_run(self, tenant_id: str, run_id: str, sink: str | None, config: dict[str, Any],
                         platform: str | None = None) -> tuple[str, ExportResult]:
        run = await self.store.get_run(tenant_id, run_id)
        name, resolved = await self.target(tenant_id, sink, config, platform)
        return name, await self.sink(name).export_trace(await self.trace(run), resolved)

    async def _export_safely(self, row: t.EvalRun, sink: str | None, config: dict[str, Any], results: list[t.EvalResult],
                             platform: str | None = None) -> dict[str, Any]:
        try:
            name, resolved = await self.target(row.tenant_id, sink, config, platform)
            result = await self.sink(name).export_eval(await self._payload(row, results), resolved)
        except Exception as exc:  # noqa: BLE001 - an export never fails the eval
            log.warning("eval run %s: export to %s failed: %s", row.id, platform or sink, exc)
            return {"status": "failed", "error": str(exc)[:500], "at": clock.now().isoformat()}
        return {"status": "exported", "url": result.url, "ids": result.ids, "at": clock.now().isoformat()}

    async def _payload(self, row: t.EvalRun, results: list[t.EvalResult]) -> EvalExport:
        cases = []
        for r in results:
            trace = None
            if r.run_id:
                try:
                    trace = await self.trace(await self.store.get_run(row.tenant_id, r.run_id), name=f"{row.eval_name}/{r.case_name}")
                except NotFound:
                    trace = None
            cases.append(ScoredCase(id=r.id, case=r.case, variant=r.variant, trace=trace, scores=r.scores or {},
                                    score=r.score, passed=r.passed))
        versions = "+".join(f"v{v['workflow_version']}" for v in row.variants)
        return EvalExport(
            eval_run_id=row.id,
            eval_name=row.eval_name,
            namespace=row.namespace,
            experiment=f"{row.eval_name} {row.workflow_name}@{versions} {row.id[-8:]}",
            summary=row.summary,
            metadata={"shoal_eval_run": row.id, "eval_version": row.eval_version, "workflow": row.workflow_name,
                      "variants": row.variants, "gate": row.gate},
            cases=cases,
        )


def _unique(cases: list[EvalCase]) -> list[EvalCase]:
    seen: dict[str, int] = {}
    out = []
    for case in cases:
        n = seen.get(case.name, 0)
        seen[case.name] = n + 1
        out.append(case if n == 0 else case.model_copy(update={"name": f"{case.name}-{n + 1}"}))
    return out


def _case_from_run(run: t.Run) -> dict[str, Any]:
    return {"name": run.id, "inputs": run.inputs, "expect": {}, "tags": [], "metadata": {"source_run": run.id}}
