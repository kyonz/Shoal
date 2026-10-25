"""Langfuse eval sink.

Each case of an eval run becomes a Langfuse trace (with a span per workflow
step) carrying every grader score; traces of one eval run share a session,
and cases imported from a Langfuse dataset are linked to a dataset run so
Langfuse's experiment comparison works. Production runs export as traces.

Config: ``public_key`` (or ``public_key_secret``), ``secret_key_secret``
(required), ``host`` (default ``https://cloud.langfuse.com``) and
``project_id`` (used to build links).
"""

from __future__ import annotations

from typing import Any

from shoal.core import clock
from shoal.core.ids import new_id
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

HOST = "https://cloud.langfuse.com"


def _event(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": new_id("lf"),
        "timestamp": clock.now().isoformat(),
        "type": kind,
        "body": {k: v for k, v in body.items() if v is not None},
    }


def trace_batch(trace: Trace, trace_id: str, *, session_id: str | None = None, extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    batch = [
        _event(
            "trace-create",
            {
                "id": trace_id,
                "name": trace.name,
                "timestamp": trace.started_at,
                "input": trace.input,
                "output": trace.output,
                "sessionId": session_id,
                "metadata": {**trace.metadata, "status": trace.status, "error": trace.error, "usage": trace.usage},
                "tags": trace.tags or None,
                **(extra or {}),
            },
        )
    ]
    for span in trace.spans:
        batch.append(
            _event(
                "span-create",
                {
                    "id": f"{trace_id}-{span.id}",
                    "traceId": trace_id,
                    "name": span.name,
                    "startTime": span.started_at,
                    "endTime": span.finished_at,
                    "output": span.output,
                    "metadata": {"step_type": span.type, "status": span.status, "usage": span.usage or None},
                    "level": "ERROR" if span.status == "failed" else None,
                    "statusMessage": str(span.error)[:1000] if span.error else None,
                },
            )
        )
    return batch


class LangfuseSink(EvalSink):
    name = "langfuse"
    description = "Langfuse traces, scores, datasets and dataset runs"
    config_schema = {
        "type": "object",
        "properties": {
            "public_key": {"type": "string"},
            "public_key_secret": {"type": "string"},
            "secret_key_secret": {"type": "string"},
            "host": {"type": "string", "default": HOST},
            "project_id": {"type": "string"},
        },
        "required": ["secret_key_secret"],
    }

    def _client(self, config: dict[str, Any]):
        require(config, "public_key", "secret_key", sink=self.name)
        return self.client(config.get("host") or HOST, auth=(config["public_key"], config["secret_key"]))

    async def _ingest(self, client: Any, batch: list[dict[str, Any]]) -> None:
        for i in range(0, len(batch), 200):
            r = await client.post("/api/public/ingestion", json={"batch": batch[i : i + 200]})
            r.raise_for_status()
            errors = (r.json() or {}).get("errors") if r.content else None
            if errors:
                raise RuntimeError(f"Langfuse rejected {len(errors)} events: {errors[0]}")

    async def export_eval(self, export: EvalExport, config: dict[str, Any]) -> ExportResult:
        batch: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        for case in export.cases:
            trace_id = case.id
            metadata = {"eval_run": export.eval_run_id, "eval": export.eval_name, "case": case.case.get("name"), "variant": case.variant}
            if case.trace is not None:
                batch += trace_batch(
                    case.trace, trace_id, session_id=export.eval_run_id,
                    extra={"metadata": {**case.trace.metadata, **metadata, "expect": case.case.get("expect")}},
                )
            else:
                batch.append(_event("trace-create", {"id": trace_id, "name": export.eval_name, "sessionId": export.eval_run_id,
                                                     "input": case.case.get("inputs"), "metadata": metadata}))
            for name, score in case.scores.items():
                detail = score.get("detail") or {}
                comment = detail.get("reasoning") or score.get("error")
                batch.append(
                    _event(
                        "score-create",
                        {"id": f"{trace_id}-{name}", "traceId": trace_id, "name": name, "value": score.get("score", 0.0),
                         "dataType": "NUMERIC", "comment": comment},
                    )
                )
            item_id = (case.case.get("metadata") or {}).get("langfuse_item_id")
            if item_id:
                links.append({"runName": export.experiment, "datasetItemId": item_id, "traceId": trace_id,
                              "metadata": {"variant": case.variant, **export.metadata}})
        async with self._client(config) as client:
            await self._ingest(client, batch)
            for link in links:
                r = await client.post("/api/public/dataset-run-items", json=link)
                r.raise_for_status()
        url = None
        if config.get("project_id"):
            url = f"{(config.get('host') or HOST).rstrip('/')}/project/{config['project_id']}/sessions/{export.eval_run_id}"
        return ExportResult(url=url, ids={"session_id": export.eval_run_id, "dataset_run": export.experiment if links else None})

    async def export_trace(self, trace: Trace, config: dict[str, Any]) -> ExportResult:
        async with self._client(config) as client:
            await self._ingest(client, trace_batch(trace, trace.id))
        url = None
        if config.get("project_id"):
            url = f"{(config.get('host') or HOST).rstrip('/')}/project/{config['project_id']}/traces/{trace.id}"
        return ExportResult(url=url, ids={"trace_id": trace.id})

    async def import_dataset(self, name: str, config: dict[str, Any], *, limit: int | None = None) -> list[EvalCase]:
        cases: list[EvalCase] = []
        page = 1
        async with self._client(config) as client:
            while limit is None or len(cases) < limit:
                r = await client.get("/api/public/dataset-items", params={"datasetName": name, "page": page, "limit": 50})
                r.raise_for_status()
                data = r.json()
                for item in data.get("data") or []:
                    if item.get("status") == "ARCHIVED":
                        continue
                    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                    cases.append(
                        EvalCase(
                            name=case_name(item.get("id", ""), len(cases), metadata),
                            inputs=as_dict(item.get("input")),
                            expect=as_dict(item.get("expectedOutput"), "output"),
                            metadata={**metadata, "langfuse_item_id": item.get("id")},
                        )
                    )
                if page >= int((data.get("meta") or {}).get("totalPages") or 1):
                    break
                page += 1
        return cases[:limit] if limit else cases
