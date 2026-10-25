resource "shoal_identity" "ada" {
  name = "ada"
  spec = {
    display_name    = "Ada Lovelace"
    email           = "ada@acme.example"
    github          = "ada"
    jira_account_id = "5b10ac8d82e05b22cc7d4ef5"
    actors          = ["user:ada@acme.example"]
  }
}
