# Shoal — Architecture

Status: **Draft v1** · Owner: @kyonz · Last updated: 2026-10-25

Shoal is an open-source, self-hostable platform for defining,
running and governing multi-agent AI workflows. It is API-first, multi-tenant,
and built from pluggable parts so organisations can swap any layer (sandbox,
model provider, integration, secret store, orchestrator) without forking.

This document is the reference design. It describes the concepts, the runtime,
the plugin contracts, the API surface and the two acceptance journeys that the
first release must satisfy.

---

## 1. Goals and non-goals

### Goals

1. **Pluggable engine.** The core is a Python library (`shoal`) with typed
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
- A general-purpose ETL/data pipeline engine. Shoal is for agentic work.

---

## 2. Concepts

| Concept | What it is |
|---|---|
| **Tenant** | Organisation and hard isolation boundary. Owns namespaces, users, groups, roles, quotas. |
| **Namespace** | A team's space inside a tenant. Holds agents, workflows, connections, secrets and runs; users and groups are members with roles. |
| **Role / binding** | A named set of permissions, granted to a user, group or service account tenant-wide or in one namespace. |
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
| **Cloud identity** | A cloud role a namespace brings (an AWS IAM role), granted to specific workflows and agents; steps get short-lived credentials for it. |
| **Sandbox profile** | Which sandbox backend to use plus image, resources, network policy, TTL. |

All of these are pydantic models in `shoal.core.models` and are serialisable to
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
because self-hosters should be able to run Shoal with just Postgres. Scheduling
sits behind the `Orchestrator` protocol (`shoal.runtime.orchestrator`): it decides
when and where a run is advanced, the executor decides what advancing means.
Each run records the orchestrator that drives it. Besides the built-in one,
Shoal ships an optional Temporal orchestrator (`SHOAL_ORCHESTRATOR=temporal`, the
`temporal` extra) for operators who already run Temporal: each run is a Temporal
workflow whose passes are activities, while the store stays the source of truth
([guide](temporal.md)). Other backends plug in through the `shoal.orchestrators`
entry point group.

---

## 4. Repository layout

```
engine/                 Python package `shoal` (library + server + worker + CLI)
  shoal/core/             pydantic models, workflow schema, expressions, errors
  shoal/plugins/          plugin registry (entry points) and base protocols
  shoal/runtime/          durable executor, scheduler, step handlers
  shoal/agents/           agent runtime, model providers, skills, tools
  shoal/sandbox/          sandbox protocol + local/docker/kubernetes/e2b backends
  shoal/integrations/     integration framework + github, jira, webhook, schedule
  shoal/campaigns/        campaign planner, controller, policies
  shoal/store/            SQLAlchemy models, repositories, migrations (Alembic)
  shoal/secrets/          secret providers (env, encrypted-db, kubernetes)
  shoal/api/              FastAPI app, routers, auth, tenancy, SSE
  shoal/cli/              `shoal` CLI (Typer)
  tests/
frontend/               React + TypeScript + Vite UI
examples/               Workflow, campaign and trigger definitions (the journeys)
deploy/                 Dockerfiles, docker-compose, Helm chart
docs/                   Architecture, guides, API reference, plugin authoring
```

---

## 5. Workflow model

Workflows are declarative and versioned (`apiVersion: shoal.dev/v1`). Example
(abbreviated from `examples/jira-bugfix/workflow.yaml`):

```yaml
apiVersion: shoal.dev/v1
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
      with: {key: "{{ inputs.issue_key }}"}

    - id: fix
      type: agent
      agent: bugfixer               # references an Agent resource
      sandbox: default              # sandbox profile
      prompt: |
        Fix the bug described below in {{ inputs.repo }}. Run the tests.
        {{ steps.fetch.output.summary }}
        {{ steps.fetch.output.description }}
      outputs:
        pr_url: {type: string}
        test_summary: {type: string}

    - id: writeback
      type: action
      action: jira.add_comment
      with:
        key: "{{ inputs.issue_key }}"
        body: "Opened {{ steps.fix.output.pr_url }}\n\n{{ steps.fix.output.test_summary }}"
```

### Step types

| Type | Purpose |
|---|---|
| `agent` | Run an agent in a sandbox until it returns structured outputs (validated against the declared schema). |
| `action` | Call an integration action (e.g. `github.create_pull_request`, `jira.transition`). |
| `script` | Run deterministic code in a sandbox: a shell `run:` line, or a `code:` program in Python, Node, sh or bash with typed `inputs` (JSON in `$SHOAL_INPUTS`) and validated `outputs` (`$SHOAL_OUTPUTS`). See §5.2. |
| `wait` | Durable wait: for a duration, until a time, until a condition/event (polled or event-driven), or for a **signal** carrying data, with a timeout. |
| `approval` | Human-in-the-loop gate, resolved via API/UI with optional assignees. |
| `condition` | Branch on an expression (`if`/`then`/`else` step lists). |
| `map` | Fan out a sub-list of steps over a list with a concurrency limit. |
| `workflow` | Call another workflow as a child run. |
| `decide` | Ask a decision model typed questions (choice / score / boolean) and branch on the answer and confidence. |

Every step accepts `if`, `retry` (attempts, backoff), `timeout`, and
`on_error` (`fail` · `continue` · `goto:<step>`). Steps form a DAG: by default
they run in order; `needs: [ids]` allows parallel branches.

### Expressions

