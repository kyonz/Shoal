# Attribution and merging

Shoal usually works through one bot account or GitHub App, so on GitHub every pull request would look like the bot's.
The connection's **attribution policy** records who each piece of work is for, and decides when Shoal may merge it.

```yaml
spec:
  integration: github
  config:
    attribution:
      mode: app                         # or user: act with the person's own token
      labels: true                      # provenance labels
      requested_by_trailer: true        # "Requested-by: @login" in PR and issue bodies
      request_initiator_review: true    # ask the person to review the PR
      assign_initiator: false
      elevation: qualified_approval     # deny | qualified_approval | allow
      require_merge_check: false        # require a passing shoal/merge-check status
      independent_merge_check: true
      required_labels: []
```

## Who the work is for

Every run has an **initiator**: the person who started it in Shoal, or the person behind the event that triggered it
(the GitHub sender, the Jira user who moved the ticket, the Slack user who asked). Shoal resolves them to an
[identity](setup.md#link-github-people-to-shoal) to learn their GitHub login. Scheduled runs belong to whoever last
changed the trigger. `run.initiator` shows the result in expressions, and the run header shows "by Priya Shah via trigger
review-new-prs".

## What Shoal adds to a pull request

When a run opens a pull request (`create_pull_request`) or an issue (`create_issue`):

| Policy | What happens |
| --- | --- |
| `requested_by_trailer` | The body ends with `Requested-by: @priya-shah`, or the person's name and "(no linked GitHub account)". |
| `labels` | Provenance labels with `label_prefix` (default `shoal:`): `shoal:automated`, and `shoal:initiator-can-merge` or `shoal:initiator-cannot-merge` from the person's permission on the repository. `shoal:user-credentials` marks work done with their own token. |
| `request_initiator_review` | The person is asked to review the pull request, so it lands in their review queue. |
| `assign_initiator` | The person is assigned. |

Assignment and review requests are best-effort: if GitHub refuses (the person isn't a collaborator, say), the pull request
is still opened and the failure is written to the audit log. The step's output includes `provenance`: the mode, the
initiator, their permission (`admin`, `maintain`, `write`, `triage`, `read`, `none` or `unknown`), whether they can merge,
and the labels added.

Opening a pull request for a branch that already has one returns the existing pull request (`created: false`), so a
retried step doesn't open duplicates.

## Merging on whose rights

The connection may have write access to a repository that the person who asked for the change doesn't. Merging there on
the app's rights would let anyone who can trigger a run merge code. `elevation` decides what happens when the initiator
lacks write access:

| `elevation` | When the initiator can't merge |
| --- | --- |
| `deny` | Never merge. `merge_pull_request` returns `merged: false` with the reason. |
| `qualified_approval` (default) | Merge only once someone with write access has approved **the commit being merged**. An approval of an earlier commit doesn't count. |
| `allow` | Merge on the app's rights. |

When the initiator has write access, or `mode: user` uses their own token, GitHub's own rules apply. A merge that used
elevation gets the label `shoal:merged-via-qualified-approval` or `shoal:merged-via-allowed`.

### What `merge_pull_request` checks

`merge_pull_request` runs `can_merge` first and merges only when it passes. Otherwise it returns `merged: false` and the
reasons, so the workflow can comment or wait instead of failing:

- the connection has push access, and the pull request is open, not a draft and mergeable (not `dirty`, `blocked` or
  `behind`);
- the review decision is `approved` (skip with `require_approval: false`) and no checks are failing or pending;
- the elevation rule above;
- the merge check passes, when `require_merge_check` is on;
- every label in `required_labels` is present.

The merge is pinned to the commit that was checked. If someone pushes in between, GitHub refuses the merge and the step
returns `merged: false`. Call `github.can_merge` yourself to show the reasons before asking someone to approve.

## Merge checks

A **merge check** records an automated review as a commit status, `shoal/merge-check`, on the pull request's head commit:

```yaml
- id: merge_check
  type: action
  action: github.set_merge_check
  with:
    repo: "{{ inputs.repo }}"
    number: "{{ inputs.number }}"
    verdict: "{{ steps.review.output.verdict }}"     # pass | fail
    summary: "{{ steps.review.output.reason }}"      # up to 140 characters, shown on the status
    details_url: "{{ run.url }}"
```

With `require_merge_check: true`, Shoal won't merge until the status is `success`. You can also make
`shoal/merge-check` a required status check in GitHub's branch protection, so humans can't merge before the review
either.

`independent_merge_check` (on by default) refuses a merge check from the step that opened the pull request, so an agent
can't approve its own change. Run the review in a separate step, ideally with a different agent. See the
[run above](README.md#five-minute-start) for a pull request review workflow that does this.

## Acting as the person

With `mode: user`, Shoal calls GitHub with the initiator's own token, the one linked to their identity
(`github_token_secret`). Pull requests, comments and merges then show the person as the author, and GitHub applies their
permissions, so elevation never comes into it. `fallback` decides what happens when they have no linked token:

| `fallback` | Meaning |
| --- | --- |
| `fail` (default) | The step fails with "*name* has not linked a GitHub account". |
| `app` | Use the connection's credential, with the policy above. |

Agents' sandboxes get the same token, so their commits are pushed as the person too. For people connecting accounts
themselves, see [per-person credentials](setup.md#each-persons-own-account).

## Audit

Every pull request opened, merge (and refused merge, with the governance decision), merge check and failed assignment is
written to the audit log with the run, the step and the initiator: `github.pull_request.created`,
`github.pull_request.merged`, `github.pull_request.merge_failed`, `github.merge_check` and `github.assignment.failed`.
See [the audit log](../administration/README.md#audit-log).
