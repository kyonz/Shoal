# AI Workflow Manager — Architecture

Status: **Draft v1** · Owner: @kyonz · Last updated: 2026-10-25

AI Workflow Manager (AWM) is an open-source, self-hostable platform for defining,
running and governing multi-agent AI workflows. It is API-first, multi-tenant,
and built from pluggable parts so organisations can swap any layer (sandbox,
model provider, integration, secret store, orchestrator) without forking.

This document is the reference design. It describes the concepts, the runtime,
the plugin contracts, the API surface and the two acceptance journeys that the
first release must satisfy.

---

## 1. Goals and non-goals

### Goals

1. **Pluggable engine.** The core is a Python library (`awm`) with typed
   (pydantic v2) contracts. Every external dependency sits behind a protocol and
   is discovered through Python entry points.
2. **Usable directly.** A polished React UI and a CLI sit on top of the same
   public HTTP API. Nothing the UI does is unavailable to integrators.
3. **Agent workflows.** Users compose workflows out of steps; agent steps run an
   agent (model + instructions + skills + tools) inside an isolated sandbox.
4. **Integrations.** A framework with three capabilities per integration:
   *triggers* (start work from external events), *actions* (do things in
   external systems) and *metadata* (discover and query external resources).
5. **Campaigns.** An orchestration layer that fans a workflow out over many
   targets (e.g. every repo in an org) with concurrency, budgets, rate limits,
   long waits (days) and progress tracking.
6. **Multi-tenant.** Hard tenant isolation in data, secrets, quotas and API keys.
7. **Multi-engine execution.** Sandboxes on local processes, Docker,
   Kubernetes, E2B, Perplexity SPACE and any other provider via a plugin.
8. **Open-source maturity.** Apache-2.0, semver'd public API, versioned workflow
   schema, OpenAPI spec, Helm chart, docker-compose, observability, docs.

### Non-goals (v1)

- A hosted SaaS control plane (the design allows it; we do not build billing).
- Training or fine-tuning models.
- A general-purpose ETL/data pipeline engine. AWM is for agentic work.

---

## 2. Concepts

| Concept | What it is |
|---|---|
| **Tenant** | Isolation boundary. Owns everything below. Has members, API keys, quotas. |
| **Skill** | A reusable, versioned capability an agent can use: instructions + tools + files (compatible with the Agent Skills `SKILL.md` format). |
| **Tool** | A callable function exposed to an agent (shell, file edit, HTTP, integration action, MCP server tool). |
| **Agent** | A named configuration: model/provider, system prompt, skills, tools, sandbox profile, limits. |
| **Workflow** | A versioned, declarative graph of **steps** with typed inputs/outputs. |
| **Step** | A node in a workflow: `agent`, `action`, `wait`, `approval`, `condition`, `map`, `workflow` (sub-workflow), `script`. |
| **Run** | One execution of a workflow version with concrete inputs. Durable; survives restarts. |
| **Integration** | A plugin type (e.g. `github`, `jira`) providing triggers, actions and metadata. |
| **Connection** | A tenant's configured instance of an integration (credentials + settings). |
| **Trigger** | A binding: event source (webhook / schedule / poll) + filter → workflow inputs. |
| **Blueprint** | A versioned, parameterised package of resources others install and wire up. |
| **Campaign** | A managed fan-out of one workflow across a target set, with policies. |
| **Identity** | A person mapped across systems (email, GitHub login, Jira account) for attribution. |
| **Sandbox profile** | Which sandbox backend to use plus image, resources, network policy, TTL. |

All of these are pydantic models in `awm.core.models` and are serialisable to
YAML/JSON, so they can be authored as files (GitOps) or via the API/UI.

---

## 3. High-level architecture

```
                    ┌──────────────────────────────────────────────┐
  React UI ───────▶ │                 API server (FastAPI)          │ ◀── CLI / SDKs / integrators
  (frontend/)       │  auth: API keys + sessions · tenancy · OpenAPI│ ◀── Webhooks (Jira, GitHub, ...)
                    └───────────────┬──────────────────────────────┘
                                    │ writes intents / reads state
                    ┌───────────────▼──────────────────────────────┐
                    │                 State store                   │
                    │  Postgres (SQLite for dev) via SQLAlchemy     │
                    │  runs · steps · events · timers · campaigns   │
                    └───────────────▲──────────────────────────────┘
                                    │ lease / checkpoint
     ┌──────────────────────────────┴───────────────────────────────┐
     │                          Workers (N)                          │
     │  Scheduler: timers, polls, campaign ticks, lease recovery     │
     │  Executor: advances runs step-by-step (durable state machine) │
     │      │                │                    │                  │
     │  Agent runtime   Integration actions   Sandbox manager        │
     │  (providers)     (github, jira, ...)   (local/docker/k8s/e2b) │
     └──────────────────────────────────────────────────────────────┘
```

