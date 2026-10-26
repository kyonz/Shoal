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

# Shoal registers (and keeps registered) the webhook in Jira itself: no Jira admin screens.
# jsondecode(shoal_trigger.ready_for_ai_registered.status.webhook) has its status and callback URL.
resource "shoal_trigger" "ready_for_ai_registered" {
  name = "jira-ready-for-ai-registered"
  spec = {
    connection = "jira-acme"
    event      = "issue_updated"
    workflow   = shoal_workflow.jira_bugfix.name
    webhook = {
      jql    = "project = BUG"
      board  = 42
      column = "Ready for AI"
    }
    inputs = {
      issue_key = "{{ event.issue_key }}"
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
