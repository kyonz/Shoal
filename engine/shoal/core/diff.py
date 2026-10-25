"""Structural diff between two resource documents (version history, rollback previews)."""

from __future__ import annotations

from typing import Any


def _escape(key: Any) -> str:
    return str(key).replace("~", "~0").replace("/", "~1")


def diff(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """Changes from ``before`` to ``after`` as ``{op, path, from?, to?}`` with JSON-pointer paths.

    ``op`` is ``add``, ``remove``, ``replace`` or ``reorder``. Lists of step-like dicts are matched by
    ``id`` so inserting a step reads as one ``add``, not a cascade of replacements.
    """
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[dict[str, Any]] = []
        for key in before.keys() | after.keys():
            sub = f"{path}/{_escape(key)}"
            if key not in after:
                out.append({"op": "remove", "path": sub, "from": before[key]})
            elif key not in before:
                out.append({"op": "add", "path": sub, "to": after[key]})
            else:
                out.extend(diff(before[key], after[key], sub))
        return sorted(out, key=lambda c: c["path"])
    if isinstance(before, list) and isinstance(after, list):
        if _keyed(before) and _keyed(after):
            return _diff_keyed(before, after, path)
        out = []
        for i in range(max(len(before), len(after))):
            sub = f"{path}/{i}"
            if i >= len(after):
                out.append({"op": "remove", "path": sub, "from": before[i]})
            elif i >= len(before):
                out.append({"op": "add", "path": sub, "to": after[i]})
            else:
                out.extend(diff(before[i], after[i], sub))
        return out
    if before == after:
        return []
    return [{"op": "replace", "path": path, "from": before, "to": after}]


def _keyed(items: list[Any]) -> bool:
    ids = [i.get("id") for i in items if isinstance(i, dict)]
    return bool(items) and len(ids) == len(items) and all(ids) and len(set(ids)) == len(ids)


def _diff_keyed(before: list[dict[str, Any]], after: list[dict[str, Any]], path: str) -> list[dict[str, Any]]:
    old = {i["id"]: i for i in before}
    new = {i["id"]: i for i in after}
    out: list[dict[str, Any]] = []
    for item in before:
        if item["id"] not in new:
            out.append({"op": "remove", "path": f"{path}/[id={item['id']}]", "from": item})
    for item in after:
        sub = f"{path}/[id={item['id']}]"
        if item["id"] not in old:
            out.append({"op": "add", "path": sub, "to": item})
        else:
            out.extend(diff(old[item["id"]], item, sub))
    if [i["id"] for i in before if i["id"] in new] != [i["id"] for i in after if i["id"] in old]:
        out.append({"op": "reorder", "path": path, "from": [i["id"] for i in before], "to": [i["id"] for i in after]})
    return out
