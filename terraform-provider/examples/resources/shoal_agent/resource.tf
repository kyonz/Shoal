resource "shoal_agent" "bugfixer" {
  name = "bugfixer"
  spec = {
    model        = "claude-opus-5-5"
    instructions = "You fix bugs. Always add a regression test and keep the diff minimal."
    skills       = ["python-testing"]
    tools        = ["shell", "read_file", "write_file", "list_files", "github.*"]
    credentials  = ["github"]
    sandbox      = "k8s-small"
    limits       = { max_turns = 120, budget_usd = 15, timeout = "90m" }
  }
}
