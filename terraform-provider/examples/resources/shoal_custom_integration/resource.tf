# A remote integration service (any language) speaking shoal.integration/v1.
resource "shoal_secret" "crm_service_token" {
  namespace = "shared"
  name      = "acme-crm-service-token"
  value     = var.crm_service_token
}

resource "shoal_custom_integration" "crm" {
  name = "acme-crm"
  spec = {
    source       = "remote"
    url          = "https://crm-integration.acme.internal"
    token_secret = shoal_secret.crm_service_token.name
  }
}

# An installed Python package, only for two teams and only as namespace connections.
resource "shoal_custom_integration" "linear" {
  name = "linear"
  spec = {
    source            = "package"
    package           = "shoal-linear"
    connection_scopes = ["namespace"]
    namespaces        = ["platform", "payments"]
  }
}

# An organisation connection to it, shared with the payments namespace only.
resource "shoal_connection" "crm" {
  namespace = "shared"
  name      = "acme-crm"
  spec = {
    integration = shoal_custom_integration.crm.name
    secrets     = { api_key = "acme-crm-api-key" }
    namespaces  = ["payments"]
  }
}
