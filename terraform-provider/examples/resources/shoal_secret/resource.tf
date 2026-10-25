resource "shoal_secret" "jira_token" {
  namespace = "payments"
  name      = "jira-api-token"
  value     = var.jira_api_token
}
