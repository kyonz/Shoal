# Claude Code agents

The `claude-code` agent runtime runs [Claude Code](https://code.claude.com) headless
(`claude -p`) inside the step's sandbox. Claude Code brings its own tools, file editing and
agent loop; Shoal supplies the prompt, the sandbox, skills, credentials and the step's output
schema, and streams what Claude Code does into the run as it happens.

Like every model route in Shoal, Claude Code signs in through a central **Gateway** managed by
admins. A `claude-code` gateway has two modes:

| Mode | Who pays | Runs it serves |
|---|---|---|
| `api_key` (default) | The gateway's Claude Console API key (or any Anthropic-compatible proxy) | Every run, including schedules, triggers and service accounts |
| `subscription` | The Claude plan (Pro, Max, Team, Enterprise) of **the person who started the run** | Only runs started by a person who has linked their own plan |

## Read this before using subscription mode

Anthropic's [legal and compliance page for Claude Code](https://code.claude.com/docs/en/legal-and-compliance)
(checked October 2026) says:

- OAuth sign-in is for purchasers of Claude subscription plans, for ordinary use of Claude Code.
- Developers may not route requests through Free, Pro or Max plan credentials **on behalf of
  their users**. Anyone running Claude Code in hosted infrastructure must have each end user
  authenticate with **their own** API key, subscription or cloud credential.
- An end user may sign in to the **unmodified** Claude Code binary with their own subscription,
  including where a platform hosts Claude Code.
- Advertised Pro and Max usage limits assume ordinary, individual use. Anthropic may enforce
  these rules without notice.

Shoal's subscription mode is built around that:

- There is no shared subscription token. Each person links their own plan, and it is used only
  for runs that person starts. Someone else's run waits for *them* to link theirs.
- Runs started by service accounts, or by schedules and triggers that do not act for a person,
  fail in subscription mode. Use an `api_key` gateway for unattended work.
- Shoal runs the official, unmodified CLI. Sign-in happens on Anthropic's own pages through
  Claude Code's own `claude setup-token` flow; Shoal never sees a password.
- The resulting token is stored encrypted as that person's credential, is write-only (nobody,
  including admins, can read it back) and can be revoked by the person or an admin.

The admin turns the mode on by setting `acknowledge_subscription_terms: true`. If you run Shoal
as a service for other people or organisations, use API keys: having your users bring a
subscription to a product you operate may not count as ordinary individual use, and the
guidance has changed several times in 2026. Ask Anthropic if you are unsure. This document is
not legal advice.

## Set up a gateway

API key mode (keys always come from secrets in the `shared` namespace):

```yaml
apiVersion: shoal.dev/v1
kind: Gateway
metadata: {name: claude-code}
spec:
  type: claude-code
  api_key_secret: anthropic-key        # becomes ANTHROPIC_API_KEY
  # base_url: https://litellm.internal  # becomes ANTHROPIC_BASE_URL
  # headers / secret_headers            # become ANTHROPIC_CUSTOM_HEADERS
```

Subscription mode:

```yaml
apiVersion: shoal.dev/v1
kind: Gateway
metadata: {name: claude-max}
spec:
  type: claude-code
  description: Claude Code on your own Claude plan
  config:
    mode: subscription
    acknowledge_subscription_terms: true
```

In the UI: **Admin → Gateways → New**, pick type `claude-code`, and choose who pays in the
**Claude Code sign-in** section.

## Link your subscription

Open **Settings → Connected accounts**. Each subscription gateway has a **Link my subscription**
button with two ways in:

1. **Sign in with Claude.** Shoal starts `claude setup-token` on the API server and shows you
   Anthropic's sign-in link. Approve access on claude.ai, paste the code it shows back into
   Shoal, and Shoal stores the one-year token the CLI prints. The server needs the `claude` CLI
   on its PATH (`SHOAL_CLAUDE_CODE_CLI` points elsewhere); build the image with
   `--build-arg CLAUDE_CODE=1` to include it. Sign-ins are held in memory for ten minutes on the replica that started them, so with
   several API replicas use sticky sessions, or the second option.
