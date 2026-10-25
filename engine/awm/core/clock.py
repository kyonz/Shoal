"""Injectable clock so long waits (days) can be tested by moving time forward."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_offset = timedelta(0)


def now() -> datetime:
    return datetime.now(UTC) + _offset


def advance(delta: timedelta) -> None:
    """Move the engine clock forward (tests and simulations only)."""
    global _offset
    _offset += delta


def reset() -> None:
    global _offset
    _offset = timedelta(0)
