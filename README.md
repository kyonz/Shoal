<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brand/shoal-logo-dark.svg">
    <img src="docs/assets/brand/shoal-logo.svg" alt="Shoal" width="320">
  </picture>
</p>

<p align="center">
  <strong>Open-source orchestration for multi-agent AI workflows.</strong><br>
  Define agents and workflows as code, run them durably in isolated sandboxes, and govern them across teams.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-0E9F8E"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-0B2E4F">
  <img alt="Status: alpha" src="https://img.shields.io/badge/status-alpha-FF7A59">
</p>

---

A shoal is a group of fish that swim together. **Shoal** does the same for AI agents: it gives each
agent a model, skills, tools and a sandbox, wires agents into versioned workflows, and runs those
workflows durably, whether a run takes ten seconds or waits a week for a code review.

Shoal is self-hostable, multi-tenant and API-first. The web UI, the `shoal` CLI and the Terraform
provider all use the same public HTTP API, and every layer (model provider, sandbox, integration,
secret store, eval platform) is a plugin you can swap without forking.

## Quickstart

### With docker compose

Postgres, the API server and a worker:

```sh
git clone https://github.com/kyonz/ai-workflow-manager.git shoal && cd shoal
cp deploy/.env.example deploy/.env          # set real secrets before exposing it
docker compose -f deploy/docker-compose.yaml --env-file deploy/.env up -d --build
```

The API and UI are on <http://localhost:8080>. Create your first tenant with the admin token from
`deploy/.env`; it prints the tenant's first API key:

```sh
uv tool install ./engine                    # installs the `shoal` CLI
shoal --server http://localhost:8080 --token "$SHOAL_ADMIN_TOKEN" tenants create acme
```

### Without Docker

```sh
cd engine && uv sync
SHOAL_ADMIN_TOKEN=change-me uv run shoal serve  # API + embedded worker on 127.0.0.1:8080, SQLite
```

### Run your first workflow

```sh
shoal login http://localhost:8080 --token <api-key>
shoal apply -f examples/hello/
shoal run hello -i name=Dana --wait
```

```text
workflow/hello (default) created v1
run run_01m5rs2jpdjadd0wnap8pf5dxr started (hello@v1)
run run_01m5rs2jpdjadd0wnap8pf5dxr succeeded
{
  "greeting": "Hello Dana"
}
```

From there, [`examples/jira-bugfix`](examples/jira-bugfix) turns a Jira ticket into a tested pull
request and [`examples/github-uplift`](examples/github-uplift) runs a change across every repository
in a GitHub org.

## What a workflow looks like

```yaml
apiVersion: shoal.dev/v1
kind: Workflow
metadata:
  name: bugfix
spec:
  inputs:
    issue_key: {type: string}
    repo: {type: string}
  connections: {jira: jira-acme, github: github-acme}
  steps:
    - id: issue
      type: action
      action: jira.get_issue
      with: {key: "{{ inputs.issue_key }}"}
    - id: fix
      type: agent
      agent: bugfixer                       # model, skills, tools and sandbox profile
      prompt: "Fix {{ steps.issue.output.summary }} in {{ inputs.repo }} and push a branch."
      outputs:
        branch: {type: string}
    - id: review
      type: approval                        # a durable human-in-the-loop gate
      message: "Open a PR for {{ steps.fix.output.branch }}?"
    - id: pr
      type: action
      action: github.create_pull_request
      with:
        repo: "{{ inputs.repo }}"
        head: "{{ steps.fix.output.branch }}"
        title: "{{ inputs.issue_key }}: {{ steps.issue.output.summary }}"
```

The full version, with tests, Jira writeback and failure handling, is in [`examples/jira-bugfix`](examples/jira-bugfix).

## Features

