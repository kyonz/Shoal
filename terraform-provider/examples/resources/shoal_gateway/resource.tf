resource "shoal_gateway" "litellm" {
  namespace = "shared"
  name      = "litellm"
  spec = {
    type           = "anthropic"
    base_url       = "https://llm-gateway.acme.internal"
    api_key_secret = "litellm-key"
  }
}