`{{ … }}` expressions are evaluated with a sandboxed Jinja2 environment over a
context of `inputs`, `steps.<id>.output`, `run`, `trigger`, `campaign`,
`target` and `secrets` (secrets are resolved late and redacted in logs/events).
Plain Jinja delimiters are used (not `${{ }}`) so specs can be written inline
in Terraform HCL, which interpolates `${…}` itself. In YAML, quote a value that
starts with `{{`, e.g. `if: "{{ steps.fix.output.tests_passed }}"`. (Decided.)

### 5.1 Versioning: uplift, adjust, roll back

Every declarative resource (workflows, agents, skills, triggers, campaigns,
evals, blueprints, …) is versioned by the server. Any change that alters its
meaning creates a new integer `version`. Resubmitting the same meaning, even
spelled differently, does not. (Decided.)

- **Change notes.** `PUT …?message=` and `POST /v1/apply?message=` store a note
  with the new version. `GET …/{name}/versions` lists history (who, when,
  note) and `GET …/{name}/versions/{n}` returns one version.
- **Diff.** `GET …/{name}/diff?from=N&to=M` (where `to` defaults to the
  current version) returns `[{op, path, from, to}]` with JSON-pointer paths.
  It diffs the spec as written, not server defaults, and matches steps by `id`,
  so inserting a step reads as one `add`. The UI and `shoal diff` render it.
- **Rollback.** `POST …/{name}/rollback {version, message?, dry_run?}` restores
  an earlier version as a **new** version. History only moves forward, so a
  rollback can itself be rolled back. `dry_run` returns the diff instead.
- **Pinning.** Runs record the workflow version they ran. A trigger can pin
  `workflow_version`, and a `workflow` step can pin `version`. A campaign pins
  `workflow_version` at launch (the current version unless set), so a long
  campaign never splits across versions mid-flight. Eval runs pin versions
  through `variants`.
- **Uplift with confidence.** The intended loop is: change the workflow (vN+1)
  → run its eval with `baseline: version:N` → the gate passes → move pinned
  triggers and campaigns to N+1. If production regresses, roll back.
  Blueprint adoptions upgrade the same way (`PUT …/blueprint-adoptions/{id}
  {version}`), with a dry-run diff first.

---

### 5.2 Script steps: deterministic work without an agent

Much of a workflow is glue that needs no judgement: reshape an API response,
dedupe a list, compute a score, call a plain HTTP endpoint, run the tests.
A `script` step does that as code, which is faster, cheaper and repeatable.
(Decided.)

```yaml
- id: summarise
  type: script
  language: python            # python · node · shell (default) · bash
  sandbox: default            # any sandbox profile, on any backend
  inputs:                     # rendered, then written as JSON
    issues: "{{ steps.fetch.output.items }}"
  outputs:                    # typed; checked like agent outputs
    count: {type: integer}
  code: |
    import shoal_step
    shoal_step.set_output("count", len(shoal_step.inputs["issues"]))
```

- **Sources.** `run` is a shell command line and is rendered as an
  expression (as before). `code` is a program used verbatim, never rendered,
  so data reaches it only through `inputs` and code cannot be injected through
  an expression. A step has exactly one of the two.
- **Contract.** The handler writes the program, `inputs.json` and an empty
  `outputs.json` under `.shoal/steps/<step>-<attempt>/` in the sandbox
  workspace and runs it with `$SHOAL_INPUTS`, `$SHOAL_OUTPUTS` and
  `$SHOAL_STEP_DIR` set. Scalar inputs are also exported as
  `$SHOAL_INPUT_<NAME>` for shell. Python and Node get a small `shoal_step`
  helper (`inputs`, `set_output`, `set_outputs`) on their module path, so
  nothing has to be installed in the image. Because the contract is files and
  environment variables, it works on every sandbox backend.
- **Outputs.** After a successful exit, the JSON object in `$SHOAL_OUTPUTS` is
  validated against `outputs` (defaults, coercion, required fields) and merged
  into the step output next to `exit_code`, `stdout`, `stderr` and
  `duration_seconds` (also under `outputs`). Missing or mistyped outputs fail
  the step. Those output names are reserved. A non-zero exit fails the step
  unless `fail_on_error: false`. Secrets are redacted from everything.

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
- **Signals** send data into a running workflow. A `wait` step with
  `signal: {name: build.done, match: "{{ signal.ok }}"}` resumes when a signal
  with that name (and a payload matching `match`) arrives, and outputs its
  `payload`. Signals come from `POST /v1/runs/{id}/signals/{name}` (permission
  `runs:signal`, audited), `shoal signal <run> <name> -d '{…}'`, or the run's
  **callback URL** `{{ run.signal_url }}/<name>`, an unauthenticated URL
  carrying an HMAC token for systems that can only call back a URL. Named
  signals are stored (`run_signals`) until a step takes them, so a reply that
  beats the workflow to its wait is not lost; each signal is taken once, and
  non-matching ones stay queued. `signal: {event: "github:pr:{{ … }}"}` instead
  takes the next integration event with that wake key while the step waits,
  with the event as payload. `max_wait` and `on_timeout` apply as for
  conditions.
- Long-running agent steps heartbeat; if a worker dies its lease expires and
  another worker resumes from the last checkpoint (the agent step restarts its
  attempt; sandbox state is reattached when the backend supports it).

The scheduler loop (in every worker, coordinated by leases) fires timers, polls
poll-based triggers and wait conditions, ticks campaigns, and recovers expired
leases.

---

## 7. Agents, skills and tools

- **Model providers** (`shoal.providers` entry point): `anthropic` (default),
  `openai`-compatible, `bedrock`, `vertex`, plus `scripted` (deterministic, for
  tests). A provider implements one async method: `complete(messages, tools,
  …) -> ModelResponse`.
