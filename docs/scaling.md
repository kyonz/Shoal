# Running Shoal at high throughput

Shoal keeps all run state in Postgres, so API servers and workers scale out
freely. What limits a busy deployment is how many database connections the
fleet opens, how many runs workers advance at once, and how fast model providers
let agents call them. This guide covers the settings for each, and how Shoal
pushes back when it is overloaded instead of slowing down for everyone.

## How Shoal behaves under overload

| What is exceeded | What happens | Setting |
|---|---|---|
| What one API process can work on at once | `503` with `Retry-After` after a short wait for a slot | `SHOAL_API_MAX_CONCURRENT_REQUESTS`, `SHOAL_API_QUEUE_TIMEOUT_SECONDS` |
| A tenant's request rate | `429` with `Retry-After` | `SHOAL_RATE_LIMIT_REQUESTS_PER_SECOND`, `SHOAL_RATE_LIMIT_BURST`, tenant quotas |
| Runs waiting for a worker | New runs get `429` until the queue drains | `SHOAL_MAX_PENDING_RUNS_PER_TENANT`, tenant quotas |
| Runs one tenant can have in progress | Workers skip that tenant's runs until some finish | `SHOAL_MAX_CONCURRENT_RUNS_PER_TENANT`, tenant quotas |
| A gateway's concurrency or per-minute limits | The call uses the next model candidate, or waits for capacity | `limits` on the Gateway |
| A provider's own rate limit (429, 529, 5xx) | The model client backs off, honouring `Retry-After`, and tries again | `SHOAL_MODEL_RETRIES`, `SHOAL_MODEL_RETRY_*` |
| Deliveries to one webhook URL | `429` with `Retry-After` | `SHOAL_WEBHOOK_RATE_LIMIT_PER_SECOND` |

A `503` means the process is busy right now and a `429` means the tenant is over
its quota; clients should wait for `Retry-After` before trying again. Answering
quickly instead of queueing every caller keeps the requests that are accepted
fast: on a 4-core test box, one API process facing 400 clients that never pause
accepted about 77 runs/s with a p99 of 2.5s and answered the rest with `503`.
Without the limit, every caller queued and the p99 was 25s. Run event streams and
the federation tunnel are long-lived and do not count against the limit.

Clients that retry run creation should send an `Idempotency-Key` header. A
retry with the same key returns the run the first request started (`200`
instead of `201`), so a timeout followed by a retry never starts two runs.

```sh
curl -X POST "$SHOAL/v1/namespaces/default/workflows/triage/runs" \
  -H "Authorization: Bearer $KEY" -H "Idempotency-Key: ticket-4411" \
  -d '{"inputs": {"ticket": "4411"}}'
```

## Database connections

Every Shoal process keeps a pool of connections (`SHOAL_DB_POOL_SIZE`, default
20) and may open `SHOAL_DB_MAX_OVERFLOW` more (default 10) for short bursts. The
fleet can open up to:

```
(API replicas × API processes + worker replicas) × (pool size + max overflow)
```

Postgres's default `max_connections` is 100, which four API processes and four
workers would already use up. Put PgBouncer in transaction mode between Shoal
and Postgres so that number becomes client connections to PgBouncer, not
Postgres backends:

- **docker compose and the Helm chart** do this for you. Compose runs a
  `pgbouncer` service; the chart deploys one in front of its bundled Postgres
  (`pgbouncer.enabled`, on by default).
- **With a managed database**, run your own pooler (PgBouncer, RDS Proxy,
  Supavisor) and set:

  | Variable | Value |
  |---|---|
  | `SHOAL_DATABASE_URL` | The pooler |
  | `SHOAL_DATABASE_DIRECT_URL` | Postgres itself. Migrations and live-update notifications (`LISTEN`) use it, since they need a session of their own. |
  | `SHOAL_DB_PGBOUNCER` | `true` when the pooler uses transaction mode, which turns off prepared statement caches |

  In Helm these are `database.url`, `database.directUrl` and `database.pgbouncer`.

Size the pool to the work a process does at once: a worker with
`SHOAL_WORKER_CONCURRENCY=16` is well served by the default 20. A pool smaller
than that makes the process open and close overflow connections all the time,
each one a new Postgres backend and TLS handshake.

## Workers and agent lanes

