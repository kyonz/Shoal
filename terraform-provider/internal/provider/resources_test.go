package provider

import (
	"fmt"
	"os"
	"regexp"
	"strings"
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

resource "shoal_cloud_identity" "aws" {
  depends_on = [shoal_namespace.team]
  namespace  = "team-acc"
  name       = "aws-deploy"
  spec = {
    provider = "aws"
    aws      = { role_arn = "arn:aws:iam::123456789012:role/shoal-deploy", region = "eu-west-1", duration = "1h" }
    allow    = { workflows = ["deploy-*"] }
  }
}

resource "shoal_skill_source" "public" {
  name = "public-skills"
  spec = {
    source = "git"
    title  = "Public skills"
    config = { url = "https://github.com/anthropics/skills", ref = "main", path = "skills" }
  }
}

resource "shoal_custom_integration" "hooks" {
  name = "acme-hooks"
  spec = {
    source            = "package"
    package           = "webhook"
    connection_scopes = ["org"]
  }
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
					resource.TestCheckResourceAttr("shoal_skill_source.public", "namespace", "shared"),
					resource.TestCheckResourceAttr("shoal_custom_integration.hooks", "namespace", "shared"),
					resource.TestCheckResourceAttr("shoal_custom_integration.hooks", "status.ready", "true"),
					resource.TestCheckResourceAttr("shoal_custom_integration.hooks", "status.connection_scopes", `["org"]`),
					resource.TestCheckResourceAttr("shoal_cloud_identity.aws", "id", "team-acc/aws-deploy"),
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
				ResourceName:      "shoal_cloud_identity.aws",
				ImportState:       true,
				ImportStateId:     "team-acc/aws-deploy",
				ImportStateVerify: true,
			},
			{
				ResourceName:      "shoal_skill_source.public",
				ImportState:       true,
				ImportStateId:     "public-skills",
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
					resource.TestMatchResourceAttr("shoal_api_key.ci", "token", regexp.MustCompile(`^shoal_`)),
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

func configFederationLink(mode string) string {
	return fmt.Sprintf(`
provider "shoal" {
  api_key = %q
}

resource "shoal_federation_link" "globex" {
  tenant = "globex-acc"
  mode   = %q
}
`, os.Getenv("SHOAL_ACC_ADMIN_TOKEN"), mode)
}

func TestAccFederationLink(t *testing.T) {
	resource.Test(t, resource.TestCase{
		PreCheck: func() {
			testAccPreCheck(t)
			if os.Getenv("SHOAL_ACC_ADMIN_TOKEN") == "" {
				t.Skip("needs the operator token (SHOAL_ACC_ADMIN_TOKEN); set when TestMain starts the engine")
			}
			admin := clientFromEnv(os.Getenv("SHOAL_ACC_ADMIN_TOKEN"))
			_, err := admin.Do(testContext(), "POST", "/v1/tenants", map[string]string{"slug": "globex-acc"}, nil)
			if err != nil && !strings.Contains(err.Error(), "409") && !strings.Contains(err.Error(), "exists") {
				t.Fatal(err)
			}
		},
		ProtoV6ProviderFactories: testAccProtoV6ProviderFactories,
		CheckDestroy: func(*terraform.State) error {
			var link struct {
				Status string `json:"status"`
			}
			admin := clientFromEnv(os.Getenv("SHOAL_ACC_ADMIN_TOKEN"))
			if _, err := admin.Do(testContext(), "GET", "/v1/federation/links/globex-acc", nil, &link); err != nil {
				return err
			}
			if link.Status != "revoked" {
				return fmt.Errorf("link status %q after destroy, want revoked", link.Status)
			}
			return nil
		},
		Steps: []resource.TestStep{
			{
				Config: configFederationLink("read_write"),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestMatchResourceAttr("shoal_federation_link.globex", "id", regexp.MustCompile(`^fed_`)),
					resource.TestCheckResourceAttr("shoal_federation_link.globex", "status", "pending"),
					resource.TestCheckResourceAttr("shoal_federation_link.globex", "connected", "false"),
					resource.TestCheckResourceAttr("shoal_federation_link.globex", "mode", "read_write"),
					resource.TestCheckResourceAttr("shoal_federation_link.globex", "effective_mode", "read"), // until the member enrolls
					resource.TestMatchResourceAttr("shoal_federation_link.globex", "enrollment_token", regexp.MustCompile(`^fedjoin_`)),
				),
			},
			{
				Config: configFederationLink("read"),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttr("shoal_federation_link.globex", "mode", "read"),
					resource.TestCheckResourceAttr("shoal_federation_link.globex", "effective_mode", "read"),
					resource.TestMatchResourceAttr("shoal_federation_link.globex", "enrollment_token", regexp.MustCompile(`^fedjoin_`)),
				),
			},
			{
				ResourceName:                         "shoal_federation_link.globex",
				ImportState:                          true,
				ImportStateId:                        "globex-acc",
				ImportStateVerify:                    true,
				ImportStateVerifyIdentifierAttribute: "tenant",
				ImportStateVerifyIgnore:              []string{"enrollment_token", "enrollment_expires_at"},
			},
		},
	})
}
