"""Aggregation, version comparison and gates. Pure functions over result rows."""

from __future__ import annotations

from collections.abc import Iterable
from statistics import fmean
from typing import Any

from awm.store import tables as t


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return round(fmean(values), 4) if values else None


def summarize_variant(results: list[t.EvalResult]) -> dict[str, Any]:
    graded = [r for r in results if r.status in ("graded", "error") and r.score is not None]
    graders: dict[str, list[float]] = {}
    for r in graded:
        for name, s in (r.scores or {}).items():
            graders.setdefault(name, []).append(float(s.get("score", 0.0)))
    return {
        "workflow_version": results[0].workflow_version if results else None,
        "cases": len(results),
        "graded": len(graded),
        "errors": sum(1 for r in results if r.status == "error"),
        "pending": sum(1 for r in results if r.status in ("pending", "running")),
        "score": _mean(r.score for r in graded if r.score is not None),
        "pass_rate": _mean(1.0 if r.passed else 0.0 for r in graded),
        "graders": {name: _mean(v) for name, v in graders.items()},
        "cost_usd": round(sum(r.cost_usd for r in results), 6),
        "mean_duration_ms": _mean(r.duration_ms for r in results if r.duration_ms is not None),
    }


def summarize(results: list[t.EvalResult]) -> dict[str, Any]:
    by_variant: dict[str, list[t.EvalResult]] = {}
    for r in results:
        by_variant.setdefault(r.variant, []).append(r)
    variants = {name: summarize_variant(rows) for name, rows in by_variant.items()}
    overall = summarize_variant(results)
    overall.pop("workflow_version")
    return {**overall, "variants": variants}


def compare(baseline: list[t.EvalResult], candidate: list[t.EvalResult]) -> dict[str, Any]:
    """Case-by-case diff of two sets of results (matched by case name)."""
    base = {r.case_name: r for r in baseline}
    cases = []
    regressions, improvements = [], []
    for r in candidate:
        b = base.get(r.case_name)
        if b is None or r.score is None or b.score is None:
            continue
        delta = round(r.score - b.score, 4)
        graders = {
            name: round(float(s.get("score", 0)) - float((b.scores or {}).get(name, {}).get("score", 0)), 4)
            for name, s in (r.scores or {}).items()
            if name in (b.scores or {})
        }
        cases.append({"case": r.case_name, "baseline": b.score, "candidate": r.score, "delta": delta, "graders": graders})
        if b.passed and not r.passed:
            regressions.append(r.case_name)
        elif r.passed and not b.passed:
            improvements.append(r.case_name)
    b_sum, c_sum = summarize_variant(baseline), summarize_variant(candidate)
    shared = {c["case"] for c in cases}
    b_shared = _mean(r.score for r in baseline if r.case_name in shared and r.score is not None)
    c_shared = _mean(r.score for r in candidate if r.case_name in shared and r.score is not None)
    return {
        "baseline": {"workflow_version": b_sum["workflow_version"], "score": b_sum["score"], "pass_rate": b_sum["pass_rate"]},
        "candidate": {"workflow_version": c_sum["workflow_version"], "score": c_sum["score"], "pass_rate": c_sum["pass_rate"]},
        "shared_cases": len(cases),
        "delta": round(c_shared - b_shared, 4) if b_shared is not None and c_shared is not None else None,
        "graders": {
            name: round(c - b, 4)
            for name, c in (c_sum["graders"] or {}).items()
            if c is not None and (b := (b_sum["graders"] or {}).get(name)) is not None
        },
        "regressions": sorted(regressions),
        "improvements": sorted(improvements),
        "cases": sorted(cases, key=lambda c: c["delta"]),
    }


def evaluate_gate(gate: dict[str, Any], summary: dict[str, Any], diffs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Every variant must reach ``fail_under`` and must not drop more than ``max_regression`` against the baseline."""
    reasons: list[str] = []
    fail_under, max_regression = gate.get("fail_under"), gate.get("max_regression")
    for name, v in summary.get("variants", {}).items():
        score = v.get("score")
        if fail_under is not None and (score is None or score < fail_under):
            reasons.append(f"{name}: score {score} is below {fail_under}")
        diff = diffs.get(name)
        if max_regression is not None and diff and diff.get("delta") is not None and -diff["delta"] > max_regression:
            reasons.append(f"{name}: score dropped {-diff['delta']:.4f} against the baseline (max {max_regression})")
    return {"passed": not reasons, "reasons": reasons, "fail_under": fail_under, "max_regression": max_regression}
