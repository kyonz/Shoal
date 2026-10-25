"""The ``shoal`` command line.

Client commands (``login``, ``apply``, ``get``, ``run``, ``logs``, ``secrets``)
talk to a server over the public API. ``serve`` and ``worker`` run the server
and the background worker from this package.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlparse

import typer
from rich.markup import escape

from shoal import __version__
from shoal.cli import config
from shoal.cli.client import APIError, Client
from shoal.cli.output import Format, age, console, describe_event, emit, err, print_yaml, styled
from shoal.core.models import KIND_BY_PLURAL
from shoal.core.scope import TENANT_SCOPED_KINDS

app = typer.Typer(
    name="shoal",
    help="Shoal: apply resources, run workflows and follow them, or run the server.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
)
secrets_app = typer.Typer(help="Manage namespace secrets (values are write-only).", no_args_is_help=True)
tenants_app = typer.Typer(help="Create and manage tenants (instance admins or the admin token).", no_args_is_help=True)
eval_app = typer.Typer(help="Run evals and gate on their scores.", no_args_is_help=True)
integrations_app = typer.Typer(
    help="Build integrations with the open SDK, and register them for your organisation.", no_args_is_help=True
)
db_app = typer.Typer(help="Database schema migrations (runs where the server runs; uses SHOAL_DATABASE_URL).", no_args_is_help=True)
federation_app = typer.Typer(help="Federated tenants: link them on the hub, join from the member.", no_args_is_help=True)
skills_app = typer.Typer(help="Browse skill sources (marketplaces) and import, upgrade or roll back skills.", no_args_is_help=True)
app.add_typer(secrets_app, name="secrets")
credentials_app = typer.Typer(help="Your own credentials for connections that act as each person.", no_args_is_help=True)
app.add_typer(credentials_app, name="credentials")
app.add_typer(tenants_app, name="tenants")
app.add_typer(eval_app, name="eval")
app.add_typer(integrations_app, name="integrations")
app.add_typer(federation_app, name="federation")
app.add_typer(skills_app, name="skills")
app.add_typer(db_app, name="db")
triggers_app = typer.Typer(help="Fire triggers by hand.", no_args_is_help=True)
campaigns_app = typer.Typer(help="Launch campaigns and follow, pause, resume, cancel or retry their runs.", no_args_is_help=True)
blueprints_app = typer.Typer(help="Adopt blueprints from the tenant's catalogue into a namespace.", no_args_is_help=True)
app.add_typer(triggers_app, name="triggers")
app.add_typer(campaigns_app, name="campaigns")
app.add_typer(blueprints_app, name="blueprints")

TERMINAL = {"succeeded", "failed", "cancelled"}

# --------------------------------------------------------------------------- shared options

Namespace = Annotated[str | None, typer.Option("--namespace", "-n", help="Namespace (default: from context, else 'default').")]
Output = Annotated[Format, typer.Option("--output", "-o", help="table, json, yaml or name.")]


@app.callback()
def main(
    ctx: typer.Context,
    server: Annotated[str | None, typer.Option(envvar="SHOAL_SERVER", help="API base URL.", show_envvar=True)] = None,
    token: Annotated[str | None, typer.Option(envvar="SHOAL_TOKEN", help="API key or session token.", show_envvar=True)] = None,
    tenant: Annotated[str | None, typer.Option(envvar="SHOAL_TENANT", help="Tenant slug, when using the system admin token.")] = None,
    context: Annotated[str | None, typer.Option(envvar="SHOAL_CONTEXT", help="Saved context to use (see 'shoal login').")] = None,
) -> None:
    ctx.obj = {"server": server, "token": token, "tenant": tenant, "context": context}


def _target(ctx: typer.Context, namespace: str | None = None) -> config.Target:
    return config.resolve(namespace=namespace, **(ctx.obj or {}))


@contextmanager
def api(ctx: typer.Context, namespace: str | None = None) -> Iterator[Client]:
    """A client for the resolved target; API errors become a message and exit code 1."""
    target = _target(ctx, namespace)
    try:
        with Client(target) as client:
            yield client
    except APIError as exc:
        fail(exc)


def fail(exc: APIError) -> None:
    prefix = f"error ({exc.status})" if exc.status else "error"
    err.print(f"[red]{prefix}:[/red] {escape(exc.message)}")
    if exc.status == 401:
        err.print("hint: run 'shoal login' or set SHOAL_TOKEN")
    results = ((exc.body or {}).get("details") or {}).get("results") if isinstance(exc.body, dict) else None
    for item in results or []:
        if item.get("error"):
            err.print(f"  {item.get('kind')}/{item.get('name')}: {escape(item['error'])}")
    raise typer.Exit(1)


# --------------------------------------------------------------------------- kinds


def _kinds() -> dict[str, tuple[str, str]]:
    """Every accepted spelling -> (kind, plural)."""
    table: dict[str, tuple[str, str]] = {}
    for plural, kind in KIND_BY_PLURAL.items():
        for spelling in (plural, kind.lower(), plural.rstrip("s"), plural.replace("-", "")):
            table[spelling] = (kind, plural)
    table.update({"wf": ("Workflow", "workflows"), "sp": ("SandboxProfile", "sandbox-profiles"),
                  "conn": ("Connection", "connections"), "policies": ("ModelPolicy", "model-policies")})
    table["model-policy"] = ("ModelPolicy", "model-policies")
    table["identity"] = ("Identity", "identities")
    for special in ("runs", "run", "namespaces", "namespace", "ns", "secrets", "secret"):
        table[special] = (special, special)
    return table


def _resolve_kind(name: str) -> tuple[str, str]:
    found = _kinds().get(name.lower())
    if found is None:
        choices = ", ".join(sorted({p for _, p in _kinds().values()}))
        err.print(f"[red]error:[/red] unknown kind '{escape(name)}'; expected one of: {choices}")
        raise typer.Exit(2)
    return found


def _resource_path(kind: str, plural: str, namespace: str, name: str | None = None) -> str:
    base = f"/v1/{plural}" if kind in TENANT_SCOPED_KINDS else f"/v1/namespaces/{quote(namespace)}/{plural}"
    return f"{base}/{quote(name)}" if name else base


def manifest(resource: dict[str, Any]) -> dict[str, Any]:
    """An API resource as an apply-able document."""
    meta = resource["metadata"]
    metadata: dict[str, Any] = {"name": meta["name"]}
    if resource["kind"] not in TENANT_SCOPED_KINDS:
        metadata["namespace"] = meta["namespace"]
    if meta.get("description"):
        metadata["description"] = meta["description"]
    if meta.get("labels"):
        metadata["labels"] = meta["labels"]
    return {"apiVersion": "shoal.dev/v1", "kind": resource["kind"], "metadata": metadata, "spec": resource["spec"]}


RESOURCE_COLUMNS = [
    ("NAME", lambda r: r["metadata"]["name"]),
    ("VERSION", lambda r: r["metadata"]["version"]),
    ("UPDATED", lambda r: age(r["metadata"]["updated_at"])),
    ("BY", lambda r: r["metadata"].get("updated_by")),
    ("DESCRIPTION", lambda r: r["metadata"].get("description")),
]
RUN_COLUMNS = [
    ("ID", lambda r: r["id"]),
    ("WORKFLOW", lambda r: f"{r['workflow_name']}@v{r['workflow_version']}"),
    ("STATUS", lambda r: styled(r["status"])),
    ("AGE", lambda r: age(r["created_at"])),
    ("STARTED BY", lambda r: r.get("started_by")),
]
STEP_COLUMNS = [
    ("STEP", lambda s: s["step_id"]),
    ("TYPE", lambda s: s["type"]),
    ("STATUS", lambda s: styled(s["status"])),
    ("ATTEMPT", lambda s: s["attempt"]),
    ("ERROR", lambda s: (s.get("error") or {}).get("message")),
]


# --------------------------------------------------------------------------- login


@app.command()
def login(
    ctx: typer.Context,
    server: Annotated[str | None, typer.Argument(help="API base URL, e.g. https://shoal.example.com.")] = None,
    token: Annotated[str | None, typer.Option("--token", "-t", help="API key (prompted for when omitted).")] = None,
    namespace: Namespace = None,
    tenant: Annotated[str | None, typer.Option(help="Tenant slug (only with the system admin token).")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Context name (default: the server's host).")] = None,
) -> None:
    """Check an API key against a server and save it as the current context."""
    opts = ctx.obj or {}
    server = (server or opts.get("server") or config.resolve().server).rstrip("/")
    if "://" not in server:
        server = f"https://{server}"
    token = token or opts.get("token") or typer.prompt("API key", hide_input=True)
    tenant = tenant or opts.get("tenant")
    target = config.Target(server=server, token=token, namespace=namespace or "default", tenant=tenant, context=None)
    try:
        with Client(target) as client:
            me = client.get("/v1/me")
    except APIError as exc:
        fail(exc)
    data = config.load()
    context = name or urlparse(server).netloc or server
    entry: dict[str, Any] = {"server": server, "token": token, "namespace": namespace or "default"}
    if tenant:
        entry["tenant"] = tenant
    data["contexts"][context] = entry
    data["current-context"] = context
    path = config.save(data)
    where = f" in tenant {me['tenant']['slug']}" if me.get("tenant") else ""
    console.print(f"Logged in to {escape(server)} as [bold]{escape(me['actor'])}[/bold]{escape(where)}.")
    console.print(f"Saved context '{escape(context)}' to {path}.", style="dim")


@app.command()
def logout(ctx: typer.Context) -> None:
    """Forget the current context's token."""
    data = config.load()
    name = (ctx.obj or {}).get("context") or data.get("current-context")
    if not name or name not in data["contexts"]:
        err.print("not logged in")
        raise typer.Exit(1)
    data["contexts"][name].pop("token", None)
    config.save(data)
    console.print(f"Removed the token for '{escape(name)}'.")


