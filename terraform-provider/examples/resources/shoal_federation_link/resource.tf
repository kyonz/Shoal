# Run with the operator's credentials (the admin token or a key with system:admin).
variable "operator_token" {
  type      = string
  sensitive = true
}

provider "shoal" {
  alias   = "operator"
  api_key = var.operator_token
}

# Globex runs Shoal in its own org; the hub reaches it through Globex's tenant connector.
resource "shoal_federation_link" "globex" {
  provider = shoal.operator
  tenant   = "globex" # an empty tenant on the hub
  mode     = "read_write"
}

# Hand this to Globex's admin: shoal federation join <hub-url> <token> ...
output "globex_enrollment_token" {
  value     = shoal_federation_link.globex.enrollment_token
  sensitive = true
}
