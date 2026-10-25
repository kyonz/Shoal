resource "shoal_model_policy" "default" {
  name = "default"
  spec = {
    deny_models = ["claude-haiku-4-5"]
    rules = [
      {
        name       = "triage-is-cheap"
        match      = { agents = ["triager"] }
        candidates = [{ model = "claude-haiku-5-5" }]
      },
      {
        name     = "everything-else"
        strategy = "fallback"
        candidates = [
          { model = "claude-opus-5-5", gateway = "litellm" },
          { model = "claude-sonnet-5-5", gateway = "anthropic" },
        ]
      },
    ]
  }
}
