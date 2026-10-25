# Bundled blueprints

Every new tenant's **Blueprints** catalogue starts with these. Adopt one into a namespace, pick your
connections and it creates the workflows, agents and triggers, labelled so you can upgrade or remove them later.
The sources are in [`engine/shoal/blueprints/library`](../../engine/shoal/blueprints/library).

| Blueprint | What it does | You supply |
|---|---|---|
| `github-pr-review` (featured) | Reviews every pull request when it is opened or updated, comments and sets the `shoal/merge-check` status. Skips drafts. | GitHub connection with its webhook pointed at Shoal |
| `jira-bugfix` (featured) | A ticket moved to **Ready for AI** gets a fix on a branch, a PR and a comment back on the ticket. | Jira and GitHub connections |
| `github-uplift` (featured) | A campaign that raises every repository in an org towards a goal (100% coverage by default) and merges what is approved. | GitHub connection, org |
| `slack-bot` (featured) | A channel bot that answers mentions in threads and asks whether the answer helped. | Slack connection, channel |
| `slack-release-approval` (featured) | `/shoal ship <service> [version]` asks for the version, waits for an approver's click in Slack and announces the decision. Call it from a deploy workflow as a gate. | Slack connection, approvers |
| `slack-release-check` | A custom Block Kit message whose menu and buttons drive the workflow. | Slack connection |
| `aws-account-report` | Weekly, an agent assumes an AWS role through a cloud identity (narrowed to `ReadOnlyAccess`), looks for cost, security and hygiene problems and posts them to Slack. | Role ARN, Slack connection |
| `production-quality-eval` | An LLM judge grades a workflow's recent runs against your rubric, gates on a minimum score and exports traces and scores to Braintrust or Langfuse. | Workflow, eval platform |

Each blueprint's README (on its catalogue page) walks through the setup outside Shoal, such as webhooks,
the Slack app or the AWS trust policy.

## Turning them off, and existing tenants

- `SHOAL_DEFAULT_BLUEPRINTS=false` creates new tenants with an empty catalogue.
- Tenants created before a blueprint shipped, or after it was deleted, get it with **Add Shoal's blueprints**
  on the catalogue page, `shoal blueprints install-defaults` or `POST /v1/blueprints/defaults`
  (needs `blueprints:publish` and `blueprints:feature`). Use `--dry-run` / `{"dry_run": true}` to preview.
- Installing creates missing blueprints and upgrades bundled copies with an older version. An upgraded copy
  keeps its status, so a blueprint you deprecated stays deprecated. A blueprint you published under the same
  name (without the `shoal.dev/bundled` label) is never touched.
- Existing adoptions keep their version until you upgrade them from the **Adopted** tab.
