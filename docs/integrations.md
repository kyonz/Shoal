# Building integrations

Anyone can add an integration to Shoal. The built-in GitHub, Jira and webhook
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
  `raise_for_status`, which does this for 408/429/5xx). The executor retries
  those under the step's retry policy.
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
| `IntegrationContract` | Subclass as `TestXContract` to run the contract checks under pytest. |
| `check_remote_service(url, token)` | The contract checks for a running remote service, also `python -m shoal.sdk.testing.contract URL --token T`. |

The **contract** checks that keys and action names are valid tool names,
every action and resource has a description and object schemas, credentials
are `SecretStr` and do not leak into the catalogue, methods are async, the
integration can be described as a remote manifest, and (given a sample
delivery) that genuine webhooks verify while tampered or unsigned ones are
rejected and parsed event types are declared. The built-in integrations pass it.

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
