resource "shoal_target_list" "core_repos" {
  name = "core-repos"
  spec = {
    connection = "github-acme"
    resource   = "repository"
    filter     = { org = "acme", archived = false }
    key        = "{{ target.full_name }}"
    items      = [{ full_name = "acme/api" }, { full_name = "acme/web" }]
  }
}

resource "shoal_campaign" "core_uplift" {
  name = "core-uplift"
  spec = {
    workflow = "repo-uplift"
    targets  = { list = shoal_target_list.core_repos.name }
    inputs   = { repo = "{{ target.full_name }}", goal = "Upgrade to Python 3.13" }
  }
}
