resource "shoal_campaign" "uplift" {
  name = "python-3-13-uplift"
  spec = {
    workflow = "repo-uplift"
    targets = {
      connection = "github-acme"
      resource   = "repository"
      filter     = { language = "Python", archived = false }
      key        = "{{ target.full_name }}"
    }
    inputs = { repo = "{{ target.full_name }}", goal = "Upgrade to Python 3.13" }
    policy = { concurrency = 10, runs_per_hour = 60, max_failures = 5 }
  }
}
