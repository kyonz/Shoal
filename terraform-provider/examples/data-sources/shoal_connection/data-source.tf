data "shoal_connection" "github" {
  namespace = "shared"
  name      = "github-acme"
}

resource "github_organization_webhook" "shoal" {
  events = ["pull_request", "pull_request_review", "issue_comment"]
  configuration {
    url          = data.shoal_connection.github.webhook_url
    content_type = "json"
  }
}
