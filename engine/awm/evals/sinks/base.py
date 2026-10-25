"""Eval sink contract (``awm.eval_sinks`` entry point group).

An eval sink connects AWM to an eval platform such as Braintrust or Langfuse:

* ``export_eval`` pushes a finished eval run (one trace per case, with step
  spans, and every grader score) so the platform's experiment views and
  dashboards work on AWM data;
* ``export_trace`` pushes one workflow run as a trace (production runs);
* ``import_dataset`` pulls a dataset's items as eval cases.

Sink settings come from the Eval spec (``sinks[].config`` / ``dataset.config``)
or the API call. Secrets are referenced by name (``api_key_secret``, ...) and
resolved by :func:`resolve_config` before the sink sees them.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from awm.core.errors import InvalidRequest
from awm.core.models import EvalCase

if TYPE_CHECKING:
    from awm.engine import Engine


@dataclass
class TraceSpan:
    """One step of a workflow run."""

    id: str
    name: str
    type: str
    status: str
    started_at: str | None
    finished_at: str | None
    output: Any = None
    error: Any = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trace:
    """One workflow run, as exported to an eval platform."""

    id: str
    name: str
    input: Any
    output: Any
    status: str
    started_at: str | None
    finished_at: str | None
    error: Any = None
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    spans: list[TraceSpan] = field(default_factory=list)


@dataclass
class ScoredCase:
    """One case of an eval run: its trace (absent if the run never started) and scores."""

    id: str
    case: dict[str, Any]
    variant: str
    trace: Trace | None
    scores: dict[str, dict[str, Any]]
    score: float | None
    passed: bool | None


@dataclass
class EvalExport:
    eval_run_id: str
    eval_name: str
    namespace: str
    experiment: str
    summary: dict[str, Any]
    metadata: dict[str, Any]
    cases: list[ScoredCase]


@dataclass
class ExportResult:
    url: str | None = None
    ids: dict[str, Any] = field(default_factory=dict)


class EvalSink(abc.ABC):
    name: str
    description: str = ""
    config_schema: dict[str, Any] = {}

    def setup(self, engine: Engine) -> None:
        self.engine = engine

    def client(self, base_url: str, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=60, transport=self.engine.http_transport, **kwargs)

    @abc.abstractmethod
    async def export_eval(self, export: EvalExport, config: dict[str, Any]) -> ExportResult: ...

    @abc.abstractmethod
    async def export_trace(self, trace: Trace, config: dict[str, Any]) -> ExportResult: ...

    @abc.abstractmethod
    async def import_dataset(self, name: str, config: dict[str, Any], *, limit: int | None = None) -> list[EvalCase]: ...


async def resolve_config(engine: Engine, tenant_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Replace ``<field>_secret`` entries with ``<field>`` holding the secret's value."""
    out: dict[str, Any] = {}
    for key, value in config.items():
        if key.endswith("_secret") and isinstance(value, str):
            secret = await engine.secrets.get(tenant_id, value)
            if secret is None:
                raise InvalidRequest(f"secret '{value}' ({key}) is not set")
            out[key.removesuffix("_secret")] = secret
        else:
            out[key] = value
    return out


def require(config: dict[str, Any], *keys: str, sink: str) -> None:
    missing = [k for k in keys if not config.get(k)]
    if missing:
        raise InvalidRequest(f"{sink} sink needs: {', '.join(missing)} (secrets as <name>_secret)")


def case_name(item_id: str, index: int, metadata: dict[str, Any] | None) -> str:
    name = (metadata or {}).get("name") or item_id or f"case-{index + 1}"
    return str(name)[:300]


def as_dict(value: Any, key: str = "input") -> dict[str, Any]:
    """Dataset inputs become workflow inputs: objects as-is, anything else under one key."""
    if isinstance(value, dict):
        return value
    return {} if value is None else {key: value}
