resource "shoal_eval" "bugfix" {
  name = "jira-bugfix-regressions"
  spec = yamldecode(file("${path.module}/evals/jira-bugfix.yaml")).spec
}