- **Agent runtimes** (`shoal.agent_runtimes`):
  - `tool-loop` — Shoal's own tool-use loop: model ↔ tools until the model
    calls the built-in `finish` tool with outputs matching the step schema.
  - `cli` — run an external coding agent CLI (e.g. Claude Code, Codex, Aider)
    inside the sandbox with the prompt, collect structured output from a
    file. This is the pragmatic choice for heavy coding tasks.
- **Skills** are directories with a `SKILL.md` (frontmatter: name,
  description) plus optional scripts/resources. They are stored per tenant,
  versioned, mounted into the sandbox, and listed to the agent so it can load
  them on demand.
- **Skill sources** (`shoal.skill_sources`) are marketplaces skills are
  imported from: `git` (Agent Skills layout, pinned to a ref), `registry`
  (`shoal.skill-source/v1` over HTTP, which `shoal.sdk.skill_server` serves for
  any source plugin) or a plugin. A `SkillSource` is tenant-wide and admin
  managed, with credentials as secrets in `shared`. Importing writes a normal
  `Skill` into a namespace labelled with source, skill, version and ref;
  upgrades show a diff and roll back through resource versioning
  ([skills.md](skills.md)).
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

- **Credential providers** (`shoal.credentials` entry point) mint the credential
  a step needs at the moment it needs it:
  - `static` — a tenant secret (PAT, API token);
  - `oauth2` — user-delegated OAuth (authorization-code flow run by the UI
    against the connection, refresh tokens stored encrypted, scopes per
    connection);
  - `github-app` — short-lived installation tokens, optionally narrowed to the
    target repository and permissions of the current step;
  - `aws` (built), `gcp`, `azure` (later) — cloud workload identity through
    `CloudIdentity` resources, below;
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
  (`iss` = Shoal, `sub` = `tenant/run/step/agent`, audience-restricted). It is
  used to call back into the Shoal API with least privilege, as the subject for
  workload-identity federation, and as the caller identity for A2A and MCP.
- **Cloud identities** (built, AWS). A namespace admin registers a
  `CloudIdentity` (`provider: aws`, `aws.role_arn`, region, duration, optional
  session policy) and grants it with `allow.workflows` (name globs, deny by
  default; `ns/name` for a `shared` identity) and optionally `allow.agents`.
  Agents and script steps list `cloud_identities:`. Per step attempt Shoal
  exchanges a credential that starts from its own deployment for an STS
  session and injects `AWS_*` env (secret parts redacted). Two trust modes:
  `web_identity` — Shoal is an OIDC issuer (`/.well-known/openid-configuration`,
  `/.well-known/jwks.json`, ES256 key derived from `SHOAL_SECRET_KEY` or set
  with `SHOAL_IDENTITY_SIGNING_KEY`), signs a five-minute token with
  `sub = tenant:<id>:namespace:<ns>:cloud-identity:<name>` and calls
  `AssumeRoleWithWebIdentity`, so Shoal holds no cloud credential; and
  `ambient` (operator opt-in, `SHOAL_AWS_AMBIENT`) — `AssumeRole` signed with
  the deployment's own identity (env, IRSA, EKS Pod Identity, ECS, IMDSv2)
  with that subject as ExternalId, so tenants sharing a deployment role
  cannot use each other's roles. Sessions carry the initiator as
  SourceIdentity and `shoal:*` session tags (tenant, namespace, workflow, run,
  step, agent, initiator); `GET …/cloud-identities/{name}/trust-policy`
  generates the role's trust policy and setup commands. Issues, refusals and
  failures go to run events and the audit log. Registering roles and changing
  grants is `namespace-admin` only (editors cannot grant their own
  workflows). Federated members mint on their own deployment. Guide:
  `docs/cloud-identities.md`.
- **Human identity.** When a run is started by a user, the user id is carried
  on the run for audit, and policy can require per-user OAuth connections
  ("act as the requester") instead of a shared service identity.

### 7.2 Agent interoperability: A2A and MCP

- **A2A outbound.** An agent can delegate to a remote agent that speaks the
  Agent2Agent (A2A) protocol. Remote agents are registered as `Agent`
  resources with `runtime: a2a` (agent card URL + credential provider), so
  they work as workflow `agent` steps and as `subagents` of a local agent.
  Shoal fetches and caches the Agent Card, sends the task, streams updates into
  run events, and maps the final artifacts onto the step's declared outputs.
- **A2A inbound.** Any agent or workflow can be published as an A2A agent:
  Shoal serves its Agent Card (skills derived from the workflow inputs/outputs,
  `securitySchemes` for API keys/OAuth) and maps A2A tasks onto runs,
  including streaming and input-required (approval) states.
- **MCP.** MCP servers are an ordinary integration (`mcp`), not a special
  case: each `Connection` with `integration: mcp` is one server (Streamable
  HTTP URL, a token secret, a tool allow-list). Its tools are listed per
  connection (`tools/list`, cached briefly) and become actions
  (`action: mcp.<tool>` with `connection:`), agent tools (`tools:
  [mcp:<connection>]` or `mcp:<connection>.<tool>`, exposed as
  `<connection>__<tool>`), wait-condition actions and API calls, like any
  dynamic integration. Read-only tools (`readOnlyHint`) are marked
  non-mutating. Credentials follow the connection's `credentials` mode, so a
  server can use one team token or each person's own token or OAuth grant
  (§11.4). Stdio-only servers run behind an MCP HTTP proxy for now; running
  them inside the agent sandbox is planned. Shoal also exposes an MCP server
  so other agents/IDEs can list and run workflows (planned).

### 7.3 LLM gateways and model routing

Model access is pluggable at two levels.

