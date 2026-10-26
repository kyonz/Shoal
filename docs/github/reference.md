# GitHub reference

Everything the GitHub integration offers, as listed under **Integrations › GitHub**.

## Connection settings

| Setting | Kind | Default | Meaning |
| --- | --- | --- | --- |
| `base_url` | config | `https://api.github.com` | The API root. GitHub Enterprise Server: `https://<host>/api/v3`. |
| `token` | secret | | A personal access or fine-grained token. |
| `app_id`, `installation_id` | config | | A GitHub App and its installation, instead of a token. |
| `private_key` | secret | | The GitHub App's private key (PEM). |
| `webhook_secret` | secret | | Required to receive webhooks; verifies `X-Hub-Signature-256`. |
| `git_author_name`, `git_author_email` | config | `Shoal`, `shoal-bot@users.noreply.github.com` | Who agents' commits are by. |
| `attribution` | config | | Provenance and merge policy. See [Attribution and merging](attribution.md). |

A connection needs either `token`, or all of `app_id`, `private_key` and `installation_id`.

## Actions

Call an action from an `action` step as `github.<action>`, or give it to an agent as a tool. Read-only actions are
marked; the rest change something on GitHub.

![GitHub's actions on the integration page, with merge_pull_request's inputs and outputs](../assets/screenshots/github/integration.png)

