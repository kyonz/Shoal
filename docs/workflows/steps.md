# Step reference

Every step has an `id` and a `type`. All of them accept the common fields `name`, `needs`, `if`, `retry`, `timeout`
and `on_error` ([how they work](README.md#how-steps-are-ordered)). This page lists each type's own fields and what it
outputs. Later steps read outputs as `steps.<id>.output.<field>`, and every step also exposes `steps.<id>.status`,
`steps.<id>.error` and `steps.<id>.attempt`.

The generated **Docs › Reference › Step types** page in the UI lists the same fields straight from the server's schema,
including step types added by plugins.

| Type | Use it to | Output |
| --- | --- | --- |
| [`action`](#action) | Call an integration action | The action's result |
| [`agent`](#agent) | Run an agent in a sandbox until it returns typed outputs | The declared outputs and `summary` |
| [`script`](#script) | Run code in a sandbox | `exit_code`, `stdout`, `stderr` and the declared outputs |
| [`set`](#set) | Compute values from expressions | The values |
| [`wait`](#wait) | Wait for a time, a condition or a signal | Depends on the mode |
| [`approval`](#approval) | Ask a person to approve or reject | `approved`, `by`, `comment`, `timed_out` |
| [`workflow`](#workflow) | Call another workflow, optionally once per item | The child's outputs, or `results` |
| [`decide`](#decide) | Ask a decision model typed questions | One answer per question |
| [`grade`](#grade) | Score something against a rubric | `level`, `score`, `confidence`, `passed` |

## action

```yaml
- id: comment
  type: action
  action: jira.add_comment          # <integration>.<action>
  connection: jira-support          # optional; defaults to spec.connections.jira
  with:
    key: "{{ inputs.issue_key }}"
    body: "Shoal opened {{ steps.pr.output.url }}"
```

`with` is rendered and validated against the action's input schema before the call. The step output is the action's
result. Every action and its fields is listed on the integration's page under **Integrations**, and in the guides for
[GitHub](../github/reference.md), [Jira](../jira/reference.md) and [Slack](../slack/reference.md). Calls carry the
run's initiator, so integrations can [attribute the work](../github/attribution.md) to the person who started it.

## agent

```yaml
- id: fix
  type: agent
  agent: bugfixer                   # optional; defaults to spec.defaults.agent
  sandbox: k8s                      # optional; defaults to the agent's, then the workflow's
  prompt: |
    Fix {{ steps.issue.output.key }}: {{ steps.issue.output.summary }}
  outputs:
    branch: {type: string}
    tests_passed: {type: boolean}
  skills: [test-coverage]           # added to the agent's own skills for this step
  tools: [github.create_pull_request]
  connections: {github: github-acme}
```

The agent works in its sandbox until it calls `finish` with the declared `outputs`, which are validated like workflow
inputs (types, defaults, required fields, coercion). If they don't validate, the agent is told what is wrong and tries
again. The output is the declared values plus the agent's `summary`. Every model turn and tool call is recorded on the
run. See [Agents](../agents/README.md).

## script

```yaml
- id: summarise
  type: script
  language: python                  # python, node, shell (default) or bash
  inputs: {issues: "{{ steps.search.output.items }}"}
  outputs:
    count: {type: integer}
  code: |
    import shoal_step
    shoal_step.set_output("count", len(shoal_step.inputs["issues"]))
```

Either `code` (a program, used verbatim) or `run` (a shell command line, rendered as an expression). Also `sandbox`,
`env`, `workdir`, `fail_on_error` and `cloud_identities`. Output: `exit_code`, `stdout`, `stderr`,
`duration_seconds`, `timed_out` and the declared outputs (also under `outputs`). See [Script steps](script-steps.md).

## set

```yaml
- id: plan
  type: set
  values:
    branch: "fix/{{ inputs.issue_key | lower }}"
    risky: "{{ steps.diff.output.files_changed > 20 }}"
```

Each value is rendered; a value that is exactly one expression keeps its native type (a number, a list, a boolean).
The output is the values.

## wait

A wait releases the worker and resumes later, after minutes or weeks. It has exactly one of four modes:

```yaml
# 1. A duration
- id: cool_off
  type: wait
  duration: 30m

# 2. Until a time (an expression producing an ISO-8601 timestamp)
- id: business_hours
  type: wait
  until: "{{ now() | add_duration('12h') }}"

# 3. Until a condition holds: poll an action, wake early on events
- id: await_review
  type: wait
  condition:
    action: github.get_pull_request
    with: {repo: "{{ inputs.repo }}", number: "{{ steps.open_pr.output.number }}"}
    check: "{{ result.review_decision == 'approved' or result.merged }}"
    poll_interval: 1h
    wake_on: ["github:pr:{{ inputs.repo }}#{{ steps.open_pr.output.number }}"]
  max_wait: 7d
  on_timeout: continue

# 4. For a signal: data sent into the run
- id: deployed
  type: wait
  signal:
    name: deployed
    match: "{{ signal.env == 'staging' }}"
  max_wait: 2h
  on_timeout: fail
```

| Mode | Output |
| --- | --- |
| `duration`, `until` | `waited_until`, `timed_out` |
| `condition` | `satisfied`, `timed_out`, `result` (the last poll), `polls` |
| `signal` | `received`, `timed_out`, `payload`, `name`, `source`, `sender`, `received_at`, `signal` |

`max_wait` limits a condition or signal wait. `on_timeout: continue` (the default) succeeds with `timed_out: true`;
`fail` fails the step. A condition is checked once straight away, then every `poll_interval` (default `15m`), and
again whenever one of the `wake_on` event keys arrives from an integration webhook, so a PR review wakes the run within
seconds instead of on the next poll.

A signal wait takes a `name` (signals sent through the API, the CLI or the run's callback URL) or an `event` (the next
integration event with that wake key). Signals that arrive before the step starts waiting are kept for it, and ones
that don't satisfy `match` stay queued. See [Signals](../runs/signals.md).

## approval

```yaml
- id: review
  type: approval
  message: "Merge {{ steps.pr.output.url }}?"
  approvers: [dana@acme.com, "group:release-managers"]
  max_wait: 3d
  on_timeout: reject                # or approve
  on_reject: fail                   # or continue
  notify:
    - {integration: slack, to: "#deploys"}
```

The run waits until someone with `runs:approve` (and, if `approvers` is set, one of them) decides. A rejection fails
the run unless `on_reject: continue`, in which case the step succeeds with `approved: false`. Output: `approved`,
`by`, `comment`, `timed_out`. See [Approvals](../runs/approvals.md).

## workflow

```yaml
# Call one child workflow
- id: notes
  type: workflow
  workflow: release-notes
  version: 4                        # optional pin
  with: {service: "{{ inputs.service }}"}

# Or fan out: one child run per item
- id: uplift_all
  type: workflow
  workflow: repo-uplift
  for_each: "{{ steps.repos.output.items }}"
  concurrency: 10                   # default 5
  with: {repo: "{{ item.full_name }}", index: "{{ index }}"}
  retry: {max_attempts: 3}
```

Each child is a full run of its own, linked to its parent, and shows up under **Runs**. With one child, the step
output is the child's outputs plus `run_id`, and a failed child fails the step. With `for_each`, the output is
`results` (one entry per item with `index`, `run_id`, `status`, `outputs` and `error`), `succeeded` and `failed`. If any
child fails, the step fails; a retry starts again only the children that didn't succeed.

## decide

```yaml
- id: route
  type: decide
  state: "{{ steps.issue.output }}"
  questions:
    area:
      type: choice
      instructions: Which team owns this bug?
      options: {payments: Payments and checkout, search: Search and catalogue, platform: Anything else}
    severity:
      type: score
      instructions: How severe is it for customers?
      levels: [cosmetic, minor, major, critical]
  min_confidence: 0.7
  on_low_confidence: approval       # continue (default), fail or approval
```

Questions are `choice`, `score` or `boolean`. The output has one entry per question with `value`, `confidence`,
`probabilities` and (for scores) `score`, plus `low_confidence` (the questions below `min_confidence`). With
`on_low_confidence: approval` the run waits for a person to review the answers. See
[Decisions and grading](../agents/decisions.md).

## grade

```yaml
- id: quality
  type: grade
  subject: "{{ steps.fix.output.summary }}"
  instructions: Does the summary explain the root cause and how it was tested?
  levels: [poor, adequate, good, excellent]   # worst first
  threshold: 0.6                    # default 0.7
  on_fail: fail                     # or continue (default)
```

Output: `level`, `score` (0 to 1), `confidence` and `passed` (`score >= threshold`).