- **Gateways** (`shoal.gateways` entry point) are how requests reach a model:
  `anthropic` (direct, official SDK), `bedrock`, `vertex`, `foundry`, any
  Anthropic-compatible base URL (LiteLLM proxy, Portkey, Cloudflare AI
  Gateway, an internal gateway), and `openai-compatible` (OpenRouter, vLLM,
  Ollama, Azure OpenAI). A gateway is a **tenant-wide resource managed
  centrally** by tenant admins (`gateways:manage`) with base URL, default
  headers, timeouts and retry policy, so a platform team can force all
  traffic through its own gateway for logging, caching and spend control.
  **Credentials are never clear text**: the API key (`api_key_secret`) and
  credential headers (`secret_headers: {header: secret}`) are names of
  encrypted secrets in the tenant's `shared` namespace, resolved at call time
  and redacted from run logs. Header or config names that look like
  credentials (`authorization`, `*-key`, `*token*`, `secret`, ...) are
  rejected in the plain fields. Agents choose a gateway by name
  (`provider:`) and may tune the model (`provider_config`: effort,
  max_tokens, ...) but cannot set endpoints or credentials (`base_url`,
  `headers`, `api_key`, ...), so nothing routes around the central gateway.
- **Model policies** (`ModelPolicy` resources, tenant-wide, managed with
  `model-policies:manage`) decide *which* model runs.
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
  API/CLI (`shoal eval run --baseline v12 --fail-under 0.9`).
