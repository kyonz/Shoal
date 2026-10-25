# Give a group the editor role in one namespace.
resource "shoal_role_binding" "payments_devs" {
  role         = "editor"
  subject_kind = "group"
  subject_id   = shoal_group.payments_devs.id
  namespace    = shoal_namespace.payments.name
}

# A tenant-wide binding (omit namespace).
resource "shoal_role_binding" "platform_admins" {
  role         = "tenant-admin"
  subject_kind = "group"
  subject_id   = shoal_group.platform_admins.id
}
