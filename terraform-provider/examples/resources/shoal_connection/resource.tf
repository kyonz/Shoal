resource "shoal_secret" "github_token" {
  name  = "github-token"
  value = var.github_token
}

resource "shoal_connection" "github" {
  namespace = "shared" # readable from every namespace
  name      = "github-acme"
  spec = {
    integration = "github"
    config = {
      attribution = { mode = "app", elevation = "qualified_approval" }
    }
    secrets = { token = shoal_secret.github_token.name }
  }
}

# Register the webhook in GitHub with the GitHub provider.
output "github_webhook_url" {
  value = shoal_connection.github.status.webhook_url
}