2. **Paste a token.** Run `claude setup-token` on your own machine and paste what it prints.

A run that reaches a Claude Code step before you have linked waits (`credential.required` in
the run, with a link to this page) and carries on by itself once you link.

The same endpoints are available to scripts: `GET /v1/me/claude-code`,
`PUT /v1/me/claude-code/{gateway}/token`, `POST /v1/me/claude-code/{gateway}/login` and
`POST /v1/me/claude-code/{gateway}/login/{login_id}` with `{"code": "..."}`, and
`DELETE /v1/me/claude-code/{gateway}`. Admins see who has linked what under
`GET /v1/users/{username}/credentials` and revoke it there.

## Use it as a model gateway

A `claude-code` gateway also works wherever Shoal calls a model: the in-app assistant
(pick the gateway in **Admin → Assistant**, or set `SHOAL_ASSISTANT_GATEWAY=claude-max`), `tool-loop` agents (`provider: claude-max`) and model
policy candidates. Each model call runs the CLI once on the Shoal server (API and workers need
`claude` on PATH, e.g. the image built with `CLAUDE_CODE=1`), with Claude Code's own tools, MCP
servers, skills and settings turned off. The conversation and the caller's tool definitions go
into the prompt, and the next turn (text and tool calls) comes back through `--json-schema`, so
Shoal still runs the tools itself.

Who it signs in as follows the same rule as agents:

- In subscription mode, the assistant uses the plan of the person chatting, and a `tool-loop`
  agent uses the plan of the person who started the run (the step waits until they link one).
  In a model policy, a candidate whose person has not linked a plan falls back to the next one.
- Calls that act for nobody (service accounts, decisions, LLM graders) need an `api_key`
  gateway.

It is slower than calling the API directly (a CLI start per turn and the whole transcript sent
each time) and ignores sampling settings such as temperature and effort. Prompt caching is up
to Claude Code.

## Write an agent

```yaml
apiVersion: shoal.dev/v1
kind: Agent
metadata: {name: fixer}
spec:
  runtime: claude-code
  provider: claude-max            # the gateway
  model: claude-opus-5-5          # or an alias such as opus or sonnet
  instructions: Fix the failing test with the smallest change.   # --append-system-prompt
  limits: {max_turns: 40, timeout: 30m}
  runtime_config:
    allowed_tools: ["Bash(git *)", "Bash(npm test*)", "Edit", "Read"]
    # disallowed_tools: ["WebFetch"]
    # permission_mode: bypassPermissions   (default: the sandbox is the boundary)
    # max_budget_usd: 5                    (API key mode)
    # fallback_model: sonnet
    # install: false                       (the image already has claude)
    # setup: npm ci
    # extra_args: ["--add-dir", "/data"]
```

Use it from an `agent` step as usual. The step's `outputs` are passed to Claude Code as
`--json-schema`, so its structured output becomes the step's outputs (plus `summary`).

What happens in the sandbox:

1. Skills are written to `.shoal/skills/` and listed in the prompt.
2. If `claude` is not on PATH, Anthropic's installer runs (`runtime_config.install`; set it to
   `false` for images that ship the CLI, or give your own command).
3. `claude -p --output-format stream-json --verbose --no-session-persistence ...` starts in the
   background, with the gateway's credential in its environment (never on a command line).
4. Shoal tails the stream every `poll_interval` seconds (default 1) and adds each message to the
   run as `agent.claude_code.init`, `agent.claude_code.message` (text and tool calls),
   `agent.claude_code.tool_results` and `agent.claude_code.result`. Secrets are redacted.
5. When the CLI exits, the result's structured output is validated against the step's outputs.

Usage reports tokens and turns. In API key mode the CLI's `total_cost_usd` counts toward run
budgets; on a subscription the plan pays, so no cost is recorded.

The generic `cli` runtime still exists for other coding-agent CLIs (Codex, Aider, ...).
