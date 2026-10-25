# Shoal engine

The Python package behind Shoal: the workflow runtime, the API server, workers, the plugin SDK and
the `shoal` CLI. See the [project README](https://github.com/kyonz/ai-workflow-manager#readme) for
what Shoal is and how to deploy it.

## Install

The package is not on PyPI yet. From a checkout of the repository:

```sh
uv tool install ./engine          # the CLI only, or: pipx install ./engine
pip install "./engine[postgres]"  # into a virtualenv, to run a server against Postgres
```

Extras: `postgres`, `kubernetes`, `docker`, `e2b`, `temporal`, or `all`.

## Run a server

```sh
cd engine
uv sync --all-extras
SHOAL_ADMIN_TOKEN=change-me uv run shoal serve   # API + embedded worker on http://127.0.0.1:8080
```

Data goes to SQLite in the current directory unless `SHOAL_DATABASE_URL` is set. Every setting is a
`SHOAL_*` environment variable, listed in [`shoal/config.py`](shoal/config.py).

## Develop

```sh
uv sync --all-extras
uv run ruff check .
uv run pytest -q                  # SQLite; set SHOAL_TEST_DATABASE_URL to run against Postgres
```

`scripts/check.sh engine` at the repository root runs the same checks. The layout:

| Path | What |
|---|---|
| `shoal/api/` | FastAPI app and routers (`/v1`, SCIM, OIDC) |
| `shoal/runtime/` | The durable executor, steps and orchestrators |
| `shoal/agents/`, `shoal/llm/` | Agent runtimes, model providers and gateways |
| `shoal/sandbox/` | Sandbox backends |
| `shoal/integrations/`, `shoal/sdk/` | Built-in integrations and the integration SDK |
| `shoal/cli/` | The `shoal` command-line client |
| `shoal/store/` | Database models and migrations |
| `tests/` | The test suite, including plugin contract tests |
