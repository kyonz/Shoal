# Membership managed by Terraform.
resource "shoal_group" "payments_devs" {
  display_name = "payments-devs"
  members      = [shoal_user.ada.id]
}

# Membership comes from SCIM or the OIDC groups claim: leave members unset.
resource "shoal_group" "platform_admins" {
  display_name = "platform-admins"
}
