# Building integrations

Integrations connect Shoal to other systems. Each one can provide three capabilities:

- **Triggers** start work from external events (webhooks, polling, schedules).
- **Actions** do things in external systems. Every action is at once a workflow `action` step, an agent tool and an API operation (`POST /v1/namespaces/{ns}/connections/{name}/actions/{action}`).
- **Metadata** (resources) discovers and queries external objects, powering campaign target selection and pickers in the UI.

The platform is open: the built-in `github`, `jira` and `webhook` integrations use exactly the same public contract as community integrations, so they double as reference implementations.

## Python SDK

Write an integration as a normal Python package and register it with an entry point:

```toml
[project.entry-points."awm.integrations"]
linear = "awm_linear:LinearIntegration"
```

The SDK (`awm.sdk`) re-exports the `Integration` base class, `@action` and `@resource` decorators, `IntegrationEvent`, `ConnectionContext` and helpers. Actions declare pydantic input and output models, which become the JSON Schemas the UI renders forms from, so a new integration needs no frontend changes.

The **test kit** (`awm.sdk.testing`) provides an in-memory engine, a mock HTTP transport, webhook signing helpers and contract tests every integration should pass. `awm integrations new linear` scaffolds a package with entry points, tests and CI.

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
