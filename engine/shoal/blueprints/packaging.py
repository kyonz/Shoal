"""Save a workflow as a blueprint (architecture §10.1).

Packaging gathers a workflow and what it uses from its namespace (agents,
skills, child workflows, and the triggers and campaigns that start it), finds
the values that tie it to one team (connections, secrets, repositories, Jira
projects, models, sandbox profiles, cloud identities) and offers each one as a
blueprint parameter. Clear-text credentials are always stripped: a credential
in a field becomes a required secret parameter, and a token found in free text
is redacted. Included resources are renamed to ``{{ prefix }}name`` and
references between them follow, so a namespace can adopt the blueprint twice.

``package`` only builds the blueprint document; publishing it is
``BlueprintService.save_from_workflow``.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from shoal.core.errors import InvalidRequest, NotFound
from shoal.core.expressions import EXPR, SECRET_REF
from shoal.core.scope import SHARED_NAMESPACE

Category = Literal[
    "connection", "secret", "credential", "repository", "organization", "jira_project", "model", "sandbox",
    "cloud_identity",
]
CATEGORY_ORDER: tuple[Category, ...] = (
    "credential", "connection", "secret", "repository", "organization", "jira_project", "model", "sandbox",
    "cloud_identity",
)
PREFIX = "{{ prefix }}"
PARAM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Field names whose clear-text value is a credential. Narrower than models.CREDENTIAL_NAME: a bare
# `key` is a Jira issue key far more often than a secret, and `*_secret` fields name a secret.
CREDENTIAL_KEY = re.compile(
    r"(^|[-_])(authorization|api[-_]?key|access[-_]?key|private[-_]?key|secret[-_]?key|token|password|passwd|"
    r"cookie|credentials?|client[-_]?secret)($|[-_])",
    re.I,
)
# Credentials recognisable by their shape, wherever they appear.
TOKEN_PATTERNS = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ATATT[A-Za-z0-9_=-]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|"
    r"(?i:bearer)\s+[A-Za-z0-9._~+/-]{20,}=*"
)
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
REPO_URL = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]*[A-Za-z0-9_-])")
REPO_KEYS = {"repo", "repository", "full_name", "repo_full_name"}
REPO_LIST_KEYS = {"repos", "repositories"}
ORG_KEYS = {"org", "organization", "owner"}
JIRA_PROJECT = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")
JIRA_PROJECT_KEYS = {"project", "project_key", "projectKey"}
JQL_PROJECT = re.compile(r"\bproject\s*(?:=|in\s*\()\s*[\"']?([A-Z][A-Z0-9_]{1,9})\b", re.I)


@dataclass
class Location:
    index: int
    path: tuple[str | int, ...]
    mode: Literal["whole", "text", "secret"] = "whole"
    pattern: re.Pattern[str] | None = None


@dataclass
class Finding:
    id: str
    category: Category
    value: str
    param: str
    type: str
    description: str
    integration: str | None = None
    default: Any = None
    required: bool = True
    enabled: bool = True
    locked: bool = False
    locations: list[Location] = field(default_factory=list)


@dataclass
class Candidate:
    kind: str
    name: str
    namespace: str
    reason: str
    document: dict[str, Any]
    included: bool = True
    root: bool = False

    @property
    def shared(self) -> bool:
        return self.namespace == SHARED_NAMESPACE


@dataclass
class Package:
    candidates: list[Candidate]
    findings: list[Finding]
    params: dict[str, dict[str, Any]]
    resources: list[dict[str, Any]]
    stripped: list[str]
    warnings: list[str]
    labels: list[str] = field(default_factory=list)

    def location(self, loc: Location) -> str:
        return self.labels[loc.index] + " · " + _dotted(self.resources[loc.index], loc.path)


# --------------------------------------------------------------------------- collecting


async def collect(store: Any, tenant_id: str, namespace: str, workflow: str) -> tuple[list[Candidate], list[str]]:
    """The workflow and the resources it uses (closure), with the triggers and campaigns that start it."""
    candidates: dict[tuple[str, str], Candidate] = {}
    warnings: list[str] = []
    queue: list[tuple[str, str, str]] = [("Workflow", workflow, "the workflow")]
    while queue:
        kind, name, reason = queue.pop(0)
        if (kind, name) in candidates:
            continue
        try:
            row = await store.get_resource_row(tenant_id, kind, name, namespace=namespace)
        except NotFound:
            if not candidates:
                raise
            warnings.append(f"{kind} '{name}' ({reason}) does not exist; adopters need one with that name")
            continue
        candidate = Candidate(kind, name, row.namespace, reason, _template(row), included=row.namespace == namespace,
                              root=not candidates)
        candidates[(kind, name)] = candidate
        if candidate.shared:  # visible from every namespace: referenced, not copied
            continue
        queue.extend(_references(kind, name, candidate.document["spec"]))
    for row in await store.list_resources(tenant_id, "Campaign", namespace=namespace):
        if (row.source or row.document["spec"]).get("workflow") == workflow:
            candidates[("Campaign", row.name)] = Candidate("Campaign", row.name, row.namespace, f"runs {workflow}",
                                                         _template(row))
    campaigns = {name for kind, name in candidates if kind == "Campaign"}
    for row in await store.list_resources(tenant_id, "Trigger", namespace=namespace):
        spec = row.source or row.document["spec"]
        if spec.get("workflow") == workflow or spec.get("campaign") in campaigns:
            target = spec.get("workflow") or spec.get("campaign")
            candidates[("Trigger", row.name)] = Candidate("Trigger", row.name, row.namespace, f"starts {target}",
                                                        _template(row))
    return list(candidates.values()), warnings


def _template(row: Any) -> dict[str, Any]:
    """What the author wrote, without server defaults or adoption labels."""
    meta = row.document.get("metadata", {})
    metadata: dict[str, Any] = {"name": row.name}
    if meta.get("description"):
        metadata["description"] = meta["description"]
    labels = {k: v for k, v in (meta.get("labels") or {}).items() if not k.startswith("shoal.dev/")}
    if labels:
        metadata["labels"] = labels
    if row.annotations:
        metadata["annotations"] = dict(row.annotations)
    spec = row.source if row.source is not None else row.document["spec"]
    return {"kind": row.kind, "metadata": metadata, "spec": copy.deepcopy(spec)}


def _references(kind: str, name: str, spec: dict[str, Any]) -> list[tuple[str, str, str]]:
    refs: list[tuple[str, str, str]] = []
    if kind == "Workflow":
        if agent := (spec.get("defaults") or {}).get("agent"):
            refs.append(("Agent", agent, f"default agent of {name}"))
        for step in spec.get("steps") or []:
            if step.get("type") == "agent":
                if step.get("agent"):
                    refs.append(("Agent", step["agent"], f"step {step.get('id')} of {name}"))
                refs += [("Skill", s, f"step {step.get('id')} of {name}") for s in step.get("skills") or []]
            if step.get("type") == "workflow" and step.get("workflow"):
                refs.append(("Workflow", step["workflow"], f"called by {name}"))
    elif kind == "Agent":
        refs += [("Skill", s, f"skill of {name}") for s in spec.get("skills") or []]
        refs += [("Agent", a, f"subagent of {name}") for a in spec.get("subagents") or []]
    return [r for r in refs if "{{" not in r[1]]


# --------------------------------------------------------------------------- packaging


def package(
    candidates: list[Candidate],
    *,
    include: set[tuple[str, str]] | None,
    choices: dict[str, dict[str, Any]],
    connections: dict[str, tuple[str, str]],
    warnings: list[str],
) -> Package:
    """Build blueprint params and resource templates.

    ``include`` picks candidates (None: the defaults; the workflow itself is always in), ``choices``
    overrides findings by id (``enabled``, ``name``, ``description``, ``default``; ids that are not
    found are ignored, so choices survive leaving a resource out), and ``connections``
    maps connection names to (integration, namespace) for the ones that exist.
    """
    warnings = list(warnings)
    known = {(c.kind, c.name) for c in candidates}
    if include is not None:
        if unknown := include - known:
            raise InvalidRequest(
                "can only include resources the workflow uses: "
                + ", ".join(f"{k} '{n}'" for k, n in sorted(unknown))
            )
        for c in candidates:
            c.included = c.root or (c.kind, c.name) in include
    chosen = [c for c in candidates if c.included]
    renamed = {(c.kind, c.name) for c in chosen}
    docs = [_rename(copy.deepcopy(c.document), renamed, warnings) for c in chosen]
    labels = [f"{c.kind} {c.name}" for c in chosen]
    for c in candidates:
        if not c.included and not c.shared:
            warnings.append(f"{c.kind} '{c.name}' ({c.reason}) is not included; adopters need one with that name")

    findings = _detect(docs, labels, connections)
    by_id = {f.id: f for f in findings}
    for fid, choice in choices.items():
        f = by_id.get(fid)
        if f is None:  # e.g. in a resource that is no longer included
            continue
        if "enabled" in choice and choice["enabled"] is not None and not f.locked:
            f.enabled = bool(choice["enabled"])
        if choice.get("name"):
            f.param = choice["name"]
        if choice.get("description") is not None:
            f.description = choice["description"]
        if "default" in choice and not f.locked:
            f.default = choice["default"]
            f.required = choice["default"] is None
    _check_names(findings)

    pkg = Package(candidates, findings, {}, docs, [], warnings, labels)
    for f in findings:
        if not f.enabled:
            if f.category in ("connection", "secret"):
                warnings.append(f"{f.category} '{f.value}' stays hard-coded; adopters need one with that name")
            continue
        for loc in f.locations:
            _apply(docs[loc.index], loc, f.param)
        param: dict[str, Any] = {"type": f.type, "description": f.description}
        if f.integration and f.type == "connection":
            param["integration"] = f.integration
        if f.default is not None:
            param["default"] = f.default
        param["required"] = f.required
        pkg.params[f.param] = param
        if f.category == "cloud_identity":
            warnings.append(f"cloud identity param '{f.param}': the adopter's identity must grant the adopted workflow")
    for i, doc in enumerate(docs):
        for path, text in list(_strings(doc)):
            cleaned = _replace_literal(text, TOKEN_PATTERNS, lambda _m: "[redacted]")
            if cleaned != text:
                _set(doc, path, cleaned)
                pkg.stripped.append(f"{labels[i]} · {_dotted(doc, path)}")
    return pkg


def _rename(doc: dict[str, Any], renamed: set[tuple[str, str]], warnings: list[str]) -> dict[str, Any]:
    """Prefix the resource's name and its references to other packaged resources."""
    kind, name, spec = doc["kind"], doc["metadata"]["name"], doc["spec"]

    def ref(k: str, value: Any) -> Any:
        return PREFIX + value if isinstance(value, str) and (k, value) in renamed else value

    doc["metadata"]["name"] = PREFIX + name
    if kind == "Workflow":
        defaults = spec.get("defaults") or {}
        if "agent" in defaults:
            defaults["agent"] = ref("Agent", defaults["agent"])
        for step in spec.get("steps") or []:
            if step.get("type") == "agent":
                if step.get("agent"):
                    step["agent"] = ref("Agent", step["agent"])
                if step.get("skills"):
                    step["skills"] = [ref("Skill", s) for s in step["skills"]]
            if step.get("type") == "workflow" and ("Workflow", step.get("workflow")) in renamed:
                step["workflow"] = ref("Workflow", step["workflow"])
                if step.pop("version", None) is not None:
                    warnings.append(f"step {step.get('id')} of {name} no longer pins a version of {step['workflow'][len(PREFIX):]}")
    elif kind == "Agent":
        if spec.get("skills"):
            spec["skills"] = [ref("Skill", s) for s in spec["skills"]]
        if spec.get("subagents"):
            spec["subagents"] = [ref("Agent", a) for a in spec["subagents"]]
    elif kind in ("Trigger", "Campaign"):
        for field_name, target in (("workflow", "Workflow"), ("campaign", "Campaign")):
            if (target, spec.get(field_name)) in renamed:
                spec[field_name] = ref(target, spec[field_name])
                if field_name == "workflow" and spec.pop("workflow_version", None) is not None:
                    warnings.append(f"{kind} {name} no longer pins a workflow version (adoption starts at version 1)")
    return doc


