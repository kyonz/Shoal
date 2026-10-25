# Keep the workflow as YAML (as exported by the visual builder) and send its spec.
resource "shoal_workflow" "jira_bugfix" {
  namespace   = "payments"
  name        = "jira-bugfix"
  description = "Fix the bug in a Jira ticket, open a PR and report back"
  labels      = { team = "payments" }

  spec = yamldecode(file("${path.module}/workflows/jira-bugfix.yaml")).spec
}

# Or write it inline. {{ }} expressions need no escaping in HCL.
resource "shoal_workflow" "hello" {
  name = "hello"
  spec = {
    inputs  = { name = { type = "string" } }
    outputs = { greeting = "{{ steps.greet.output.text }}" }
    steps = [
      { id = "greet", type = "set", values = { text = "Hello {{ inputs.name }}" } },
    ]
  }
}
