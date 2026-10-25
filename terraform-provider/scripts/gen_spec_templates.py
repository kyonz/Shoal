"""Write tfplugindocs templates that add a `spec` field reference to each
declarative resource page, generated from the engine's pydantic models.

Run from terraform-provider/:  (cd ../engine && uv run python ../terraform-provider/scripts/gen_spec_templates.py)
"""

from __future__ import annotations

import pathlib
from typing import Any

from shoal.core.models import RESOURCE_KINDS

OUT = pathlib.Path(__file__).resolve().parent.parent / "templates" / "resources"
TYPE_NAMES = {
    "Workflow": "workflow", "Agent": "agent", "Skill": "skill", "Connection": "connection", "Trigger": "trigger",
    "Campaign": "campaign", "SandboxProfile": "sandbox_profile", "Gateway": "gateway", "ModelPolicy": "model_policy",
    "Eval": "eval", "Identity": "identity", "CloudIdentity": "cloud_identity", "Blueprint": "blueprint", "SkillSource": "skill_source",
    "CustomIntegration": "custom_integration",
}


def type_of(prop: dict[str, Any], defs: dict[str, Any]) -> str:
    if "$ref" in prop:
        return "object"
    if "anyOf" in prop:
        kinds = [type_of(p, defs) for p in prop["anyOf"] if p.get("type") != "null"]
        return " or ".join(dict.fromkeys(kinds)) or "any"
    if "oneOf" in prop:
        return "object"
    if "enum" in prop:
        return "one of " + ", ".join(f"`{v}`" for v in prop["enum"])
    if "const" in prop:
        return f"`{prop['const']}`"
    t = prop.get("type", "any")
    if t == "array":
        return f"list of {type_of(prop.get('items', {}), defs)}"
    if t == "object":
        return "map" if "additionalProperties" in prop else "object"
    return {"integer": "number", "boolean": "bool"}.get(t, t)


def default_of(prop: dict[str, Any]) -> str:
    if "default" not in prop or prop["default"] in (None, [], {}):
        return ""
    d = prop["default"]
    if isinstance(d, bool):
        d = str(d).lower()
    return f" Default: `{d}`."


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for kind, model in RESOURCE_KINDS.items():
        name = TYPE_NAMES.get(kind)
        if not name:
            continue
        schema = model.model_fields["spec"].annotation.model_json_schema(mode="validation")
        defs = schema.get("$defs", {})
        required = set(schema.get("required", []))
        rows = []
        for field, prop in schema.get("properties", {}).items():
            req = "**required**" if field in required else "optional"
            desc = (prop.get("description") or "").replace("\n", " ").replace("{{", '{{"{{"}}')
            rows.append(f"| `{field}` | {type_of(prop, defs)} | {req} | {desc}{default_of(prop)} |")
        doc = (model.model_fields["spec"].annotation.__doc__ or "").strip().replace("{{", '{{"{{"}}')
        body = f'''---
page_title: "{{{{.Name}}}} {{{{.Type}}}} - {{{{.ProviderName}}}}"
subcategory: ""
description: |-
{{{{ .Description | plainmarkdown | trimspace | prefixlines "  " }}}}
---

# {{{{.Name}}}} ({{{{.Type}}}})

{{{{ .Description | trimspace }}}}

## Example Usage

{{{{ tffile (printf "examples/resources/%s/resource.tf" .Name) }}}}

{{{{ .SchemaMarkdown | trimspace }}}}

## `spec` fields

{doc + chr(10) + chr(10) if doc and not doc.startswith("Model") else ""}These are the top-level fields of the {kind} spec, the same as `spec:` in YAML. Nested objects are described in the
server's JSON Schema at `GET /v1/schemas` (also used by the UI builder for validation).

| Field | Type | | Description |
|---|---|---|---|
{chr(10).join(rows)}

## Import

{{{{ codefile "shell" (printf "examples/resources/%s/import.sh" .Name) }}}}
'''
        (OUT / f"{name}.md.tmpl").write_text(body)
        print("wrote", name)


if __name__ == "__main__":
    main()