**Workflows and runs**
- Declarative, versioned workflows with typed inputs and outputs, authored as YAML (GitOps) or through the API and UI.
- Step types: `agent`, `action`, `script`, `wait`, `approval`, `condition`, `map`, `workflow` (sub-workflow) and `decide`, with retries, timeouts, `on_error` and parallel branches via `needs`.
- A durable executor on Postgres: runs survive restarts and can wait for days. No Temporal required.
- Versioning with change notes, diffs, rollback and pinned versions; live run events over SSE.

**Agents**
- Agents combine a model, a system prompt, skills (`SKILL.md`), tools, MCP servers, sub-agents and limits on turns, tokens, time and cost.
- Runtimes: Shoal's own tool-use loop, or an external coding agent CLI (Claude Code, Codex, Aider) inside the sandbox.
- Model providers: Anthropic and any OpenAI-compatible API, routed through LLM gateways (direct, LiteLLM, Portkey, OpenRouter and others) under per-namespace model policies.
- Agent interoperability over A2A and MCP, and delegated credentials with workload identity.

**Sandboxes**
- Every agent and script step runs in a sandbox: `local`, `docker`, `kubernetes`, `e2b`, Perplexity `space` (with suspend and wake during long waits) or any provider that speaks the remote sandbox protocol.

**Integrations and triggers**
- GitHub, Jira, generic webhooks and schedules, each with triggers, actions and metadata queries.
- An integrations SDK and test kit, plus a language-neutral remote protocol: write an integration in any language ([guide](docs/integrations.md)).
- Skill marketplaces: import `SKILL.md` skills from git repositories, registries or any skill source plugin, with version labels, upgrade diffs and rollback ([guide](docs/skills.md)).

**Campaigns, blueprints and evals**
- Campaigns fan one workflow out over many targets with concurrency, budgets, rate limits and progress tracking.
- Blueprints package resources so other teams can install and wire them up.
- Evals score workflows against datasets, gate CI on regressions and export to Braintrust or Langfuse.
- Decision models (`decide` steps) ask typed questions and branch on the answer and its confidence.

**Governance**
- Hard multi-tenancy; namespaces, roles and bindings; users, groups and service accounts; OIDC login and SCIM provisioning.
- Initiator attribution: every run records who started it, and actions in external systems can run with that person's authority.
- Encrypted secrets that are write-only through the API.

**Operate it your way**
- One container image for API, worker and CLI; docker compose and a Helm chart ([deploy guide](deploy/README.md)).
- The `shoal` CLI ([reference](docs/cli.md)) and a [Terraform provider](terraform-provider/README.md) that manages every resource as code.
- Prometheus metrics, health checks and an OpenAPI spec with interactive docs at `/v1/docs`.

## Documentation

| Read | For |
|---|---|
| [Architecture](docs/architecture.md) | Concepts, the runtime, plugin contracts and the API surface |
| [CLI](docs/cli.md) | Connecting, applying resources, runs, secrets, evals |
| [Integrations](docs/integrations.md) | Building integrations with the SDK or the remote protocol |
| [Skill sources](docs/skills.md) | Importing skills from marketplaces, and writing skill source plugins |
| [Deploying](deploy/README.md) | Container image, docker compose and Kubernetes |
| [Terraform provider](terraform-provider/README.md) | Managing Shoal as code |
| [Brand](docs/brand.md) | Logo, colours and how to use them |

## Repository layout

```
engine/               Python engine: library, API server, worker and the `shoal` CLI
frontend/             React + TypeScript web UI
terraform-provider/   Terraform provider for every Shoal resource
deploy/               Dockerfile, docker compose and Helm chart
examples/             Workflows, agents, triggers and campaigns to start from
templates/            Starter project for a new integration
docs/                 Architecture and guides
```

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) to set up a development
environment and run the tests, and follow our [Code of Conduct](CODE_OF_CONDUCT.md). Please report
security issues privately as described in [SECURITY.md](SECURITY.md).

> **Naming note.** The project was started as "Shoal", which is why the Python package,
> CLI command, environment variables and resource `apiVersion` still use `shoal`. These will move to
> Shoal names in a later release.

## License

Shoal is licensed under the [Apache License 2.0](LICENSE).