# --------------------------------------------------------------------------- detection


class _Findings:
    def __init__(self, labels: list[str]):
        self.items: dict[str, Finding] = {}
        self.labels = labels
        self.names: set[str] = set()

    def add(self, fid: str, category: Category, value: str, base: str, loc: Location, **kw: Any) -> Finding:
        f = self.items.get(fid)
        if f is None:
            name = _param_name(base)
            n = 2
            while name in self.names:
                name, n = f"{_param_name(base)}_{n}", n + 1
            self.names.add(name)
            f = Finding(fid, category, value, name, **kw)
            self.items[fid] = f
        if not any(x.index == loc.index and x.path == loc.path and x.mode == loc.mode for x in f.locations):
            f.locations.append(loc)
        return f


def _detect(docs: list[dict[str, Any]], labels: list[str], connections: dict[str, tuple[str, str]]) -> list[Finding]:
    out = _Findings(labels)

    def connection(i: int, path: tuple[str | int, ...], value: Any, hint: str | None, mode: str = "whole") -> None:
        if not isinstance(value, str) or not value or "{{" in value:
            return
        integration, ns = connections.get(value, (hint, None))
        loc = Location(i, path, "whole") if mode == "whole" else Location(
            i, path, "text", re.compile(rf"(?<=:){re.escape(value)}$"))
        out.add(f"connection:{value}", "connection", value, integration or "connection", loc, type="connection",
                integration=integration,
                description=f"{_title(integration) if integration else 'A'} connection (was '{value}').",
                default=value if ns == SHARED_NAMESPACE else None, required=ns != SHARED_NAMESPACE)

    def simple(category: Category, i: int, path: tuple[str | int, ...], value: Any, base: str, type_: str,
               description: str, keep_default: bool = True) -> None:
        if not isinstance(value, str) or not value or "{{" in value:
            return
        out.add(f"{category}:{value}", category, value, base, Location(i, path), type=type_,
                description=description, default=value if keep_default else None, required=not keep_default)

    for i, doc in enumerate(docs):
        kind, spec = doc["kind"], doc["spec"]
        # credentials in fields: always stripped
        for path, text in _strings(doc):
            key = next((p for p in reversed(path) if isinstance(p, str)), "")
            if CREDENTIAL_KEY.search(key) and not key.endswith(("_secret", "_secrets")) and "{{" not in text \
                    and not _is_reference_field(kind, path) and text.strip():
                fid = f"credential:{i}:{'.'.join(map(str, path))}"
                out.add(fid, "credential", "", key, Location(i, path), type="secret", locked=True,
                        description=f"Secret holding {key} (a clear-text value was removed).")
        # connections
        if kind == "Workflow":
            for key, value in (spec.get("connections") or {}).items():
                connection(i, ("spec", "connections", key), value, key)
            for s, step in enumerate(spec.get("steps") or []):
                action = step.get("action") or (step.get("condition") or {}).get("action") or ""
                hint = action.split(".", 1)[0] or None
                connection(i, ("spec", "steps", s, "connection"), step.get("connection"), hint)
                if isinstance(step.get("condition"), dict):
                    connection(i, ("spec", "steps", s, "condition", "connection"), step["condition"].get("connection"), hint)
                for key, value in (step.get("connections") or {}).items():
                    connection(i, ("spec", "steps", s, "connections", key), value, key)
        elif kind == "Agent":
            for c, cred in enumerate(spec.get("credentials") or []):
                if isinstance(cred, str) and ":" in cred:
                    integration, conn = cred.split(":", 1)
                    connection(i, ("spec", "credentials", c), conn, integration, mode="text")
        elif kind == "Trigger":
            connection(i, ("spec", "connection"), spec.get("connection"), None)
        elif kind == "Campaign":
            connection(i, ("spec", "targets", "connection"), (spec.get("targets") or {}).get("connection"), None)
        # secrets referenced by name in expressions
        for path, text in _strings(doc):
            for m in EXPR.finditer(text):
                for ref in SECRET_REF.finditer(m.group(1)):
                    name = ref.group(1) or ref.group(2)
                    pattern = re.compile(
                        rf"secrets\.{re.escape(name)}(?![A-Za-z0-9_-])|secrets\[\s*['\"]{re.escape(name)}['\"]\s*\]")
                    out.add(f"secret:{name}", "secret", name, name, Location(i, path, "secret", pattern), type="secret",
                            description=f"Secret used as '{name}' in the original workflow.")
        # models, sandboxes, cloud identities
        if kind == "Agent":
            simple("model", i, ("spec", "model"), spec.get("model"), "model", "string", "Model the agent runs on.")
            simple("sandbox", i, ("spec", "sandbox"), spec.get("sandbox"), "sandbox", "string",
                   "Sandbox profile the agent runs in.")
            for c, ident in enumerate(spec.get("cloud_identities") or []):
                simple("cloud_identity", i, ("spec", "cloud_identities", c), ident, "cloud_identity", "string",
                       "Cloud identity (in the adopting namespace) the agent uses.", keep_default=False)
        if kind == "Workflow":
            simple("sandbox", i, ("spec", "defaults", "sandbox"), (spec.get("defaults") or {}).get("sandbox"),
                   "sandbox", "string", "Sandbox profile the workflow runs in.")
            for s, step in enumerate(spec.get("steps") or []):
                simple("sandbox", i, ("spec", "steps", s, "sandbox"), step.get("sandbox"), "sandbox", "string",
                       "Sandbox profile the workflow runs in.")
                for c, ident in enumerate(step.get("cloud_identities") or []):
                    simple("cloud_identity", i, ("spec", "steps", s, "cloud_identities", c), ident, "cloud_identity",
                           "string", "Cloud identity (in the adopting namespace) the script uses.", keep_default=False)
        # repositories, organisations and Jira projects named in fields
        for path, text in _strings(doc):
            key = next((p for p in reversed(path) if isinstance(p, str)), "")
            literal = "{{" not in text
            if literal and (key in REPO_KEYS or key in REPO_LIST_KEYS) and REPO.match(text):
                out.add(f"repository:{text}", "repository", text, "repo", Location(i, path), type="string",
                        description="Repository (owner/name).", default=text, required=False)
            elif literal and key in ORG_KEYS and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", text):
                out.add(f"organization:{text}", "organization", text, "org", Location(i, path), type="string",
                        description="Organisation (owner of the repositories).", default=text, required=False)
            elif literal and key in JIRA_PROJECT_KEYS and JIRA_PROJECT.match(text):
                out.add(f"jira_project:{text}", "jira_project", text, "jira_project", Location(i, path), type="string",
                        description="Jira project key.", default=text, required=False)
            for segment in _literal_segments(text):
                for m in REPO_URL.finditer(segment):
                    repo = m.group(1).removesuffix(".git")
                    if REPO.match(repo):
                        out.add(f"repository:{repo}", "repository", repo, "repo", Location(i, path, "text"),
                                type="string", description="Repository (owner/name).", default=repo, required=False)
                for m in JQL_PROJECT.finditer(segment):
                    key_ = m.group(1)
                    out.add(f"jira_project:{key_}", "jira_project", key_, "jira_project", Location(i, path, "text"),
                            type="string", description="Jira project key.", default=key_, required=False)

    # a repository or project named once is replaced wherever it appears as text
    for f in out.items.values():
        if f.category == "repository":
            f.locations = _text_locations(docs, f, rf"(?<![A-Za-z0-9_.-]){re.escape(f.value)}(?![A-Za-z0-9_-])")
        elif f.category == "jira_project":
            pattern = re.compile(rf"\b{re.escape(f.value)}\b")
            f.locations = [Location(x.index, x.path, x.mode, pattern if x.mode == "text" else None) for x in f.locations]
    return sorted(out.items.values(), key=lambda f: CATEGORY_ORDER.index(f.category))