| Action | Inputs | Output |
| --- | --- | --- |
| `get_repository` (read-only) | `repo` | The [repository](#repository) fields. |
| `create_pull_request` | `repo`, `title`, `head` (the branch), `base` (default: the default branch), `body`, `draft`, `labels` | `number`, `url`, `state`, `title`, `head`, `base`, `draft`, `created` (false if the branch already had an open PR), `provenance` |
| `get_pull_request` (read-only) | `repo`, `number` | `number`, `url`, `state`, `merged`, `draft`, `mergeable`, `mergeable_state`, `review_decision` (`approved`, `changes_requested`, `review_required`), `approvals`, `changes_requested`, `checks` (`success`, `failure`, `pending`, `none`), `head_sha`, `updated_at` |
| `can_merge` (read-only) | `repo`, `number` | `can_merge`, `reasons`, `permission` (the connection's), `head_sha`, `governance` |
| `merge_pull_request` | `repo`, `number`, `method` (`merge`, `squash` (default), `rebase`), `commit_title`, `require_approval` (default true) | `merged`, `sha`, `reason`, `governance` |
| `set_merge_check` | `repo`, `number`, `verdict` (`pass`, `fail`), `summary` (≤140 characters), `details_url` | `state`, `sha` |
| `add_comment` | `repo`, `number`, `body` (markdown) | `id`, `url` |
| `close_pull_request` | `repo`, `number`, `comment` | `merged: false`, `reason: closed` |
| `create_issue` | `repo`, `title`, `body`, `labels` | `number`, `url`, `provenance` |
| `get_file` (read-only) | `repo`, `path`, `ref` (default: the default branch) | `path`, `content` (text), `exists`, `sha` |

`repo` is always `owner/name`. `add_comment` works on issues and pull requests alike. `get_file` returns
`exists: false` rather than failing for a missing file. `merge_pull_request` returns `merged: false` with a reason instead
of failing when the merge isn't allowed, which `can_merge` explains; see
[What merge_pull_request checks](attribution.md#what-merge_pull_request-checks).

`governance` is the policy decision: `mode`, `initiator`, `initiator_permission`, `elevation` (`not_needed`, `denied`,
`awaiting_qualified_approval`, `qualified_approval` or `allowed`), `qualified_approvers`, `merge_check` and `reasons`.

```yaml
- id: open_pr
  type: action
  action: github.create_pull_request
  with:
    repo: "{{ inputs.repo }}"
    head: "{{ steps.fix.output.branch }}"
    title: "fix: {{ steps.issue.output.summary }}"
    body: |
      {{ steps.fix.output.root_cause }}

      Fixes {{ inputs.issue_key }}. Opened by Shoal ([run]({{ run.url }})).
    labels: [automated]
```

## Resources

Resources are what [campaigns](../runs/campaigns.md) and
`POST /v1/namespaces/{ns}/connections/{name}/resources/{type}/query` page through.

### `repository`

Repositories of an org, or the ones the connection's account can see.

| Filter | Default | Meaning |
| --- | --- | --- |
| `org` | | List this organization's repositories. Without it, the account's own (`affiliation`). |
| `owner` | | Only repositories owned by this user or org. |
| `affiliation` | `owner,organization_member` | Without `org`: which of the account's repositories. |
| `permission` | | The least permission the connection must have: `admin`, `maintain`, `push`, `triage` or `pull`. |
| `archived` | `false` | `true` for archived only; leave empty for both. |
| `fork` | | `true` or `false` to include only forks or only non-forks. |
| `visibility` | `all` | `public` or `private`. |
| `topic`, `language` | | A GitHub topic, or the main language (case-insensitive). |
| `name` | | A glob on the name, for example `svc-*`. |

Each item has `full_name`, `name`, `owner`, `default_branch`, `private`, `archived`, `fork`, `language`, `topics`, `url`,
`clone_url` and `permission` (the connection's).

### `pull_request`

Pull requests of one repository.

| Filter | Default | Meaning |
| --- | --- | --- |
| `repo` | | `owner/name`. |
| `state` | `open` | `closed` or `all`. |
| `head` | | Only pull requests from this branch, `owner:branch`. |

Each item has `repo`, `number`, `title`, `state`, `url`, `head` (the branch) and `author`.

## Events

| Event | Wake keys |
| --- | --- |
| `<event>.<action>`, or `<event>` for events without an action (`push`) | `github:pr:<repo>#<n>` for each pull request it concerns, and `github:repo:<repo>` |

Each event adds `repo`, `number`, `sender`, `event` and `received_at` to GitHub's payload. `ping` is ignored. See
[Triggers](triggers.md).

## In agent sandboxes

An agent that lists `credentials: [github]` (the workflow's GitHub connection) or `credentials: [github:github-acme]`
gets these in its sandbox:

| Variable | Value |
| --- | --- |
| `GITHUB_TOKEN`, `GH_TOKEN` | The connection's token, an installation token, or the person's own (`mode: user`). |
| `GH_HOST` | `github.com`, or the Enterprise host. |
| `GIT_AUTHOR_*`, `GIT_COMMITTER_*` | `git_author_name` and `git_author_email`. |
| `GIT_CONFIG_*` | A rewrite so `git clone https://$GH_HOST/owner/repo` and `git push` authenticate without prompts. |

```yaml
apiVersion: shoal.dev/v1
kind: Agent
metadata: {name: uplift}
spec:
  credentials: [github]
  tools: [shell, read_file, write_file, edit_file]
```

So an agent's instructions can simply say "clone the repository, create a branch, commit and push", and `gh pr view`
works too. Token values are redacted from the run's events and logs.

## Security

- Deliveries without a valid `X-Hub-Signature-256` are rejected; a connection without `webhook_secret` rejects every
  delivery.
- Tokens are secrets in the connection's namespace and never appear in the resource, the run, or its events.
- Installation tokens are cached per App installation and refreshed five minutes before they expire.
- A generic webhook can't send `github:` wake keys, so only GitHub can wake runs waiting on GitHub events.

## API

| Call | What it does |
| --- | --- |
| `POST /v1/namespaces/{ns}/connections/{name}/test` | Checks the token or mints an installation token. |
| `GET /v1/namespaces/{ns}/connections/{name}/webhook` | The webhook URL and setup instructions. |
| `POST /v1/hooks/{id}` | Where GitHub delivers events. |
| `GET /v1/integrations/github` | This integration's settings, actions, resources and events. |
