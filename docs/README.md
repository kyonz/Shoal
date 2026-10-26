# Shoal documentation

Shoal runs AI workflows for teams: agents that change code, triage tickets and answer in Slack, started by events,
waiting for people when they need to, and governed by your organisation's access rules. These guides cover every
feature, from writing a first workflow to running Shoal for many organisations. The same pages are built into the web UI
under **Docs**.

## Build

- [Workflows](workflows/README.md): what a workflow is, and how to write one.
  - [Steps](workflows/steps.md), [expressions](workflows/expressions.md), [script steps](workflows/script-steps.md)
  - [The visual builder](workflows/builder.md) and [versions](workflows/versions.md)
- [Agents](agents/README.md): models, prompts, tools, sub-agents, runtimes and limits.
  - [Gateways and model policies](agents/gateways.md): where model calls go, and which models are allowed.
  - [Decisions](agents/decisions.md): `decide` and `grade` steps.
  - [Sandboxes](agents/sandboxes.md): where agent and script steps run, and how.
- [Skills](skills.md): `SKILL.md` skills, and importing them from marketplaces.
- [Evals](evals/README.md): score workflows against datasets and gate releases on the results.

## Run

- [Runs](runs/README.md): following a run, retrying and cancelling.
  - [Triggers](runs/triggers.md): start runs from webhooks, events and schedules.
  - [Approvals](runs/approvals.md): pause for a person's decision.
  - [Signals and waiting](runs/signals.md): send data into a waiting run, or wait for an event.
  - [Campaigns](runs/campaigns.md): run one workflow over many targets, and [target lists](runs/campaigns.md#target-lists) to share them.
- [The assistant](assistant/README.md): set things up and explain failures from any page.

## Connect

- [Connections and secrets](connections/README.md): connect services, store credentials, and act as each person.
- [GitHub](github/README.md): [setup](github/setup.md), [triggers](github/triggers.md),
  [attribution and merge governance](github/attribution.md), [reference](github/reference.md).
- [Jira](jira/README.md): [setup](jira/setup.md), [triggers](jira/triggers.md), [reference](jira/reference.md).
- [Slack](slack/README.md): setup, approvals, interactive messages, channel bots and slash commands.
- [Cloud identities](cloud-identities.md): short-lived AWS credentials for steps.
- [Building integrations](integrations.md): your own integrations, in Python or any language.

## Share

- [Blueprints](blueprints/README.md): package a workflow so other teams can adopt it.
- [Bundled blueprints](blueprints/bundled.md): the ones every tenant starts with.

## Administer

- [The admin console](administration/README.md): shared settings and the audit log.
- [Access](administration/access.md): users, groups, roles, sign-in, SCIM, API keys and identities.
- [Tenants and the instance console](administration/tenants.md): many organisations on one deployment.
- [Federation](administration/federation.md): tenants on their own deployments, managed from one hub.

## Operate

- [Deploying](../deploy/README.md): the container image, docker compose and the Helm chart.
- [Operating Shoal](operations/README.md): workers, leases, scaling, metrics and settings.
- [Running at high throughput](scaling.md): connection pools, agent worker pools, quotas and overload behaviour.
- [Running on Temporal](temporal.md): use a Temporal cluster you already run.
- [Dispatch queues](dispatch-queues.md): let workers hear about due runs from Amazon SQS instead of polling Postgres.
- [The CLI](cli.md) and the [Terraform provider](../terraform-provider/docs/index.md).
- [Plugins](plugins.md): add model providers, sandbox backends, secret backends and more.

## Reference

- [Architecture](architecture.md): concepts, the runtime, plugin contracts and the API surface.
- The HTTP API is described at `/v1/openapi.json`, with interactive docs at `/v1/docs` on every server.
- [Brand](brand.md): the logo and colours.
