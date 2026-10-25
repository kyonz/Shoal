package provider

import (
	"fmt"
	"regexp"
	"testing"

	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
	"github.com/hashicorp/terraform-plugin-testing/terraform"
)

const workflowYAML = `
inputs:
  name: {type: string}
outputs:
  greeting: "{{ steps.greet.output.text }}"
steps:
  - id: greet
    type: set
    values: {text: "Hello {{ inputs.name }}"}
  - id: pause
    type: wait
    needs: [greet]
    duration: 90m
`

func configWorkflows(greeting string) string {
	return fmt.Sprintf(`
# Resources below rely on the provider's default namespace.
provider "shoal" {
  namespace = "team-acc"
}

resource "shoal_namespace" "team" {
  name         = "team-acc"
  display_name = "Team (acceptance)"
}

resource "shoal_secret" "token" {
  depends_on = [shoal_namespace.team]
  name  = "github-token"
  value = "ghp_example"
}

resource "shoal_connection" "github" {
  depends_on = [shoal_namespace.team]
  name = "github"
  spec = {
    integration = "github"
    config      = { attribution = { mode = "app" } }
    secrets     = { token = shoal_secret.token.name }
  }
}

resource "shoal_workflow" "hello" {
  depends_on = [shoal_namespace.team]
  name        = "hello"
  description = "Says hello"
  labels      = { team = "platform" }
  spec        = yamldecode(<<-YAML
%s
  YAML
  )
}

resource "shoal_workflow" "inline" {
  depends_on = [shoal_namespace.team]
  name = "inline"
  spec = {
    inputs = { name = { type = "string", default = "world" } }
    steps = [
      { id = "greet", type = "set", values = { text = "%s {{ inputs.name }}" } },
    ]
  }
}

resource "shoal_identity" "ada" {
  name = "ada"
  spec = { display_name = "Ada Lovelace", email = "ada@acme.example", github = "ada", actors = ["user:ada@acme.example"] }
}
`, workflowYAML, greeting)
}