def _text_locations(docs: list[dict[str, Any]], f: Finding, regex: str) -> list[Location]:
    pattern = re.compile(regex)
    whole = {(x.index, x.path) for x in f.locations if x.mode == "whole"}
    out = [x for x in f.locations if x.mode == "whole"]
    for i, doc in enumerate(docs):
        for path, text in _strings(doc):
            if (i, path) not in whole and any(pattern.search(s) for s in _literal_segments(text)):
                out.append(Location(i, path, "text", pattern))
    return out


def _is_reference_field(kind: str, path: tuple[str | int, ...]) -> bool:
    """Fields that name a secret rather than hold one."""
    return kind == "Connection" and path[:2] == ("spec", "secrets")


def _title(integration: str) -> str:
    return {"github": "GitHub", "gitlab": "GitLab", "mcp": "MCP"}.get(integration, integration.capitalize())


def _param_name(base: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", base).strip("_").lower() or "param"
    return f"p_{name}" if name[0].isdigit() else name


def _check_names(findings: list[Finding]) -> None:
    seen: dict[str, str] = {}
    for f in findings:
        if not f.enabled:
            continue
        if not PARAM_NAME.match(f.param):
            raise InvalidRequest(f"parameter name '{f.param}' must be letters, digits and _ (not starting with a digit)")
        if f.param in seen:
            raise InvalidRequest(f"two parameters are named '{f.param}' ({seen[f.param]} and {f.id})")
        seen[f.param] = f.id


# --------------------------------------------------------------------------- tree helpers


def _strings(value: Any, path: tuple[str | int, ...] = ()) -> list[tuple[tuple[str | int, ...], str]]:
    """Every string leaf with its path."""
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in _strings(v, (*path, k))]
    if isinstance(value, list):
        return [x for n, v in enumerate(value) for x in _strings(v, (*path, n))]
    return []


