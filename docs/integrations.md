# Building integrations

Anyone can add an integration to Shoal. The built-in GitHub, Jira, Slack and webhook
integrations use exactly the contract described here, so they double as
reference implementations (`engine/shoal/integrations/`).

An integration contributes up to three things:

| Capability | Declared with | Becomes |
| --- | --- | --- |
| **Actions** | `@action` methods with pydantic input/output models | workflow `action` steps, agent tools (`<key>__<action>`), `POST …/connections/{name}/actions/{action}` |
| **Resources** | `@resource` methods returning a `Page` | campaign target selection, UI pickers, `POST …/resources/{name}/query` |
| **Events** | `events` plus `verify_webhook` / `parse_webhook` | triggers (`Trigger.spec.event`) and wake-ups for waiting runs (`wake_keys`) |

A tenant connects to an integration with a `Connection`: non-secret settings in
`config`, credentials referenced by name in `secrets`. The UI renders the
connection form from the integration's `connection_model` (fields typed
`SecretStr` are stored as secrets), so a new integration needs no frontend work.

There are two ways to ship one:

1. **A Python package** with an `shoal.integrations` entry point, installed next
   to the engine. Start from the template (below).
2. **A remote service** in any language that speaks the HTTP protocol at the
   end of this page. Python integrations can also be served this way with one
   command, so the same code can run in-process or out-of-process.

## Quick start

```bash
shoal integrations new linear --title Linear     # or: python -m shoal.sdk.scaffold linear
cd shoal-linear && uv sync && uv run pytest
```

The project is a working integration for a sample ticket tracker, with
actions, a paginated resource, signed webhooks, unit tests, an end-to-end
trigger test, the contract tests, CI and a Dockerfile. Replace the sample API
calls with the real service.

## The SDK (`shoal.sdk`)

```python
from shoal.sdk import BaseModel, ConnectionContext, EventDef, Field, Integration, IntegrationEvent, Page, SecretStr
from shoal.sdk import action, json_body, raise_for_status, resource, verify_hmac_sha256


class LinearConnection(BaseModel):
    api_key: SecretStr
    webhook_secret: SecretStr | None = None


class CreateIssue(BaseModel):
    team_id: str
    title: str = Field(description="Issue title.")


class LinearIntegration(Integration):
    key = "linear"
    title = "Linear"
    description = "Issues and projects in Linear."
    connection_model = LinearConnection
    events = [EventDef("Issue.*", "Issue created, updated or removed.")]

    @action(description="Create an issue.", input=CreateIssue)
    async def create_issue(self, ctx: ConnectionContext, params: CreateIssue) -> dict:
        async with ctx.http(base_url="https://api.linear.app") as client:
            response = await client.post("/graphql", json={...},
                                         headers={"Authorization": ctx.config.api_key.get_secret_value()})
        raise_for_status(response, "Linear")
        return response.json()["data"]

    async def verify_webhook(self, ctx, headers, body) -> bool:
        return verify_hmac_sha256(ctx.config.webhook_secret, body, headers.get("linear-signature"))

    async def parse_webhook(self, ctx, headers, body) -> list[IntegrationEvent]:
        payload = json_body(body)
        return [IntegrationEvent(type=f"{payload['type']}.{payload['action']}", data=payload,
                                 wake_keys=[f"linear:issue:{payload['data']['id']}"])]
```

Rules worth knowing:

- **Always use `ctx.http()`** for outbound calls. It applies the engine's
  timeouts and lets tests swap in a mock transport.
- **Raise `IntegrationError(retryable=True)`** for transient failures (or use
  `raise_for_status`, which does this for 408/425/429/5xx). Network errors from
  `ctx.http()` (DNS, refused connections, timeouts) become retryable
  `IntegrationError`s for you. The executor retries those under the step's
  retry policy.
- **Mark read-only actions `mutating=False`.** Mutating actions are audited
  when called through the API.
- **Reject unsigned webhooks** when no secret is configured, rather than
  accepting anyone who finds the URL.
- **Prefix wake keys with your integration's key** (`linear:issue:42`). Keys whose
  prefix names another installed integration (`github:...`) are dropped, so one
  connection's webhooks cannot hand forged events to runs waiting on another's.
- **Fill `IntegrationEvent.actor`** (`actor(email=..., linear=user_id)`) so
  runs started by the event are attributed to the person who caused it.
