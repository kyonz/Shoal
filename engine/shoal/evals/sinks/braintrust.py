"""Braintrust eval sink.

Eval runs become Braintrust experiments (one root span per case with a child
span per workflow step, and every grader score); production runs go to the
project's logs; datasets are imported as eval cases.

Config: ``project`` (name, required), ``api_key_secret`` (required),
``api_url`` (default ``https://api.braintrust.dev``), ``app_url`` (default
``https://www.braintrust.dev``) and ``org`` (organisation name, used to build
experiment links).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from shoal.core.errors import NotFound
from shoal.core.models import EvalCase
from shoal.evals.sinks.base import (
    EvalExport,
    EvalSink,
    ExportResult,
    Trace,
    as_dict,
    case_name,
    require,
)

API_URL = "https://api.braintrust.dev"
APP_URL = "https://www.braintrust.dev"
_SPAN_TYPES = {"agent": "llm", "decide": "llm", "grade": "score", "action": "tool", "script": "tool"}


def _ts(value: str | None) -> float | None:
    return datetime.fromisoformat(value).timestamp() if value else None


def _metrics(start: str | None, end: str | None, usage: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {"start": _ts(start), "end": _ts(end)}
    if "input_tokens" in usage:
        metrics["prompt_tokens"] = usage["input_tokens"]
    if "output_tokens" in usage:
        metrics["completion_tokens"] = usage["output_tokens"]
    if "cost_usd" in usage:
        metrics["estimated_cost"] = usage["cost_usd"]
    return {k: v for k, v in metrics.items() if v is not None}


def trace_events(trace: Trace, root_id: str, *, extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    root = {
        "id": root_id,
        "span_id": root_id,
        "root_span_id": root_id,
        "input": trace.input,
        "output": trace.output,
        "error": trace.error,
        "metadata": trace.metadata,
        "tags": trace.tags or None,
        "metrics": _metrics(trace.started_at, trace.finished_at, trace.usage),
        "span_attributes": {"name": trace.name, "type": "task"},
        **(extra or {}),
    }
    events = [{k: v for k, v in root.items() if v is not None}]
    for span in trace.spans:
        span_id = f"{root_id}:{span.id}"
        event = {
            "id": span_id,
            "span_id": span_id,
            "root_span_id": root_id,
            "span_parents": [root_id],
            "output": span.output,
            "error": span.error,
            "metadata": {"step_type": span.type, "status": span.status},
            "metrics": _metrics(span.started_at, span.finished_at, span.usage),
            "span_attributes": {"name": span.name, "type": _SPAN_TYPES.get(span.type, "function")},
        }
        events.append({k: v for k, v in event.items() if v is not None})
    return events


class BraintrustSink(EvalSink):
    name = "braintrust"
    description = "Braintrust experiments, logs and datasets"
    config_schema = {
        "type": "object",
        "properties": {
            "project": {"type": "string"},
            "api_key_secret": {"type": "string"},
            "api_url": {"type": "string", "default": API_URL},
            "app_url": {"type": "string", "default": APP_URL},
            "org": {"type": "string"},
        },
        "required": ["project", "api_key_secret"],
    }

    def _client(self, config: dict[str, Any]):
        require(config, "api_key", "project", sink=self.name)
        return self.client(config.get("api_url") or API_URL, headers={"Authorization": f"Bearer {config['api_key']}"})

    async def _project_id(self, client: Any, config: dict[str, Any]) -> str:
        r = await client.post("/v1/project", json={"name": config["project"]})  # returns the existing project
        r.raise_for_status()
        return r.json()["id"]

    async def export_eval(self, export: EvalExport, config: dict[str, Any]) -> ExportResult:
        async with self._client(config) as client:
            project_id = await self._project_id(client, config)
            r = await client.post(
                "/v1/experiment",
                json={
                    "project_id": project_id,
                    "name": export.experiment,
                    "description": f"Shoal eval {export.namespace}/{export.eval_name} ({export.eval_run_id})",
                    "metadata": export.metadata,
                    "ensure_new": True,
                },
            )
            r.raise_for_status()
            experiment = r.json()
            events: list[dict[str, Any]] = []
            for case in export.cases:
                scores = {name: s.get("score") for name, s in case.scores.items()}
                extra = {
                    "expected": case.case.get("expect") or None,
                    "scores": scores,
                    "metadata": {
                        **(case.trace.metadata if case.trace else {}),
                        **(case.case.get("metadata") or {}),
                        "case": case.case.get("name"),
                        "variant": case.variant,
                        "passed": case.passed,
                        "grader_details": {n: s.get("detail") or s.get("error") for n, s in case.scores.items()},
                    },
                }
                if case.trace is None:
                    events.append({"id": case.id, "input": case.case.get("inputs"), **extra})
                else:
                    events += trace_events(case.trace, case.id, extra=extra)
            for i in range(0, len(events), 200):
                r = await client.post(f"/v1/experiment/{experiment['id']}/insert", json={"events": events[i : i + 200]})
                r.raise_for_status()
        url = None
        if config.get("org"):
            app = (config.get("app_url") or APP_URL).rstrip("/")
            url = f"{app}/app/{config['org']}/p/{config['project']}/experiments/{experiment.get('name', export.experiment)}"
        return ExportResult(url=url, ids={"project_id": project_id, "experiment_id": experiment["id"]})

    async def export_trace(self, trace: Trace, config: dict[str, Any]) -> ExportResult:
        async with self._client(config) as client:
            project_id = await self._project_id(client, config)
            r = await client.post(f"/v1/project_logs/{project_id}/insert", json={"events": trace_events(trace, trace.id)})
            r.raise_for_status()
        return ExportResult(ids={"project_id": project_id, "root_span_id": trace.id})

    async def import_dataset(self, name: str, config: dict[str, Any], *, limit: int | None = None) -> list[EvalCase]:
        async with self._client(config) as client:
            r = await client.get("/v1/dataset", params={"project_name": config["project"], "dataset_name": name})
            r.raise_for_status()
            objects = r.json().get("objects") or []
            if not objects:
                raise NotFound(f"Braintrust dataset '{name}' not found in project '{config['project']}'")
            dataset_id = objects[0]["id"]
            cases: list[EvalCase] = []
            cursor = None
            while limit is None or len(cases) < limit:
                body: dict[str, Any] = {"limit": min(100, limit - len(cases)) if limit else 100}
                if cursor:
                    body["cursor"] = cursor
                r = await client.post(f"/v1/dataset/{dataset_id}/fetch", json=body)
                r.raise_for_status()
                data = r.json()
                for item in data.get("events") or []:
                    metadata = item.get("metadata") or {}
                    cases.append(
                        EvalCase(
                            name=case_name(item.get("id", ""), len(cases), metadata),
                            inputs=as_dict(item.get("input")),
                            expect=as_dict(item.get("expected"), "output"),
                            tags=list(item.get("tags") or []),
                            metadata={**metadata, "braintrust_id": item.get("id")},
                        )
                    )
                cursor = data.get("cursor")
                if not cursor or not data.get("events"):
                    break
        return cases[:limit] if limit else cases