- **API server** is stateless; horizontally scalable.
- **Workers** are stateless; all run state is in the store. Any worker can
  resume any run. Work is claimed with row-level leases
  (`SELECT … FOR UPDATE SKIP LOCKED` on Postgres).
- **Store** is the single source of truth. An append-only `run_events` table is
  the audit log and the feed for the UI's live view (SSE).

### Why a built-in durable executor (and not Temporal by default) — decided

A week-long "wait for review" means runs must be durable. We implement a small
durable state machine on top of the database rather than requiring Temporal,
because self-hosters should be able to run AWM with just Postgres. The executor
sits behind the `Orchestrator` protocol so a Temporal (or other) backend can be
added as a plugin later without touching workflows.

---

## 4. Repository layout

```
engine/                 Python package `awm` (library + server + worker + CLI)
  awm/core/             pydantic models, workflow schema, expressions, errors
  awm/plugins/          plugin registry (entry points) and base protocols
  awm/runtime/          durable executor, scheduler, step handlers
  awm/agents/           agent runtime, model providers, skills, tools
  awm/sandbox/          sandbox protocol + local/docker/kubernetes/e2b backends
  awm/integrations/     integration framework + github, jira, webhook, schedule
  awm/campaigns/        campaign planner, controller, policies
  awm/store/            SQLAlchemy models, repositories, migrations (Alembic)
  awm/secrets/          secret providers (env, encrypted-db, kubernetes)
  awm/api/              FastAPI app, routers, auth, tenancy, SSE
  awm/cli/              `awm` CLI (Typer)
  tests/
frontend/               React + TypeScript + Vite UI
examples/               Workflow, campaign and trigger definitions (the journeys)
deploy/                 Dockerfiles, docker-compose, Helm chart
docs/                   Architecture, guides, API reference, plugin authoring
```

---

## 5. Workflow model

Workflows are declarative and versioned (`apiVersion: awm.dev/v1`). Example
(abbreviated from `examples/jira-bugfix/workflow.yaml`):

```yaml
apiVersion: awm.dev/v1
kind: Workflow
metadata:
  name: jira-bugfix
spec:
  inputs:
    issue_key: {type: string}
    repo:      {type: string}
  steps:
    - id: fetch
      type: action
      action: jira.get_issue
      with: {key: "${{ inputs.issue_key }}"}

    - id: fix
      type: agent
      agent: bugfixer               # references an Agent resource
      sandbox: default              # sandbox profile
      prompt: |
        Fix the bug described below in ${{ inputs.repo }}. Run the tests.
        ${{ steps.fetch.output.summary }}
        ${{ steps.fetch.output.description }}
      outputs:
        pr_url: {type: string}
        test_summary: {type: string}

    - id: writeback
      type: action
      action: jira.add_comment
      with:
        key: "${{ inputs.issue_key }}"
        body: "Opened ${{ steps.fix.output.pr_url }}\n\n${{ steps.fix.output.test_summary }}"
```

### Step types

| Type | Purpose |
|---|---|
| `agent` | Run an agent in a sandbox until it returns structured outputs (validated against the declared schema). |
| `action` | Call an integration action (e.g. `github.create_pull_request`, `jira.transition`). |
| `script` | Run a command in a sandbox (deterministic glue, test runs). |
| `wait` | Durable wait: for a duration, until a time, or until a condition/event (polled or event-driven) with a timeout. |
| `approval` | Human-in-the-loop gate, resolved via API/UI with optional assignees. |
| `condition` | Branch on an expression (`if`/`then`/`else` step lists). |
| `map` | Fan out a sub-list of steps over a list with a concurrency limit. |
| `workflow` | Call another workflow as a child run. |
| `decide` | Ask a decision model typed questions (choice / score / boolean) and branch on the answer and confidence. |

Every step accepts `if`, `retry` (attempts, backoff), `timeout`, and
`on_error` (`fail` · `continue` · `goto:<step>`). Steps form a DAG: by default
they run in order; `needs: [ids]` allows parallel branches.

### Expressions

