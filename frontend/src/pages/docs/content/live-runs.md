# Approvals and live runs

Every execution of a workflow is a **run**. Runs are durable: they survive restarts, and a run can wait for days (for a review, a timer or an approval) without holding a worker.

## Run states

```
run:   pending → running → (waiting ↔ running)* → succeeded | failed | cancelled
step:  pending → running → waiting? → succeeded | failed | skipped | cancelled
```

A step is **skipped** when its `if` expression is false. A run is **waiting** when every runnable step is parked, for example on a `wait` step, an `approval` step or a retry backoff.

## The run viewer

Open a run from **Runs**, from a workflow, or from a campaign target. The viewer shows:

- **Header**: status, workflow and version, who started it (the initiator), duration and cost, with **Cancel** and **Rerun** actions.
- **Graph**: the workflow graph coloured by step status. Running steps pulse; edges into running steps are animated.
- **Timeline**: every run event in order (`run.started`, `step.started`, `step.succeeded`, `wait.polled`, `approval.requested`, …).
- **Step details**: output, error, attempt, usage and timestamps for the selected step.
- **Agent transcript**: for agent steps, each model turn (`agent.turn`) with its text and tool calls, followed by the tool results (`agent.tool_result`).

### Live updates

The viewer subscribes to `GET /v1/runs/{id}/events` with `Accept: text/event-stream`. Each event arrives as it is written, and the stream ends with an `end` event once the run finishes. If the connection drops, the viewer reconnects and resumes from the last event it saw (`Last-Event-ID`), so nothing is missed.

## Approvals

An `approval` step pauses the run and asks a person to decide:

```yaml
- id: review
  type: approval
  message: "Ship the fix for {{ inputs.issue_key }}?"
  approvers: [dana@acme.com]
  max_wait: 3d
  on_timeout: reject
```

While it waits, the run viewer shows a banner with the message and **Approve** / **Reject** buttons, with an optional comment. Deciding needs the `runs:approve` permission in the run's namespace. The same decision can be made through the API:

```bash
curl -X POST "$SHOAL_URL/v1/runs/$RUN_ID/steps/review/approval" \
  -H "Authorization: Bearer $AWM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"approved": true, "comment": "Looks good"}'
```

`decide` steps with `on_low_confidence: approval` also hold the run for a person when the decision model is unsure.

## Cancel and rerun

**Cancel** asks the executor to stop the run (`POST /v1/runs/{id}/cancel`); running steps are cancelled at their next checkpoint. **Rerun** starts a new run of the same workflow version with the same inputs (`POST /v1/runs/{id}/rerun`) and labels it `rerun_of`.
