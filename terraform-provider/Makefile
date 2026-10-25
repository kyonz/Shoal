TF ?= $(shell command -v terraform)

build:
	go build -o terraform-provider-shoal .

# Unit tests only.
test:
	go test ./...

# Acceptance tests: starts the engine from ../engine (needs uv) unless SHOAL_ENDPOINT/SHOAL_API_KEY are set.
testacc:
	TF_ACC=1 TF_ACC_TERRAFORM_PATH=$(TF) go test ./... -count=1 -timeout 20m -v

# Regenerate docs/ (spec field tables come from the engine's models).
docs:
	cd ../engine && uv run python ../terraform-provider/scripts/gen_spec_templates.py
	go run github.com/hashicorp/terraform-plugin-docs/cmd/tfplugindocs generate --provider-name shoal

.PHONY: build test testacc docs