- **Online evals** sample production runs (e.g. "was the Jira fix PR
  merged within 7 days?", judge-scored PR quality) and trend them on the
  dashboard.
- **Versions and comparison.** Every eval result records the workflow
  version it ran (and the versions of the agents and sub-workflows it used),
  so scores are kept per resource version: `GET …/evals/{name}/scores`
  trends them, an eval run can pin `variants` (version × input overrides) to
  A/B versions in one run, and `baseline` (`previous`, `version:N` or a run
  id) adds a case-by-case diff (regressions, improvements, grader deltas).
- **Eval platforms.** `shoal.eval_sinks` plugins connect Braintrust and
  Langfuse (and any other platform): finished eval runs export as
  experiments / sessions (one trace per case with step spans, plus every
  grader score), production runs export as traces, and datasets import as
  cases (live via `spec.dataset`, or into a new Eval version via
  `POST …/evals/{name}/import`). A failed export never fails the eval.
  An `EvalPlatform` (tenant-wide, kept in `shared`, managed with
  `eval-platforms:manage`) holds one sink's settings once, with its secrets
  named from `shared` (clear-text credentials are rejected). Datasets, eval
  sinks and the import / export endpoints then say `platform: <name>`, and
  any inline `config` is merged over the platform's. `POST
  /v1/eval-platforms/{name}/test` checks the credentials and `GET
  …/datasets` lists dataset names for pickers.
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

- **Decision providers** (`shoal.decision_providers` entry point) share one
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
- **Where Shoal uses decisions natively:**
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
| `space` | [Perplexity SPACE](https://github.com/perplexityai/SPACE): self-hosted Firecracker microVM sandboxes over its HTTP API (operations polling, SSE command streams, `files:read`/`files:write`, idempotency keys). Supports **suspend/wake**, which Shoal uses to park a sandbox during long `wait` steps. |
| `remote` | Generic adapter for any provider that implements the small Shoal Remote Sandbox HTTP contract. |

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
  operations (`POST /v1/namespaces/{namespace}/connections/{name}/actions/{action}`).
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

### 9.1 Open integrations platform (SDK)

Anyone can build an integration; the built-ins use exactly the same public
contract, so they double as reference implementations.

- **Python SDK** (`shoal.sdk`, re-exporting the `Integration` base,
  `@action`/`@resource` decorators, `IntegrationEvent`, `ConnectionContext` and
  helpers) plus a **test kit** (`shoal.sdk.testing`: an in-memory engine, a
  mock HTTP transport, webhook signing helpers and contract tests that every
  integration should pass) and a project template (`shoal integrations new
  linear`) producing a package with entry points, tests and CI.
- **Out-of-process integrations** over HTTP, so integrations can be written in
  any language and deployed separately: the service serves a manifest
  (`GET /manifest`: key, title, connection JSON schema, actions with
  input/output schemas, resources, events) and endpoints
  `POST /actions/{name}`, `POST /resources/{name}/query`,
  `POST /webhooks/verify` and `POST /webhooks/parse`, receiving the resolved
  connection config per call. Tenants register one with a
  `RemoteIntegration` connection (`url`, auth token); Shoal adapts it to the same
  `Integration` interface, so actions become steps and agent tools
  automatically.
- **Catalogue and marketplace.** Installed integrations (built-in, Python
  packages, remote) appear in `/v1/integrations` and the UI catalogue with
  their schemas; community integrations can be listed alongside blueprints
  (§10.1) with verification status.
- Implemented in `engine/shoal/sdk/` (SDK, `shoal.sdk.testing`, `shoal.sdk.server`,
  `shoal.sdk.scaffold`), `engine/shoal/integrations/remote.py` (the `remote`
  adapter and `register_remote_integration`) and `templates/integration/`.
  The author guide and the full protocol are in
  [integrations.md](integrations.md).

---

## 10. Campaigns

A campaign = **workflow** × **target selector** × **policies**.

```yaml
apiVersion: shoal.dev/v1
kind: Campaign
metadata: {name: test-coverage-uplift}
spec:
  workflow: repo-uplift
  targets:
    source: github.repository
    connection: github-acme
    filter: {owner: acme, permission: admin, archived: false}
  inputs:
    repo: "{{ target.full_name }}"
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
one team builds once and other teams in the same organisation adopt and wire
up to their own systems. The marketplace is a catalogue **inside each
tenant**: it is how namespace owners discover and adopt what other teams have
published. It is not a shared, cross-tenant marketplace for the Shoal service
— decided.

- **Contents.** A blueprint bundles resources (workflow, agents, skills,
  triggers, campaign templates, sandbox profile hints) plus a README, icon,
  tags, author and semver version. Resources inside it are templates with
  `{{ params.* }}` placeholders.
- **Parameters.** It declares what the adopter must supply: connections by
  integration type ("a `github` connection", "a `jira` connection"), secrets,
  values (e.g. Jira column name, coverage goal) and optional choices
  (sandbox profile, model policy). The UI renders an adopt wizard from these
  declarations, with pickers for the connections the namespace can see
  (its own and `shared`).
- **Adopt.** A namespace owner (`blueprints:adopt` plus write access for each
  kind the blueprint creates) adopts a blueprint into their namespace with
  `POST /v1/namespaces/{ns}/blueprints/{name}/adopt {params, prefix, version?,
  dry_run}`. Expressions over `params`, `prefix` and `blueprint` are rendered.
  Run-time expressions (`inputs`, `steps`, `event`, …) are left as written.
  The resulting resources are applied in that namespace and labelled with
  `shoal.dev/blueprint`, `shoal.dev/blueprint-version` and `shoal.dev/adoption`, so
  the workflow can be opened in the visual builder and customised. `prefix`
  lets one namespace adopt the same blueprint twice, and a name clash is a
  409. Adoptions are listed under `…/blueprint-adoptions`. `PUT …/{id}
  {version?, params?, dry_run}` upgrades or re-parameterises an adoption and
  deletes resources the new version no longer has. `DELETE` removes the
  adoption and what it created. (Built.)
- **Publishing.** Blueprints are tenant-wide resources. Teams publish them
  from a namespace (`blueprints:publish`); they move through
  `draft → published → featured → deprecated`. Featuring is done by tenant
  admins and can require passing evals (§7.4). A blueprint can optionally be
  limited to certain namespaces.
- **Catalogue.** Searchable within the tenant, with categories, integration
  filters, which namespaces have adopted it, success rates and eval scores
  from real runs, and the README.
- The two acceptance journeys ship as the first blueprints:
  **GitHub org uplift** and **Jira bugfix with writeback**.

---

## 11. Multi-tenancy, auth and security

- **Tenants and namespaces.** A *tenant* is an organisation (hard isolation:
  every row carries `tenant_id`, all store queries are tenant-scoped by
  construction, Postgres row-level security as defence in depth). Inside a
  tenant, **namespaces** group the things a team owns: agents, skills,
  workflows, connections, triggers, campaigns, sandbox profiles, secrets and
  runs all live in exactly one namespace (gateways, model policies, identities
  and blueprints are tenant-wide, stored in `shared`) (`default` exists in every
  tenant). Users and groups are added to a namespace as members with a role, so
  a platform team can own `platform` while product teams get their own
  namespaces. Resource names are unique per (tenant, namespace, kind).
  Lookups fall back from the run's namespace to a tenant-wide `shared`
  namespace, so platform teams can publish shared connections and skills that other namespaces use without being able to see their secrets.
- **Many tenants on one instance.** One deployment hosts any number of
  tenants (federation adds tenants on other deployments, §11.2). A person has
  one **account** on the instance, keyed by their identity provider's
  `(issuer, sub)`, linked to at most one user in each tenant they belong to;
  roles stay per tenant. A session belongs to the account and points at one
  tenant at a time: `GET /v1/me/tenants` lists the tenants to switch to and
  `POST /v1/me/tenant` issues a session for another one (the UI's tenant
  switcher). People sign in either through the **instance sign-in**
  (`SHOAL_INSTANCE_OIDC`, one IdP for everyone) or through a tenant's own
  providers; both link the account to tenant users with the same identity.
  Only the instance sign-in links by verified email and applies the
  `SHOAL_INSTANCE_ADMINS` bootstrap, because tenant admins configure their own
  providers and those must not vouch for people in other tenants.
- **Instance admins and tenant admins.** Accounts can hold an instance role:
  `instance-admin` (create, rename, suspend, resume and delete tenants; give
  people roles in any tenant; grant instance roles; act as tenant admin in any
  tenant, audited as `account:<email>`) or `instance-auditor` (read-only
  everywhere, including audit). Tenant admins (`tenant-admin` role) run their
  own tenant as before. Instance admins work at `/v1/instance/*` and
  `/v1/tenants/*`; instance-level events (tenants created or suspended,
  instance roles, instance sign-ins) go to the instance audit log
  (`GET /v1/instance/audit`). Suspending a tenant refuses its members' calls and
  new runs, keeps its data, and leaves running work to finish. Nobody can change
  their own instance role, and the last instance admin cannot be removed unless
  the break-glass `SHOAL_ADMIN_TOKEN` is set.
- **Users, groups and service accounts.** Users come from OIDC sign-in or SCIM
  provisioning; groups come from SCIM or from an OIDC `groups` claim. Service
  accounts are non-human principals that own API keys.
- **RBAC.** Permissions are `resource:verb` strings (`workflows:read`,
  `workflows:write`, `runs:create`, `runs:approve`, `runs:cancel`,
  `connections:write`, `secrets:write`, `campaigns:launch`, `members:manage`,
  `roles:manage`, `blueprints:publish`, `audit:read`, …, `*` for all). A
  **role** is a named set of permissions; built-in roles are `tenant-admin`,
  `namespace-admin`, `editor`, `operator` (run, approve, cancel), `viewer`, and
  admins can define custom roles. A **role binding** grants a role to a
  subject (user, group or service account) either tenant-wide or in one
  namespace. Every API call is authorised by `authorize(principal, permission,
  namespace)`; the UI hides what the principal cannot do using the same check
  (`GET /v1/me/permissions`).
- **OIDC sign-in.** Generic OpenID Connect (Okta, Microsoft Entra ID, Google,
  Keycloak, Auth0, Dex): authorization-code flow with PKCE, ID tokens verified
  against the provider's JWKS (issuer, audience, expiry, nonce), short-lived
  Shoal session tokens afterwards; signing out revokes the session token
  itself, not only the cookie. Claims map to the user record and optionally
  to role bindings (`groups` claim → group membership). Configured per tenant
  (several providers allowed) and optionally once for the whole instance, plus
  a break-glass admin token for first setup.
- **SCIM 2.0 provisioning.** `/scim/v2/Users`, `/scim/v2/Groups`,
  `/ServiceProviderConfig`, `/ResourceTypes`, `/Schemas` with filtering
  (`userName eq "…"`) and PATCH, authenticated by a per-tenant SCIM bearer
  token. Deactivating a user in the IdP disables their sessions and API keys
  immediately; group membership changes flow through to role bindings.
- **API keys**: `shoal_<prefix>_<secret>`, stored as SHA-256 hashes, owned by a
  user or service account, restricted to namespaces and a role (never more
  than the owner holds), with expiry, last-used, and revocation.
  `SHOAL_API_KEY_MAX_DAYS` caps how long a key may live (keys created without
  an expiry get the cap). A key
  limited to namespaces keeps only the tenant-level `:read` permissions of its
  owner, never tenant administration.
- **Nobody hands out more than they hold.** Granting a role, changing a custom
  role's permissions, changing a group that has role bindings, choosing an
  OIDC provider's default role, minting a key for a service account, and
  managing (deactivating, deleting, editing) a user or service account all
  need the caller to hold every permission involved. Nobody can deactivate or
  delete themselves.
- **Secrets**: pluggable `SecretProvider` (`env`, `db` encrypted with a
  KEK, `kubernetes`, Vault later). Workflows reference secrets by name; they
  are injected at the last moment and redacted from events and logs.
- **Quotas**: per-tenant concurrency, sandbox minutes and token budgets.
- **Audit log**: every mutating API call and every agent tool call is recorded.
  Behind reverse proxies, `SHOAL_TRUSTED_PROXIES` (addresses or CIDRs) makes
  the log record the client address from `X-Forwarded-For` instead of the
  proxy's.

---

### 11.1 Initiator attribution and delegated authority

Shoal is usually operated by a platform team, so its credentials (a GitHub App,
a service account) can reach repositories the person who triggered a run
cannot. Shoal therefore tracks *who drove the work* separately from *which
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
  - *Provenance labels* on PRs/issues, e.g. `shoal:automated` and
    `shoal:initiator-can-merge` / `shoal:initiator-cannot-merge`, plus a
    `Requested-by: @login` trailer in PR bodies and `Co-authored-by` commit
    trailers, so downstream systems can tell whether the work was driven by
    someone with the right permissions.
  - *Forced ownership* — assign the PR to the initiator and/or request their
    review, or assign to the repository's CODEOWNERS.
  - *Merge gating* — when the initiator lacks write access to the target
    repository, Shoal will not use the App's broader rights to merge on its
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
  `sandbox-profiles`, `evals` (+runs, scores, import), `eval-runs`
  (+results, compare, export, cancel), `eval-sinks`, `eval-platforms`
  (+test, datasets), `hooks/{trigger_id}`.
- Live updates: `GET /v1/runs/{id}/events` (SSE).
- Outbound: tenant-configurable webhooks for run/campaign lifecycle events.

### 11.2 Federated tenants (tenant connectors)

A tenant can be **hosted** (its data and execution live in the central
deployment) or **federated**: it runs on its own Shoal deployment inside the
organisation's infrastructure (its own database, workers, sandboxes, secrets
and network) and is managed from the central deployment. Both kinds can mix in
one control plane, and a federated deployment can also sit next to the hub.

