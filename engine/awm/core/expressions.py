"""``{{ expr }}`` expressions evaluated in a sandboxed Jinja2 environment.

Plain Jinja delimiters (rather than ``{{ }}``) keep specs writable inline in
Terraform HCL, which interpolates ``${...}`` itself. In YAML, quote a value
that starts with ``{{``.

* A string that is exactly one expression evaluates to the native value
  (``"{{ steps.fix.output.pr_number }}"`` -> ``42``).
* Any other string containing expressions is rendered as text.
* Dicts and lists are evaluated recursively.

Undefined names evaluate to ``None`` (native) or ``""`` (text) instead of
raising, so optional upstream outputs are easy to work with.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from jinja2 import ChainableUndefined, TemplateError
from jinja2.nativetypes import NativeEnvironment
from jinja2.sandbox import ImmutableSandboxedEnvironment

from awm.core.duration import parse_duration

EXPR = re.compile(r"\{\{\s*(.+?)\s*\}\}", re.DOTALL)
_SINGLE = re.compile(r"^\s*\{\{\s*(.+?)\s*\}\}\s*$", re.DOTALL)
SECRET_REF = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_\-]*)|secrets\[['\"]([^'\"]+)['\"]\]")


class ExpressionError(ValueError):
    pass


class _Undefined(ChainableUndefined):
    def __str__(self) -> str:
        return ""

    def __bool__(self) -> bool:
        return False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _add_duration(ts: str | datetime, duration: str) -> str:
    base = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    return (base + parse_duration(duration)).isoformat()


def _setup(env: Any) -> Any:
    env.filters.update(
        tojson=lambda v, indent=None: json.dumps(v, indent=indent, default=str),
        fromjson=lambda v: json.loads(v) if isinstance(v, str) else v,
        add_duration=_add_duration,
        duration_seconds=lambda v: parse_duration(v).total_seconds(),
    )
    env.globals.update(now=_now, timedelta=timedelta, len=len, min=min, max=max)
    return env


class _SandboxedNative(ImmutableSandboxedEnvironment, NativeEnvironment):
    pass


_text_env = _setup(ImmutableSandboxedEnvironment(undefined=_Undefined, autoescape=False))
_native_env = _setup(_SandboxedNative(undefined=_Undefined, autoescape=False))


def _plain(value: Any) -> Any:
    if isinstance(value, ChainableUndefined):
        return None
    return value


def is_expression(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value


def evaluate_expression(expr: str, context: Mapping[str, Any]) -> Any:
    """Evaluate a bare expression (no ``{{ }}``), returning a native value."""
    try:
        compiled = _native_env.compile_expression(expr, undefined_to_none=False)
        return _plain(compiled(**context))
    except TemplateError as exc:
        raise ExpressionError(f"error in expression '{expr}': {exc}") from exc


def render(value: Any, context: Mapping[str, Any]) -> Any:
    """Evaluate expressions inside ``value`` (recursively for dicts/lists)."""
    if isinstance(value, str):
        if "{{" not in value:
            return value
        single = _SINGLE.match(value)
        if single and "{{" not in single.group(1):
            return evaluate_expression(single.group(1), context)
        try:
            return _text_env.from_string(value).render(**context)
        except TemplateError as exc:
            raise ExpressionError(f"error rendering '{value[:80]}': {exc}") from exc
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    return value


def truthy(expr: str | None, context: Mapping[str, Any]) -> bool:
    """Evaluate a condition. Accepts ``{{ x }}`` or a bare expression."""
    if expr is None:
        return True
    single = _SINGLE.match(expr)
    result = evaluate_expression(single.group(1) if single else expr, context)
    if isinstance(result, str):
        return result.strip().lower() not in ("", "false", "0", "no", "none", "null")
    return bool(result)


def referenced_secrets(value: Any) -> set[str]:
    """Names of secrets referenced anywhere inside ``value``."""
    found: set[str] = set()
    if isinstance(value, str):
        for match in SECRET_REF.finditer(value):
            found.add(match.group(1) or match.group(2))
    elif isinstance(value, dict):
        for v in value.values():
            found |= referenced_secrets(v)
    elif isinstance(value, list):
        for v in value:
            found |= referenced_secrets(v)
    return found
