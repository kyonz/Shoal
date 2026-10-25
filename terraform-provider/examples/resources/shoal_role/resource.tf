resource "shoal_role" "runner" {
  name        = "runner"
  description = "Run and approve workflows, read everything"
  permissions = ["*:read", "runs:create", "runs:approve", "runs:cancel"]
}
