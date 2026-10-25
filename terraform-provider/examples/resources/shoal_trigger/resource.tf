resource "shoal_trigger" "ready_for_ai" {
  name = "jira-ready-for-ai"
  spec = {
    connection = "jira-acme"
    event      = "issue_updated"
    filter     = "{{ event.status_change and event.status_change.to == 'Ready for AI' }}"
    workflow   = shoal_workflow.jira_bugfix.name
    inputs = {
      issue_key = "{{ event.issue_key }}"
      repo      = "acme/api"
    }
  }
}

resource "shoal_trigger" "nightly" {
  name = "nightly-uplift"
  spec = {
    event    = "schedule"
    schedule = "0 2 * * *"
    timezone = "Europe/London"
    workflow = "deps-uplift"
  }
}