A worker advances up to `SHOAL_WORKER_CONCURRENCY` runs at once (default 16).
An agent step holds its slot while the agent loop runs, and most of that time is
spent waiting for the model, so workers that run agents can run far more at once
than workers that run short, CPU-bound steps.

Runs whose workflow has an `agent`, `decide` or `grade` step go to the **agent
lane**; all others go to the **standard lane**. `SHOAL_WORKER_LANES` picks which
lanes a worker takes (default both). Run two pools so short workflows never
queue behind long agent runs:

```sh
# standard pool: CPU-bound, modest concurrency
SHOAL_WORKER_LANES='["standard"]' SHOAL_WORKER_CONCURRENCY=16 shoal worker
# agent pool: I/O-bound, high concurrency, no scheduler
SHOAL_WORKER_LANES='["agent"]' SHOAL_WORKER_CONCURRENCY=200 shoal worker --no-scheduler
```

In Helm, set `worker.agentPool.enabled=true` (with `replicaCount`, `concurrency`
and `resources`). In compose, start the `agents` profile and set
`SHOAL_WORKER_LANES='["standard"]'` for the main worker.

Workers take turns across tenants when they claim runs, so one tenant's backlog
of thousands of runs does not delay another tenant's first run.

## Model gateways

Give each Gateway the limits of your provider tier. Every worker shares them
through the database:

```yaml
apiVersion: shoal.dev/v1
kind: Gateway
metadata: {name: anthropic}
spec:
  type: anthropic
  api_key_secret: anthropic-api-key
  limits:
    max_concurrency: 50          # calls in flight at once
    requests_per_minute: 4000
    tokens_per_minute: 2000000   # input plus output
    max_wait: 5m                 # longest a call waits for room before failing
```

When a gateway is at a limit, a call goes to the next candidate of its model
policy if there is one with room, and otherwise waits. When every candidate
answers with a rate limit or outage, the model client waits (`Retry-After` when
the provider sends one, otherwise exponential back-off from
`SHOAL_MODEL_RETRY_BACKOFF_SECONDS`) and tries again, up to `SHOAL_MODEL_RETRIES`
times, before the step fails.

## Tenant quotas

Server-wide defaults come from the `SHOAL_RATE_LIMIT_*` and `SHOAL_MAX_*`
settings. The system admin overrides them per tenant. Unset fields keep the
default and `0` means unlimited:

```sh
curl -X PUT "$SHOAL/v1/tenants/acme/quotas" -H "Authorization: Bearer $SHOAL_ADMIN_TOKEN" \
  -d '{"requests_per_second": 200, "request_burst": 400, "max_pending_runs": 50000, "max_concurrent_runs": 100}'
```

`GET /v1/quotas` shows a tenant its own quotas. Rate limits are counted in each
API process, so a tenant's effective rate is the per-process rate times the
number of API processes.

## Webhooks and live updates

Webhook deliveries are verified, stored and answered with `202` at once.
Workers then parse them, wake waiting runs and fire triggers, so a burst from a
large organisation never holds a sender's connection open past its timeout. Set
`SHOAL_WEBHOOK_PROCESSING=inline` to process deliveries inside the request
instead (the response then lists woken runs and fired triggers).

Live run views (`GET /v1/runs/{id}/events` as SSE, and long polls) wake on
Postgres notifications: every event insert sends `NOTIFY`, and each API process
keeps one `LISTEN` connection. A stream reads the database only when its run has
something new, plus a safety check every 15 seconds. If the listener connection
drops, streams fall back to polling every 2 seconds until it is back.

## Read replicas

Set `SHOAL_DATABASE_READ_URL` to a replica and run lists, dashboards and audit
queries read from it. Everything that decides what to do next (claims, waits,
quotas, event streams) reads the primary.

## Retention

Events of finished runs are deleted after `SHOAL_RUN_EVENT_RETENTION_DAYS`
(default 400; `0` keeps them) and audit entries after
`SHOAL_AUDIT_RETENTION_DAYS`. Processed webhook deliveries are kept for a day, and each connection's usage
history for `SHOAL_CONNECTION_ACTIVITY_RETENTION_DAYS` (default 30), at most
`SHOAL_CONNECTION_ACTIVITY_MAX_ENTRIES` (default 5000) entries per connection.
Deletes run hourly in small batches.