- Helpers: `verify_hmac_sha256`, `hmac_sha256`, `verify_token`,
  `encode_cursor`/`decode_cursor` for opaque pagination, `json_body`, `actor`.

Register the package:

```toml
[project.entry-points."shoal.integrations"]
linear = "shoal_linear:LinearIntegration"
```

## The test kit (`shoal.sdk.testing`)

```python
from shoal.sdk.testing import IntegrationContract, IntegrationHarness, MockHTTP, engine_harness, signed_webhook

api = MockHTTP(host="api.linear.app")
api.post("/graphql", json={"data": {"issueCreate": {"success": True}}})

harness = IntegrationHarness(LinearIntegration, config={"api_key": "k"}, http=api)
await harness.call("create_issue", {"team_id": "T", "title": "Bug"})
assert api.requests("POST", "/graphql")[0].json["variables"]["title"] == "Bug"
```

| Tool | Use it for |
| --- | --- |
| `MockHTTP` | Mock the provider API. Path templates (`/repos/{owner}/{repo}`, `{path:path}`), static JSON or handler functions, recorded calls, `assert_all_called()`. Unmocked calls fail the test. |
| `IntegrationHarness` | Call actions, queries (`query_all` follows cursors), `test_connection` and webhooks with no database. |
| `engine_harness(...)` | A real throwaway engine with your integration registered: create connections (`connect`), apply YAML resources (`apply`), deliver webhooks to the connection URL (`webhook`) and check that triggers fire and runs start. |
| `signed_webhook`, `github_webhook` | Build correctly signed deliveries. |
| `MockSlack`, `slack_request` | A fake Slack: Web API state (messages, updates, modals, users) and signed clicks, modal submissions, events and slash commands. |
| `IntegrationContract` | Subclass as `TestXContract` to run the contract checks under pytest. |
| `check_remote_service(url, token)` | The contract checks for a running remote service, also `shoal integrations check URL --token T` from the command line. |

The **contract** checks that keys and action names are valid tool names,
every action and resource has a description and object schemas, credentials
are `SecretStr` and do not leak into the catalogue, methods are async, the
integration can be described as a remote manifest, and (given a sample
delivery) that genuine webhooks verify while tampered or unsigned ones are
rejected and parsed event types are declared. The built-in integrations pass it.

## Approvals, buttons and forms (interactions)

An integration that can show people buttons can also resolve approval steps and
send runs data. Set `supports_approvals = True` and implement:

