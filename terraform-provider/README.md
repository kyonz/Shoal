# Terraform provider for Shoal

Manage Shoal namespaces, workflows, agents, skills, connections, triggers, campaigns, sandbox profiles, cloud
identities, gateways, model policies, evals, blueprints, secrets and access (users, groups, service accounts, roles, role bindings, API
keys, OIDC) as code. Reference docs are in [`docs/`](docs/index.md); examples in [`examples/`](examples/).

```terraform
provider "shoal" {
  endpoint  = "https://shoal.example.com" # or SHOAL_ENDPOINT
  namespace = "payments"                  # default namespace
}                                         # api_key from SHOAL_API_KEY

resource "shoal_workflow" "bugfix" {
  name = "jira-bugfix"
  spec = yamldecode(file("${path.module}/workflows/jira-bugfix.yaml")).spec
}
```

## Develop

```sh
make build     # build the provider binary
make testacc   # acceptance tests against a real engine (started from ../engine with uv)
make docs      # regenerate docs/
```

The schema mirrors the API one-to-one: each resource is one API object and attribute names are the API's field names.
