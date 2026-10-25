"""The ``awm`` command line.

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

from awm import __version__
from awm.cli import config
from awm.cli.client import APIError, Client
from awm.cli.output import Format, age, console, describe_event, emit, err, print_yaml, styled
from awm.core.models import KIND_BY_PLURAL
from awm.core.scope import TENANT_SCOPED_KINDS

app = typer.Typer(
    name="awm",
    help="AI Workflow Manager: apply resources, run workflows and follow them, or run the server.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
)
secrets_app = typer.Typer(help="Manage namespace secrets (values are write-only).", no_args_is_help=True)
tenants_app = typer.Typer(help="Create and list tenants (needs the system admin token).", no_args_is_help=True)
app.add_typer(secrets_app, name="secrets")
app.add_typer(tenants_app, name="tenants")

TERMINAL = {"succeeded", "failed", "cancelled"}

# --------------------------------------------------------------------------- shared options

Namespace = Annotated[str | None, typer.Option("--namespace", "-n", help="Namespace (default: from context, else 'default').")]
Output = Annotated[Format, typer.Option("--output", "-o", help="table, json, yaml or name.")]


@app.callback()
def main(
    ctx: typer.Context,
    server: Annotated[str | None, typer.Option(envvar="AWM_SERVER", help="API base URL.", show_envvar=True)] = None,
    token: Annotated[str | None, typer.Option(envvar="AWM_TOKEN", help="API key or session token.", show_envvar=True)] = None,
    tenant: Annotated[str | None, typer.Option(envvar="AWM_TENANT", help="Tenant slug, when using the system admin token.")] = None,
    context: Annotated[str | None, typer.Option(envvar="AWM_CONTEXT", help="Saved context to use (see 'awm login').")] = None,
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
        err.print("hint: run 'awm login' or set AWM_TOKEN")
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
    return {"apiVersion": "awm.dev/v1", "kind": resource["kind"], "metadata": metadata, "spec": resource["spec"]}


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
    server: Annotated[str | None, typer.Argument(help="API base URL, e.g. https://awm.example.com.")] = None,
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
            err.print("runs cannot be deleted; use 'awm cancel' to stop one")
            raise typer.Exit(2)
        if plural in ("namespaces", "namespace", "ns"):
            path = f"/v1/namespaces/{quote(name)}"
        elif plural in ("secrets", "secret"):
            path = f"/v1/namespaces/{quote(ns)}/secrets/{quote(name)}"
        else:
            path = _resource_path(kind_name, plural, ns, name)
        client.request("DELETE", path)
    console.print(f"{plural.rstrip('s')}/{escape(name)} deleted")


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
    console.print(f"next: awm login {escape(client.target.server)} --token <key>", style="dim")


@tenants_app.command("list")
def tenants_list(ctx: typer.Context, output: Output = Format.table) -> None:
    """List tenants."""
    with api(ctx) as client:
        emit(client.get("/v1/tenants"), output, [("SLUG", lambda t: t["slug"]), ("NAME", lambda t: t.get("name")),
                                                  ("AGE", lambda t: age(t.get("created_at")))], name=lambda t: t["slug"])


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
    host: Annotated[str, typer.Option(envvar="AWM_HOST", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(envvar="AWM_PORT", help="Port.")] = 8080,
    worker: Annotated[bool, typer.Option("--worker/--no-worker", help="Also run a worker and scheduler in this process.")] = True,
    workers: Annotated[int, typer.Option("--processes", help="Uvicorn worker processes (API only; implies --no-worker when > 1).")] = 1,
) -> None:
    """Run the API server (configured through AWM_* environment variables)."""
    import uvicorn

    from awm.config import get_settings

    settings = get_settings()
    log_config = _configure_logging(settings.log_level, settings.log_json)
    if workers > 1:
        os.environ["AWM_SERVE_WORKER"] = "0"
        uvicorn.run("awm.cli.main:_app_factory", factory=True, host=host, port=port, workers=workers,
                    log_config=log_config, proxy_headers=True, forwarded_allow_ips="*")
        return
    from awm.api.app import create_app

    uvicorn.run(create_app(settings, run_worker=worker), host=host, port=port, log_config=log_config,
                proxy_headers=True, forwarded_allow_ips="*")


def _app_factory() -> Any:
    from awm.api.app import create_app

    return create_app(run_worker=os.environ.get("AWM_SERVE_WORKER", "1") == "1")


@app.command("worker")
def worker_cmd(
    scheduler: Annotated[bool, typer.Option("--scheduler/--no-scheduler", help="Also fire schedules and campaign ticks.")] = True,
) -> None:
    """Run a background worker (no HTTP). Scale these out next to 'serve --no-worker'."""
    import asyncio
    import signal

    from awm.config import get_settings
    from awm.engine import Engine

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
            logging.getLogger("awm.worker").info("worker stopped")

    asyncio.run(main_loop())


if __name__ == "__main__":  # pragma: no cover
    app()
