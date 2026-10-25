resource "shoal_blueprint" "jira_bugfix" {
  name = "jira-bugfix"
  spec = yamldecode(file("${path.module}/blueprints/jira-bugfix.yaml")).spec
}
