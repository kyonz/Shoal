"""Load resources from YAML/JSON files or text (multi-document)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from shoal.core.models import ResourceBase, parse_resource


def load_text(text: str) -> list[ResourceBase]:
    documents = [d for d in yaml.safe_load_all(text) if d]
    out: list[ResourceBase] = []
    for doc in documents:
        if isinstance(doc, dict) and doc.get("kind") == "List":
            out.extend(parse_resource(item) for item in doc.get("items", []))
        else:
            out.append(parse_resource(doc))
    return out


def load_path(path: str | Path) -> list[ResourceBase]:
    """Load a file, or every *.yaml / *.yml / *.json file under a directory (sorted)."""
    path = Path(path)
    files = sorted(p for p in path.rglob("*") if p.suffix in (".yaml", ".yml", ".json")) if path.is_dir() else [path]
    out: list[ResourceBase] = []
    for file in files:
        out.extend(load_text(file.read_text()))
    return out


def dump(resources: list[ResourceBase]) -> str:
    docs: list[dict[str, Any]] = [r.to_document() for r in resources]
    return yaml.safe_dump_all(docs, sort_keys=False)
