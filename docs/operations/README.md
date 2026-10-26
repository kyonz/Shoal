# Operating Shoal

Shoal is an API server, any number of workers, and a database. This page covers how those pieces share the work, how to
scale them, what to watch, and the settings operators change most. To install it, see the
[deploy guide](../../deploy/README.md) (container image, docker compose and the Helm chart).

## The pieces

| Process | Command | Does |
| --- | --- | --- |
| **API** | `shoal serve --no-worker` | Serves `/v1`, webhooks, A2A, sign-in and the UI. Stateless: run as many replicas as you like behind a load balancer. |
| **Worker** | `shoal worker` | Claims runs and advances them step by step, creates and drives [sandboxes](../agents/sandboxes.md#how-a-sandbox-runs), and calls models and integrations. Stateless between steps. |
| **Database** | Postgres (`SHOAL_DATABASE_URL`) | Holds everything: resources, runs and their checkpoints, events, secrets (with the `db` backend), the audit log. SQLite works for development. |

`shoal serve` on its own runs the API with an embedded worker, which is fine for development and small installs. The
Helm chart runs two API replicas and two workers by default, each with a PodDisruptionBudget and an optional HPA.

## How workers share the work

There is no queue to run besides the database. Each worker loops:

1. **Claim** runs that are due (new, woken by an event or signal, or whose wait or retry timer has passed), up to its
   free capacity (`SHOAL_WORKER_CONCURRENCY`, default 16), taking turns across tenants so one tenant's backlog doesn't
   delay another's. Claiming takes a **lease** on the run (`SHOAL_LEASE_SECONDS`, default 60), so no other worker
   touches it.
2. **Advance** each run: run its ready steps, checkpoint after each one, and renew the lease every third of its length
   while it works.
3. When a run reaches a wait (an approval, a signal, `max_wait: 7d`), **release** it. A waiting run holds no worker and
   no memory; a webhook, signal or timer makes it due again.
4. With nothing to claim, sleep `SHOAL_POLL_INTERVAL_SECONDS` (default 1) and look again.

Every worker also runs the **scheduler** every five seconds unless started with `--no-scheduler`: it fires scheduled
triggers, ticks campaigns and evals, and applies audit retention. Running it on several workers is safe.

### When a worker dies

Its leases stop being renewed and expire within `SHOAL_LEASE_SECONDS`. Another worker claims each run, finds the step that
was `running`, and runs that attempt again from the last checkpoint. A Kubernetes sandbox that is still running is
reattached, so its workspace survives. Make steps safe to repeat: a script or agent step should check whether something already
exists before it creates it. See
[Liveness and failures](../agents/sandboxes.md#liveness-and-failures-kubernetes).

If the run itself is what kills its worker (it runs the process out of memory, say), every worker that claims it dies the
same way. Shoal counts the passes in a row that ended with the worker gone, and after `SHOAL_MAX_LOST_LEASES` (default 5)
marks the run failed, with that reason, instead of handing it to the next worker. A pass that finishes resets the count, and
a worker that shuts down cleanly releases its leases, so restarts and deploys do not count.

A lower lease means faster recovery but more database writes. The default suits most installs.

## Scaling

- **More runs at once:** add workers, or raise `SHOAL_WORKER_CONCURRENCY` on each. Most of an agent run's time is spent
  waiting on models, sandboxes and other services, so a worker can carry many of them; scale on CPU, and watch the
  database's connection count.
- **Agent lanes:** runs with an `agent`, `decide` or `grade` step go to the agent lane, the rest to the standard lane.
  Run a separate high-concurrency agent pool (`SHOAL_WORKER_LANES='["agent"]'`, or `worker.agentPool.enabled` in
  Helm) so short workflows never queue behind long agent runs. See
  [Workers and agent lanes](../scaling.md#workers-and-agent-lanes).
- **More API traffic:** add API replicas, or `shoal serve --processes N --no-worker`.
- **Long-running work** costs nothing while it waits. A run that waits a week for a review holds no worker; its sandbox
  can be suspended with `suspend_on_wait` where the backend supports it.
- **Overload and quotas:** a busy API process answers `503` with `Retry-After` rather than queueing everyone, and a
  tenant over its request rate or pending-run quota gets `429`. The system admin can set quotas per tenant. See
  [How Shoal behaves under overload](../scaling.md#how-shoal-behaves-under-overload) and
  [Tenant quotas](../scaling.md#tenant-quotas).
- **Keeping bursts in check:** a trigger's `rate_limit` and `overlap`, and a campaign's `concurrency`, stop one busy
  workflow from filling every worker.
- **Database:** connection pools, PgBouncer and read replicas are covered in
  [Running Shoal at high throughput](../scaling.md#database-connections).
- **Temporal:** if you already run Temporal, Shoal can hand it the scheduling of runs instead of the database. Workers
  then run as Temporal activity workers. See [Running Shoal on Temporal](../temporal.md).
- **Amazon SQS:** workers can hear about due runs from an SQS queue instead of polling the database every second,
  with the database still the source of truth. See [Dispatch queues](../dispatch-queues.md).

## Health and metrics

| Endpoint | Meaning |
| --- | --- |
| `GET /healthz` | The process is up. Use for liveness. |
| `GET /readyz` | The process can reach the database. Use for readiness. |
| `GET /metrics` | Prometheus text: `shoal_up`, and `shoal_runs{tenant,status}` with run counts per tenant and status. The Helm chart can add a `ServiceMonitor`. |
| `GET /v1/info` | Version, enabled features and installed plugins. |

Logs go to standard output. `SHOAL_LOG_LEVEL` sets the level (default `INFO`) and `SHOAL_LOG_JSON=true` writes one JSON
object per line for your log pipeline. For what a run did, use its events (`GET /v1/runs/{id}/events`) and the run
viewer; for who changed what, the [audit log](../administration/README.md#audit-log).

## Settings

Every setting is an environment variable with the `SHOAL_` prefix (or a line in `.env`). The ones operators change most:

| Setting | Default | Meaning |
| --- | --- | --- |
| `SHOAL_DATABASE_URL` | SQLite file | `postgresql+asyncpg://user:pass@host/shoal` in production. Migrations run at start-up, or with `shoal db upgrade`. |
| `SHOAL_SECRET_KEY` | an insecure development key | Encrypts stored secrets and signs run identity tokens. **Set your own.** |
| `SHOAL_PUBLIC_URL` | `http://localhost:8080` | The external URL, used in webhook URLs, links and A2A cards. |
| `SHOAL_ADMIN_TOKEN` | | The break-glass admin token, for first setup. |
| `SHOAL_INSTANCE_OIDC`, `SHOAL_INSTANCE_ADMINS` | | One sign-in for everyone, and who becomes an instance admin. See [Tenants](../administration/tenants.md#signing-in). |
| `SHOAL_WORKER_CONCURRENCY` | `16` | Runs one worker advances at once. |
| `SHOAL_WORKER_LANES` | both | Which runs a worker takes: `standard`, `agent` or both. |
| `SHOAL_LEASE_SECONDS` | `60` | How long a worker's claim on a run lasts without renewal. |
| `SHOAL_POLL_INTERVAL_SECONDS` | `1` | How often an idle worker looks for work. |
| `SHOAL_DISPATCH_QUEUE` | `database` | `sqs` to have workers long-poll Amazon SQS for due runs, with `SHOAL_DISPATCH_SQS_*`. See [Dispatch queues](../dispatch-queues.md). |
| `SHOAL_ORCHESTRATOR` | `builtin` | `temporal` to schedule runs on Temporal, with `SHOAL_TEMPORAL_*`. |
| `SHOAL_SECRET_PROVIDERS` | `db,env` | Secret backends, in lookup order. See [Connections and secrets](../connections/README.md#where-values-are-stored). |
| `SHOAL_SECRET_ENV_SHARED` | `true` | Turn off on deployments that host several organisations. |
| `SHOAL_API_KEY_MAX_DAYS` | `0` (no limit) | The longest an API key may live. |
| `SHOAL_AUDIT_RETENTION_DAYS` | `400` | Delete audit entries older than this. `0` keeps them. |
| `SHOAL_CONNECTION_ACTIVITY_RETENTION_DAYS` | `30` | Delete [connection usage history](../connections/README.md#usage-history) older than this. `0` keeps it. |
| `SHOAL_CONNECTION_ACTIVITY_MAX_ENTRIES` | `5000` | The most usage history entries kept per connection (oldest dropped). `0`: no cap. |
| `SHOAL_TRUSTED_PROXIES` | | Proxies whose `X-Forwarded-For` is believed, for audit addresses. |
| `SHOAL_CORS_ORIGINS`, `SHOAL_STATIC_DIR` | | For serving the UI from elsewhere, or from the API process. |
| `SHOAL_SANDBOX_REAP_INTERVAL_SECONDS` | `600` | How often workers delete [leftover Kubernetes sandboxes](../agents/sandboxes.md#cleanup). `0` turns it off. |
| `SHOAL_DEFAULT_BLUEPRINTS` | `true` | Install the [bundled blueprints](../blueprints/bundled.md) in new tenants. |
| `SHOAL_ASSISTANT_*` | | The [assistant](../assistant/README.md): on or off, gateway, model, effort. |
| `SHOAL_DEFAULT_DECISION_PROVIDER` | `llm` | The provider for [decisions](../agents/decisions.md). |

Database pools, overload limits, quotas, model retries and webhook processing are in the
[high-throughput settings reference](../scaling.md#settings-reference). The full list, with descriptions, is in
`engine/shoal/config.py`.

## Upgrades and backups

- **Upgrades.** Roll out the new image; the API applies database migrations at start-up (or run `shoal db upgrade`
  first), and workers wait for it. Runs carry on across a rolling upgrade because their state is in the database: a
  worker that stops mid-step loses its lease and another one resumes the run.
- **Backups.** Back up Postgres; it is the only state. With the `db` secret backend, keep `SHOAL_SECRET_KEY` with it,
  since stored secrets can't be decrypted without it.

## Managing Shoal as code

- [`shoal` CLI](../cli.md): apply resources, start and follow runs, manage secrets, tenants and federation.
- [Terraform provider](../../terraform-provider/docs/index.md): every resource kind, plus users, groups, roles, API
  keys and federation links.
- [Plugins](../plugins.md): add integrations, sandbox backends, model providers, secret backends and more.
