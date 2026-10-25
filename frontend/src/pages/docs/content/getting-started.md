# Getting started

This guide takes you from signing in to watching your first workflow run live. It takes about five minutes.

## Sign in

Open Shoal and enter your organisation's short name (for example `acme`). If your administrator has set up single sign-on, you will see a **Continue with …** button for each OpenID Connect provider (Okta, Entra ID, Google, Keycloak and so on). Shoal uses the authorization-code flow with PKCE and keeps you signed in with a short-lived session cookie.

No single sign-on yet? Choose **Use an API key instead** and paste a key (`shoal_<prefix>_<secret>`). When a tenant is created, the server returns a bootstrap key for its `admin` service account; use it once to configure sign-in under **Admin console › Sign-in & SCIM**. The system admin token also works here; enter the tenant's short name alongside it.

> Keys pasted on the sign-in page are stored only in this browser. Sign out from the account menu to remove them.

## Pick a namespace

Everything you build lives in a **namespace**: a team's space inside your organisation. Agents, workflows, connections, secrets, triggers, campaigns and runs all belong to exactly one namespace, and every organisation starts with `default`.

Use the switcher at the top left to change namespace. Shoal keeps you on the same section (Workflows, Runs, …) when you switch. Resources published in the tenant-wide `shared` namespace (connections, gateways, skills) can be used from any namespace without exposing their secrets.

What you can see and change depends on your role in each namespace. Shoal hides actions you are not allowed to take, using the same permission check as the API (`GET /v1/me/permissions`).

## Build your first workflow

1. Go to **Workflows** and choose **New workflow**.
2. Drag a **Set** step from the palette onto the canvas and give it a value, for example `message: "Hello {{ inputs.name }}"`.
3. Open **Workflow settings** (click an empty part of the canvas) and add an input called `name` of type `string`.
4. Drag a **Script** step below it and connect the bottom handle of the first step to the top handle of the second. The connection means "run after" (the step's `needs`).
5. In the script, type `echo "{{ steps.` and pick the upstream output from the autocompletion list.
6. Press <kbd>⌘S</kbd> (or <kbd>Ctrl+S</kbd>) to save. Shoal validates the workflow against the server before it saves and shows any problems on the affected steps.

Prefer text? Switch to the **YAML** view at any time; both views edit the same document. See [Using the workflow builder](/docs/guides/workflow-builder) for the details.

## Run it

Choose **Run**. Shoal generates a form from the workflow's inputs; fill it in and start the run. You are taken straight to the run viewer.

You can also start runs from the API:

```bash
curl -X POST "$SHOAL_URL/v1/namespaces/default/workflows/hello/runs" \
  -H "Authorization: Bearer $SHOAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"name": "Ana"}}'
```

## Watch it live

The run viewer shows the same graph as the builder, coloured by step status, and updates live over server-sent events (`GET /v1/runs/{id}/events`). Select a step to see its output, errors, attempts, usage and, for agent steps, the full transcript of model turns and tool calls. Approval steps show **Approve** and **Reject** buttons to anyone with the `runs:approve` permission. Read more in [Approvals and live runs](/docs/guides/live-runs).

## Next steps

- Connect GitHub or Jira under **Integrations** and add a **Trigger** so runs start on their own.
- Fan a workflow out over many targets with a **Campaign**.
- Read the [architecture](/docs/architecture) for how the durable runtime works.
