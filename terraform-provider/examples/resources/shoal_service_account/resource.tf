resource "shoal_service_account" "ci" {
  name        = "github-actions"
  description = "Applies Shoal config from CI"
}

resource "shoal_role_binding" "ci" {
  role         = "editor"
  subject_kind = "user" # service accounts bind as users
  subject_id   = shoal_service_account.ci.id
  namespace    = "payments"
}