```
          Central deployment (hub)                         Organisation network
 ┌─────────────────────────────────────┐          ┌──────────────────────────────────┐
 │ UI · API · OIDC · Terraform target   │          │ Member deployment (tenant "acme") │
 │ tenants: hosted-a, hosted-b, acme ───┼──tunnel──┼─ tenant connector (dials out)     │
 │ hub stores: link, health, rollups    │  mTLS    │ API · workers · Postgres · secrets │
 └─────────────────────────────────────┘          │ sandboxes · GitHub/Jira in-network │
                                                   └──────────────────────────────────┘
```

- **Same software, two roles.** A member is an ordinary Shoal deployment with
  federation enabled. The **tenant connector** runs inside the member's API
  process and keeps an outbound HTTPS long-poll open to the hub
  (`POST /v1/federation/tunnel`). Each poll returns the answers to earlier
  calls and picks up newly queued ones. Nothing in the organisation has to
  accept inbound traffic, and it passes through ordinary egress proxies. The
  connector authenticates with a link credential issued at enrollment. Hubs
  that terminate TLS with client certificates can require mTLS as well.
  (Built. Long-poll over HTTPS was chosen over a WebSocket because it is
  proxy-friendly, and because the queue lives in the hub's database, any hub
  replica can take the call and any replica can serve the poll.)