- `request_approval(ctx, request)`: post the request (`request.message`, `to`,
  `options` from the step's `with`, `run_url`, `deadline`) and return a JSON
  reference to what you posted;
- `approval_resolved(ctx, request, ref, outcome)`: show the outcome there. It is
  called whatever decided the step (your buttons, the UI, the API, a timeout);
- in `parse_webhook`, return an `IntegrationEvent` with an `Interaction`
  (`kind="approval"` with `run_id`, `step_id`, `approved`, `comment`, or
  `kind="signal"` with `run_id`, `signal`, `payload`) and `actor` hints.

Never decide in the integration: the engine resolves the actor to a Shoal user
(through `Identity` hints and email), checks `runs:approve` / `runs:signal` and
the step's `approvers`, and only then records the decision. Its outcome is in
`result["interactions"]` for `webhook_reply(ctx, headers, body, result)`, which
lets you answer the delivery yourself (`WebhookReply(status, body)`). Two more
optional hooks: `setup_manifest` (an app manifest shown on the connection, built
from `setup_features`: each a `SetupFeature` with the scopes and events it needs
and why; `features_used` recommends them from triggers and workflows and
`setup_create_url` links to a prefilled "create app" page) and
`install_fields` + `install_url` / `complete_install` (an OAuth app install
that stores tokens as the connection's secrets).

Integrations can also give triggers structured filters: `trigger_filter_model`
is a pydantic model for a trigger's `match:` (it becomes a form in the trigger
editor) and `trigger_matches(ctx, event, options)` returns `None` when the event
passes, or the reason it does not. Any trigger can also set
`rate_limit: {max, per, key}`; events over the limit are recorded as
`rate_limited` deliveries instead of starting runs.

## Slack

> The full Slack guide, with screenshots, is in [docs/slack](slack/README.md): [setup](slack/setup.md),
> [approvals](slack/approvals.md), [interactive messages](slack/interactive-messages.md),
> [message builder](slack/message-builder.md), [channel bots](slack/channel-bots.md),
> [events and slash commands](slack/events-and-commands.md) and the [reference](slack/reference.md).

Slack is built in. One Slack app per connection. Creating a Slack connection in
the UI opens a **guided setup** (Connections → the connection → Guided setup)
that walks through the steps below: pick what the app does (messages and
approvals, @mentions, channel bots, DMs, reactions, a slash command, files) and
it lists the OAuth scopes and event subscriptions each feature needs and why,
recommends the ones the connection's triggers and workflows use, and generates
the manifest (YAML or JSON, or **Create app in Slack**, which opens Slack with the
manifest filled in). The choice is saved as the connection's `features`
(`app_name` and `slash_command` name the app and its command); the manifest and
Add to Slack ask only for those scopes, and the connection test reports any the
app was not granted (`missing_scopes`). The same data is at
`GET /v1/namespaces/{ns}/connections/{name}/setup` (`?features=a,b` previews a
different choice).

1. Create the connection (`integration: slack`), open it and copy the **app
   manifest** (Event Subscriptions, Interactivity and the slash command already
   point at the connection's URL). Create the app from it at api.slack.com/apps.
2. Store the app's **signing secret** on the connection (every request from
   Slack is verified; unsigned or older than five minutes is rejected).
3. Install: **Add to Slack** (set `client_id` and the `client_secret` secret) or
   paste a bot token (`bot_token`). Invite the app to its channels.
4. Link people: Slack users map to Shoal users by `Identity.slack_id`, or by
   email when the app can read it (`users:read.email`). Unknown people can
   click, but nothing happens except a private note telling them why.

```yaml
kind: Connection
metadata: {name: slack}
spec:
  integration: slack
  config: {default_channel: "#deploys", client_id: "123.456"}
  secrets: {signing_secret: slack-signing-secret, client_secret: slack-client-secret, bot_token: slack-bot-token}
```

**Approvals.** The message has Approve / Reject (and Open in Shoal) buttons and
is updated with the outcome. `with: {ask_comment: true}` asks for a comment in a
modal; `title`, `approve_label`, `reject_label` and `thread_ts` are optional.

```yaml
- id: go
  type: approval
  message: "Deploy *{{ inputs.service }}* {{ inputs.version }} to prod?"
  approvers: ["group:release-managers"]
  max_wait: 4h
  notify:
    - integration: slack
      to: "#deploys"            # a channel id or #name, a user id, or an email (DM)
```

**Actions:** `slack.post_message` (text, Block Kit `blocks`, `thread_ts`, and
`buttons` that send this run a signal), `slack.update_message`, `slack.reply`
(answer in a message's thread), `slack.post_ephemeral`, `slack.add_reaction`,
`slack.request_input` (a button opening a form; answers arrive as a signal
`{values, user}`), `slack.get_conversation` (a thread or a channel's latest
messages plus a `transcript` for an agent) and `slack.lookup_user`. Resource:
`channel`.

```yaml
- id: ask
  type: action
  action: slack.request_input
  with:
    text: Which version should we ship?
    signal: release
    fields:
      - {name: version, label: Version}
      - {name: env, label: Environment, type: select, options: [staging, prod]}
- id: answer
  type: wait
  signal: {name: release}       # steps.answer.output.payload.values.version
```

**Custom messages with hooks.** Any Block Kit message can talk back to the run
that posted it. Add a `shoal:` key to a button, select, date picker, checkbox
group or overflow menu (Shoal strips it and wires the element up before
posting):

- `shoal: {signal: choice}` sends this run the signal `choice` with
  `{action_id, block_id, value, label, user, channel, message_ts}`; a `wait`
  step with `signal: {name: choice}` resumes and later steps branch on
  `steps.<wait>.output.payload.value`;
- `shoal: {signal: choice, modal: <view>}` (buttons only) opens that Block Kit
  modal first and sends its answers as `values` (keyed by each input's
  `action_id`);
- `shoal: {event: true}` raises a `block_actions` event instead, so a trigger can
  start a new run from the click;
- `shoal: {signal: choice, after: keep}` leaves the controls in place; by
  default they are replaced with
  "*who* chose *what*" once someone clicks (`after_click` on the action sets it
  for the whole message).

A message-level `signal:` binds every interactive element that has no `shoal:`
key of its own. Each click is still checked: the person must map to a Shoal
user with `runs:signal` in the run's namespace. Blocks can be a list or a JSON
string, and expressions like `{{ inputs.version }}` are filled in first. The
**message builder** (Connections → a Slack connection → Message builder) lays
out blocks with a live, clickable preview, lists the hooks, and generates the
`post_message` and `wait` steps; `examples/blueprints/slack-release-check.yaml`
is a worked example.

```yaml
- id: post
  type: action
  action: slack.post_message
  with:
    channel: "#releases"
    text: "{{ inputs.service }} is ready to ship"
    blocks:
      - {type: section, text: {type: mrkdwn, text: "*{{ inputs.service }}* passed staging."}}
      - type: actions
        elements:
          - {type: button, text: {type: plain_text, text: Go}, style: primary, value: go, shoal: {signal: release}}
          - type: button
            text: {type: plain_text, text: Hold}
            style: danger
            value: hold
            shoal:
              signal: release
              modal:
                title: {type: plain_text, text: Hold the release}
                blocks:
                  - type: input
                    label: {type: plain_text, text: "Why?"}
                    element: {type: plain_text_input, action_id: why}
- id: decision
  type: wait
  signal: {name: release}
- id: ship
  type: action
  if: "{{ steps.decision.output.payload.value == 'go' }}"
  action: slack.post_message
  with: {channel: "#releases", text: "Shipping"}
```

**Channel bots.** A trigger on the `message` event wires a channel to a
workflow, so the app answers whatever it is invited to. Its `match:` filters
(all of which must hold) choose the messages: `channels`, `mentioned` (the bot
was @-mentioned), `direct_messages`, `contains` (any), `contains_all`,
`excludes`, `regex` (named groups land in `event.match`), `case_sensitive`,
`users`, `exclude_users`, `groups` (Shoal groups or identities), `threads`
(`any`, `top_level`, `replies`), `include_bots`, `has_files` and `reactions`.
Message events carry `bot_user_id`, `command_text` (the text without the
mention), `mentioned`, `is_dm`, `is_reply`, `reply_ts` (the thread to answer in),
`from_bot`, `has_files` and `files`. Use `rate_limit` to keep a busy channel in
check and `dedupe_key` against Slack's retries. The `slack-bot` blueprint reacts
with :eyes:, reads the thread with `get_conversation`, answers with `reply` and
asks whether the answer helped; the connection page lists every bot wired to it.

```yaml
kind: Trigger
metadata: {name: help-bot}
spec:
  connection: slack
  event: message
  workflow: slack-answer
  match: {channels: ["#help"], mentioned: true}
  rate_limit: {max: 20, per: 1m, key: "{{ event.user }}"}
  dedupe_key: "{{ event.event_id }}"
  inputs: {channel: "{{ event.channel }}", ts: "{{ event.reply_ts }}", question: "{{ event.command_text }}"}
```

**Events** for triggers: `app_mention` (`event.command_text` is the text after
the mention), `message` (housekeeping subtypes and the app's own messages are
skipped; other bots' messages only pass `include_bots`),
`reaction_added`, `slash_command` (`event.command`, `event.text`, `event.args`;
the person gets a private answer naming the runs started), `block_actions` and
`view_submission` for your own Block Kit, `shortcut` and `message_action`.
Wake keys: `slack:thread:<channel>:<ts>` (replies in a thread, also
`post_message`'s `thread_key`), `slack:channel:<channel>`,
`slack:message:<channel>:<ts>` (reactions, button clicks) and
`slack:action:<action_id>`. Slack retries an event it did not get an answer to
in three seconds; set `dedupe_key: "{{ event.event_id }}"` on triggers that must
not fire twice.

## Per-user credentials

Any connection can act with each person's own credential instead of one shared
one: set `credentials: per_user` (or `per_user_or_shared`) on the connection and,
optionally, `user_fields` (which connection fields people supply; default: the
fields in `secrets`). Integrations need no code for this: `ctx.config` simply
holds the person's values, and `ctx.acting_as` names whose they are. Add `oauth:`
(authorize URL, token URL, scopes, client id, client secret secret) to let people
connect with OAuth instead of pasting a token. See architecture §11.4.

## MCP servers

The built-in `mcp` integration turns any Model Context Protocol server into a
connection, with its tools as actions, so most "integrations" need no code at all:

```yaml
kind: Connection
metadata: {name: linear}
spec:
  integration: mcp
  config:
    url: https://mcp.linear.app/mcp
    tools: ["list_*", "create_issue"]     # optional allow-list (globs)
  secrets: {token: linear-team-token}     # sent as a bearer token
  credentials: per_user_or_shared         # or each person's own token / OAuth
```

Workflows call `action: mcp.create_issue` with `connection: linear`; agents list
`tools: [mcp:linear]` and see `linear__create_issue`.

## Remote integrations (any language)

A remote integration is an HTTP service speaking protocol
`shoal.integration/v1`. [examples/remote-integration-node](../examples/remote-integration-node/server.mjs)
is a complete one in dependency-free Node.js; any Python integration can be
served with:

```bash
SHOAL_INTEGRATION_TOKEN=change-me python -m shoal.sdk.server shoal_linear:LinearIntegration --port 8080
```

### Connecting one

**Per tenant:** create a connection with `integration: remote`. `url` and
`token` locate the service; every other field (config or secret) is passed to
the service as its connection config. Actions are called as
`remote.<action>` and agents get `remote__<action>` tools.

```yaml
apiVersion: shoal.dev/v1
kind: Connection
metadata: { name: incidents }
spec:
  integration: remote
  config: { url: "https://incidents-integration.internal" }
  secrets: { token: incidents-service-token, api_key: incidents-api-key }
```

**Operator-wide:** `await register_remote_integration(engine.registry, url, token)`
fetches the manifest and registers the service under its own key, so it shows
up in the catalogue like a built-in (`incidents.create_incident`) and tenants
connect to it with an ordinary connection.

The URL must be `https` (plain `http` is allowed for loopback, or with
`allow_insecure: true`) because the service receives the connection's secrets.

Connection URLs are set by tenants, so every integration call (remote, MCP,
GitHub Enterprise and Jira base URLs included) refuses hosts that resolve into
`SHOAL_INTEGRATION_BLOCKED_NETWORKS`: by default the link-local and cloud
metadata ranges. A multi-tenant deployment can add its private ranges, e.g.
`SHOAL_INTEGRATION_BLOCKED_NETWORKS='["169.254.0.0/16", "fe80::/10", "10.0.0.0/8"]'`.

### Protocol

Every request carries `Authorization: Bearer <token>` and
`X-Shoal-Protocol: shoal.integration/v1`. A service must reject requests with a
missing or wrong token with `401`.

| Endpoint | Request body | Response |
| --- | --- | --- |
| `GET /manifest` | | the manifest (below) |
| `POST /test` (if `manifest.test`) | `connection`, `context` | `{"ok": true, ...}` |
| `POST /actions/{name}` | `connection`, `context`, `params` | `{"output": {...}}` |
| `POST /resources/{name}/query` | `connection`, `context`, `filter`, `cursor`, `limit` | `{"items": [...], "next_cursor": "..." \| null}` |
| `POST /webhooks/verify` (if `manifest.webhooks`) | `connection`, `context`, `headers` (lower-cased), `body_b64` | `{"valid": true \| false}` |
| `POST /webhooks/parse` (if `manifest.webhooks`) | same | `{"events": [{"type", "id", "data", "wake_keys", "actor"}]}` |

`connection` is the resolved connection config with secrets revealed;
`context` is `{"tenant_id", "connection", "run_id", "initiator"}`.

Errors are any non-2xx status with
`{"error": {"message": "...", "retryable": false}}`. Unknown actions and
resources return `404`; invalid input returns `422`. When `retryable` is
omitted, `408`, `425`, `429` and `5xx` are treated as retryable.

The manifest:

```json
{
  "protocol": "shoal.integration/v1",
  "key": "incidents",
  "title": "Incidents",
  "description": "Open and resolve incidents.",
  "version": "1.0.0",
  "connection_schema": {"type": "object", "properties": {"api_key": {"type": "string"}}, "required": ["api_key"]},
  "secret_fields": ["api_key"],
  "actions": [{"name": "create_incident", "description": "Open an incident.", "mutating": true,
               "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
               "output_schema": null}],
  "resources": [{"name": "incident", "description": "Incidents.", "filter_schema": {"type": "object"}, "key_field": "id"}],
  "events": [{"name": "incident.opened", "description": "An incident was opened.", "example": {}}],
  "webhooks": true,
  "test": true
}
```

Shoal caches a manifest for 60 seconds per connection and refreshes it on
"Test connection". Action input is validated by the service; Shoal checks only
the schema's `required` keys before calling, for a clearer error.
