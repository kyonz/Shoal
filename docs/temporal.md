# Running Shoal on Temporal

Shoal needs nothing but its database to run durable workflows: the built-in
orchestrator keeps each run's next wake-up in Postgres and workers claim due runs
with a lease. If you already operate [Temporal](https://temporal.io), you can hand
run scheduling to it instead. Workflows, the API, the UI, the CLI, campaigns and
evals work the same either way, and no workflow definition changes.

## What changes and what does not

With `SHOAL_ORCHESTRATOR=temporal`, every new run is a Temporal workflow with the id
`shoal-run-<run id>` on Shoal's task queue. Temporal takes over:

- **Timers.** `wait` steps with `duration` or `until`, condition polls, step retry
  back-off and workflow timeouts become durable Temporal timers.
- **Wake-ups.** Shoal signals, approvals, integration events (`wake_on`), cancels and
  child runs finishing send the workflow a `wake` signal, so the run continues at once.
- **Recovery.** If a worker dies mid-pass, Temporal retries the pass on another worker
  after the heartbeat timeout.
- **Visibility.** Each pass over a run is an activity in the Temporal UI, next to your
  other workflows.

Shoal's database stays the source of truth for everything else: resources and their
versions, the run record and the workflow version it is pinned to, step state and
outputs, events, signals and approvals. A pass over a run is the same code the
built-in workers run, under the same per-run lease, so the steps of one run never
execute on two workers at once and run-scoped sandboxes behave identically.

| Feature | Built-in | Temporal |
|---|---|---|
| Timed waits, condition waits, retries | `next_wake_at` in the database, polled by workers | Durable Temporal timers |
| Approvals, signals, integration events | Wake by database update | `wake` signal to the run's workflow |
| Cancel | API, CLI or UI | Same, plus cancelling the workflow in Temporal |
| Rerun, campaigns, sub-workflows | Each run is a new run | Each run is a new workflow |
| Version pinning | Runs keep the workflow version they started on | Same (the version lives in Shoal's store) |
| Lost workers | Lease expires, another worker continues | Activity heartbeat times out, Temporal retries |

### Determinism

The Temporal workflow (`shoal.runtime.temporal.workflow`) contains no Shoal logic. It
runs a pass as the `shoal.advance` activity, then waits for a timer or a `wake`
signal, and repeats. Model calls, tools, sandboxes, integrations and every database
write happen inside activities, so the workflow code is deterministic and does not
change when your Shoal workflows do. Histories start fresh (continue-as-new) every
500 passes, or sooner when Temporal suggests it. The test suite replays a recorded
history against the current workflow code.

## Setting it up

You can connect Temporal from the instance console or with environment variables.
A setting saved in the console wins over the environment.

### From the instance console

Instance admins open **Instance console → Orchestrator**:

1. Enter the frontend address, the namespace and a task queue for this installation.
2. For Temporal Cloud or mTLS, choose a tenant and pick secrets from its `shared`
   namespace: an API key, or a client certificate and key, and a CA when the server
   certificate is not publicly trusted. Only the secret names are saved. The values
   are read when the setting is applied, so save again after rotating a secret. That
   tenant's admins can change those secrets, so use a tenant your platform team runs.
3. **Test connection** reaches the namespace and tells you how many workers poll the
   task queue.
4. Choose **Temporal** under "New runs start on" and save. Shoal checks the connection
   again before switching.

Every API server and worker picks up the change within a few seconds, with no restart.
Runs already going keep the orchestrator they started on. Switching back to built-in
leaves the connection saved, so Temporal keeps driving its runs until they finish.
Shoal refuses to drop the connection while runs still need it. **Use environment
settings** forgets the console setting. The same is available as
`GET/PUT/DELETE /v1/instance/orchestrator` and `POST /v1/instance/orchestrator/test`.

Workers still need the `temporal` extra (the container image has it). Versioning,
timeouts and the reconcile interval stay environment settings.

### With environment variables

1. Install the extra: `pip install './engine[temporal]'` (or `uv sync --extra temporal` in
   `engine/`). The container image already includes it.
2. Point every Shoal process (API servers and workers) at your cluster. API servers
   need it too, because they start and wake runs.

| Variable | Default | |
|---|---|---|
| `SHOAL_ORCHESTRATOR` | `builtin` | `temporal` to use Temporal |
| `SHOAL_TEMPORAL_ADDRESS` | `localhost:7233` | Frontend `host:port` |
| `SHOAL_TEMPORAL_NAMESPACE` | `default` | |
| `SHOAL_TEMPORAL_TASK_QUEUE` | `shoal` | One per Shoal installation sharing a namespace |
| `SHOAL_TEMPORAL_API_KEY` | | Temporal Cloud API key; turns on TLS |
| `SHOAL_TEMPORAL_TLS` | `false` | TLS without client certificates |
| `SHOAL_TEMPORAL_TLS_CERT_PATH`, `SHOAL_TEMPORAL_TLS_KEY_PATH` | | mTLS client certificate and key |
| `SHOAL_TEMPORAL_TLS_CA_PATH` | | CA for the server certificate |
| `SHOAL_TEMPORAL_PASS_TIMEOUT_SECONDS` | `86400` | Longest single pass, so it bounds the longest step |
| `SHOAL_TEMPORAL_RECONCILE_AFTER_SECONDS` | `60` | How long a run can stay due before Shoal starts or signals its workflow again |
| `SHOAL_TEMPORAL_BUILD_ID` | | Turns on [worker versioning](#worker-versioning) |
| `SHOAL_TEMPORAL_DEPLOYMENT_NAME` | task queue | Worker deployment name |
| `SHOAL_TEMPORAL_VERSIONING_BEHAVIOR` | `auto-upgrade` | Or `pinned` |

Activity heartbeats use `SHOAL_LEASE_SECONDS` as their timeout, and
`SHOAL_WORKER_CONCURRENCY` caps concurrent passes per worker.

**docker compose.** Set the variables in `deploy/.env`, or try it against a throwaway
dev server:

```sh
docker compose -f deploy/docker-compose.yaml -f deploy/docker-compose.temporal.yaml up -d --build
```

**Helm.**

```sh
helm upgrade --install shoal deploy/helm/shoal -n shoal \
  --set orchestrator.type=temporal \
  --set orchestrator.temporal.address=temporal-frontend.temporal.svc:7233
```

For Temporal Cloud, put the API key in a Secret and set
`orchestrator.temporal.apiKeySecret.name` (and `.key`). For mTLS, set
`orchestrator.temporal.mtlsSecret` to a Secret with `tls.crt` and `tls.key`
(`mtlsCa: true` if it also holds `ca.crt`). See `orchestrator` in `values.yaml`.

## Operating it

- **If Temporal is down**, API servers keep accepting runs. The runs wait in the
  database, and the scheduler starts their workflows once Temporal is reachable
  again and the runs have been due for `SHOAL_TEMPORAL_RECONCILE_AFTER_SECONDS`.
  The same check repairs a lost signal or a workflow terminated by hand.
- **Switching orchestrators** only affects new runs. Each run records the orchestrator
  that drives it, and workers keep running built-in runs alongside Temporal ones, so
  you can turn Temporal on without draining. Switching back in the instance console
  keeps Temporal driving its runs while the connection stays saved. With
  `SHOAL_ORCHESTRATOR`, let the Temporal runs finish (or cancel them) before
  switching back, because built-in workers do not pick them up.
- **Several installations** can share a Temporal namespace if each has its own task
  queue. Otherwise one installation's workers take the other's passes and cannot find
  the runs in their database.
- **Cancelling in Temporal** cancels the Shoal run the same way the API does: steps
  are cancelled, child runs are cancelled and sandboxes are released.
- **Workflow ids** are `shoal-run-<run id>`, with the tenant, namespace, workflow and
  version in the memo, so you can find a run's workflow from the Shoal UI and the
  other way round.

### Worker versioning

Shoal's workflow code is small and kept replay-compatible across releases, so
unversioned workers are fine, and so is `auto-upgrade`. If your organization uses
Temporal worker versioning, set `SHOAL_TEMPORAL_BUILD_ID` (for example to the image
tag) and promote each build with
`temporal worker deployment set-current-version`. `pinned` keeps every run on the
build it started on. That means keeping old workers running until their runs finish,
which can take weeks for runs that wait for reviews.

## Testing against Temporal

The whole engine test suite can run with Temporal driving every run:

```sh
temporal server start-dev --headless &
cd engine && SHOAL_TEST_TEMPORAL_ADDRESS=localhost:7233 uv run pytest
```

`scripts/check.sh engine` does this automatically when the `temporal` CLI is installed.
