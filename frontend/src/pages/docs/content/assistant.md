# The assistant and script steps

The assistant is a chat panel on every page. Open it with the **Assistant** button in the top bar or <kbd>⌘J</kbd> (<kbd>Ctrl+J</kbd>). Describe what you want in plain words and it sets it up through the same public API the UI and CLI use:

- workflows, including the code for deterministic steps;
- agents, skills, sandbox profiles, gateways and model policies;
- connections, triggers (webhooks, integration events, schedules) and campaigns;
- evals, runs, approvals, rollbacks, and questions such as "why did my last run fail?".

## Before you start: a model gateway

The assistant runs on a model through an LLM gateway: by default the server's `anthropic` provider (set `ANTHROPIC_API_KEY` on the server) or a Gateway in **Admin console → Gateways** named by `SHOAL_ASSISTANT_GATEWAY`. Until that works, the panel does not take messages. It says what is missing instead: tenant admins get a button to the gateway setup, and everyone else is told to ask an admin. The same goes for a tenant model policy that does not allow the assistant's model (`SHOAL_ASSISTANT_MODEL`). The panel picks up the fix by itself within a few seconds.

## Approving changes

The assistant reads freely, but every change waits for you. It shows a card with a one-line summary and, for resources, the plan from a dry run (created, updated or unchanged, per document) and the YAML. Choose **Apply** or **Approve** to go ahead, or **Decline**. If you send a new message instead, the waiting changes are declined.

Under **⋯ → Make changes without asking** you can let it apply changes directly. Either way it acts with your own permissions in the current namespace, and everything it does is in the audit log with the source `assistant`.

Never paste secret values into the chat. The assistant refers to secrets by name and points you to **Secrets** to set them.

Conversations are kept in your browser, per organisation. **New chat** starts over.

## Script steps

Use a `script` step for work that needs no judgement: reshape data, dedupe, compute, call a plain HTTP endpoint, run tests. It is faster, cheaper and repeatable compared with an agent step. The assistant prefers script steps and writes the code for you; in the builder, **Write with assistant** on a script step asks it to.

```yaml
- id: dedupe
  type: script
  language: python          # python, node, shell or bash
  inputs:
    emails: "{{ inputs.emails }}"
  outputs:
    emails: {type: array}
    removed: {type: integer}
  code: |
    import shoal_step
    raw = shoal_step.inputs["emails"]
    clean = sorted({e.strip().lower() for e in raw if "@" in e})
    shoal_step.set_output("emails", clean)
    shoal_step.set_output("removed", len(raw) - len(clean))
```

- **Inputs** are expressions, rendered and passed to the program as JSON in the file named by `$SHOAL_INPUTS`. Shell scripts also get scalar inputs as `$SHOAL_INPUT_<NAME>`.
- **Code** is used exactly as written. `{{ }}` is not rendered inside it, so pass data through inputs.
- **Outputs** are written as a JSON object to `$SHOAL_OUTPUTS`. In Python, `import shoal_step` and call `set_output(name, value)`. In Node, use `require("shoal_step")` and `setOutput`. Declared outputs are type-checked, and later steps read them as `steps.dedupe.output.emails`.
- The step also outputs `exit_code`, `stdout` and `stderr`. A non-zero exit fails the step unless **Fail the step on a non-zero exit code** is off.
- The code runs in the step's sandbox profile (local, Docker, Kubernetes, E2B and so on). Use the standard library, or a profile whose image has what you need.

For a one-line shell command, switch the step to **Command** (`run:`). Expressions are rendered there, for example `pytest -q {{ inputs.path }}`.
