# Decisions and grading

Many steps in an automation aren't writing anything: they're judgements. Is this ticket a bug an agent can fix? Which
team owns it? Is this tool call safe? Is this summary good enough? Shoal answers these with **decision models**: given a
state (text or JSON) and typed questions, they return answers with probabilities and a confidence, fast and cheaply,
and you branch on the answers.

## Where decisions are used

| Where | What it asks | Guide |
| --- | --- | --- |
| `decide` step | Your questions about anything in the run; branch with `if`. | [Below](#the-decide-step) |
| `grade` step | Where an output sits on a rubric; fail or flag below a threshold. | [Below](#the-grade-step) |
| Trigger triage | Whether an event deserves a run at all. | [Triggers](../runs/triggers.md#triage-with-a-decision-model) |
| Tool-call guard | Whether an agent's tool call is irreversible, off-task, out of scope or injected. | [Agents](README.md#tool-call-guard) |
| Model routing | How hard a task is, to pick the model. | [Model policies](gateways.md#model-policies) |
| Evals | `decision` graders. | [Evals](../evals/README.md) |

## Questions

| `type` | Asks | Answer `value` |
| --- | --- | --- |
| `choice` | Pick one of 2 to 255 `options` (key: description). | The option key. |
| `score` | Place it on ordered `levels`, lowest first. | The level, plus `score` from 0 (lowest) to 1 (highest). |
| `boolean` | How likely a statement is true. | `true` or `false`. |

Each answer also has `confidence` (0 to 1) and `probabilities` for every option or level. Write `instructions` as the
question you'd ask a careful colleague, and option descriptions that don't overlap.

## The `decide` step

```yaml
- id: route
  type: decide
  state: |
    {{ steps.issue.output.summary }}

    {{ steps.issue.output.description }}
  questions:
    area:
      type: choice
      instructions: Which team owns this bug?
      options: {payments: Payments and checkout, search: Search and catalogue, platform: Anything else}
    automatable:
      type: boolean
      instructions: A coding agent could fix this without a product decision.
  min_confidence: 0.7
  on_low_confidence: approval       # continue (default), fail, or approval

- id: fix
  type: agent
  needs: [route]
  if: "{{ steps.route.output.automatable.value and steps.route.output.area.value != 'platform' }}"
  prompt: ...
```

The output has an entry per question (`steps.route.output.area.value`, `.confidence`, `.probabilities`), plus
`low_confidence` (the questions under `min_confidence`) and `provider`. With `on_low_confidence: approval`, the run waits
for a person to review the answers like an [approval](../runs/approvals.md), and the output then includes their review
as `reviewed`. `fail` fails the step instead.

## The `grade` step

```yaml
- id: quality
  type: grade
  subject: "{{ steps.fix.output.root_cause }}"
  instructions: Does this explain the root cause, not just the symptom, and how the fix was tested?
  levels: [poor, adequate, good, excellent]     # worst first
  threshold: 0.6                                # minimum score, 0 to 1 (default 0.7)
  on_fail: continue                             # or fail
```

Output: `level`, `score`, `confidence`, `probabilities`, `passed` and `threshold`. Use `on_fail: continue` and an `if` on
`passed` to retry the agent with feedback or ask a person, and `on_fail: fail` to stop.

## Providers

| Provider | What it is | Settings (`provider_config`) |
| --- | --- | --- |
| `llm` (default) | Any model through your [gateways](gateways.md), asked for calibrated probabilities with a structured tool call. Self-hostable. | `gateway` (default `anthropic`), `model` (default `claude-haiku-5-5`), `effort` (default `low`) |
| `jev` | TypeSafe's Jev, a "System One" model built for typed decisions, answering in well under a second at a fraction of LLM cost. Needs `pip install 'shoal[jev]'` (the container image includes it). | `api_key_secret` (or `TYPESAFE_API_KEY` on the worker), `model`, `base_url` |

Choose per use with `provider:` and `provider_config:` on the step, the triage block, the guard or the model policy
(`decision_provider`). The server default is `SHOAL_DEFAULT_DECISION_PROVIDER` (`llm`).

```yaml
- id: route
  type: decide
  provider: jev
  provider_config: {api_key_secret: typesafe-api-key}
```

Jev is hosted only and closed-weight, so for data that must stay in your environment use `llm` with your own gateway.
The tenant's `default` model policy applies to the `llm` provider's model as it does to agents.

## Seeing decisions

Every decision is a run event with the question, the answer, the probabilities and the provider, and its usage counts
toward the step's cost. In the run viewer, a `decide` step's output shows each answer with its confidence.