`${{ … }}` expressions are evaluated with a sandboxed Jinja2 environment over a
context of `inputs`, `steps.<id>.output`, `run`, `trigger`, `campaign`,
`target` and `secrets` (secrets are resolved late and redacted in logs/events).

---

## 6. Durable runtime

A run is a persisted state machine:

```
run:   pending → running → (waiting ↔ running)* → succeeded | failed | cancelled
step:  pending → running → waiting? → succeeded | failed | skipped | cancelled
```

- The executor computes the **ready set** (steps whose `needs` are complete and
  whose `if` is true), dispatches each to its handler, and **checkpoints** the
  result before moving on. Handlers are idempotent by `(run_id, step_id,
  attempt)` keys, which are also passed to integrations as idempotency keys.
- A handler returns one of `Completed(output)`, `Failed(error, retryable)` or
  `Suspended(resume_at | event_key)`. Suspension releases the worker; a
  **timer** row or an **event subscription** row brings the run back.
- `wait` steps with conditions are re-evaluated on each wake (e.g. "PR approved
  or merged"), with an absolute `timeout` (e.g. `7d`) and an `on_timeout`
  branch.
- Long-running agent steps heartbeat; if a worker dies its lease expires and
  another worker resumes from the last checkpoint (the agent step restarts its
  attempt; sandbox state is reattached when the backend supports it).

The scheduler loop (in every worker, coordinated by leases) fires timers, polls
poll-based triggers and wait conditions, ticks campaigns, and recovers expired
leases.

---

## 7. Agents, skills and tools

- **Model providers** (`awm.providers` entry point): `anthropic` (default),
  `openai`-compatible, `bedrock`, `vertex`, plus `scripted` (deterministic, for
  tests). A provider implements one async method: `complete(messages, tools,
  …) -> ModelResponse`.
- **Agent runtimes** (`awm.agent_runtimes`):
  - `tool-loop` — AWM's own tool-use loop: model ↔ tools until the model
    calls the built-in `finish` tool with outputs matching the step schema.
  - `cli` — run an external coding agent CLI (e.g. Claude Code, Codex, Aider)
    inside the sandbox with the prompt, collect structured output from a
    file. This is the pragmatic choice for heavy coding tasks.
- **Skills** are directories with a `SKILL.md` (frontmatter: name,
  description) plus optional scripts/resources. They are stored per tenant,
  versioned, mounted into the sandbox, and listed to the agent so it can load
  them on demand.
- **Tools** come from: built-ins (`shell`, `read_file`, `write_file`,
  `edit_file`, `http`), integration actions (exposed automatically, scoped by
  the connection), and MCP servers.
- **Multi-agent.** A workflow composes agents by step. Within a step an agent
  can delegate to sub-agents declared in its `subagents:` list (exposed as a
  `delegate` tool) — this keeps orchestration visible in the workflow graph.
- **Limits.** Per agent step: max turns, max tokens, wall-clock timeout,
  cost budget. Usage is recorded per step and rolled up to run, campaign and
  tenant.

### 7.1 Agent authentication and credentials

Agents act on external systems, so *how they authenticate* is pluggable and
kept out of the model's reach.

- **Credential providers** (`awm.credentials` entry point) mint the credential
  a step needs at the moment it needs it:
  - `static` — a tenant secret (PAT, API token);
  - `oauth2` — user-delegated OAuth (authorization-code flow run by the UI
    against the connection, refresh tokens stored encrypted, scopes per
    connection);
  - `github-app` — short-lived installation tokens, optionally narrowed to the
    target repository and permissions of the current step;
  - `aws-sts`, `gcp-wif`, `azure-federated` — workload identity: AWM presents
    its run identity token and exchanges it for cloud credentials;
  - `vault` — dynamic secrets from HashiCorp Vault/OpenBao.
- **Scoping and lifetime.** Credentials are resolved per step attempt, scoped
  to that step's connections, expire with the step, and are redacted from
  events, logs and transcripts.
- **Keeping secrets away from the model.** Integration actions run in the
  worker, so the model only ever sees tool inputs/outputs. Code running in the
  sandbox gets credentials through env/files scoped to the sandbox, or through
  an egress proxy that injects auth headers for allow-listed hosts so the raw
  token never enters the sandbox (backends that support it: SPACE credential
  references, Kubernetes sidecar proxy).
- **Run identity.** Each agent step receives a signed, short-lived JWT
  (`iss` = AWM, `sub` = `tenant/run/step/agent`, audience-restricted). It is
  used to call back into the AWM API with least privilege, as the subject for
  workload-identity federation, and as the caller identity for A2A and MCP.
- **Human identity.** When a run is started by a user, the user id is carried
  on the run for audit, and policy can require per-user OAuth connections
  ("act as the requester") instead of a shared service identity.

### 7.2 Agent interoperability: A2A and MCP

- **A2A outbound.** An agent can delegate to a remote agent that speaks the
  Agent2Agent (A2A) protocol. Remote agents are registered as `Agent`
  resources with `runtime: a2a` (agent card URL + credential provider), so
  they work as workflow `agent` steps and as `subagents` of a local agent.
  AWM fetches and caches the Agent Card, sends the task, streams updates into
  run events, and maps the final artifacts onto the step's declared outputs.
- **A2A inbound.** Any agent or workflow can be published as an A2A agent:
  AWM serves its Agent Card (skills derived from the workflow inputs/outputs,
  `securitySchemes` for API keys/OAuth) and maps A2A tasks onto runs,
  including streaming and input-required (approval) states.
- **MCP.** Agents consume MCP servers as tool sources (stdio inside the
  sandbox, or remote HTTP with OAuth via the credential providers). AWM also
  exposes an MCP server so other agents/IDEs can list and run workflows.

### 7.3 LLM gateways and model routing

Model access is pluggable at two levels.

- **Gateways** (`awm.gateways` entry point) are how requests reach a model:
  `anthropic` (direct, official SDK), `bedrock`, `vertex`, `foundry`, any
  Anthropic-compatible base URL (LiteLLM proxy, Portkey, Cloudflare AI
  Gateway, an internal gateway), and `openai-compatible` (OpenRouter, vLLM,
  Ollama, Azure OpenAI). A gateway is a tenant-configurable resource with
  credentials, base URL, default headers, timeouts and retry policy, so a
  platform team can force all traffic through its own gateway for logging,
  caching and spend control.
- **Model policies** (`ModelPolicy` resources) decide *which* model runs.
  Agents name a model directly (`claude-opus-5-5`) or a policy/class
  (`model: policy:coding`). A policy is an ordered set of rules matching on
  tenant, agent, workflow, labels and task class, and yields candidates with:
  - allow/deny lists per tenant (only approved models and gateways);
  - cost ceilings per call/step/run, and downgrade rules when a budget is low;
  - data-residency constraints (region/`inference_geo`, "no third-party
    gateways for this tenant");
  - fallbacks on rate limits, outages or refusals (provider-side fallbacks
    are used where the gateway supports them);
  - weighted splits for A/B comparisons, recorded per run so evals can
    compare them.
- The chosen gateway and model, the reason (rule id) and usage/cost are
  written to the run events, so spend and quality can be attributed.

### 7.4 Evaluations

Workflows and agents change (prompt, model, skills, workflow version), so
quality is measured, not assumed.

- **`Eval` resources** define a dataset of cases (workflow inputs plus
  expectations, or a query over past runs) and one or more **graders**:
  expression checks over outputs, JSON-schema checks, a script grader that
  runs in a sandbox (e.g. "do the tests pass on the produced branch"), and
  LLM-as-judge graders with a rubric and a pinned judge model.
- **Eval runs** reuse the campaign engine: each case is a target, so
  concurrency, budgets and progress tracking come for free. Results are
  scored per case and aggregated per variant (workflow version × agent ×
  model policy), with diffs against a baseline.
- **Gates.** An eval can gate promotion of a new workflow version or model
  policy (must not regress beyond a threshold), and runs in CI through the
  API/CLI (`awm eval run --baseline v12 --fail-under 0.9`).
- **Online evals** sample production runs (e.g. "was the Jira fix PR
  merged within 7 days?", judge-scored PR quality) and trend them on the
  dashboard.
- **Inline quality gates.** A `grade` step type scores an upstream output with
  a rubric inside a workflow, so a run can retry, escalate to a human or stop
  when quality is below a threshold.

### 7.5 Decision models (System One / Jev)

Many steps in an agentic system are not generation but *decisions*: is this
ticket suitable for automation, is this tool call safe, which model should
handle this, is this PR good enough to merge. Typed "System One" decision
models such as TypeSafe's **Jev** answer exactly that: given a `state` (text
or JSON) and named questions, they return typed answers with probabilities
and a confidence value, in roughly 70-500 ms and at a fraction of LLM cost.
They cannot generate text or code, so they complement agents rather than
replace them.

- **Decision providers** (`awm.decision_providers` entry point) share one
  contract: `decide(state, questions) -> answers`. Question types are
  `choice` (pick one of up to 255 options), `score` (place on an ordered
  rubric) and `boolean` (Jev's *noul*: probability a statement is true).
  Providers: `jev` (TypeSafe API via its SDK, or via OpenRouter / Vercel AI
  Gateway where configured) and `llm` (any model through the gateways in
  §7.3 using structured output) as the self-hostable fallback.
- **`decide` step.** A workflow step that asks typed questions about any
  expression (issue text, diff summary, prior outputs) and exposes
  `answer`, `probabilities` and `confidence` for branching with `if`. A
  `min_confidence` setting routes low-confidence decisions to a human
  `approval` or to an LLM fallback instead of acting.
- **Where AWM uses decisions natively:**
  - *Trigger triage* — semantic trigger filters ("is this a reproducible bug
    in scope for automation?") evaluated before a run starts.
  - *Tool-call gating* — in the agent runtime, mutating tool calls are
    screened (irreversibility, intent mismatch, scope) against thresholds;
    calls above them are blocked or held for approval.
  - *Model routing* — model policies (§7.3) can score task difficulty and
    pick the model and effort from the result.
  - *Campaign targeting* — classify targets (e.g. "repo has a test suite")
    before fan-out so budget is not spent on unsuitable targets.
  - *Merge checks and grading* — cheap rubric scores for the agent-assisted
    merge check (§11.1), the `grade` step and online evals (§7.4).
  - *Content screening* — flag likely prompt injection in fetched pages and
    webhook payloads before they reach an agent (a filter, not a security
    boundary).
- **Caveats.** Jev is closed-weight and hosted only (no on-premises option),
  which matters for data residency; performance figures are vendor claims.
  Every decision use is therefore optional, provider-pluggable, logged with
  its probabilities, and backed by thresholds the operator controls.

---

## 8. Sandboxes (multi-engine execution)

```python
class SandboxBackend(Protocol):
    name: str
    async def create(self, spec: SandboxSpec) -> Sandbox: ...
    async def attach(self, sandbox_id: str) -> Sandbox: ...   # reattach after worker restart

class Sandbox(Protocol):
    id: str
    async def exec(self, cmd: list[str] | str, *, cwd=None, env=None, timeout=None) -> ExecResult: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def read_file(self, path: str) -> bytes: ...
    async def close(self) -> None: ...
    # Optional capability (Sandbox.supports_suspend): persist VM state and free
    # compute during long waits, then resume with the workspace intact.
    async def suspend(self) -> None: ...
    async def resume(self) -> None: ...
```

| Backend | Notes |
|---|---|
| `local` | Subprocess in a temp dir. Dev/test only; no isolation. |
| `docker` | One container per sandbox via the Docker API. |
| `kubernetes` | One Pod per sandbox (namespace per tenant optional), `exec` over the K8s API, resource limits, NetworkPolicy, `activeDeadlineSeconds`. Works with gVisor/Kata runtime classes. |
| `e2b` | E2B cloud sandboxes (and self-hosted E2B) via the `e2b` SDK. |
| `space` | [Perplexity SPACE](https://github.com/perplexityai/SPACE): self-hosted Firecracker microVM sandboxes over its HTTP API (operations polling, SSE command streams, `files:read`/`files:write`, idempotency keys). Supports **suspend/wake**, which AWM uses to park a sandbox during long `wait` steps. |
| `remote` | Generic adapter for any provider that implements the small AWM Remote Sandbox HTTP contract. |

SPACE also ships an E2B compatibility wrapper, so the `e2b` backend can target
a SPACE deployment by pointing the E2B domain at it.

`SandboxSpec` carries image/template, CPU/memory, env, network egress policy,
TTL and labels (`tenant`, `run`, `step`) for cost attribution. Sandbox profiles
are tenant-configurable resources, so one tenant can run on K8s while another
uses E2B.

---

## 9. Integrations framework

```python
class Integration(Protocol):
    key: str                                  # "github"
    connection_schema: type[BaseModel]        # credentials/settings (secret refs)
    triggers: dict[str, TriggerSpec]          # "issue.transitioned", "pull_request.review"
    actions:  dict[str, ActionSpec]           # "create_pull_request", "add_comment"
    resources: dict[str, ResourceSpec]        # metadata: "repository", "issue"

    async def verify_webhook(self, conn, request) -> bool: ...
    async def parse_webhook(self, conn, request) -> list[Event]: ...
    async def poll(self, conn, trigger, cursor) -> tuple[list[Event], cursor]: ...
    async def call_action(self, conn, action, params, ctx) -> BaseModel: ...
    async def query(self, conn, resource, filter, page) -> Page[Resource]: ...
```

- **Actions** have pydantic input/output models, so they are simultaneously
  workflow `action` steps, agent tools (JSON schema from the model) and API
  operations (`POST /v1/connections/{id}/actions/{action}`).
- **Triggers**: `webhook` (signature-verified, one URL per connection
  `/v1/hooks/{webhook_id}`), `poll` (cursor persisted) and **`schedule`**
  (cron expressions with time zones, e.g. `0 9 * * MON-FRI` in
  `Pacific/Auckland`; overlap policy `skip`/`allow`; missed-run catch-up off by
  default). Schedules are first-class in the UI and API, can start workflows or
  campaigns, and are evaluated by the scheduler loop with leases so each tick
  fires exactly once across workers.
  A trigger has a **filter** (expression over the event, e.g. "issue moved to
  column *Ready for AI*") and an **input mapping** into workflow inputs, plus
  dedupe keys and concurrency (e.g. one run per issue).
- **Metadata** (`resources`) powers campaign target selection and UI pickers,
  e.g. `github.repository` filtered by `owner`, `topic`, `language`,
  `permission: admin`.
- Built-in integrations in v1: `github`, `jira`, `webhook` (generic, HMAC),
  `schedule`, `http`. Slack/Linear/GitLab follow the same contract.

---

## 10. Campaigns

A campaign = **workflow** × **target selector** × **policies**.

```yaml
apiVersion: awm.dev/v1
kind: Campaign
metadata: {name: test-coverage-uplift}
spec:
  workflow: repo-uplift
  targets:
    source: github.repository
    connection: github-acme
    filter: {owner: acme, permission: admin, archived: false}
  inputs:
    repo: "${{ target.full_name }}"
    goal: "Raise test coverage to 100%"
  policy:
    concurrency: 10            # max active runs
    rate: {runs_per_hour: 50}
    budget: {usd: 500}
    on_failure: continue       # or pause_after: 5
  schedule: {start: now}
```

- The **planner** resolves targets through metadata queries and snapshots them
  (so the target set is reproducible and auditable).
- The **controller** (ticked by the scheduler) starts runs within concurrency,
  rate and budget limits; supports pause / resume / cancel / retry-failed and
  adding targets later.
- Progress: per-target status, aggregate counts, cost, and outcome summaries
  (`steps.*.output`) shown in a dashboard.

## 10.1 Blueprints and the marketplace

A **blueprint** is a reusable, versioned package of an agent workflow that
someone builds once and others wire up to their own systems.

- **Contents.** A blueprint bundles resources (workflow, agents, skills,
  triggers, campaign templates, sandbox profile hints) plus a README, icon,
  tags, author and semver version. Resources inside it are templates with
  `${{ params.* }}` placeholders.
- **Parameters.** It declares what the consumer must supply: connections by
  integration type ("a `github` connection", "a `jira` connection"), secrets,
  values (e.g. Jira column name, coverage goal) and optional choices
  (sandbox profile, model policy). The UI renders an install wizard from these
  declarations, with pickers for existing connections.
- **Install.** Installing renders the templates with the parameters and
  applies the resulting resources under a name prefix, labelled
  `awm.dev/blueprint=<name>@<version>` and linked to the installation, so
  the workflow can be opened in the visual builder and customised.
  Upgrading to a new version shows a diff and re-applies, keeping local
  overrides where possible. Uninstall removes what was installed.
- **Publishing and promotion.** Blueprints move through
  `draft → published → promoted (featured/verified) → deprecated`.
  Visibility is per tenant (private), install-wide (shared with every tenant
  in the deployment) or public. Promotion is an approval workflow run by
  platform admins and can require passing evals (§7.4).
- **Marketplace.** A searchable catalogue with categories, integration
  filters, install counts, success rates and eval scores taken from real
  runs, ratings, and the README.
- **Registries.** Sources are pluggable (`awm.registries`): the local
  database, a Git repository of blueprint folders, an OCI registry, or an
  HTTP index. That lets an organisation host an internal marketplace and the
  open-source community share a public one.
- The two acceptance journeys ship as the first blueprints:
  **GitHub org uplift** and **Jira bugfix with writeback**.

---

## 11. Multi-tenancy, auth and security

- Every row carries `tenant_id`; all repository queries are tenant-scoped by
  construction (the repository layer takes a `TenantContext`, there is no
  unscoped query API). Postgres row-level security is enabled as defence in
  depth.
- **API keys**: `awm_<prefix>_<secret>`, stored as SHA-256 hashes, with
  scopes (`workflows:read`, `runs:write`, `admin`, …), expiry, last-used, and
  revocation. Users authenticate to the UI via OIDC (and a bootstrap admin
  for local installs); service accounts use API keys.
- **RBAC**: roles `owner`, `admin`, `editor`, `operator`, `viewer` per tenant.
- **Secrets**: pluggable `SecretProvider` (`env`, `db` encrypted with a
  KEK, `kubernetes`, Vault later). Workflows reference secrets by name; they
  are injected at the last moment and redacted from events and logs.
- **Quotas**: per-tenant concurrency, sandbox minutes and token budgets.
- **Audit log**: every mutating API call and every agent tool call is recorded.

---

### 11.1 Initiator attribution and delegated authority

AWM is usually operated by a platform team, so its credentials (a GitHub App,
a service account) can reach repositories the person who triggered a run
cannot. AWM therefore tracks *who drove the work* separately from *which
credential performed it*, and lets policy decide what the run may do.

- **Initiator.** Every run records an initiator: the user behind the API key
  or UI session, the human in a webhook event (e.g. the Jira user who moved
  the ticket), or whoever launched the campaign. Child runs and campaign runs
  inherit it.
- **Identities.** `Identity` resources map a person across systems
  (`email`, `github` login, `jira` account id, Slack id). Users can link their
  GitHub account through OAuth, which stores a per-user token as a secret.
  Webhook actors are resolved to identities so a Jira-triggered run knows the
  GitHub login of the person who asked.
- **Auth modes per connection** (`attribution.mode`):
  - `user` — act with the initiator's own GitHub OAuth token. GitHub's own
    permissions apply; the run fails clearly if the user has not linked an
    account (optional `fallback: app`).
  - `app` — act with the GitHub App / service credentials, and apply the
    governance policy below to keep the initiator's authority visible and
    enforced.
- **Governance policy (GitHub App mode)**, enforced inside the integration so
  workflows cannot bypass it, with every decision written to the audit log
  and the step output:
  - *Provenance labels* on PRs/issues, e.g. `awm:automated` and
    `awm:initiator-can-merge` / `awm:initiator-cannot-merge`, plus a
    `Requested-by: @login` trailer in PR bodies and `Co-authored-by` commit
    trailers, so downstream systems can tell whether the work was driven by
    someone with the right permissions.
  - *Forced ownership* — assign the PR to the initiator and/or request their
    review, or assign to the repository's CODEOWNERS.
  - *Merge gating* — when the initiator lacks write access to the target
    repository, AWM will not use the App's broader rights to merge on its
    own. It requires an approval from someone who does have write access
    (checked per reviewer), and optionally an *agent-assisted merge check*:
    a reviewer agent step that checks the diff against policy and posts its
    verdict as a review before merge. Policies can also require labels or
    block merging entirely (`elevation: deny | qualified_approval | allow`).

## 12. API

- REST + JSON under `/v1`, generated OpenAPI 3.1 spec, cursor pagination,
  `Idempotency-Key` header support on POSTs, problem+json errors.
- Resources: `tenants`, `api-keys`, `agents`, `skills`, `workflows`
  (+versions), `runs` (+steps, events, cancel, approve), `connections`
  (+actions, resources), `triggers`, `campaigns` (+targets, pause/resume),
  `sandbox-profiles`, `hooks/{trigger_id}`.
- Live updates: `GET /v1/runs/{id}/events` (SSE).
- Outbound: tenant-configurable webhooks for run/campaign lifecycle events.

---

## 13. Acceptance journeys

### J1 — GitHub org uplift campaign

> "Get all the repos I own to 100% (coverage), wait for people to review (max 1
> week), then merge if we have merge rights."

1. Connection `github-acme` (GitHub App or PAT).
2. Workflow `repo-uplift`:
   `agent: uplift` (clone, raise coverage, open PR; outputs `pr_number`) →
   `wait` until `github.pull_request` is approved **or** 7 days elapse,
   polling every 30 min and woken early by PR review webhooks →
   `condition` approved and `github.can_merge` → `action: github.merge_pull_request`
   else `action: github.add_comment` ("closing the loop" note).
3. Campaign over `github.repository` filtered to `owner: acme, permission: admin`.
4. Dashboard shows per-repo state: *PR open → awaiting review (3d left) → merged*.

### J2 — Jira webhook bugfix with writeback

1. Connection `jira-acme`; Jira webhook (advanced/automation webhook) points at
   the trigger URL with a shared secret.
2. Trigger filter: `event.issue_event_type == "issue_generic" and
   event.changelog.status.to == "Ready for AI"`; input mapping pulls
   `issue_key` and the repo from a custom field; dedupe on issue key.
3. Workflow `jira-bugfix`: fetch issue → transition to *In Progress* →
   agent fixes bug & runs tests in a K8s/E2B sandbox → open PR → comment on
   the ticket with PR + test results → transition to *In Review* (or back with
   a failure comment on error via `on_error`).

Both journeys ship as runnable examples and as end-to-end tests using the
`scripted` provider, `local` sandbox and fake GitHub/Jira servers.

---

## 14. Frontend (React)

The UI is a first-class client of the public API (no private endpoints), built
with React + TypeScript + Vite, TanStack Query, React Router, Tailwind and
React Flow (`@xyflow/react`). Goals: polished, advanced, yet obvious to a
first-time user.

- **Visual workflow builder** — a drag-and-drop canvas:
  - a palette of step types (agent, action, script, wait, approval, workflow,
    set) and integration actions discovered from `/v1/integrations`, dragged
    onto the canvas;
  - edges are `needs` dependencies, drawn by connecting handles; auto-layout
    (dagre) with manual positioning persisted in workflow metadata;
  - an inspector panel that renders each step's form from its JSON schema
    (prompt editor with `${{ }}` autocompletion of inputs and upstream outputs,
    agent/sandbox/connection pickers, retry/timeout/if settings);
  - live validation (unknown references, cycles, missing inputs) mirrored from
    the server's pydantic validation;
  - two-way sync with a YAML code view, undo/redo, keyboard shortcuts,
    minimap, versions and diff, and "Run" with a generated input form.
- **Run viewer** — the same graph colour-coded live over SSE, a step timeline,
  agent transcripts (model turns and tool calls), outputs, approvals and
  cancel/retry.
- **Campaigns dashboard** — progress, per-target status table, cost, and
  pause/resume/cancel controls.
- **Integrations** — catalogue, connection forms rendered from schemas, webhook
  URLs to copy, trigger builder with a test-event dry run.
- **Agents, skills, sandbox profiles, API keys, secrets** — CRUD screens.
- Command palette (⌘K), dark/light themes, responsive layout, accessible
  components.

## 15. Observability and operations

- Structured JSON logs, OpenTelemetry traces (run → step → tool call),
  Prometheus metrics (`awm_runs_total`, `awm_step_duration_seconds`,
  `awm_tokens_total`, `awm_sandbox_seconds_total`).
- Deploy: `docker compose up` (API, worker, Postgres, UI) and a Helm chart.
- Config via env vars (`AWM_DATABASE_URL`, `AWM_SECRET_KEY`, …) with a
  pydantic-settings model.

---

## 16. Plugin authoring

Plugins are normal Python packages exposing entry points:

```toml
[project.entry-points."awm.integrations"]
linear = "awm_linear:LinearIntegration"

[project.entry-points."awm.sandboxes"]
modal = "awm_modal:ModalBackend"
```

Groups: `awm.integrations`, `awm.sandboxes`, `awm.providers`,
`awm.agent_runtimes`, `awm.gateways`, `awm.decision_providers`,
`awm.credentials`, `awm.secrets`, `awm.steps`, `awm.orchestrators`, `awm.registries`.
The UI renders connection/action forms from the plugins' JSON schemas, so a
new plugin needs no frontend changes.

---

## 17. Delivery plan

1. Architecture doc + repo scaffolding, CI.
2. Core models, expressions, plugin registry.
3. Store + durable executor + step handlers; local sandbox; scripted provider.
4. Agent runtime (tool loop + CLI runtime), Anthropic provider, skills.
5. Integrations framework + GitHub + Jira + webhook/schedule triggers.
6. Campaigns.
7. API server: tenancy, API keys, all resources, SSE.
8. Kubernetes, Docker, E2B and SPACE sandbox backends.
9. React UI with the drag-and-drop workflow builder.
10. Journeys as examples + e2e tests; docker-compose, Helm, docs.

## 18. Open questions

- Product name (working name: AWM).
- OIDC provider defaults for the UI (generic OIDC proposed).
