data "shoal_permissions" "all" {}

# A read-everything role built from the server's permission list.
resource "shoal_role" "auditor" {
  name        = "auditor"
  permissions = [for p in data.shoal_permissions.all.all : p if endswith(p, ":read")]
}
