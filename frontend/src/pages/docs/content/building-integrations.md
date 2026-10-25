# Building integrations

Integrations connect Shoal to other systems. Each one can provide three capabilities:

- **Triggers** start work from external events (webhooks, polling, schedules).
- **Actions** do things in external systems. Every action is at once a workflow `action` step, an agent tool and an API operation (`POST /v1/namespaces/{ns}/connections/{name}/actions/{action}`).
- **Metadata** (resources) discovers and queries external objects, powering campaign target selection and pickers in the UI.

The platform is open: the built-in `github`, `jira` and `webhook` integrations use exactly the same public contract as community integrations, so they double as reference implementations.

## Python SDK

Write an integration as a normal Python package and register it with an entry point:

```toml
[project.entry-points."shoal.integrations"]
linear = "shoal_linear:LinearIntegration"
```

The SDK (`shoal.sdk`) re-exports the `Integration` base class, `@action` and `@resource` decorators, `IntegrationEvent`, `ConnectionContext` and helpers. Actions declare pydantic input and output models, which become the JSON Schemas the UI renders forms from, so a new integration needs no frontend changes.

The **test kit** (`shoal.sdk.testing`) provides an in-memory engine, a mock HTTP transport, webhook signing helpers and contract tests every integration should pass. `shoal integrations new linear` scaffolds a package with entry points, tests and CI.

## Remote integrations (any language)

Integrations can also run as separate HTTP services, written in any language. The service serves:

| Endpoint | Purpose |
|---|---|
| `GET /manifest` | Key, title, connection JSON schema, actions with input/output schemas, resources and events |
| `POST /actions/{name}` | Run an action |
| `POST /resources/{name}/query` | Query a metadata resource |
| `POST /webhooks/verify` | Verify a webhook signature |
| `POST /webhooks/parse` | Turn a webhook into events |

Each call receives the resolved connection config. A tenant registers the service with a `RemoteIntegration` connection (`url` and an auth token), and Shoal adapts it to the same interface, so its actions become steps and agent tools automatically.

## In the UI

Installed integrations (built-in, Python packages and remote) appear in the **Integrations** catalogue with their actions, events and resources. Create a **Connection** to configure credentials (secret fields reference namespace secrets by name), copy its webhook URL, and add **Triggers** with a filter expression and an input mapping. Use **Test** on a trigger to dry-run it against a sample event before enabling it.

See section 9 of the [architecture](/docs/architecture#9-integrations-framework) for the full contract.

## MCP servers

Any [Model Context Protocol](https://modelcontextprotocol.io) server that speaks Streamable HTTP can be a connection: pick **MCP server** under **New connection**, set its `url`, and put its bearer token in the `token` secret. Optionally list the tools you allow with globs in `tools`. The connection's **Actions** tab lists the server's tools. Give an agent every tool with `mcp:<connection>`, or a single one with `mcp:<connection>.<tool>`. The agent's **Tools** picker offers both. Workflows can call the tools as `action` steps too.

## Each person's own credential

A connection's **Whose credential** setting decides who the calls act as:

- **Shared**: one credential from the connection's secrets for every run.
- **Each person's own**: the credential of the person the run acts for. If they haven't connected, the step waits, the run viewer shows **Connect your account**, and the run carries on once they do.
- **Own, else shared**: theirs when they have one, otherwise the shared one.

People connect under **Connected accounts** (also in the account menu), either by pasting a token or, when the connection has OAuth settings, by signing in with the service. Values are encrypted and write-only. Admins can see which connections a person has connected on the user's page in **Admin console → Users**, and revoke them, but never read the values. Service accounts always use the shared credential.
