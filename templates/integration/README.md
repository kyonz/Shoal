# shoal-starter

A Starter integration for [Shoal](https://github.com/kyonz/ai-workflow-manager).

| Capability | What it gives you |
| --- | --- |
| Actions | `starter.create_ticket`, `starter.add_comment`, `starter.get_ticket` as workflow steps and agent tools |
| Resources | `ticket` for campaign targets and UI pickers |
| Events | `ticket.created`, `ticket.updated` webhooks that fire triggers and wake waiting runs |

## Develop

```bash
uv sync
uv run pytest
```

`tests/test_contract.py` runs the Shoal integration contract (naming, schemas,
secret handling, webhook signing). Keep it passing; the catalogue relies on it.

## Install into Shoal

Install the package next to the engine. The `shoal.integrations` entry point in
`pyproject.toml` registers it, and `starter` appears in **Integrations** with a
connection form generated from `StarterConnection`.

```bash
pip install shoal-starter
```

## Or run it as a remote service

Any integration can run out of process and be connected over HTTP:

```bash
SHOAL_INTEGRATION_TOKEN=change-me python -m shoal.sdk.server shoal_starter:StarterIntegration --host 0.0.0.0 --port 8080
```

Then create a `remote` connection in Shoal with `url` set to the service and
`token` to the same value (or have an operator register it under its own key).
The `Dockerfile` builds that service.

```yaml
apiVersion: shoal.dev/v1
kind: Connection
metadata: { name: my-starter }
spec:
  integration: remote
  config: { url: "https://starter-integration.internal", base_url: "https://api.starter.example" }
  secrets: { token: starter-service-token, api_token: starter-api-token, webhook_secret: starter-webhook-secret }
```
