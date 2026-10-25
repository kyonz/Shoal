"""Rendering for the CLI: tables for people, JSON and YAML for scripts."""

from __future__ import annotations

import enum
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

console = Console()
err = Console(stderr=True, soft_wrap=True)


class Format(enum.StrEnum):
    table = "table"
    json = "json"
    yaml = "yaml"
    name = "name"


Column = tuple[str, Callable[[dict[str, Any]], Any]]


def emit(data: Any, fmt: Format, columns: Sequence[Column] = (), *, name: Callable[[dict[str, Any]], str] | None = None) -> None:
    """Print one object or a list in the chosen format."""
    if fmt is Format.json:
        console.print_json(json.dumps(data, default=str))
        return
    if fmt is Format.yaml:
        print_yaml(data)
        return
    items = data if isinstance(data, list) else [data]
    if fmt is Format.name:
        for item in items:
            print(name(item) if name else item.get("name", ""))
        return
    if not columns:
        print_yaml(data)
        return
    table = Table(box=None, pad_edge=False, header_style="bold")
    for title, _ in columns:
        table.add_column(title, overflow="fold")
    for item in items:
        table.add_row(*(cell(fn(item)) for _, fn in columns))
    console.print(table)


def print_yaml(data: Any, *, documents: bool = False) -> None:
    if documents and isinstance(data, list):
        print(yaml.safe_dump_all(data, sort_keys=False, allow_unicode=True), end="")
    else:
        print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), end="")


def cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, dict | list):
        return json.dumps(value, default=str)
    return str(value)


def age(timestamp: str | None) -> str:
    """``2026-01-02T03:04:05Z`` -> ``3h`` (relative to now)."""
    if not timestamp:
        return "-"
    try:
        then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - then).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{max(seconds, 0)}s"


STATUS_STYLE = {
    "succeeded": "green", "failed": "red", "cancelled": "yellow", "running": "cyan", "waiting": "magenta",
    "created": "green", "updated": "cyan", "unchanged": "dim", "invalid": "red",
}


def styled(status: str | None) -> str:
    if not status:
        return "-"
    style = STATUS_STYLE.get(status)
    return f"[{style}]{status}[/{style}]" if style else status


def describe_event(event: dict[str, Any]) -> str:
    """One readable line for a run event."""
    data = event.get("data") or {}
    kind = event.get("type", "")
    if kind == "step.failed" or kind == "step.retrying":
        detail = data.get("error")
    elif kind.startswith("run.") and kind != "run.started":
        detail = data.get("error") or data.get("outputs")
    elif kind == "step.succeeded":
        detail = data.get("output")
    else:
        detail = data
    text = cell(detail) if detail not in (None, {}, []) else ""
    return text if len(text) <= 240 else text[:237] + "..."
