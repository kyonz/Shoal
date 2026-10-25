# Using the workflow builder

The visual builder edits the same declarative workflow (`apiVersion: awm.dev/v1`, `kind: Workflow`) you can write by hand. Anything you build on the canvas can be read, diffed and applied as YAML, and anything written as YAML opens on the canvas.

## The canvas

- **Palette** (left): every step type (`agent`, `action`, `script`, `wait`, `approval`, `workflow`, `set`, `decide`, `grade`) plus the actions of every installed integration, discovered from `GET /v1/integrations`. Drag an item onto the canvas, or search the palette and press <kbd>Enter</kbd> to add it below the selected step.
- **Steps** are the nodes. Each shows its id, type and a short summary (agent name, action, duration), plus badges for `if`, `retry` and `on_error`.
- **Connections** are dependencies. Drag from a step's bottom handle to another step's top handle to say "run after". Select an edge and press <kbd>Delete</kbd> to remove it.
- **Minimap and controls** sit in the corners; scroll to zoom, drag the background to pan, and drag a box with <kbd>Shift</kbd> held to select several steps.

### How connections map to `needs`

A step without `needs` runs after the step before it in the list. The builder keeps your YAML minimal: when a step depends only on the step before it, `needs` is omitted; otherwise it is written out explicitly (including `needs: []` for a step that should start immediately). Steps with no path between them run in parallel.

## The inspector

Select a step to edit it in the inspector on the right. Its form is rendered from the step's JSON Schema (`GET /v1/schemas/steps`), so plugin step types work without UI changes. Common settings appear on every step:

| Setting | Meaning |
|---|---|
| `id` | Unique step id, used in expressions as `steps.<id>` |
| `if` | Expression; the step is skipped when it is false |
| `retry` | Attempts, backoff and backoff factor |
| `timeout` | Maximum duration, e.g. `30m` |
| `on_error` | `fail` the run, or `continue` past the failure |

Pickers list the agents, sandbox profiles and connections in the current namespace. Action steps render their parameters from the integration action's input schema.

Click an empty part of the canvas to edit **workflow settings**: inputs, outputs, default agent and sandbox, and default connections per integration.

## Expressions

Values can contain `{{ … }}` expressions, evaluated by a sandboxed Jinja2 environment. While typing inside `{{`, the editor suggests:

- `inputs.<name>` for workflow inputs,
- `steps.<id>.output.<field>` for outputs of upstream steps only,
- `steps.<id>.status`, `run.id`, `run.url`, `trigger`, `campaign`, `target` and `secrets.<name>`.

In YAML, quote any value that starts with `{{`, for example `if: "{{ steps.fix.output.tests_passed }}"`.

## YAML view

Switch between **Canvas**, **Split** and **YAML** at the top of the builder. Edits flow both ways: changing the graph rewrites the YAML, and valid YAML updates the graph as you type. If the YAML does not parse, the error is shown inline and the canvas keeps the last valid version.

## Validation

The builder checks as you work: duplicate ids, references to unknown or downstream steps, dependency cycles and missing required fields. As you edit it also asks the server for a dry run (`POST /v1/apply?dry_run=true`), so you see exactly the errors the API would return.

## Versions

Every save creates a new version. Open **Versions** to see who changed what and compare any version with the current one.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| <kbd>⌘K</kbd> | Command palette |
| <kbd>⌘S</kbd> | Save |
| <kbd>⌘Z</kbd> | Undo |
| <kbd>⌘⇧Z</kbd> | Redo |
| <kbd>Delete</kbd> / <kbd>Backspace</kbd> | Delete selected steps or connections |
| <kbd>⌘D</kbd> | Duplicate the selected step |
| <kbd>L</kbd> | Auto-layout the graph |

On Windows and Linux use <kbd>Ctrl</kbd> instead of <kbd>⌘</kbd>.

## Layout

Use **Auto-layout** (<kbd>L</kbd>) to arrange steps top to bottom. Positions you set by hand are saved with the workflow (in the `ui.shoal.dev/layout` annotation) and do not create a new workflow version on their own.
