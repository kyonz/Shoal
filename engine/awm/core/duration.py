"""Human friendly durations (``30s``, ``15m``, ``7d``, ``1h30m``) as a pydantic type."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema

_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w)")


def parse_duration(value: Any) -> timedelta:
    if isinstance(value, timedelta):
        return value
    if isinstance(value, int | float):
        return timedelta(seconds=value)
    if not isinstance(value, str):
        raise ValueError(f"invalid duration: {value!r}")
    text = value.strip().lower().replace(" ", "")
    if text.isdigit():
        return timedelta(seconds=int(text))
    pos, total = 0, 0.0
    for match in _PART.finditer(text):
        if match.start() != pos:
            raise ValueError(f"invalid duration: {value!r}")
        total += float(match.group(1)) * _UNITS[match.group(2)]
        pos = match.end()
    if pos != len(text) or pos == 0:
        raise ValueError(f"invalid duration: {value!r}")
    return timedelta(seconds=total)


def format_duration(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    if seconds == 0:
        return "0s"
    out = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, seconds = divmod(seconds, size)
        if count:
            out.append(f"{count}{unit}")
    return "".join(out)


Duration = Annotated[
    timedelta,
    BeforeValidator(parse_duration),
    PlainSerializer(format_duration, return_type=str),
    WithJsonSchema({"type": "string", "pattern": r"^(\d+(\.\d+)?(ms|s|m|h|d|w))+$", "examples": ["30m", "7d"]}),
]
