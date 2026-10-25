"""A small JSON Schema validator for the ``json_schema`` grader.

Covers the keywords graders need in practice (draft 2020-12 subset): ``type``,
``enum``, ``const``, ``properties``, ``required``, ``additionalProperties``,
``items``, ``minItems``/``maxItems``, ``minLength``/``maxLength``, ``pattern``,
``minimum``/``maximum``, ``anyOf``/``oneOf``/``allOf`` and ``not``. Unknown
keywords are ignored.
"""

from __future__ import annotations

import re
from typing import Any

_TYPES: dict[str, Any] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list | tuple),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: dict[str, Any] | bool, path: str = "$") -> list[str]:
    """Return a list of error messages (empty when ``value`` is valid)."""
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: not allowed"]
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPES.get(t, lambda v: True)(value) for t in types):
            return [f"{path}: expected {' or '.join(types)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must be {schema['const']!r}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match {schema['pattern']!r}")
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: '{name}' is required")
        for name, item in value.items():
            if name in props:
                errors += validate(item, props[name], f"{path}.{name}")
            elif "additionalProperties" in schema:
                errors += validate(item, schema["additionalProperties"], f"{path}.{name}")
    if isinstance(value, list | tuple):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict | bool):
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], f"{path}[{i}]")
    for sub in schema.get("allOf", []):
        errors += validate(value, sub, path)
    if "anyOf" in schema and not any(not validate(value, sub, path) for sub in schema["anyOf"]):
        errors.append(f"{path}: matches none of anyOf")
    if "oneOf" in schema and sum(not validate(value, sub, path) for sub in schema["oneOf"]) != 1:
        errors.append(f"{path}: must match exactly one of oneOf")
    if "not" in schema and not validate(value, schema["not"], path):
        errors.append(f"{path}: must not match 'not'")
    return errors