- **Enrollment.** An operator creates a federated tenant on the hub, which
  returns a single-use enrollment token. The member's admin runs
  `shoal federation join <hub-url> --token … --max-mode read_write --namespace …
  --grant tenant-admin=hub_group:platform`, choosing the **maximum access** it
  grants, which namespaces it exposes, and the first role bindings for hub
  people. The hub stores the link (`shoal federation link <tenant>` creates it
  and `shoal federation links` shows connection health), never the member's
  data or secrets. Only an empty tenant can be federated; a revoked link can be
  replaced by linking the tenant again. Until then the hub refuses the tenant's
  calls with `503 federation_revoked` instead of serving it as an empty hosted
  tenant, and the member's Federation page says the hub rejected its link.
  A member that leaves (`shoal federation leave`) tells the hub, which marks
  the link `left` and refuses the same way, and removes the role bindings it
  gave hub people and groups. Operators can manage links as code with
  the Terraform `shoal_federation_link` resource.
- **Access modes per link.**
  - `read`: the hub can list and view resources, runs, events, campaigns,
    evals and audit, and stream run events. Any write returns
    `403 federation_read_only`.
  - `read_write`: the hub can also apply resources, start, cancel and approve
    runs, launch campaigns, manage namespaces, roles and bindings, and write
    secret values (write-only, never read back).
  - The member sets the ceiling and the hub can only ask for less. The member
    can also expose only some namespaces and choose `metadata` visibility,
    which hides step outputs, prompts and logs from the hub.
- **Member is the source of truth.** Requests for a federated tenant are
  forwarded live through the tunnel; data is not replicated to the hub. A
  forwarded call waits in the hub's queue encrypted, and is deleted as soon as
  it is answered or times out. The hub keeps only link status and the
  member's version (rollups such as run counts and spend are planned). When the hub is unreachable the member
  keeps running on its own; the hub shows the tenant as disconnected.
- **One API.** Clients (UI, CLI, Terraform, A2A) talk to the hub with the
  same `/v1` API for hosted and federated tenants; the hub routes by tenant.
  They can also talk to the member directly.
- **Identity and authorisation, twice.** People sign in at the hub. Each
  forwarded request carries a one-minute assertion signed with the link secret
  (HS256). It names the user, their hub groups, the request id, and the
  method and path it was issued for, so it cannot be replayed against
  another call. The member authorises it again with its own
  RBAC, where hub people act as `hub:user:<email>` and are bound to roles like
  any subject, by `hub_user` (email or service actor) or `hub_group` (hub group name).
  The member reads its ceiling afresh for every call, so lowering it applies at once,
  and a member that exposes only some namespaces shows the hub only their audit entries. A call succeeds only if the hub
  allows it, the link mode allows it, and the member's RBAC allows it. The
  hub's operator token gets no rights in a member by default: a member that
  wants operators to browse binds `hub_user` `system:admin` explicitly. (Decided.)
- **Audit on both sides.** The hub records who asked for what; the member
  records the action with `via: hub`, the hub request id and the user, so
  either side can reconstruct it.
- **Compatibility.** The member reports its version and protocol on every
  poll, and the hub shows it in `GET /v1/federation/links`.
- **Hub-local vs forwarded.** The hub serves its own directory, sign-in, API
  keys, OIDC, SCIM, webhooks and the federation endpoints. Everything else
  under `/v1` for a federated tenant is forwarded, including `/v1/me/permissions`,
  so the UI shows what the member allows.
- **Live run events.** The tunnel carries request/response pairs, not streams,
  so the hub serves `GET /v1/runs/{id}/events` as SSE itself: it long-polls the
  member's event list (`?after=<last id>&wait=15`) through the tunnel and relays
  each event, then `end` when the run finishes. The connector serves forwarded
  requests concurrently and answers each as soon as it is done, so a waiting
  poll never delays other calls.
- **Not in scope:** moving a tenant between hosted and federated while
  running (export and re-import instead), and cross-tenant workflows.

### 11.4 Where secrets live

