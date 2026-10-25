# Tenant-wide; needs gateways:manage. Secrets live in the `shared` namespace.
variable "litellm_key" {
  type      = string
  sensitive = true
}

resource "shoal_secret" "litellm_key" {
  namespace = "shared"
  name      = "litellm-key"
  value     = var.litellm_key
}

resource "shoal_gateway" "litellm" {
  name = "litellm"
  spec = {
    type           = "openai-compatible"
    base_url       = "https://llm-gateway.acme.internal/v1"
    api_key_secret = shoal_secret.litellm_key.name
    headers        = { "x-team" = "platform" }                  # non-secret headers only
    secret_headers = { "x-litellm-api-key" = shoal_secret.litellm_key.name }
  }
}
