# Expressions

Most values in a workflow can contain `{{ … }}` expressions. They are evaluated with a sandboxed Jinja2 environment when
the step runs, so they can use the outputs of earlier steps.

```yaml
with:
  key: "{{ inputs.issue_key }}"
  body: "Opened {{ steps.pr.output.url }} for {{ run.initiator.display_name or 'the team' }}"
if: "{{ steps.fix.status == 'succeeded' and steps.fix.output.tests_passed }}"
```

## Native values and text

- A value that is exactly one expression keeps its type. `"{{ steps.pr.output.number }}"` is the number `42`, and
  `"{{ steps.search.output.items }}"` is a list.
- Any other string is rendered as text: `"PR #{{ steps.pr.output.number }}"` is `"PR #42"`.
- Lists and objects are rendered recursively.
- An undefined name is `null` as a native value and an empty string in text, instead of an error, so optional outputs
  are easy to handle: `"{{ steps.fix.output.summary or 'no summary' }}"`.

Shoal uses plain `{{ }}` rather than `${{ }}` so specs can be written inline in Terraform HCL, which interpolates
`${…}` itself. In YAML, **quote any value that starts with `{{`**, or YAML reads it as a mapping.

## What you can reference

| Name | Contents |
| --- | --- |
| `inputs.<name>` | The run's inputs, after defaults and type coercion. |
| `steps.<id>.output.<field>` | An earlier step's output. See the [step reference](steps.md) for each type's fields. |
| `steps.<id>.status` | `pending`, `running`, `waiting`, `succeeded`, `failed`, `skipped` or `cancelled`. |
| `steps.<id>.error`, `steps.<id>.attempt` | The step's error (when it failed) and its attempt number. |
| `run.id`, `run.url`, `run.namespace`, `run.workflow`, `run.version`, `run.labels` | The run itself. `run.url` links to the run viewer. |
| `run.started_by`, `run.initiator` | Who or what started the run, and the person it acts for (`actor`, `source`, `identity`, `display_name`, `email`, `github`, `jira_account_id`, `slack_id`). |
| `run.signal_url` | The run's callback URL. Other systems can POST to `{{ run.signal_url }}/<name>` to [signal](../runs/signals.md) it without an API key. |
| `trigger`, `event` | For runs started by a [trigger](../runs/triggers.md): the trigger and the event that fired it. |
| `campaign`, `target` | For runs started by a [campaign](../runs/campaigns.md): the campaign and this run's target. |
| `secrets.<name>` | A secret in the run's namespace (or `shared`). Resolved at the last moment and redacted from logs, events and outputs. |

Some fields add their own names: `item` and `index` in a `for_each` sub-workflow, `result` in a wait condition's
`check`, `signal` in a signal wait's `match`, and `outputs`, `expect` and `case` in [eval graders](../evals/README.md).

## Functions and filters

All of Jinja2's built-in filters and tests work (`lower`, `default`, `join`, `length`, `selectattr`, `in`, …), plus:

| Name | Example | Result |
| --- | --- | --- |
| `now()` | `{{ now() }}` | The current time, ISO-8601 in UTC. |
| `add_duration` | `{{ now() \| add_duration('2h') }}` | A timestamp plus a duration (`30s`, `15m`, `2h`, `7d`). |
| `duration_seconds` | `{{ '1h30m' \| duration_seconds }}` | `5400.0` |
| `tojson` / `fromjson` | `{{ steps.api.output \| tojson(indent=2) }}` | JSON text, or a value parsed from JSON text. |
| `len`, `min`, `max` | `{{ len(steps.search.output.items) }}` | |
| `timedelta` | `{{ timedelta(hours=1) }}` | |

The environment is sandboxed: expressions can read data, but not call methods that change it or reach anything outside
the context.

## Conditions

`if` on a step, `filter` on a trigger and `check` on a wait condition are conditions. They accept either
`"{{ expr }}"` or a bare expression. A result counts as false when it is `false`, `null`, `0`, an empty string, or the
text `false`, `0`, `no`, `none` or `null`.

## Where expressions are not rendered

- A script step's `code` is used verbatim. Pass data in through its `inputs`. See [Script steps](script-steps.md).
- In blueprint resource templates, only `params`, `prefix` and `blueprint` are filled in when a team adopts the
  blueprint. Every other expression is left for run time. See [Blueprints](../blueprints/README.md).

## Autocomplete

In the builder, typing `{{` in any expression field suggests inputs, the outputs of upstream steps, `run.*` and the
other names above. See [The builder](builder.md#expressions-in-fields).
