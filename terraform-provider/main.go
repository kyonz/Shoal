// Command terraform-provider-shoal is the Shoal Terraform provider.
package main

import (
	"context"
	"flag"
	"log"

	"github.com/hashicorp/terraform-plugin-framework/providerserver"

	"github.com/kyonz/shoal/terraform-provider/internal/provider"
)

// version is set by the release build (-ldflags "-X main.version=...").
var version = "dev"

//go:generate go run github.com/hashicorp/terraform-plugin-docs/cmd/tfplugindocs generate --provider-name shoal

func main() {
	var debug bool
	flag.BoolVar(&debug, "debug", false, "run with support for debuggers like delve")
	flag.Parse()

	err := providerserver.Serve(context.Background(), provider.New(version), providerserver.ServeOpts{
		Address: "registry.terraform.io/kyonz/shoal",
		Debug:   debug,
	})
	if err != nil {
		log.Fatal(err)
	}
}
