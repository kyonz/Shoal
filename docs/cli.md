# The `awm` CLI

`awm` ships with the engine (`pip install awm` or `uv tool install ./engine`). Client
commands talk to a server over the public API; `serve` and `worker` run one.

## Connecting

```sh
awm login https://awm.example.com          # prompts for an API key, checks it, saves a context
awm whoami
```

Contexts live in `~/.config/awm/config.yaml` (mode 0600; override with `AWM_CONFIG`).
Flags win over environment variables, which win over the current context:

| Flag | Environment | Meaning |
|---|---|---|
| `--server` | `AWM_SERVER` | API base URL |
| `--token` | `AWM_TOKEN` | API key or session token |
| `--tenant` | `AWM_TENANT` | Tenant slug, with the system admin token |
| `--context` | `AWM_CONTEXT` | Saved context to use |
| `-n/--namespace` | `AWM_NAMESPACE` | Namespace (default `default`) |

Bootstrap a fresh server with its admin token: `awm --token $AWM_ADMIN_TOKEN tenants create acme`
prints the tenant's first API key.

## Resources

```sh
awm apply -f examples/jira-bugfix/          # files, directories (recursive) or - for stdin
awm apply -f workflow.yaml --dry-run
awm get workflows                           # also: agents, connections, triggers, evals, wf, ...
awm get workflow hello -o yaml > hello.yaml # apply-able YAML
awm get secrets / namespaces / runs
awm delete trigger nightly
```

`apply` is all-or-nothing: if any document is invalid nothing is written and each error is listed.
Every listing takes `-o table|json|yaml|name`.

## Runs

```sh
awm run hello -i name=Dana -i count=3 --follow   # values parse as JSON when they can
awm run hello --inputs-file inputs.yaml --wait   # exit 0 only if the run succeeds
awm logs run_01... -f
awm get run run_01...
awm cancel run_01...
```

## Secrets

```sh
awm secrets set gh-token                 # prompts (hidden)
awm secrets set gh-token --from-file token.txt
awm secrets set gh-token --from-env GITHUB_TOKEN
awm secrets list
```

Values are write-only: the API never returns them.

## Evals as a CI gate

```sh
awm eval run triage-eval --baseline previous --fail-under 0.9 --max-regression 0.02 --wait
```

With `--wait` the command exits 1 unless the eval completes and its gate passes; the gate's
reasons are printed. `awm eval get <id>` shows a past run.

## Integrations

`awm integrations new servicenow --title ServiceNow` scaffolds an integration project from the
SDK template (see [integrations.md](integrations.md)).

## Running the server

```sh
awm serve                    # API + embedded worker on 127.0.0.1:8080 (AWM_HOST / AWM_PORT)
awm serve --no-worker        # API only
awm worker                   # a worker (and scheduler) without HTTP; run as many as you need
```

Both read the `AWM_*` settings in `engine/awm/config.py` (database URL, secret key, admin token,
public URL, ...). See [deploy/README.md](../deploy/README.md) for Docker, compose and Helm.
