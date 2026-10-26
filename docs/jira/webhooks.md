# Webhooks Shoal registers

A Jira trigger can have Shoal create the webhook in Jira for it, instead of someone
copying a URL into Jira's admin screens. Add a `webhook` block to the trigger:

```yaml
apiVersion: shoal.dev/v1
kind: Trigger
metadata: {name: jira-ready-for-ai}
spec:
  workflow: jira-bugfix
  connection: jira-acme
  event: issue_updated
  webhook:
    jql: project = BUG AND issuetype = Bug
    board: 42
    column: Ready for AI
  inputs:
    issue_key: "{{ event.issue_key }}"
  dedupe_key: "{{ event.issue_key }}:{{ event.timestamp }}"
```

When the trigger is saved (UI, `shoal apply`, the API or Terraform), Shoal:

1. works out what to register: the Jira events from the trigger's `event`
   (`issue_updated` becomes `jira:issue_updated`; `issue_*` becomes created, updated and
   deleted), and a JQL filter from `jql` plus the statuses of the board column
   (`status in (10001)`);
2. creates the webhook in Jira with its own callback URL
   (`<connection webhook URL>/<registration id>`) and its own signing secret;
3. shows the outcome on the trigger: the **Registered webhook** card in the UI,
   `status.webhook` in the API and Terraform, `shoal triggers webhook <name>`.

After that the webhook follows the trigger. Changing the trigger updates the Jira webhook,
disabling or deleting the trigger deletes it, and moving the trigger to another
connection moves it. A background reconcile, once a minute on the worker, does the same
for changes that did not go through the API (blueprints, skills), refreshes webhooks before
they expire, re-creates expired ones, and retries failures with backoff (2 minutes, then
doubling, at most hourly). `shoal triggers sync <name>` or **Sync now** re-registers straight
away, for example after someone deleted the webhook in Jira.

A trigger with `webhook` fires only from deliveries to its own URL. Deliveries to the
connection's URL (hand-made webhooks, Automation rules) go to the other triggers on the
connection, so moving a trigger to registration never fires it twice.

## The settings

| Field | Meaning |
| --- | --- |
| `jql` | Only issues matching this JQL. Required for dynamic webhooks, which Jira does not allow without a filter. |
| `board` + `column` | Fire when an issue moves into any status mapped to that board column (Jira Software). The column's statuses are looked up when the webhook is registered. |
| `statuses` | Or name the statuses (names or ids) directly. |
| `events` | Jira webhook events, if the trigger's `event` is not enough, e.g. `[jira:issue_created, jira:issue_updated]`. |
| `fields` | Dynamic webhooks: send issue updates only when these field ids change. With `statuses` or `column` it defaults to `[status]`. |
| `mode` | `admin`, `dynamic` or `auto` (the default: `dynamic` for OAuth 2.0 and Connect credentials, otherwise `admin`). |
| `require_signature` | Default `true`. Turn it off only for Data Center versions that cannot sign webhooks (below). |

With `statuses` or `column`, the trigger fires only when the issue *moves into* one of
those statuses (`event.status_change.to`), or is created in one. Updates to an issue that
is already there do not fire it, even when Jira sends them. The trigger's own `filter`
and `dedupe_key` still apply after that.

## Jira Cloud and Data Center

| | Admin webhooks | Dynamic webhooks |
| --- | --- | --- |
| Where | Cloud and Data Center | Cloud only |
| API | `/rest/webhooks/1.0/webhook` | `/rest/api/3/webhook` |
| Credential | A Jira administrator's: `email` + `api_token` (Cloud) or `personal_access_token` (Data Center) | An OAuth 2.0 (3LO) app (`oauth_token` + `cloud_id`) or a Connect app (`connect_app_key` + `connect_shared_secret`) |
| Updates | in place | replaced: the new one is created, then the old one deleted |
| Expiry | none | 30 days, refreshed by Shoal 7 days before |
| Signature | `X-Hub-Signature: sha256=<HMAC of the body>` with the registration's secret | a JWT in `Authorization`, HS256 with the app's client secret (OAuth 2.0) or shared secret (Connect); its `matchedWebhookIds` must include this registration's webhook |
| Limits | per site | 5 webhooks per app and user (OAuth 2.0); 100 per app (Connect) |

**Admin webhooks** see every issue the filter matches. Use an administrator account kept
for automation.

**Dynamic webhooks for OAuth 2.0 apps** are registered as a person, and Jira only sends
events for issues that person can see. Give the connection `oauth:` with
`credentials: per_user` (or `per_user_or_shared`), and Shoal registers and refreshes the
webhook as whoever last saved the trigger, with their own token, refreshing that token as
needed. Set `oauth_client_secret` on the connection (the app's client secret) so deliveries
can be verified. The app needs the `read:jira-work` and `manage:jira-webhook` scopes.

**Connect apps** call the API with a JWT signed by the installation's shared secret (Shoal
computes the query string hash), and deliveries are checked against the same secret and,
when `connect_client_key` is set, its issuer. Jira expects a Connect app's webhook URLs under the app's base URL,
so make that Shoal's public URL.

**Data Center** has admin webhooks only (`api_version: "2"`, a PAT of an administrator).
Shoal sends the signing secret with the webhook. Versions that predate webhook secrets
ignore it and deliver unsigned, which Shoal rejects. For those, set
`require_signature: false`: the callback URL then contains two random ids (the connection's
and the registration's) and is the only protection, so keep it out of logs and use HTTPS.

## Checking a registration

```sh
shoal triggers webhook jira-ready-for-ai     # status, callback URL, events, JQL, Jira id, expiry, last delivery, error
shoal triggers sync jira-ready-for-ai        # register again now
shoal connections webhooks jira-acme         # every registered webhook on the connection
```

`GET /v1/namespaces/{ns}/triggers/{name}/webhook` returns the same as JSON, including
`desired` (what the trigger asks for now) next to `state` (what Jira holds).
`POST /v1/namespaces/{ns}/trigger-webhook-preview` shows what would be registered for
unsaved settings. The UI uses it for the preview under the settings.

A failed registration does not stop the trigger from being saved. The error shows on the
trigger (for example "you must be a Jira administrator to manage webhooks" or "board 42 has
no column 'Ready'") and is retried.

## Testing

`shoal.sdk.testing.MockJira` is a Jira site in memory: issues, workflows, boards, and admin
and dynamic webhooks that deliver, signed the way Jira signs them. See
[`engine/tests/test_jira_webhooks.py`](../../engine/tests/test_jira_webhooks.py) for the whole journey: a ticket moves into the column,
the bugfix workflow runs, and it writes back to the ticket.

```python
jira = MockJira(app_secret="...")
jira.add_issue("BUG-7", "Crash on login")
jira.add_board(42, {"Ready for AI": ["Ready for AI"], "Done": ["Done"]})
async with engine_harness(http=jira) as world:
    await world.connect("jira", "jira", config={...}, secrets={...})
    await world.apply(WORKFLOW, TRIGGER_WITH_WEBHOOK)   # registers the webhook in the mock
    jira.move("BUG-7", "Ready for AI")                  # like dragging the card
    await world.flush(jira)                             # Jira delivers; the trigger fires
```