## Dispatch queues

Every worker polls the runs table about once a second for due runs. With many
replicas that is a steady stream of claim queries even when nothing is due.
`SHOAL_DISPATCH_QUEUE=sqs` has workers long-poll Amazon SQS for wake-ups
instead and sweep the database only every `SHOAL_DISPATCH_SWEEP_SECONDS` (15s).
The database stays the source of truth and claims still take a lease, so
tenant fairness and caps work the same way ([guide](dispatch-queues.md)).

## Temporal

`SHOAL_ORCHESTRATOR=temporal` replaces the claim loop with Temporal timers and
task queues ([guide](temporal.md)). Shoal's database stays the source of truth,
so every pass still reads and writes Postgres, and the connection, quota and
gateway settings above apply in the same way. Temporal helps most when you need
very large numbers of concurrent timers and wake-ups.

## Settings reference

| Variable | Default | What it does |
|---|---|---|
| `SHOAL_DB_POOL_SIZE` | 20 | Connections each process keeps open |
| `SHOAL_DB_MAX_OVERFLOW` | 10 | Extra connections allowed briefly |
| `SHOAL_DB_POOL_TIMEOUT_SECONDS` | 10 | How long a request waits for a free connection |
| `SHOAL_DB_POOL_RECYCLE_SECONDS` | 1800 | Replace connections older than this |
| `SHOAL_DB_PGBOUNCER` | false | The database URL is a transaction-mode pooler |
| `SHOAL_DATABASE_DIRECT_URL` | the database URL | Direct connection for migrations and `LISTEN` |
| `SHOAL_DATABASE_READ_URL` | none | Replica for lists, dashboards and audit queries |
| `SHOAL_API_MAX_CONCURRENT_REQUESTS` | 64 | Requests one API process works on at once (0: unlimited) |
| `SHOAL_API_QUEUE_TIMEOUT_SECONDS` | 1 | How long a request waits for a slot before `503` |
| `SHOAL_WORKER_CONCURRENCY` | 16 | Runs a worker advances at once |
| `SHOAL_MAX_LOST_LEASES` | 5 | Fail a run after this many passes in a row ended with its worker dying (0: never) |
| `SHOAL_WORKER_LANES` | `["standard","agent"]` | Which runs a worker takes |
| `SHOAL_DISPATCH_QUEUE` | database | `database` (poll the runs table) or `sqs` ([guide](dispatch-queues.md)) |
| `SHOAL_DISPATCH_SWEEP_SECONDS` | 15 | With SQS, how often workers still look in the database |
| `SHOAL_RATE_LIMIT_REQUESTS_PER_SECOND` | 100 | Per tenant, per API process (0: off) |
| `SHOAL_RATE_LIMIT_BURST` | 200 | Requests allowed above the rate in a burst |
| `SHOAL_MAX_PENDING_RUNS_PER_TENANT` | 10000 | Waiting runs before new runs get 429 (0: off) |
| `SHOAL_MAX_CONCURRENT_RUNS_PER_TENANT` | 0 | Runs of one tenant in progress at once (0: off) |
| `SHOAL_MODEL_RETRIES` | 4 | Rounds over a model's candidates when all are rate limited |
| `SHOAL_MODEL_RETRY_BACKOFF_SECONDS` | 2 | First wait between rounds; doubles each round |
| `SHOAL_MODEL_RETRY_MAX_BACKOFF_SECONDS` | 60 | Longest wait between rounds |
| `SHOAL_MODEL_CALL_LEASE_SECONDS` | 900 | A gateway slot is reclaimed after this if its worker died |
| `SHOAL_AUTH_CACHE_SECONDS` | 5 | How long a process reuses an authenticated key or session |
| `SHOAL_QUOTA_CACHE_SECONDS` | 10 | How long a process caches a tenant's quotas |
| `SHOAL_WEBHOOK_PROCESSING` | queue | `queue` or `inline` |
| `SHOAL_WEBHOOK_RATE_LIMIT_PER_SECOND` | 100 | Per webhook URL, per API process |
| `SHOAL_RUN_EVENT_RETENTION_DAYS` | 400 | Delete old events of finished runs (0: keep) |

Revoking a key or changing a role takes effect at once in the process that made
the change and within `SHOAL_AUTH_CACHE_SECONDS` everywhere else.