func TestAccDeclarativeResources(t *testing.T) {
	resource.Test(t, resource.TestCase{
		PreCheck:                 func() { testAccPreCheck(t) },
		ProtoV6ProviderFactories: testAccProtoV6ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: configWorkflows("Hello"),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttr("shoal_namespace.team", "display_name", "Team (acceptance)"),
					resource.TestCheckResourceAttr("shoal_workflow.hello", "id", "team-acc/hello"),
					resource.TestCheckResourceAttr("shoal_workflow.hello", "namespace", "team-acc"),
					resource.TestCheckResourceAttr("shoal_workflow.hello", "version", "1"),
					resource.TestCheckResourceAttr("shoal_workflow.hello", "labels.team", "platform"),
					resource.TestCheckResourceAttr("shoal_workflow.inline", "version", "1"),
					resource.TestMatchResourceAttr("shoal_connection.github", "status.webhook_url", regexp.MustCompile(`/v1/hooks/`)),
					resource.TestCheckResourceAttr("shoal_identity.ada", "namespace", "shared"),
					resource.TestCheckResourceAttrSet("shoal_secret.token", "updated_at"),
				),
			},
			{
				// A spec change is an in-place update with a new version.
				Config: configWorkflows("Hi"),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttr("shoal_workflow.inline", "version", "2"),
					resource.TestCheckResourceAttr("shoal_workflow.hello", "version", "1"),
				),
			},
			{
				ResourceName:      "shoal_workflow.hello",
				ImportState:       true,
				ImportStateId:     "team-acc/hello",
				ImportStateVerify: true,
			},
			{
				ResourceName:      "shoal_identity.ada",
				ImportState:       true,
				ImportStateId:     "ada",
				ImportStateVerify: true,
			},
			{
				ResourceName:            "shoal_secret.token",
				ImportState:             true,
				ImportStateId:           "team-acc/github-token",
				ImportStateVerify:       true,
				ImportStateVerifyIgnore: []string{"value"},
			},
			{
				ResourceName:      "shoal_namespace.team",
				ImportState:       true,
				ImportStateId:     "team-acc",
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccInvalidSpecIsReported(t *testing.T) {
	resource.Test(t, resource.TestCase{
		PreCheck:                 func() { testAccPreCheck(t) },
		ProtoV6ProviderFactories: testAccProtoV6ProviderFactories,
		Steps: []resource.TestStep{{
			Config: `
resource "shoal_workflow" "bad" {
  name = "bad"
  spec = { steps = [{ id = "a", type = "set", values = {}, needs = ["missing"] }] }
}`,
			ExpectError: regexp.MustCompile(`unknown step 'missing'`),
		}},
	})
}

const configAccess = `
resource "shoal_namespace" "payments" {
  name = "payments-acc"
}

resource "shoal_service_account" "ci" {
  name        = "ci-acc"
  description = "CI pipelines"
}

resource "shoal_role" "runner" {
  name        = "runner-acc"
  description = "Run workflows"
  permissions = ["workflows:read", "runs:*"]
}

resource "shoal_user" "ada" {
  username     = "ada@acme.example"
  email        = "ada@acme.example"
  display_name = "Ada Lovelace"
}

resource "shoal_group" "devs" {
  display_name = "payments-devs-acc"
  members      = [shoal_user.ada.id]
}

resource "shoal_role_binding" "devs" {
  role         = "editor"
  subject_kind = "group"
  subject_id   = shoal_group.devs.id
  namespace    = shoal_namespace.payments.name
}

resource "shoal_role_binding" "ci" {
  role         = shoal_role.runner.name
  subject_kind = "user"
  subject_id   = shoal_service_account.ci.id
  namespace    = shoal_namespace.payments.name
}

resource "shoal_api_key" "ci" {
  name            = "github-actions"
  service_account = shoal_service_account.ci.name
  scopes          = ["runs:create", "workflows:read"]
  namespaces      = [shoal_namespace.payments.name]
}

resource "shoal_oidc_provider" "okta" {
  name          = "okta"
  display_name  = "Okta"
  issuer        = "https://acme.okta.example"
  client_id     = "shoal"
  client_secret = "shh"
  groups_claim  = "groups"
  default_role  = "viewer"
}

data "shoal_permissions" "all" {}
data "shoal_me" "me" {}
`

func TestAccAccessResources(t *testing.T) {
	resource.Test(t, resource.TestCase{
		PreCheck:                 func() { testAccPreCheck(t) },
		ProtoV6ProviderFactories: testAccProtoV6ProviderFactories,
		Steps: []resource.TestStep{
			{
				Config: configAccess,
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttrSet("shoal_service_account.ci", "id"),
					resource.TestCheckResourceAttr("shoal_role.runner", "permissions.#", "2"),
					resource.TestCheckResourceAttr("shoal_group.devs", "members.#", "1"),
					resource.TestCheckResourceAttr("shoal_role_binding.devs", "namespace", "payments-acc"),
					resource.TestMatchResourceAttr("shoal_api_key.ci", "token", regexp.MustCompile(`^awm_`)),
					resource.TestCheckResourceAttr("shoal_oidc_provider.okta", "scopes.#", "3"),
					resource.TestCheckResourceAttr("shoal_oidc_provider.okta", "auto_provision", "true"),
					resource.TestCheckTypeSetElemAttr("data.shoal_permissions.all", "tenant.*", "users:manage"),
					resource.TestCheckResourceAttr("data.shoal_me.me", "actor", "sa:admin"),
					checkKeyWorks("shoal_api_key.ci"),
				),
			},
			{
				ResourceName:      "shoal_role.runner",
				ImportState:       true,
				ImportStateId:     "runner-acc",
				ImportStateVerify: true,
			},
			{
				ResourceName:      "shoal_group.devs",
				ImportState:       true,
				ImportStateVerify: true,
			},
			{
				ResourceName:      "shoal_role_binding.devs",
				ImportState:       true,
				ImportStateVerify: true,
			},
			{
				ResourceName:            "shoal_oidc_provider.okta",
				ImportState:             true,
				ImportStateId:           "okta",
				ImportStateVerify:       true,
				ImportStateVerifyIgnore: []string{"client_secret"},
			},
		},
	})
}

// checkKeyWorks uses the minted key against the API.
func checkKeyWorks(name string) resource.TestCheckFunc {
	return func(s *terraform.State) error {
		rs, ok := s.RootModule().Resources[name]
		if !ok {
			return fmt.Errorf("%s not found", name)
		}
		c := clientFromEnv(rs.Primary.Attributes["token"])
		var me struct {
			Actor string `json:"actor"`
		}
		if _, err := c.Do(testContext(), "GET", "/v1/me", nil, &me); err != nil {
			return err
		}
		if me.Actor != "sa:ci-acc" {
			return fmt.Errorf("key acts as %q, want sa:ci-acc", me.Actor)
		}
		if _, err := c.Do(testContext(), "GET", "/v1/users", nil, nil); err == nil {
			return fmt.Errorf("a runs-only key could list users")
		}
		return nil
	}
}
