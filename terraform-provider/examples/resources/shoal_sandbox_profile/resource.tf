resource "shoal_sandbox_profile" "k8s_small" {
  name = "k8s-small"
  spec = {
    backend   = "kubernetes"
    image     = "ghcr.io/acme/agent-runner:2026.10"
    cpu       = 2
    memory_mb = 4096
    ttl       = "4h"
  }
}