@app.command()
def whoami(ctx: typer.Context, output: Output = Format.table) -> None:
    """Show who the current credentials act as."""
    with api(ctx) as client:
        me = client.get("/v1/me")
    if output is not Format.table:
        emit(me, output)
        return
    tenant = me["tenant"]["slug"] if me.get("tenant") else "-"
    console.print(f"{escape(me['actor'])} (tenant: {escape(tenant)}, server: {escape(client.target.server)})")
    if me.get("namespaces"):
        console.print(f"namespaces: {escape(', '.join(me['namespaces']))}")


@app.command()
def version() -> None:
    """Print the CLI version."""
    print(__version__)


# --------------------------------------------------------------------------- apply / get / delete


def _read_sources(paths: list[Path]) -> str:
    documents: list[str] = []
    for path in paths:
        if str(path) == "-":
            documents.append(sys.stdin.read())
        elif path.is_dir():
            files = sorted(p for p in path.rglob("*") if p.suffix in {".yaml", ".yml", ".json"} and p.is_file())
            documents.extend(p.read_text() for p in files)
        elif path.exists():
            documents.append(path.read_text())
        else:
            err.print(f"[red]error:[/red] {escape(str(path))} does not exist")
            raise typer.Exit(2)
    return "\n---\n".join(documents)


@app.command()
def apply(
    ctx: typer.Context,
    filenames: Annotated[list[Path], typer.Option("--filename", "-f", help="File, directory (recursive) or '-' for stdin.")],
    namespace: Namespace = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and show what would change.")] = False,
    output: Output = Format.table,
) -> None:
    """Create or update resources from YAML or JSON documents. All-or-nothing."""
    body = _read_sources(filenames)
    with api(ctx, namespace) as client:
        results = client.request(
            "POST", "/v1/apply", params={"namespace": client.target.namespace, "dry_run": dry_run},
            content=body.encode(), headers={"Content-Type": "application/yaml"},
        )
    if output is Format.table:
        suffix = " (dry run)" if dry_run else ""
        for r in results:
            where = r["namespace"] if r["kind"] not in TENANT_SCOPED_KINDS else "tenant"
            version_ = f" v{r['version']}" if r.get("version") else ""
            console.print(f"{r['kind'].lower()}/{escape(r['name'])} ({escape(where)}) {styled(r['outcome'])}{version_}{suffix}")
        return
    emit(results, output, name=lambda r: f"{r['kind'].lower()}/{r['name']}")


@app.command()
def get(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="workflows, agents, connections, triggers, runs, secrets, namespaces, ...")],
    name: Annotated[str | None, typer.Argument(help="Name (or run id) to show one.")] = None,
    namespace: Namespace = None,
    output: Output = Format.table,
    label: Annotated[list[str] | None, typer.Option("--label", "-l", help="Filter by label key=value (resources).")] = None,
    workflow: Annotated[str | None, typer.Option(help="Filter runs by workflow.")] = None,
    status: Annotated[str | None, typer.Option(help="Filter runs by status.")] = None,
    limit: Annotated[int, typer.Option(help="Maximum runs to list.", max=200)] = 50,
) -> None:
    """List resources of a kind, or show one."""
    kind_name, plural = _resolve_kind(kind)
    with api(ctx, namespace) as client:
        ns = client.target.namespace
        if plural in ("runs", "run"):
            _get_runs(client, ns, name, output, workflow=workflow, status=status, limit=limit)
        elif plural in ("namespaces", "namespace", "ns"):
            data = client.get(f"/v1/namespaces/{quote(name)}") if name else client.get("/v1/namespaces")
            emit(data, output, [("NAME", lambda n: n["name"]), ("DISPLAY NAME", lambda n: n["display_name"]),
                                ("AGE", lambda n: age(n["created_at"])), ("DESCRIPTION", lambda n: n.get("description"))])
        elif plural in ("secrets", "secret"):
            path = f"/v1/namespaces/{quote(ns)}/secrets"
            data = client.get(f"{path}/{quote(name)}") if name else client.get(path)
            emit(data, output, SECRET_COLUMNS)
        else:
            path = _resource_path(kind_name, plural, ns, name)
            data = client.get(path) if name else client.request("GET", path, params={"label": label or []})
            if output in (Format.yaml, Format.json):
                docs = manifest(data) if name else [manifest(r) for r in data]
                if output is Format.yaml:
                    print_yaml(docs, documents=not name)
                else:
                    emit(docs, output)
            else:
                emit(data, output, RESOURCE_COLUMNS, name=lambda r: f"{plural}/{r['metadata']['name']}")


def _get_runs(client: Client, ns: str, run_id: str | None, output: Format, **filters: Any) -> None:
    if not run_id:
        page = client.get(f"/v1/namespaces/{quote(ns)}/runs", **filters)
        emit(page["items"] if output is not Format.json else page, output, RUN_COLUMNS, name=lambda r: r["id"])
        return
    run = client.get(f"/v1/runs/{quote(run_id)}")
    if output is not Format.table:
        emit(run, output, name=lambda r: r["id"])
        return
    console.print(f"[bold]{run['id']}[/bold]  {escape(run['workflow_name'])}@v{run['workflow_version']}  {styled(run['status'])}")
    console.print(f"namespace {escape(run['namespace'])} · started by {escape(run.get('started_by') or '-')} · {age(run['created_at'])} ago")
    if run.get("error"):
        console.print(f"[red]error:[/red] {escape(json.dumps(run['error'], default=str))}")
    if run.get("outputs"):
        console.print(f"outputs: {escape(json.dumps(run['outputs'], default=str))}")
    if run.get("steps"):
        console.print()
        emit(run["steps"], Format.table, STEP_COLUMNS)


@app.command()
def delete(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="Resource kind, 'secrets' or 'namespaces'.")],
    name: Annotated[str, typer.Argument()],
    namespace: Namespace = None,
) -> None:
    """Delete a resource."""
    kind_name, plural = _resolve_kind(kind)
    with api(ctx, namespace) as client:
        ns = client.target.namespace
        if plural in ("runs", "run"):
            err.print("runs cannot be deleted; use 'shoal cancel' to stop one")
            raise typer.Exit(2)
        if plural in ("namespaces", "namespace", "ns"):
            path = f"/v1/namespaces/{quote(name)}"
        elif plural in ("secrets", "secret"):
            path = f"/v1/namespaces/{quote(ns)}/secrets/{quote(name)}"
        else:
            path = _resource_path(kind_name, plural, ns, name)
        client.request("DELETE", path)
    console.print(f"{_singular(plural)}/{escape(name)} deleted")


def _singular(plural: str) -> str:
    """'model-policies' -> 'model-policy', 'workflows' -> 'workflow'."""
    if plural == "ns":
        return "namespace"
    if plural.endswith("ies"):
        return plural[:-3] + "y"
    return plural.removesuffix("s")


# --------------------------------------------------------------------------- runs


def _parse_inputs(pairs: list[str], inputs_file: Path | None) -> dict[str, Any]:
    import yaml

    inputs: dict[str, Any] = {}
    if inputs_file:
        loaded = yaml.safe_load(sys.stdin.read() if str(inputs_file) == "-" else inputs_file.read_text())
        if not isinstance(loaded, dict):
            err.print("[red]error:[/red] the inputs file must hold a mapping")
            raise typer.Exit(2)
        inputs.update(loaded)
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            err.print(f"[red]error:[/red] expected key=value, got '{escape(pair)}'")
            raise typer.Exit(2)
        try:
            inputs[key] = json.loads(raw)  # numbers, booleans, lists, objects
        except ValueError:
            inputs[key] = raw
    return inputs


