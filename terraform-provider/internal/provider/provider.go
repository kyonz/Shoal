// Package provider implements the Shoal Terraform provider.
package provider

import (
	"context"
	"os"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/provider/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/kyonz/shoal/terraform-provider/internal/client"
)

// DefaultNamespace is used for namespaced resources when neither the
// resource nor the provider sets one.
const DefaultNamespace = "default"

var _ provider.Provider = (*shoalProvider)(nil)

type shoalProvider struct {
	version string
}

// New returns a provider factory for the given build version.
func New(version string) func() provider.Provider {
	return func() provider.Provider { return &shoalProvider{version: version} }
}

type providerModel struct {
	Endpoint  types.String `tfsdk:"endpoint"`
	APIKey    types.String `tfsdk:"api_key"`
	Namespace types.String `tfsdk:"namespace"`
	Tenant    types.String `tfsdk:"tenant"`
}

// providerData is handed to every resource and data source.
type providerData struct {
	client    *client.Client
	namespace string
}

func (p *shoalProvider) Metadata(_ context.Context, _ provider.MetadataRequest, resp *provider.MetadataResponse) {
	resp.TypeName = "shoal"
	resp.Version = p.version
}

func (p *shoalProvider) Schema(_ context.Context, _ provider.SchemaRequest, resp *provider.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Manage [Shoal](https://github.com/kyonz/shoal) workflows, agents, integrations and access as code.",
		Attributes: map[string]schema.Attribute{
			"endpoint": schema.StringAttribute{
				Optional:            true,
				MarkdownDescription: "Base URL of the Shoal server, e.g. `https://shoal.example.com`. Defaults to `$SHOAL_ENDPOINT`.",
			},
			"api_key": schema.StringAttribute{
				Optional:            true,
				Sensitive:           true,
				MarkdownDescription: "API key (or the operator token together with `tenant`). Defaults to `$SHOAL_API_KEY`.",
			},
			"namespace": schema.StringAttribute{
				Optional:            true,
				MarkdownDescription: "Namespace for namespaced resources that do not set one. Defaults to `$SHOAL_NAMESPACE`, then `default`.",
			},
			"tenant": schema.StringAttribute{
				Optional:            true,
				MarkdownDescription: "Tenant slug, only needed with the operator token (API keys already belong to a tenant). Defaults to `$SHOAL_TENANT`.",
			},
		},
	}
}

func valueOrEnv(v types.String, env, fallback string) string {
	if !v.IsNull() && !v.IsUnknown() && v.ValueString() != "" {
		return v.ValueString()
	}
	if e := os.Getenv(env); e != "" {
		return e
	}
	return fallback
}

func (p *shoalProvider) Configure(ctx context.Context, req provider.ConfigureRequest, resp *provider.ConfigureResponse) {
	var cfg providerModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &cfg)...)
	if resp.Diagnostics.HasError() {
		return
	}
	endpoint := valueOrEnv(cfg.Endpoint, "SHOAL_ENDPOINT", "")
	apiKey := valueOrEnv(cfg.APIKey, "SHOAL_API_KEY", "")
	if endpoint == "" {
		resp.Diagnostics.AddAttributeError(path.Root("endpoint"), "Missing Shoal endpoint",
			"Set `endpoint` in the provider block or the SHOAL_ENDPOINT environment variable.")
	}
	if apiKey == "" {
		resp.Diagnostics.AddAttributeError(path.Root("api_key"), "Missing Shoal API key",
			"Set `api_key` in the provider block or the SHOAL_API_KEY environment variable. Create a key under Settings → API keys, or with `POST /v1/api-keys`.")
	}
	if resp.Diagnostics.HasError() {
		return
	}
	data := &providerData{
		client:    client.New(endpoint, apiKey, valueOrEnv(cfg.Tenant, "SHOAL_TENANT", ""), "terraform-provider-shoal/"+p.version),
		namespace: valueOrEnv(cfg.Namespace, "SHOAL_NAMESPACE", DefaultNamespace),
	}
	resp.ResourceData = data
	resp.DataSourceData = data
}

func (p *shoalProvider) Resources(_ context.Context) []func() resource.Resource {
	out := []func() resource.Resource{
		NewNamespaceResource,
		NewSecretResource,
		NewRoleResource,
		NewRoleBindingResource,
		NewServiceAccountResource,
		NewUserResource,
		NewGroupResource,
		NewAPIKeyResource,
		NewOIDCProviderResource,
		NewFederationLinkResource,
	}
	for _, kind := range declarativeKinds {
		out = append(out, newDeclarativeResource(kind))
	}
	return out
}

func (p *shoalProvider) DataSources(_ context.Context) []func() datasource.DataSource {
	return []func() datasource.DataSource{
		NewMeDataSource,
		NewPermissionsDataSource,
		NewNamespaceDataSource,
		NewConnectionDataSource,
	}
}

// configureData extracts providerData in Configure methods.
func configureData(data any, diags interface {
	AddError(string, string)
}) *providerData {
	if data == nil {
		return nil // provider not configured yet (validation)
	}
	pd, ok := data.(*providerData)
	if !ok {
		diags.AddError("Unexpected provider data", "This is a bug in the provider.")
		return nil
	}
	return pd
}
