# A public git repository in the Agent Skills layout, pinned to a tag.
resource "shoal_skill_source" "anthropic" {
  name = "anthropic-skills"
  spec = {
    source = "git"
    title  = "Anthropic skills"
    config = { url = "https://github.com/anthropics/skills", ref = "main", path = "skills" }
  }
}

# A private repository: the token is a secret in the `shared` namespace.
resource "shoal_secret" "skills_token" {
  namespace = "shared"
  name      = "internal-skills-token"
  value     = var.internal_skills_token
}

resource "shoal_skill_source" "internal" {
  name = "internal-skills"
  spec = {
    source     = "git"
    config     = { url = "https://github.com/acme/agent-skills", ref = "v2.3.0" }
    secrets    = { token = shoal_secret.skills_token.name }
    namespaces = ["platform", "payments"]
  }
}

# A marketplace speaking shoal.skill-source/v1 (or a source run with shoal.sdk.skill_server).
resource "shoal_skill_source" "registry" {
  name = "acme-registry"
  spec = {
    source = "registry"
    config = { url = "https://skills.acme.internal" }
  }
}
