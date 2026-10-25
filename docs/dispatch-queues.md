# Dispatch queues: Postgres or Amazon SQS

A dispatch queue is how workers hear that a run is due. By default there is no
separate queue: the `runs` table is the queue, and every worker polls it about
once a second. With `SHOAL_DISPATCH_QUEUE=sqs`, workers long-poll an Amazon SQS
queue instead and only look in the database every 15 seconds as a safety net.
That takes the polling load off Postgres, which matters most with many worker
replicas or many quiet ones.

The database stays the source of truth either way. A message only says "run X
is due at time T". A worker that receives one checks the run in the database
and claims it with a lease before advancing it, exactly as it would after
polling. A duplicate, early or late message is harmless, and a lost one costs at
most one sweep interval.

This is separate from the [orchestrator](temporal.md). The queue only applies
to runs the built-in orchestrator drives; runs on Temporal use Temporal's own
task queues.

## Choosing

| | `database` (default) | `sqs` |
|---|---|---|
| Needs | Nothing but the database | A standard SQS queue (one per lane if you split worker pools) |
| How workers find due runs | Poll the runs table every `SHOAL_POLL_INTERVAL_SECONDS` (1s) | Long-poll SQS (20s), sweep the runs table every `SHOAL_DISPATCH_SWEEP_SECONDS` (15s) |
| Database load while idle | One claim query per worker per second | One claim query per worker per 15s |
| Latency from start or wake to a pass | Up to one poll interval | As soon as a worker with a free slot receives the message |
| Timers | `next_wake_at` in the runs table | A delayed message (SQS allows 15 minutes; longer waits are sent again) |
| Tenant fairness | Workers take turns across tenants and stop at each tenant's cap | The same, plus SQS fair queues (each tenant is its own message group) |

Stay on `database` unless the polling shows up in your database metrics, or
you want lower start latency than one poll interval.

## Setting up SQS

1. Create a **standard** queue (FIFO queues are refused: they allow no
   per-message delay). The defaults are fine. If you attach a
   dead-letter queue, give it a high `maxReceiveCount` (100 or more), because a
   run whose tenant is at its concurrency cap goes back on the queue until a
   slot frees up.
2. Give the deployment's own AWS identity (IRSA, EKS Pod Identity, ECS task
   role, instance role, or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`) these
   actions on the queue: `sqs:SendMessage`, `sqs:ReceiveMessage`,
   `sqs:DeleteMessage` and `sqs:ChangeMessageVisibility`. API servers send messages too, so they need the
   same role as workers.
3. Point Shoal at it:

   ```sh
   SHOAL_DISPATCH_QUEUE=sqs
   SHOAL_DISPATCH_SQS_QUEUE_URL=https://sqs.eu-west-1.amazonaws.com/123456789012/shoal-dispatch
   ```

Workers log `dispatch queue: sqs` when they start. If SQS is unreachable or
refuses a call, they log a warning once and keep advancing runs from the
database, polling it every second until SQS recovers, so an outage never stops
runs.

The [Terraform module](../deploy/terraform/sqs-dispatch/) creates the queues
and an IAM policy, and outputs the settings above.

### A queue per lane

When you run a separate agent worker pool
([agent lanes](scaling.md#workers-and-agent-lanes)), give the agent lane its own
queue so each pool only receives its own runs:

```sh
SHOAL_DISPATCH_SQS_QUEUE_URL=https://sqs.eu-west-1.amazonaws.com/123456789012/shoal-dispatch
SHOAL_DISPATCH_SQS_LANE_QUEUE_URLS='{"agent": "https://sqs.eu-west-1.amazonaws.com/123456789012/shoal-dispatch-agent"}'
```

With one queue for both lanes, a standard-only worker that receives an agent
run's message hands it straight back, which works but wastes receives.

### Helm

```yaml
dispatch:
  queue: sqs
  sqs:
    queueUrl: https://sqs.eu-west-1.amazonaws.com/123456789012/shoal-dispatch
    agentQueueUrl: https://sqs.eu-west-1.amazonaws.com/123456789012/shoal-dispatch-agent
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/shoal
```

### LocalStack and VPC endpoints

`SHOAL_DISPATCH_SQS_ENDPOINT` sends every call to another endpoint, such as
LocalStack (`http://localhost:4566`) or an interface VPC endpoint.
`SHOAL_DISPATCH_SQS_REGION` sets the signing region when it cannot be read
from the queue URL.

## How it works

- **Publishing.** Whenever a built-in run's `next_wake_at` is set (the run
  starts, a signal, approval, event or child run wakes it, it parks until a
  timer, or it backs off after an error), the process that wrote it sends a
  message once the transaction commits. Messages are sent in the background
  and batched while a send is in flight, so a checkpoint never waits on SQS.
- **Receiving.** Each worker long-polls the queues for its lanes, but only when
  it has a free slot, and takes no more messages than it has slots for. It
  claims those runs with a lease, taking turns across tenants and stopping at
  each tenant's `max_concurrent_runs`, then deletes the messages.
- **Messages that do not lead to a pass** are deleted when there is nothing to
  do: the run finished, another worker holds its lease (that worker publishes
  the next wake-up when it parks), or the run was woken again since (that wake
  sent its own message). A run that is due but over its tenant's cap goes back
  on the queue for a few seconds, a little longer each time.
- **Timers longer than 15 minutes.** SQS delays a message by at most 15
  minutes, so a run waiting a week gets its message early. The worker sees the
  run is not due yet and sends the message again for the rest of the wait.
- **The sweep.** Every `SHOAL_DISPATCH_SWEEP_SECONDS`, workers still claim due
  runs from the database. That finds runs whose message was lost (a failed
  send, a process that died before sending) and runs whose worker died in the
  middle of a pass (its lease expired, and no one else will send a message).
- **When SQS fails.** While a worker's sends or receives fail, it polls the
  database every `SHOAL_POLL_INTERVAL_SECONDS`, as it does without a queue, and
  goes back to sweeping once SQS answers again.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `SHOAL_DISPATCH_QUEUE` | `database` | `database`, `sqs`, or a plugin's name |
| `SHOAL_DISPATCH_SWEEP_SECONDS` | 15 | With an external queue, how often workers still claim due runs from the database |
| `SHOAL_DISPATCH_WAIT_SECONDS` | 20 | How long a receive waits for a message (0 to 20) |
| `SHOAL_DISPATCH_SQS_QUEUE_URL` | none | Standard queue for every lane without its own |
| `SHOAL_DISPATCH_SQS_LANE_QUEUE_URLS` | `{}` | A queue per lane, as JSON |
| `SHOAL_DISPATCH_SQS_REGION` | from the URL | Region to sign requests for |
| `SHOAL_DISPATCH_SQS_ENDPOINT` | the queue URL's host | LocalStack or a VPC endpoint |
| `SHOAL_DISPATCH_SQS_FAIR_QUEUES` | true | Send each tenant's messages as its own message group |

## Other queues

Redis, Google Pub/Sub, RabbitMQ and others fit the same interface: a plugin in
the `shoal.dispatch_queues` entry point group that publishes wake-ups with a
delay, receives them, hands them back and deletes them. See
[Writing plugins](plugins.md#dispatch-queues-shoaldispatch_queues).
