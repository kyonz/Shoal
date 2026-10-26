# Remote integration in Node.js

A complete Shoal integration in ~180 lines of dependency-free Node.js, to show
that integrations can be written in any language. It implements the
`shoal.integration/v1` protocol (see [docs/integrations.md](../../docs/integrations.md)).

```bash
SHOAL_INTEGRATION_TOKEN=change-me node server.mjs
# check it against the contract every integration must pass
shoal integrations check http://127.0.0.1:8080 --token change-me
```

Connect it to a tenant:

```yaml
apiVersion: shoal.dev/v1
kind: Connection
metadata: { name: incidents }
spec:
  integration: remote
  config: { url: "https://incidents-integration.internal" }
  secrets: { token: incidents-service-token, api_key: incidents-api-key, webhook_secret: incidents-webhook-secret }
```

Workflows then call `remote.create_incident` with `connection: incidents`, and
agents get `remote__create_incident` as a tool.
