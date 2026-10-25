# The `shoal` CLI

`shoal` is the command-line client for Shoal. It ships with the engine: from a checkout, `uv tool install ./engine`
or `pipx install ./engine` (it is not on PyPI yet). Client commands talk to a server over the public API; `serve` and `worker` run one.

## Connecting

```sh
shoal login https://shoal.example.com          # prompts for an API key, checks it, saves a context
shoal whoami
```

Contexts live in `~/.config/shoal/config.yaml` (mode 0600; override with `SHOAL_CONFIG`).
Flags win over environment variables, which win over the current context:

| Flag | Environment | Meaning |
|---|---|---|
| `--server` | `SHOAL_SERVER` | API base URL |
| `--token` | `SHOAL_TOKEN` | API key or session token |
| `--tenant` | `SHOAL_TENANT` | Tenant slug, with the system admin token |
| `--context` | `SHOAL_CONTEXT` | Saved context to use |
| `-n/--namespace` | `SHOAL_NAMESPACE` | Namespace (default `default`) |

Bootstrap a fresh server with its admin token: `shoal --token $SHOAL_ADMIN_TOKEN tenants create acme`
prints the tenant's first API key.

## Resources

```sh
shoal apply -f examples/jira-bugfix/          # files, directories (recursive) or - for stdin
shoal apply -f workflow.yaml --dry-run
shoal get workflows                           # also: agents, connections, triggers, evals, wf, ...
shoal get workflow hello -o yaml > hello.yaml # apply-able YAML
shoal get secrets / namespaces / runs
shoal delete trigger nightly
```

`apply` is all-or-nothing: if any document is invalid nothing is written and each error is listed.
Every listing takes `-o table|json|yaml|name`.

## Runs

```sh
shoal run hello -i name=Dana -i count=3 --follow   # values parse as JSON when they can
shoal run hello --inputs-file inputs.yaml --wait   # exit 0 only if the run succeeds
shoal logs run_01... -f
shoal get run run_01...
shoal cancel run_01...
shoal approve run_01...                     # the run's waiting approval step (or name it: run_01... publish)
shoal approve run_01... publish --reject -m "not yet"
shoal signal run_01... build.done -d '{"ok": true}'   # resume a wait step with data
```

`run --wait` and `--follow` print the `shoal approve` command when the run stops at an approval.
A rejection does not fail the run: the step's output has `approved: false`, so gate the steps after
it with `if: "{{ steps.<id>.output.approved }}"`.

## Secrets

```sh
shoal secrets set gh-token                 # prompts (hidden)
shoal secrets set gh-token --from-file token.txt
shoal secrets set gh-token --from-env GITHUB_TOKEN
shoal secrets list
```

Values are write-only: the API never returns them.

## Evals as a CI gate

```sh
shoal eval run triage-eval --baseline previous --fail-under 0.9 --max-regression 0.02 --wait
```

With `--wait` the command exits 1 unless the eval completes and its gate passes; the gate's
reasons are printed. `shoal eval get <id>` shows a past run.

```sh
shoal eval list triage-eval                   # recent runs: status, score, pass rate, gate
shoal eval compare <candidate-id> <baseline-id>   # case-by-case diff, regressions and improvements
shoal eval scores triage-eval                 # scores per workflow version
shoal eval cancel <id>
```

## Skills from marketplaces

```sh
shoal skills sources                          # skill sources this namespace can browse
shoal skills browse acme-skills -q pdf        # list or search one
shoal skills show acme-skills skills/pdf      # SKILL.md, files and version
shoal skills import acme-skills skills/pdf --dry-run   # the diff, nothing written
shoal skills import acme-skills skills/pdf    # as Skill 'pdf' (--name to choose, --version to pin)
shoal skills imports --check                  # imported skills and whether a newer version exists
shoal skills upgrade pdf                      # shows the diff, asks, applies
shoal skills rollback pdf 1                   # restore an earlier version
```

Sources themselves are tenant-wide resources: `shoal apply` a `SkillSource` and
`shoal get skill-sources` (see [skills.md](skills.md)).

## Integrations

`shoal integrations new servicenow --title ServiceNow` scaffolds an integration project from the
SDK template (see [integrations.md](integrations.md)).

## Running the server

```sh
shoal serve                    # API + embedded worker on 127.0.0.1:8080 (SHOAL_HOST / SHOAL_PORT)
shoal serve --no-worker        # API only
shoal worker                   # a worker (and scheduler) without HTTP; run as many as you need
```

Both read the `SHOAL_*` settings in `engine/shoal/config.py` (database URL, secret key, admin token,
public URL, ...). See [deploy/README.md](../deploy/README.md) for Docker, compose and Helm.