@app.command()
def run(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name.")],
    inputs: Annotated[list[str] | None, typer.Option("--input", "-i", help="Input key=value; values parse as JSON when they can.")] = None,
    inputs_file: Annotated[Path | None, typer.Option("--inputs-file", help="YAML or JSON file of inputs ('-' for stdin).")] = None,
    version_: Annotated[int | None, typer.Option("--version", help="Run this workflow version (default: latest).")] = None,
    label: Annotated[list[str] | None, typer.Option("--label", "-l", help="Run label key=value.")] = None,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Stream the run's events until it finishes.")] = False,
    wait: Annotated[bool, typer.Option("--wait", "-w", help="Wait for the run to finish without streaming.")] = False,
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """Start a workflow run. With --follow or --wait, exit 1 unless it succeeds."""
    body = {
        "inputs": _parse_inputs(inputs or [], inputs_file),
        "version": version_,
        "labels": dict(item.split("=", 1) for item in label or [] if "=" in item),
    }
    with api(ctx, namespace) as client:
        started = client.request("POST", f"/v1/namespaces/{quote(client.target.namespace)}/workflows/{quote(workflow)}/runs", json=body)
        if output is Format.table:
            err.print(f"run [bold]{started['id']}[/bold] started ({escape(workflow)}@v{started['workflow_version']})")
        elif not (follow or wait):
            emit(started, output, name=lambda r: r["id"])
        if not (follow or wait):
            return
        final = _follow(client, started["id"], show=follow and output is Format.table)
        if output is not Format.table:
            emit(client.get(f"/v1/runs/{quote(started['id'])}"), output, name=lambda r: r["id"])
        elif final.get("outputs"):
            console.print_json(json.dumps(final["outputs"], default=str))
    raise typer.Exit(0 if final["status"] == "succeeded" else 1)


def _follow(client: Client, run_id: str, *, show: bool, step: str | None = None) -> dict[str, Any]:
    """Stream events until the run is terminal; returns the final run."""
    path = f"/v1/runs/{quote(run_id)}/events"
    if step:
        path += f"?step_id={quote(step)}"
    for event, data in client.events(path):
        if event == "end":
            break
        if show:
            print_event(data)
        if data.get("type") == "approval.requested" and data.get("step_id"):
            err.print(f"waiting for approval: shoal approve {run_id} {escape(data['step_id'])} (or --reject)")
    final = client.get(f"/v1/runs/{quote(run_id)}")
    err.print(f"run {final['id']} {styled(final['status'])}")
    return final


def print_event(event: dict[str, Any]) -> None:
    stamp = (event.get("created_at") or "")[11:19]
    step = event.get("step_id") or "run"
    kind = event.get("type", "")
    style = "red" if kind.endswith(("failed", ".error")) else "green" if kind.endswith("succeeded") else "cyan"
    detail = describe_event(event)
    console.print(f"[dim]{stamp}[/dim] {escape(step):<16} [{style}]{escape(kind)}[/{style}] {escape(detail)}", highlight=False)


@app.command()
def logs(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep streaming until the run finishes.")] = False,
    step: Annotated[str | None, typer.Option(help="Only this step's events.")] = None,
    output: Output = Format.table,
) -> None:
    """Show a run's events."""
    with api(ctx) as client:
        if follow:
            path = f"/v1/runs/{quote(run_id)}/events" + (f"?step_id={quote(step)}" if step else "")
            for event, data in client.events(path):
                if event == "end":
                    break
                if output is Format.json:
                    print(json.dumps(data, default=str), flush=True)
                else:
                    print_event(data)
            return
        events = client.get(f"/v1/runs/{quote(run_id)}/events", step_id=step)
    if output is Format.table:
        for event in events:
            print_event(event)
    else:
        emit(events, output)


@app.command()
def cancel(ctx: typer.Context, run_id: Annotated[str, typer.Argument(help="Run id.")]) -> None:
    """Ask a run to stop."""
    with api(ctx) as client:
        result = client.request("POST", f"/v1/runs/{quote(run_id)}/cancel")
    console.print(f"run {result['id']} {styled(result['status'])} (cancel requested)")


@app.command()
def approve(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    step: Annotated[str | None, typer.Argument(help="Approval step id (default: the run's only waiting approval).")] = None,
    reject: Annotated[bool, typer.Option("--reject", help="Reject instead of approving.")] = False,
    comment: Annotated[str | None, typer.Option("--comment", "-m", help="Comment recorded with the decision.")] = None,
) -> None:
    """Approve (or --reject) a run's approval step."""
    with api(ctx) as client:
        if step is None:
            run = client.get(f"/v1/runs/{quote(run_id)}")
            waiting = [s["step_id"] for s in run.get("steps") or [] if s["type"] == "approval" and s["status"] == "waiting"]
            if len(waiting) != 1:
                found = ", ".join(waiting) if waiting else "none"
                err.print(f"[red]error:[/red] name the approval step; waiting approvals in {escape(run_id)}: {escape(found)}")
                raise typer.Exit(2)
            step = waiting[0]
        client.request("POST", f"/v1/runs/{quote(run_id)}/steps/{quote(step)}/approval",
                       json={"approved": not reject, "comment": comment})
    console.print(f"step {escape(step)} {'rejected' if reject else 'approved'} (run {escape(run_id)})")


@app.command()
def signal(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    name: Annotated[str, typer.Argument(help="Signal name, as in the wait step's `signal: {name: ...}`.")],
    data: Annotated[str | None, typer.Option("--data", "-d", help="Payload: JSON, or plain text.")] = None,
) -> None:
    """Send a signal (data) to a run; a wait step for it resumes."""
    payload: Any = None
    if data is not None:
        try:
            payload = json.loads(data)
        except ValueError:
            payload = data
    with api(ctx) as client:
        result = client.request("POST", f"/v1/runs/{quote(run_id)}/signals/{quote(name)}", json={"payload": payload})
    console.print(f"signal {result['id']} sent to run {run_id}")


@app.command()
def rerun(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id to start again, with the same inputs and workflow version.")],
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Stream the new run's events until it finishes.")] = False,
    wait: Annotated[bool, typer.Option("--wait", "-w", help="Wait for the new run to finish without streaming.")] = False,
) -> None:
    """Start a run again with the same inputs. With --follow or --wait, exit 1 unless it succeeds."""
    with api(ctx) as client:
        started = client.request("POST", f"/v1/runs/{quote(run_id)}/rerun")
        err.print(f"run [bold]{started['id']}[/bold] started ({escape(started['workflow_name'])}@v{started['workflow_version']}, rerun of {escape(run_id)})")
        if not (follow or wait):
            return
        final = _follow(client, started["id"], show=follow)
        if final.get("outputs"):
            console.print_json(json.dumps(final["outputs"], default=str))
    raise typer.Exit(0 if final["status"] == "succeeded" else 1)


def _parse_pairs(pairs: list[str], what: str) -> dict[str, Any]:
    """key=value pairs; values parse as JSON when they can."""
    values: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            err.print(f"[red]error:[/red] {what} must be key=value, got '{escape(pair)}'")
            raise typer.Exit(2)
        try:
            values[key] = json.loads(raw)
        except ValueError:
            values[key] = raw
    return values


# --------------------------------------------------------------------------- triggers


@triggers_app.command("fire")
def triggers_fire(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Trigger name.")],
    data: Annotated[str | None, typer.Option("--data", "-d", help="Event payload as JSON ({{ event }} in the trigger).")] = None,
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """Fire a trigger now, as if its event had arrived."""
    try:
        payload = json.loads(data) if data else {}
    except ValueError as exc:
        err.print(f"[red]error:[/red] --data must be JSON: {escape(str(exc))}")
        raise typer.Exit(2) from exc
    if not isinstance(payload, dict):
        err.print("[red]error:[/red] --data must be a JSON object")
        raise typer.Exit(2)
    with api(ctx, namespace) as client:
        result = client.request("POST", f"/v1/namespaces/{quote(client.target.namespace)}/triggers/{quote(name)}/fire", json={"data": payload})
    if output is not Format.table:
        emit(result, output, name=lambda r: r.get("run_id") or r.get("campaign_id") or r["trigger"])
        return
    started = result.get("run_id") or result.get("campaign_id")
    detail = f": started {started}" if started else f" ({result['reason']})" if result.get("reason") else ""
    console.print(f"trigger {escape(name)} {styled(result['status'])}{escape(detail)}")
    if result["status"] not in ("fired", "skipped", "duplicate"):
        raise typer.Exit(1)


# --------------------------------------------------------------------------- campaigns

CAMPAIGN_COLUMNS = [
    ("ID", lambda c: c["id"]),
    ("NAME", lambda c: c["name"]),
    ("STATUS", lambda c: styled(c["status"])),
    ("PROGRESS", lambda c: _progress(c["stats"])),
    ("AGE", lambda c: age(c["created_at"])),
    ("STARTED BY", lambda c: c.get("created_by")),
]


def _progress(stats: dict[str, Any]) -> str:
    total = stats.get("total") or 0
    done = sum(stats.get(k) or 0 for k in ("succeeded", "failed", "skipped", "cancelled"))
    failed = stats.get("failed") or 0
    return f"{done}/{total}" + (f" ({failed} failed)" if failed else "")


@campaigns_app.command("launch")
def campaigns_launch(
    ctx: typer.Context,
    campaign: Annotated[str, typer.Argument(help="Campaign resource name.")],
    inputs: Annotated[list[str] | None, typer.Option("--input", "-i", help="Extra or override workflow input key=value.")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Name for this campaign run.")] = None,
    paused: Annotated[bool, typer.Option("--paused", help="Create the run without starting it ('campaigns resume' starts it).")] = False,
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """Launch a campaign: one workflow run per target."""
    body = {"name": name, "inputs": _parse_pairs(inputs or [], "--input"), "start": not paused}
    with api(ctx, namespace) as client:
        row = client.request("POST", f"/v1/namespaces/{quote(client.target.namespace)}/campaigns/{quote(campaign)}/launch", json=body)
    if output is not Format.table:
        emit(row, output, name=lambda c: c["id"])
        return
    console.print(f"campaign run [bold]{row['id']}[/bold] {styled(row['status'])} ({escape(campaign)})")


@campaigns_app.command("list")
def campaigns_list(
    ctx: typer.Context,
    status: Annotated[str | None, typer.Option(help="Only campaign runs with this status.")] = None,
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """List campaign runs in a namespace."""
    with api(ctx, namespace) as client:
        rows = client.get(f"/v1/namespaces/{quote(client.target.namespace)}/campaign-runs", status=status)
    emit(rows, output, CAMPAIGN_COLUMNS, name=lambda c: c["id"])


@campaigns_app.command("get")
def campaigns_get(
    ctx: typer.Context,
    campaign_id: Annotated[str, typer.Argument(help="Campaign run id.")],
    output: Output = Format.table,
) -> None:
    """Show a campaign run and its progress."""
    with api(ctx) as client:
        row = client.get(f"/v1/campaign-runs/{quote(campaign_id)}")
    if output is not Format.table:
        emit(row, output, name=lambda c: c["id"])
        return
    console.print(f"[bold]{row['id']}[/bold]  {escape(row['name'])}  {styled(row['status'])}")
    console.print(f"namespace {escape(row['namespace'])} · started by {escape(row.get('created_by') or '-')} · {age(row['created_at'])} ago")
    stats = row["stats"]
    counts = ", ".join(f"{k} {stats[k]}" for k in ("pending", "running", "succeeded", "failed", "skipped", "cancelled") if stats.get(k))
    console.print(f"progress {_progress(row['stats'])}" + (f" · {escape(counts)}" if counts else ""))
    if row.get("error"):
        console.print(f"[red]error:[/red] {escape(json.dumps(row['error'], default=str))}")


def _campaign_operation(ctx: typer.Context, campaign_id: str, operation: str) -> None:
    with api(ctx) as client:
        row = client.request("POST", f"/v1/campaign-runs/{quote(campaign_id)}/{operation}")
    console.print(f"campaign run {row['id']} {styled(row['status'])}")


@campaigns_app.command("pause")
def campaigns_pause(ctx: typer.Context, campaign_id: Annotated[str, typer.Argument(help="Campaign run id.")]) -> None:
    """Stop starting new targets; runs in flight carry on."""
    _campaign_operation(ctx, campaign_id, "pause")


@campaigns_app.command("resume")
def campaigns_resume(ctx: typer.Context, campaign_id: Annotated[str, typer.Argument(help="Campaign run id.")]) -> None:
    """Start (or carry on) a paused campaign run."""
    _campaign_operation(ctx, campaign_id, "resume")


@campaigns_app.command("cancel")
def campaigns_cancel(ctx: typer.Context, campaign_id: Annotated[str, typer.Argument(help="Campaign run id.")]) -> None:
    """Cancel a campaign run and its runs in flight."""
    _campaign_operation(ctx, campaign_id, "cancel")


@campaigns_app.command("retry")
def campaigns_retry(ctx: typer.Context, campaign_id: Annotated[str, typer.Argument(help="Campaign run id.")]) -> None:
    """Run the failed targets again."""
    _campaign_operation(ctx, campaign_id, "retry")


# --------------------------------------------------------------------------- blueprints


@blueprints_app.command("adopt")
def blueprints_adopt(
    ctx: typer.Context,
    blueprint: Annotated[str, typer.Argument(help="Blueprint name in the tenant's catalogue.")],
    params: Annotated[list[str] | None, typer.Option("--param", "-p", help="Blueprint parameter key=value.")] = None,
    prefix: Annotated[str, typer.Option(help="Prefix for the names of the resources it creates.")] = "",
    version: Annotated[str | None, typer.Option("--version", help="Blueprint version (default: the current one).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be created; write nothing.")] = False,
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """Adopt a blueprint: create its workflows, agents, triggers and other resources in a namespace."""
    body = {"params": _parse_pairs(params or [], "--param"), "prefix": prefix, "version": version, "dry_run": dry_run}
    with api(ctx, namespace) as client:
        ns = client.target.namespace
        result = client.request("POST", f"/v1/namespaces/{quote(ns)}/blueprints/{quote(blueprint)}/adopt", json=body)
    if output is not Format.table:
        emit(result, output)
        return
    suffix = " (dry run)" if dry_run else ""
    for r in result["resources"]:
        console.print(f"{r['kind'].lower()}/{escape(r['name'])} ({escape(ns)}) {r.get('outcome') or 'adopted'}{suffix}")
    for r in result.get("removed") or []:
        console.print(f"{r['kind'].lower()}/{escape(r['name'])} ({escape(ns)}) removed{suffix}")
    if result.get("adoption"):
        console.print(f"adoption {result['adoption']['id']} of {escape(blueprint)} v{escape(result['adoption']['blueprint_version'])}")


# --------------------------------------------------------------------------- secrets

SECRET_COLUMNS = [("NAME", lambda s: s["name"]), ("NAMESPACE", lambda s: s["namespace"]), ("UPDATED", lambda s: age(s["updated_at"]))]


@secrets_app.command("list")
def secrets_list(ctx: typer.Context, namespace: Namespace = None, output: Output = Format.table) -> None:
    """List secret names (never values)."""
    with api(ctx, namespace) as client:
        emit(client.get(f"/v1/namespaces/{quote(client.target.namespace)}/secrets"), output, SECRET_COLUMNS)


@secrets_app.command("set")
def secrets_set(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument()],
    value: Annotated[str | None, typer.Option("--value", help="The value (prefer --from-file or the prompt: flags end up in shell history).")] = None,
    from_file: Annotated[Path | None, typer.Option("--from-file", help="Read the value from a file ('-' for stdin).")] = None,
    from_env: Annotated[str | None, typer.Option("--from-env", help="Read the value from an environment variable.")] = None,
    namespace: Namespace = None,
) -> None:
    """Create or replace a secret. Prompts for the value when no source is given."""
    if sum(x is not None for x in (value, from_file, from_env)) > 1:
        err.print("[red]error:[/red] pass only one of --value, --from-file and --from-env")
        raise typer.Exit(2)
    if from_file is not None:
        secret = sys.stdin.read() if str(from_file) == "-" else from_file.read_text()
        secret = secret.removesuffix("\n")
    elif from_env is not None:
        if from_env not in os.environ:
            err.print(f"[red]error:[/red] ${escape(from_env)} is not set")
            raise typer.Exit(2)
        secret = os.environ[from_env]
    elif value is not None:
        secret = value
    else:
        secret = typer.prompt(f"Value for {name}", hide_input=True)
    with api(ctx, namespace) as client:
        row = client.request("PUT", f"/v1/namespaces/{quote(client.target.namespace)}/secrets/{quote(name)}", json={"value": secret})
    console.print(f"secret/{escape(row['name'])} set in {escape(row['namespace'])}")


CREDENTIAL_COLUMNS = [
    ("CONNECTION", lambda c: f"{c['namespace']}/{c['connection']}"), ("KIND", lambda c: c["kind"]),
    ("FIELDS", lambda c: ",".join(c["fields"])), ("UPDATED", lambda c: age(c["updated_at"])),
]


@credentials_app.command("list")
def credentials_list(ctx: typer.Context, output: Output = Format.table) -> None:
    """List the connections you have connected (never values)."""
    with api(ctx) as client:
        emit(client.get("/v1/me/credentials"), output, CREDENTIAL_COLUMNS)


@credentials_app.command("set")
def credentials_set(
    ctx: typer.Context,
    connection: Annotated[str, typer.Argument(help="<namespace>/<connection>.")],
    field: Annotated[str, typer.Option("--field", help="Connection field the value fills.")] = "token",
) -> None:
    """Store your own credential for a connection (prompts for the value)."""
    namespace, _, name = connection.rpartition("/")
    value = typer.prompt(f"{field} for {connection}", hide_input=True)
    with api(ctx) as client:
        client.request("PUT", f"/v1/me/credentials/{quote(namespace or 'default')}/{quote(name)}", json={"values": {field: value}})
    console.print(f"connected {connection}")


@credentials_app.command("connect")
def credentials_connect(ctx: typer.Context, connection: Annotated[str, typer.Argument(help="<namespace>/<connection>.")]) -> None:
    """Connect your account with OAuth: prints the sign-in URL."""
    namespace, _, name = connection.rpartition("/")
    with api(ctx) as client:
        result = client.request("POST", f"/v1/me/credentials/{quote(namespace or 'default')}/{quote(name)}/oauth")
    console.print(f"Open this URL in a browser signed in to Shoal as you, to connect {connection}:\n{result['authorize_url']}")


@credentials_app.command("remove")
def credentials_remove(ctx: typer.Context, connection: Annotated[str, typer.Argument(help="<namespace>/<connection>.")]) -> None:
    """Remove your credential for a connection."""
    namespace, _, name = connection.rpartition("/")
    with api(ctx) as client:
        client.request("DELETE", f"/v1/me/credentials/{quote(namespace or 'default')}/{quote(name)}")
    console.print(f"removed {connection}")


@secrets_app.command("get")
def secrets_get(ctx: typer.Context, name: Annotated[str, typer.Argument()], namespace: Namespace = None, output: Output = Format.table) -> None:
    """Show a secret's metadata (the API never returns values)."""
    with api(ctx, namespace) as client:
        emit(client.get(f"/v1/namespaces/{quote(client.target.namespace)}/secrets/{quote(name)}"), output, SECRET_COLUMNS)


@secrets_app.command("delete")
def secrets_delete(ctx: typer.Context, name: Annotated[str, typer.Argument()], namespace: Namespace = None) -> None:
    """Delete a secret."""
    with api(ctx, namespace) as client:
        client.request("DELETE", f"/v1/namespaces/{quote(client.target.namespace)}/secrets/{quote(name)}")
    console.print(f"secret/{escape(name)} deleted")


# --------------------------------------------------------------------------- evals

EVAL_DONE = {"completed", "cancelled", "failed"}
VARIANT_COLUMNS = [
    ("VARIANT", lambda v: v["name"]),
    ("VERSION", lambda v: v.get("workflow_version")),
    ("CASES", lambda v: v.get("cases")),
    ("ERRORS", lambda v: v.get("errors")),
    ("SCORE", lambda v: None if v.get("score") is None else f"{v['score']:.3f}"),
    ("PASS RATE", lambda v: None if v.get("pass_rate") is None else f"{v['pass_rate']:.0%}"),
]


@eval_app.command("run")
def eval_run(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Eval name.")],
    version_: Annotated[int | None, typer.Option("--version", help="Workflow version to evaluate (default: current).")] = None,
    baseline: Annotated[str | None, typer.Option(help="'previous', 'version:N', an eval run id, or 'none'.")] = None,
    fail_under: Annotated[float | None, typer.Option(help="Fail the gate when a variant scores below this (0-1).")] = None,
    max_regression: Annotated[float | None, typer.Option(help="Fail the gate when the score drops more than this against the baseline.")] = None,
    case: Annotated[list[str] | None, typer.Option("--case", help="Only run these cases.")] = None,
    inputs: Annotated[list[str] | None, typer.Option("--input", "-i", help="Input override key=value for every case.")] = None,
    label: Annotated[list[str] | None, typer.Option("--label", "-l", help="Eval run label key=value.")] = None,
    wait: Annotated[bool, typer.Option("--wait", "-w", help="Wait for the result; exit 1 unless the gate passes.")] = False,
    poll: Annotated[float, typer.Option(help="Seconds between status checks with --wait.", min=0.01)] = 2.0,
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """Start an eval run. With --wait it is a CI gate: exit 0 only when the gate passes."""
    import time

    body = {
        "version": version_, "baseline": baseline, "fail_under": fail_under, "max_regression": max_regression,
        "cases": case or None, "inputs": _parse_inputs(inputs or [], None),
        "labels": dict(item.split("=", 1) for item in label or [] if "=" in item),
    }
    with api(ctx, namespace) as client:
        row = client.request("POST", f"/v1/namespaces/{quote(client.target.namespace)}/evals/{quote(name)}/runs",
                             json={k: v for k, v in body.items() if v is not None})
        if output is Format.table:
            err.print(f"eval run [bold]{row['id']}[/bold] started ({escape(name)} v{row['eval_version']}, "
                      f"{escape(row['workflow_name'])}@v{row['workflow_version']})")
        if wait:
            while row["status"] not in EVAL_DONE:
                time.sleep(poll)
                row = client.get(f"/v1/eval-runs/{quote(row['id'])}")
    if output is not Format.table:
        emit(row, output, name=lambda r: r["id"])
    elif wait:
        _print_eval(row)
    if wait:
        passed = row["status"] == "completed" and (row.get("gate") or {}).get("passed", True)
        raise typer.Exit(0 if passed else 1)


@eval_app.command("get")
def eval_get(ctx: typer.Context, eval_run_id: Annotated[str, typer.Argument(help="Eval run id.")], output: Output = Format.table) -> None:
    """Show an eval run's scores and gate."""
    with api(ctx) as client:
        row = client.get(f"/v1/eval-runs/{quote(eval_run_id)}")
    if output is Format.table:
        _print_eval(row)
    else:
        emit(row, output, name=lambda r: r["id"])


def _pct(value: Any) -> str | None:
    return None if value is None else f"{value:.0%}"


def _score(value: Any) -> str | None:
    return None if value is None else f"{value:.3f}"


def _signed(value: Any) -> str | None:
    return None if value is None else f"{value:+.3f}"


EVAL_RUN_COLUMNS = [
    ("ID", lambda r: r["id"]),
    ("EVAL", lambda r: r["eval_name"]),
    ("STATUS", lambda r: styled(r["status"])),
    ("WORKFLOW", lambda r: f"{len(r['variants'])} variants" if len(r.get("variants") or []) > 1 else f"v{r['workflow_version']}"),
    ("SCORE", lambda r: _score((r.get("summary") or {}).get("score"))),
    ("PASS RATE", lambda r: _pct((r.get("summary") or {}).get("pass_rate"))),
    ("GATE", lambda r: None if "passed" not in (r.get("gate") or {}) else ("passed" if r["gate"]["passed"] else "failed")),
    ("AGE", lambda r: age(r["created_at"])),
]


@eval_app.command("list")
def eval_list(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Only runs of this eval.")] = None,
    status: Annotated[str | None, typer.Option(help="Comma-separated statuses, e.g. running,completed.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=200)] = 20,
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """List eval runs, newest first."""
    params = {"eval": name, "status": status, "limit": limit}
    with api(ctx, namespace) as client:
        page = client.get(f"/v1/namespaces/{quote(client.target.namespace)}/eval-runs", **params)
    emit(page["items"], output, EVAL_RUN_COLUMNS, name=lambda r: r["id"])


@eval_app.command("cancel")
def eval_cancel(ctx: typer.Context, eval_run_id: Annotated[str, typer.Argument(help="Eval run id.")]) -> None:
    """Cancel a running eval run and the workflow runs it started."""
    with api(ctx) as client:
        row = client.request("POST", f"/v1/eval-runs/{quote(eval_run_id)}/cancel")
    console.print(f"eval run {escape(row['id'])} {styled(row['status'])}")


CASE_DIFF_COLUMNS = [
    ("CASE", lambda c: c["case"]),
    ("BASELINE", lambda c: _score(c["baseline"])),
    ("CANDIDATE", lambda c: _score(c["candidate"])),
    ("DELTA", lambda c: _signed(c["delta"])),
]


@eval_app.command("compare")
def eval_compare(
    ctx: typer.Context,
    eval_run_id: Annotated[str, typer.Argument(help="Candidate eval run id.")],
    baseline: Annotated[str, typer.Argument(help="Baseline eval run id.")],
    output: Output = Format.table,
) -> None:
    """Case-by-case diff of an eval run against a baseline run."""
    with api(ctx) as client:
        diff = client.get(f"/v1/eval-runs/{quote(eval_run_id)}/compare", baseline=baseline)
    if output is not Format.table:
        emit(diff, output)
        return
    for variant, d in (diff.get("variants") or {}).items():
        b, c = d.get("baseline") or {}, d.get("candidate") or {}
        console.print(f"[bold]{escape(variant)}[/bold]  v{b.get('workflow_version')} {_score(b.get('score'))} -> "
                      f"v{c.get('workflow_version')} {_score(c.get('score'))}  delta {_signed(d.get('delta'))} "
                      f"over {d.get('shared_cases')} shared cases")
        if d.get("regressions"):
            console.print(f"  [red]regressed:[/red] {escape(', '.join(d['regressions']))}")
        if d.get("improvements"):
            console.print(f"  [green]improved:[/green] {escape(', '.join(d['improvements']))}")
        changed = [x for x in d.get("cases") or [] if x["delta"]]
        if changed:
            emit(changed, Format.table, CASE_DIFF_COLUMNS)


SCORE_COLUMNS = [
    ("VERSION", lambda v: f"v{v['workflow_version']}"),
    ("RUNS", lambda v: v["runs"]),
    ("MEAN SCORE", lambda v: _score(v["mean_score"])),
    ("LATEST", lambda v: _score((v.get("latest") or {}).get("score"))),
    ("PASS RATE", lambda v: _pct((v.get("latest") or {}).get("pass_rate"))),
    ("LATEST RUN", lambda v: (v.get("latest") or {}).get("eval_run_id")),
]


@eval_app.command("scores")
def eval_scores(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Eval name.")],
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """Scores per workflow version across completed runs of an eval."""
    with api(ctx, namespace) as client:
        data = client.get(f"/v1/namespaces/{quote(client.target.namespace)}/evals/{quote(name)}/scores")
    emit(data["versions"], output, SCORE_COLUMNS, name=lambda v: str(v["workflow_version"]))


def _print_eval(row: dict[str, Any]) -> None:
    summary = row.get("summary") or {}
    console.print(f"[bold]{row['id']}[/bold]  {escape(row['eval_name'])} v{row['eval_version']}  "
                  f"{styled('succeeded' if row['status'] == 'completed' else row['status'])}")
    variants = [{"name": k, **v} for k, v in (summary.get("variants") or {}).items()]
    if variants:
        emit(variants, Format.table, VARIANT_COLUMNS)
    gate = row.get("gate") or {}
    if "passed" in gate:
        verdict = "[green]passed[/green]" if gate["passed"] else "[red]failed[/red]"
        console.print(f"gate {verdict}")
        for reason in gate.get("reasons") or []:
            console.print(f"  - {escape(reason)}")
    if row.get("error"):
        console.print(f"[red]error:[/red] {escape(json.dumps(row['error'], default=str))}")


# --------------------------------------------------------------------------- integrations


@integrations_app.command("new")
def integrations_new(
    name: Annotated[str, typer.Argument(help="Integration key, e.g. 'servicenow'.")],
    dest: Annotated[Path | None, typer.Option("--dest", help="Directory to create (default: shoal-<name>).")] = None,
    title: Annotated[str | None, typer.Option("--title", help="Display name, e.g. 'ServiceNow'.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Write into a non-empty directory.")] = False,
) -> None:
    """Scaffold a new integration project from the SDK template."""
    from shoal.sdk.scaffold import new_integration

    try:
        target = new_integration(name, dest, title=title, force=force)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        err.print(f"[red]error:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    console.print(f"Created integration project in {escape(str(target))}")


@integrations_app.command("list")
def integrations_list(ctx: typer.Context, namespace: Namespace = None, output: Output = Format.table) -> None:
    """Integrations this namespace can connect: installed ones and the ones your organisation registered."""
    with api(ctx, namespace) as client:
        data = client.get("/v1/integrations", namespace=client.target.namespace)
    emit(data, output, [
        ("KEY", lambda i: i["key"]), ("TITLE", lambda i: i["title"]), ("SOURCE", lambda i: i.get("source")),
        ("LEVELS", lambda i: ",".join(i.get("connection_scopes") or [])), ("ACTIONS", lambda i: len(i["actions"])),
    ], name=lambda i: i["key"])


@integrations_app.command("register")
def integrations_register(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Key to register it under (actions are called <name>.<action>).")],
    url: Annotated[str | None, typer.Option(help="Remote integration service base URL (https).")] = None,
    token_secret: Annotated[str | None, typer.Option(help="Secret in `shared` with the bearer token for the service.")] = None,
    package: Annotated[str | None, typer.Option(help="Installed package: entry point name, distribution, or module:Class.")] = None,
    scope: Annotated[list[str] | None, typer.Option(
        "--scope", help="Where connections may live: org and/or namespace (repeat; default: what the integration allows)."
    )] = None,
    allow_namespace: Annotated[list[str] | None, typer.Option(
        "--allow-namespace", help="Only these namespaces may see and connect it (repeat; default: all)."
    )] = None,
    title: Annotated[str | None, typer.Option(help="Display name (default: the manifest's).")] = None,
    allow_insecure: Annotated[bool, typer.Option("--allow-insecure", help="Permit plain http to a non-loopback host.")] = False,
    output: Output = Format.table,
) -> None:
    """Register a custom integration for the organisation; Shoal pulls its manifest."""
    if bool(url) == bool(package):
        err.print("[red]error:[/red] give exactly one of --url or --package")
        raise typer.Exit(2)
    spec: dict[str, Any] = {"source": "remote" if url else "package", "url": url, "token_secret": token_secret,
                            "package": package, "title": title, "connection_scopes": scope or [],
                            "namespaces": allow_namespace or []}
    if allow_insecure:
        spec["allow_insecure"] = True
    with api(ctx) as client:
        row = client.request("PUT", f"/v1/custom-integrations/{quote(name)}",
                             json={"spec": {k: v for k, v in spec.items() if v not in (None, [])}})
    if output is not Format.table:
        emit(row, output)
        return
    _print_custom_status(name, row.get("status") or {})


@integrations_app.command("refresh")
def integrations_refresh(
    ctx: typer.Context, name: Annotated[str, typer.Argument(help="Registered integration.")], output: Output = Format.table
) -> None:
    """Pull a registered integration's manifest again (after the service or package changed)."""
    with api(ctx) as client:
        status = client.request("POST", f"/v1/custom-integrations/{quote(name)}/refresh")
    if output is not Format.table:
        emit(status, output)
        return
    _print_custom_status(name, status)


def _print_custom_status(name: str, status: dict[str, Any]) -> None:
    if not status.get("ready"):
        console.print(f"registered [bold]{escape(name)}[/bold], but it is not ready: {escape(str(status.get('error')))}")
        return
    console.print(
        f"registered [bold]{escape(name)}[/bold] ({escape(str(status.get('title')))}): {status.get('actions', 0)} actions, "
        f"{status.get('events', 0)} events; connections at {' and '.join(status.get('connection_scopes') or [])} level"
    )
@integrations_app.command("check")
def integrations_check(
    url: Annotated[str, typer.Argument(help="Base URL of a running remote integration service.")],
    token: Annotated[
        str | None, typer.Option("--token", envvar="SHOAL_INTEGRATION_TOKEN", help="The service's bearer token.")
    ] = None,
) -> None:
    """Run the integration contract checks against a remote integration service (any language)."""
    import asyncio

    from shoal.sdk.testing.contract import check_remote_service

    problems = asyncio.run(check_remote_service(url, token))
    for problem in problems:
        err.print(f"[red]FAIL[/red] {escape(problem)}")
    if problems:
        err.print(f"{len(problems)} problem(s)")
        raise typer.Exit(1)
    console.print(f"{escape(url)} passes the integration contract")


# --------------------------------------------------------------------------- tenants


@tenants_app.command("create")
def tenants_create(
    ctx: typer.Context,
    slug: Annotated[str, typer.Argument()],
    name: Annotated[str | None, typer.Option(help="Display name.")] = None,
    admin_email: Annotated[str | None, typer.Option(help="First human admin (bound to tenant-admin).")] = None,
    output: Output = Format.table,
) -> None:
    """Create a tenant and print its bootstrap API key (shown once)."""
    with api(ctx) as client:
        created = client.request("POST", "/v1/tenants", json={"slug": slug, "name": name, "admin_email": admin_email})
    if output is not Format.table:
        emit(created, output)
        return
    console.print(f"tenant [bold]{escape(created['tenant']['slug'])}[/bold] created")
    console.print(f"bootstrap API key (shown once): {created['api_key']}")
    console.print(f"next: shoal login {escape(client.target.server)} --token <key>", style="dim")


@tenants_app.command("list")
def tenants_list(ctx: typer.Context, output: Output = Format.table) -> None:
    """List tenants."""
    with api(ctx) as client:
        emit(client.get("/v1/tenants"), output, [("SLUG", lambda t: t["slug"]), ("NAME", lambda t: t.get("name")),
                                                  ("STATUS", lambda t: t.get("status", "active")),
                                                  ("AGE", lambda t: age(t.get("created_at")))], name=lambda t: t["slug"])


@tenants_app.command("suspend")
def tenants_suspend(ctx: typer.Context, slug: Annotated[str, typer.Argument()]) -> None:
    """Suspend a tenant: its members are locked out and no new runs start (data is kept)."""
    with api(ctx) as client:
        client.request("PATCH", f"/v1/tenants/{slug}", json={"status": "suspended"})
    console.print(f"tenant [bold]{escape(slug)}[/bold] suspended")


@tenants_app.command("resume")
def tenants_resume(ctx: typer.Context, slug: Annotated[str, typer.Argument()]) -> None:
    """Resume a suspended tenant."""
    with api(ctx) as client:
        client.request("PATCH", f"/v1/tenants/{slug}", json={"status": "active"})
    console.print(f"tenant [bold]{escape(slug)}[/bold] resumed")


@tenants_app.command("add-member")
def tenants_add_member(
    ctx: typer.Context,
    slug: Annotated[str, typer.Argument()],
    email: Annotated[str, typer.Argument(help="The person's email; they are invited if they have not signed in yet.")],
    role: Annotated[str, typer.Option(help="Role in that tenant, granted tenant-wide.")] = "tenant-admin",
) -> None:
    """Give someone a role in a tenant."""
    with api(ctx) as client:
        member = client.request("POST", f"/v1/tenants/{slug}/members", json={"email": email, "role": role})
    console.print(f"{escape(member['username'])} now has {', '.join(member['roles'])} in [bold]{escape(slug)}[/bold]")


# --------------------------------------------------------------------------- database


async def _with_connection(fn: Any) -> Any:
    from shoal.config import get_settings
    from shoal.store.store import Store

    store = Store(get_settings().database_url)
    try:
        async with store.engine.begin() as conn:
            return await conn.run_sync(fn)
    finally:
        await store.close()


@db_app.command("upgrade")
def db_upgrade(revision: Annotated[str, typer.Argument(help="Target revision.")] = "head") -> None:
    """Apply migrations. The server also does this at start-up."""
    import asyncio

    from shoal.config import get_settings
    from shoal.store.store import Store

    async def run() -> str | None:
        store = Store(get_settings().database_url)
        try:
            return await store.migrate(revision)
        finally:
            await store.close()

    console.print(f"database at revision {asyncio.run(run())}")


@db_app.command("current")
def db_current() -> None:
    """Show the database's migration revision."""
    import asyncio

    from shoal.store import migrate

    console.print(asyncio.run(_with_connection(migrate.current)) or "not migrated")


@db_app.command("revision")
def db_revision(message: Annotated[str, typer.Option("-m", "--message", help="What changed.")]) -> None:
    """Developers: autogenerate a migration from model changes (run against an up-to-date database)."""
    import asyncio

    from shoal.store import migrate

    asyncio.run(_with_connection(lambda conn: migrate.revision(conn, message)))


# --------------------------------------------------------------------------- federation


def _parse_grant(text: str) -> dict[str, Any]:
    """``ROLE=hub_user:alice@acme.com`` or ``ROLE=hub_group:platform[@NAMESPACE]``."""
    role, _, subject = text.partition("=")
    kind, _, rest = subject.partition(":")
    subject_id, _, namespace = rest.rpartition("@") if kind == "hub_group" and "@" in rest else (rest, "", "")
    if not role or kind not in ("hub_user", "hub_group") or not subject_id:
        raise typer.BadParameter(f"expected ROLE=hub_user:EMAIL or ROLE=hub_group:NAME[@NAMESPACE], got {text!r}")
    return {"role": role, "subject_kind": kind, "subject_id": subject_id, "namespace": namespace or None}


@federation_app.command("link")
def federation_link(
    ctx: typer.Context,
    tenant: Annotated[str, typer.Argument(help="Slug of an empty tenant on this hub.")],
    mode: Annotated[str, typer.Option(help="read or read_write (the member can lower it).")] = "read_write",
    output: Output = Format.table,
) -> None:
    """On the hub (operator token): federate a tenant and print its enrollment token."""
    with api(ctx) as client:
        created = client.request("POST", "/v1/federation/links", json={"tenant": tenant, "mode": mode})
    if output is not Format.table:
        emit(created, output)
        return
    console.print(f"tenant [bold]{escape(tenant)}[/bold] is federated ({mode}); waiting for its member to join")
    console.print(f"enrollment token (single use, expires {created['enrollment_expires_at']}): {created['enrollment_token']}")
    console.print(f"on the member: shoal federation join {escape(client.target.server)} --token <token>", style="dim")


@federation_app.command("links")
def federation_links(ctx: typer.Context, output: Output = Format.table) -> None:
    """On the hub: list federated tenants and whether their members are connected."""
    with api(ctx) as client:
        emit(client.get("/v1/federation/links"), output, [
            ("TENANT", lambda li: li["tenant"]), ("STATUS", lambda li: li["status"]),
            ("CONNECTED", lambda li: "yes" if li["connected"] else "no"), ("MODE", lambda li: li["effective_mode"]),
            ("SEEN", lambda li: age(li.get("last_seen_at"))),
        ], name=lambda li: li["tenant"])


@federation_app.command("join")
def federation_join(
    ctx: typer.Context,
    hub_url: Annotated[str, typer.Argument(help="The hub's base URL.")],
    token: Annotated[str, typer.Option(help="Enrollment token from the hub operator.")],
    max_mode: Annotated[str, typer.Option(help="The most the hub may do: read or read_write.")] = "read",
    namespace: Annotated[list[str] | None, typer.Option(help="Expose only these namespaces (repeatable).")] = None,
    visibility: Annotated[str, typer.Option(help="full, or metadata to hide run data from the hub.")] = "full",
    grant: Annotated[list[str] | None, typer.Option(help="ROLE=hub_user:EMAIL or ROLE=hub_group:NAME (repeatable).")] = None,
    output: Output = Format.table,
) -> None:
    """On the member (tenant admin): join this tenant to a hub."""
    body = {"hub_url": hub_url, "token": token, "max_mode": max_mode, "namespaces": namespace or [],
            "visibility": visibility, "grants": [_parse_grant(g) for g in grant or []]}
    with api(ctx) as client:
        joined = client.request("PUT", "/v1/federation/membership", json=body)
    if output is not Format.table:
        emit(joined, output)
        return
    console.print(f"joined [bold]{escape(hub_url)}[/bold]: the hub may {joined['effective_mode'].replace('_', '/')}"
                  f" (ceiling {joined['max_mode']})")


@federation_app.command("leave")
def federation_leave(ctx: typer.Context) -> None:
    """On the member: leave the hub. This deployment keeps running on its own."""
    with api(ctx) as client:
        client.request("DELETE", "/v1/federation/membership")
    console.print("left the hub")


# --------------------------------------------------------------------------- skills


def _skill_ns(client: Client) -> str:
    return f"/v1/namespaces/{quote(client.target.namespace)}"


def _print_changes(changes: list[dict[str, Any]]) -> None:
    for change in changes:
        style = {"add": "green", "remove": "red"}.get(change["op"], "yellow")
        line = f"{change['op']:<8} {change['path']}"
        if change["op"] == "replace" and isinstance(change.get("to"), str | int | float) and len(str(change.get("to"))) < 60:
            line += f": {change.get('from')!s} -> {change.get('to')!s}"
        console.print(escape(line), style=style)


def _print_import(result: dict[str, Any]) -> None:
    previous = f" (was {result['previous_version']})" if result.get("previous_version") else ""
    verb = "would be" if result.get("dry_run") else "is"
    console.print(f"skill/{escape(result['name'])} {verb} {result['outcome']}: {escape(result['source'])}/"
                  f"{escape(result['skill_id'])} {escape(result['version'])}{escape(previous)}")
    _print_changes(result.get("changes") or [])
    for path in result.get("skipped_files") or []:
        err.print(f"[yellow]skipped[/yellow] {escape(path)} (binary or too large)")


@skills_app.command("sources")
def skills_sources(ctx: typer.Context, namespace: Namespace = None, output: Output = Format.table) -> None:
    """List the skill sources this namespace can browse."""
    with api(ctx, namespace) as client:
        emit(client.get(f"{_skill_ns(client)}/skill-sources"), output, [
            ("NAME", lambda s: s["name"]), ("TYPE", lambda s: s["source"]), ("TITLE", lambda s: s["title"]),
        ])


@skills_app.command("browse")
def skills_browse(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Skill source name.")],
    query: Annotated[str | None, typer.Option("--query", "-q", help="Search text.")] = None,
    limit: Annotated[int, typer.Option(help="Skills per page.")] = 50,
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """List or search a source's skills."""
    with api(ctx, namespace) as client:
        page = client.get(f"{_skill_ns(client)}/skill-sources/{quote(source)}/skills", q=query, limit=limit)
    emit(page["items"], output, [
        ("ID", lambda s: s["id"]), ("VERSION", lambda s: s.get("version") or ""),
        ("INSTALLED", lambda s: ", ".join(i["name"] for i in s["installed"])), ("DESCRIPTION", lambda s: s["description"][:80]),
    ], name=lambda s: s["id"])
    if page.get("next_cursor") and output is Format.table:
        err.print("more skills: narrow the search with --query", style="dim")


@skills_app.command("show")
def skills_show(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Skill source name.")],
    skill: Annotated[str, typer.Argument(help="Skill id within the source.")],
    version: Annotated[str | None, typer.Option(help="Version or ref (default: what the source is pinned to).")] = None,
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """Show a skill's SKILL.md, files and version."""
    with api(ctx, namespace) as client:
        detail = client.get(f"{_skill_ns(client)}/skill-sources/{quote(source)}/skills/{quote(skill, safe='/')}", version=version)
    if output is not Format.table:
        emit(detail, output)
        return
    console.print(f"[bold]{escape(detail['name'])}[/bold] {escape(detail['version'])}"
                  + (f" ({detail['ref'][:12]})" if detail.get("ref") else ""))
    console.print(escape(detail["description"]))
    for name in sorted(detail["files"]):
        console.print(f"  {escape(name)}", style="dim")
    console.print()
    console.print(escape(detail["instructions"]))


@skills_app.command("import")
def skills_import(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Skill source name.")],
    skill: Annotated[str, typer.Argument(help="Skill id within the source.")],
    name: Annotated[str | None, typer.Option(help="Skill resource name (default: the skill's name).")] = None,
    version: Annotated[str | None, typer.Option(help="Version or ref to import.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change without writing.")] = False,
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """Import a skill from a source into the namespace."""
    body = {"source": source, "skill": skill, "name": name, "version": version, "dry_run": dry_run}
    with api(ctx, namespace) as client:
        result = client.request("POST", f"{_skill_ns(client)}/skill-imports", json=body)
    if output is not Format.table:
        emit(result, output)
        return
    _print_import(result)


@skills_app.command("imports")
def skills_imports(
    ctx: typer.Context,
    check: Annotated[bool, typer.Option("--check", help="Ask each source whether there is a newer version.")] = False,
    namespace: Namespace = None,
    output: Output = Format.table,
) -> None:
    """List skills imported from skill sources."""
    columns = [("NAME", lambda s: s["name"]), ("SOURCE", lambda s: s["source"]), ("SKILL", lambda s: s["skill_id"]),
               ("VERSION", lambda s: s["version"])]
    if check:
        columns.append(("LATEST", lambda s: s.get("latest_version") or s.get("check_error") or ""))
        columns.append(("UPDATE", lambda s: {True: "yes", False: "no"}.get(s.get("update_available"), "?")))
    with api(ctx, namespace) as client:
        emit(client.get(f"{_skill_ns(client)}/skill-imports", check=check or None), output, columns)


@skills_app.command("upgrade")
def skills_upgrade(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Imported skill resource name.")],
    version: Annotated[str | None, typer.Option(help="Version or ref to move to (default: the source's current one).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only show the diff.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    namespace: Namespace = None,
) -> None:
    """Re-import a skill from its source, showing the diff first."""
    with api(ctx, namespace) as client:
        path = f"{_skill_ns(client)}/skill-imports/{quote(name)}/upgrade"
        preview = client.request("POST", path, json={"version": version, "dry_run": True})
        _print_import(preview)
        if dry_run or preview["outcome"] == "unchanged":
            return
        if not yes and not typer.confirm("Apply this upgrade?"):
            raise typer.Exit(1)
        result = client.request("POST", path, json={"version": version})
    console.print(f"skill/{escape(name)} is now {escape(result['version'])} (resource version {result['resource_version']}); "
                  f"undo with: shoal skills rollback {escape(name)} {result['resource_version'] - 1}", style="dim")


@skills_app.command("rollback")
def skills_rollback(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Skill resource name.")],
    version: Annotated[int, typer.Argument(help="Resource version to restore (see 'shoal get skill NAME -o yaml').")],
    namespace: Namespace = None,
) -> None:
    """Restore an earlier version of a skill (as a new version)."""
    with api(ctx, namespace) as client:
        row = client.request("POST", f"{_skill_ns(client)}/skills/{quote(name)}/rollback", json={"version": version})
    labels = row["metadata"].get("labels") or {}
    origin = f" ({labels['shoal.dev/skill-version']})" if "shoal.dev/skill-version" in labels else ""
    console.print(f"skill/{escape(name)} restored to version {version}{escape(origin)} as version {row['metadata']['version']}")


# --------------------------------------------------------------------------- server processes


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": self.formatTime(record), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def _configure_logging(level: str, as_json: bool) -> dict[str, Any]:
    """Configure root logging and return a matching uvicorn log config."""
    handler = logging.StreamHandler()
    handler.setFormatter(_JSONFormatter() if as_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "loggers": {name: {"level": level.upper(), "handlers": [], "propagate": True} for name in ("uvicorn", "uvicorn.access", "uvicorn.error")},
    }


@app.command()
def serve(
    host: Annotated[str, typer.Option(envvar="SHOAL_HOST", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(envvar="SHOAL_PORT", help="Port.")] = 8080,
    worker: Annotated[bool, typer.Option("--worker/--no-worker", help="Also run a worker and scheduler in this process.")] = True,
    workers: Annotated[int, typer.Option("--processes", help="Uvicorn worker processes (API only; implies --no-worker when > 1).")] = 1,
) -> None:
    """Run the API server (configured through SHOAL_* environment variables)."""
    import uvicorn

    from shoal.config import get_settings

    settings = get_settings()
    log_config = _configure_logging(settings.log_level, settings.log_json)
    if workers > 1:
        os.environ["SHOAL_SERVE_WORKER"] = "0"
        uvicorn.run("shoal.cli.main:_app_factory", factory=True, host=host, port=port, workers=workers,
                    log_config=log_config, proxy_headers=True, forwarded_allow_ips="*")
        return
    from shoal.api.app import create_app

    uvicorn.run(create_app(settings, run_worker=worker), host=host, port=port, log_config=log_config,
                proxy_headers=True, forwarded_allow_ips="*")


def _app_factory() -> Any:
    from shoal.api.app import create_app

    return create_app(run_worker=os.environ.get("SHOAL_SERVE_WORKER", "1") == "1")


@app.command("worker")
def worker_cmd(
    scheduler: Annotated[bool, typer.Option("--scheduler/--no-scheduler", help="Also fire schedules and campaign ticks.")] = True,
) -> None:
    """Run a background worker (no HTTP). Scale these out next to 'serve --no-worker'."""
    import asyncio
    import signal

    from shoal.config import get_settings
    from shoal.engine import Engine

    settings = get_settings()
    _configure_logging(settings.log_level, settings.log_json)

    async def main_loop() -> None:
        engine = Engine(settings)
        await engine.start()
        task = asyncio.create_task(engine.run_worker(scheduler=scheduler))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            await engine.close()
            logging.getLogger("shoal.worker").info("worker stopped")

    asyncio.run(main_loop())


if __name__ == "__main__":  # pragma: no cover
    app()
