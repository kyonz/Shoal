resource "shoal_oidc_provider" "okta" {
  name            = "okta"
  display_name    = "Sign in with Okta"
  issuer          = "https://acme.okta.com"
  client_id       = var.okta_client_id
  client_secret   = var.okta_client_secret
  groups_claim    = "groups"
  allowed_domains = ["acme.com"]
  default_role    = "viewer"
}