Every credential is encrypted at rest, write-only through the API, resolved at
the moment of use and redacted from run logs. The `env` provider reads
`SHOAL_SECRET_<TENANT>__<NAME>` and then `SHOAL_SECRET_<NAME>`; the second is
readable by every tenant, so instances shared by several organisations set
`SHOAL_SECRET_ENV_SHARED=false`. Variables that configure Shoal itself
(`SHOAL_SECRET_KEY`, `SHOAL_SECRET_PROVIDERS`) are never handed out as
secrets, and names that would contain `__` are not looked up (so tenant
`acme` cannot read tenant `acme--ops`'s variables); env secrets are
tenant-wide, readable from every namespace. Deleting a namespace deletes its secrets, role bindings and
personal credentials; deleting a user deletes their personal credentials.
There are four scopes:

| Scope | Holds | Managed by |
|---|---|---|
| **Deployment** | Platform keys (`SHOAL_SECRET_KEY`, operator LLM keys, federation link secrets), from env or a secret backend (`shoal.secrets` plugins: env, db; Vault/KMS planned) | Operator |
| **Tenant** (`shared` namespace) | Gateway keys and `secret_headers`, org-wide connections, OAuth client secrets | Tenant admins |
| **Namespace** | A team's connection secrets | Namespace admins/editors |
| **User** | A person's own token or OAuth grant for one connection (`user_credentials`) | That person only; admins can list and revoke, never read |

**Per-user credentials.** A connection chooses whose credential calls use:
`credentials: shared` (default), `per_user` or `per_user_or_shared`, and
which of its fields people supply (`user_fields`, default: the fields in
`secrets`). The person a call acts for is the run's initiator, mapped to a
Shoal user through identities (§11.1), so a run started from a Jira webhook
acts for the person behind the event. Service accounts and unknown people
never use personal credentials. When a `per_user` connection meets someone
who has not connected it, the step **waits** (event `credential.required`
with a link to connect) and resumes as soon as they save the credential,
rather than failing. People connect under `/v1/me/credentials` (paste a token,
or `oauth:` on the connection runs an authorization-code flow with PKCE and
refreshes access tokens before they expire; the callback only saves the
grant in the browser that started the flow or one signed in as that person,
so an authorize link sent to someone else cannot collect their account) or with `shoal credentials
set|connect`. Every set, removal and revocation is audited. This applies to
every integration, MCP servers included.

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
    (prompt editor with `{{ }}` autocompletion of inputs and upstream outputs,
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
- **Agents, skills, sandbox profiles, API keys, secrets** — CRUD screens,
  all scoped by a namespace switcher in the top bar.
- **Admin console** — users and groups (with SCIM status), namespaces and
  their members, roles (built-in and custom, edited as a permission matrix),
  role bindings, OIDC providers and SCIM tokens, API keys and service
  accounts, model policies and gateways, and the audit log with filters.
- **Sign-in** through the tenant's OIDC provider(s); the UI shows only what
  the signed-in principal may do.
- Command palette (⌘K), dark/light themes, responsive layout, accessible
  components.

### 14.1 The assistant

A chat panel (⌘J) on every page sets up anything Shoal offers (workflows,
agents, triggers, connections, campaigns, evals, gateways, runs, approvals)
and writes `script` step code so deterministic work runs as code instead of
through an agent. (Decided.)

- **Public API only.** `POST /v1/assistant/chat` runs a tool loop on the
  configured gateway and model (`SHOAL_ASSISTANT_GATEWAY`, `_MODEL`, `_EFFORT`,
  `_MAX_TURNS`; the tenant's `default` model policy applies). Its tools
  (`list_endpoints`, `describe_endpoint`, `api_get`, `apply_resources`,
  `api_call`) call the API in-process over ASGI with the caller's own
  credentials, through the full middleware stack. RBAC, auditing (source
  `assistant`, sharing the chat's request id) and federation forwarding apply
  unchanged, and the assistant can never do more than the user could.
- **Changes need approval.** Reads run immediately. `apply_resources` first
  dry-runs the YAML (validation errors go back to the model to fix), then
  returns as a pending action with the per-document plan. `api_call` (POST,
  PUT, PATCH, DELETE) is pending too. The UI shows each as a card to approve
  or decline, unless the user turns on auto-approve. Sign-in, webhook,
  SCIM and assistant endpoints are not reachable as tools.
- **Stateless server.** The client sends the history (Anthropic Messages
  shape) with each message and keeps it per tenant in the browser, so no
  table is needed. A forged history buys nothing: every action still runs
  with the caller's credentials.

### 14.2 Built-in documentation

The product ships its own documentation at `/docs` in the UI, so it always
matches the running version.

- **Source**: Markdown files in `docs/` (guides, concepts, integration and SDK
  guides, this architecture document), bundled into the frontend at build
  time and also published as a static site for the open-source project.
- **Sections**: *Getting started* (install, first workflow, first trigger),
  *Concepts* (tenants and namespaces, agents, workflows, steps, runs,
  triggers, campaigns, blueprints, identities), *Guides* (the GitHub uplift and
  Jira bugfix journeys, schedules, approvals, decisions with Jev, model
  policies, sandboxes, attribution and governance), *Administration* (OIDC,
  SCIM, roles, namespaces, API keys, secrets, audit), *Building integrations*
  (SDK, test kit, remote protocol), *Reference* (resource schemas generated
  from the pydantic models, step types, expression syntax, the OpenAPI
  reference rendered from `/v1/openapi.json`, CLI) and *Internals* (the
  architecture: executor, leases, waits, sandboxes, plugin system).
- **In the UI**: full-text search (client-side index), a table of contents,
  code samples with copy buttons, and contextual "Learn more" links from
  every screen and inspector field to the relevant page. Reference pages are
  generated from the same schemas the API validates against, so they cannot
  drift.

## 15. Observability and operations

- Structured JSON logs, OpenTelemetry traces (run → step → tool call),
  Prometheus metrics (`shoal_runs_total`, `shoal_step_duration_seconds`,
  `shoal_tokens_total`, `shoal_sandbox_seconds_total`).
- Deploy: `docker compose up` (API, worker, Postgres, UI) and a Helm chart.
- Config via env vars (`SHOAL_DATABASE_URL`, `SHOAL_SECRET_KEY`, …) with a
  pydantic-settings model.

---

## 16. Plugin authoring

Plugins are normal Python packages exposing entry points:

```toml
[project.entry-points."shoal.integrations"]
linear = "shoal_linear:LinearIntegration"

[project.entry-points."shoal.sandboxes"]
modal = "shoal_modal:ModalBackend"
```

Groups: `shoal.integrations`, `shoal.sandboxes`, `shoal.providers`,
`shoal.agent_runtimes`, `shoal.gateways`, `shoal.decision_providers`, `shoal.eval_sinks`, `shoal.skill_sources`,
`shoal.credentials`, `shoal.secrets`, `shoal.steps`, `shoal.orchestrators`.
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
7. API server: tenancy, namespaces, RBAC, OIDC, SCIM, API keys, all resources, SSE.
8. Kubernetes, Docker, E2B and SPACE sandbox backends.
9. React UI with the drag-and-drop workflow builder.
10. Built-in docs, journeys as examples + e2e tests; docker-compose, Helm.

## 18. Open questions

- ~~Product name~~ — decided: **Shoal**. The Python package, CLI, `SHOAL_*` settings and `shoal.dev/v1` apiVersion keep their `shoal` names until a later release.