def _get(doc: Any, path: tuple[str | int, ...]) -> Any:
    for p in path:
        doc = doc[p]
    return doc


def _set(doc: Any, path: tuple[str | int, ...], value: Any) -> None:
    _get(doc, path[:-1])[path[-1]] = value


def _literal_segments(text: str) -> list[str]:
    return EXPR.split(text)[::2] if "{{" in text else [text]


def _replace_literal(text: str, pattern: re.Pattern[str], repl: Any) -> str:
    """Substitute outside ``{{ }}`` expressions only."""
    out: list[str] = []
    pos = 0
    for m in EXPR.finditer(text):
        out += [pattern.sub(repl, text[pos:m.start()]), m.group(0)]
        pos = m.end()
    out.append(pattern.sub(repl, text[pos:]))
    return "".join(out)


def _apply(doc: dict[str, Any], loc: Location, param: str) -> None:
    current = _get(doc, loc.path)
    if not isinstance(current, str):
        return
    if loc.mode == "whole":
        value = f"{{{{ secrets[params.{param}] }}}}" if _is_credential_target(doc, loc) else f"{{{{ params.{param} }}}}"
        _set(doc, loc.path, value)
    elif loc.mode == "text" and loc.pattern is not None:
        _set(doc, loc.path, _replace_literal(current, loc.pattern, lambda _m: f"{{{{ params.{param} }}}}"))
    elif loc.mode == "secret" and loc.pattern is not None:
        pattern = loc.pattern
        _set(doc, loc.path, EXPR.sub(lambda m: pattern.sub(f"secrets[params.{param}]", m.group(0)), current))


def _is_credential_target(doc: dict[str, Any], loc: Location) -> bool:
    key = next((p for p in reversed(loc.path) if isinstance(p, str)), "")
    return bool(CREDENTIAL_KEY.search(key)) and not key.endswith(("_secret", "_secrets"))


def _dotted(doc: dict[str, Any], path: tuple[str | int, ...]) -> str:
    parts: list[str] = []
    node: Any = doc
    for n, p in enumerate(path):
        if n == 0 and p == "spec":
            node = node[p]
            continue
        if isinstance(p, int):
            item = node[p]
            parts.append(item["id"] if isinstance(item, dict) and isinstance(item.get("id"), str) else f"[{p}]")
        else:
            parts.append(str(p))
        node = node[p]
    return ".".join(parts) or "spec"
