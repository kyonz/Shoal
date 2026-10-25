resource "shoal_api_key" "ci" {
  name            = "github-actions"
  service_account = shoal_service_account.ci.name
  scopes          = ["workflows:*", "runs:create"]
  namespaces      = ["payments"]
}

output "ci_key" {
  value     = shoal_api_key.ci.token
  sensitive = true
}
