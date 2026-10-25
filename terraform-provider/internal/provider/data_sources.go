package provider

import (
	"context"
	"net/http"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/kyonz/shoal/terraform-provider/internal/client"
)

func configureDS(req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) *providerData {
	return configureData(req.ProviderData, &resp.Diagnostics)
}

// --------------------------------------------------------------------------- shoal_me

type meDataSource struct{ data *providerData }

type meModel struct {
	Actor      types.String `tfsdk:"actor"`
	Kind       types.String `tfsdk:"kind"`
	UserID     types.String `tfsdk:"user_id"`
	Tenant     types.String `tfsdk:"tenant"`
	Namespaces types.Set    `tfsdk:"namespaces"`
}

func NewMeDataSource() datasource.DataSource { return &meDataSource{} }

func (d *meDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_me"
}

func (d *meDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "The principal the provider is authenticated as (`GET /v1/me`).",
		Attributes: map[string]schema.Attribute{
			"actor":      schema.StringAttribute{Computed: true, MarkdownDescription: "e.g. `sa:terraform` or `user:ada@acme.io`."},
			"kind":       schema.StringAttribute{Computed: true, MarkdownDescription: "`user`, `service` or `system`."},
			"user_id":    schema.StringAttribute{Computed: true},
			"tenant":     schema.StringAttribute{Computed: true, MarkdownDescription: "Tenant slug."},
			"namespaces": schema.SetAttribute{Computed: true, ElementType: types.StringType, MarkdownDescription: "Namespaces the caller can see."},
		},
	}
}

func (d *meDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	d.data = configureDS(req, resp)
}

func (d *meDataSource) Read(ctx context.Context, _ datasource.ReadRequest, resp *datasource.ReadResponse) {
	var me client.Me
	if _, err := d.data.client.Do(ctx, http.MethodGet, "/v1/me", nil, &me); err != nil {
		resp.Diagnostics.AddError("Could not read the current principal", err.Error())
		return
	}
	m := meModel{Actor: types.StringValue(me.Actor), Kind: types.StringValue(me.Kind), UserID: strValue(me.UserID),
		Tenant: types.StringNull(), Namespaces: setOf(me.Namespaces)}
	if me.Tenant != nil {
		m.Tenant = types.StringValue(me.Tenant.Slug)
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

// --------------------------------------------------------------------------- shoal_permissions

type permissionsDataSource struct{ data *providerData }

type permissionsModel struct {
	Namespace types.Set `tfsdk:"namespace"`
	Tenant    types.Set `tfsdk:"tenant"`
	All       types.Set `tfsdk:"all"`
}

func NewPermissionsDataSource() datasource.DataSource { return &permissionsDataSource{} }

func (d *permissionsDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_permissions"
}

func (d *permissionsDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Every permission the server knows (`GET /v1/permissions`), for building custom roles.",
		Attributes: map[string]schema.Attribute{
			"namespace": schema.SetAttribute{Computed: true, ElementType: types.StringType, MarkdownDescription: "Permissions that apply inside a namespace."},
			"tenant":    schema.SetAttribute{Computed: true, ElementType: types.StringType, MarkdownDescription: "Permissions only granted by tenant-wide bindings."},
			"all":       schema.SetAttribute{Computed: true, ElementType: types.StringType},
		},
	}
}

func (d *permissionsDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	d.data = configureDS(req, resp)
}

func (d *permissionsDataSource) Read(ctx context.Context, _ datasource.ReadRequest, resp *datasource.ReadResponse) {
	var perms []client.Permission
	if _, err := d.data.client.Do(ctx, http.MethodGet, "/v1/permissions", nil, &perms); err != nil {
		resp.Diagnostics.AddError("Could not list permissions", err.Error())
		return
	}
	var ns, tenant, all []string
	for _, p := range perms {
		all = append(all, p.Name)
		if p.Scope == "tenant" {
			tenant = append(tenant, p.Name)
		} else {
			ns = append(ns, p.Name)
		}
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &permissionsModel{Namespace: setOf(ns), Tenant: setOf(tenant), All: setOf(all)})...)
}

// --------------------------------------------------------------------------- shoal_namespace

type namespaceDataSource struct{ data *providerData }

func NewNamespaceDataSource() datasource.DataSource { return &namespaceDataSource{} }

func (d *namespaceDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_namespace"
}

func (d *namespaceDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Look up an existing namespace (`GET /v1/namespaces/{name}`).",
		Attributes: map[string]schema.Attribute{
			"id":           schema.StringAttribute{Computed: true},
			"name":         schema.StringAttribute{Required: true},
			"display_name": schema.StringAttribute{Computed: true},
			"description":  schema.StringAttribute{Computed: true},
			"created_at":   schema.StringAttribute{Computed: true},
		},
	}
}

func (d *namespaceDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	d.data = configureDS(req, resp)
}

func (d *namespaceDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var m namespaceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out client.Namespace
	if _, err := d.data.client.Do(ctx, http.MethodGet, client.Path("v1", "namespaces", m.Name.ValueString()), nil, &out); err != nil {
		resp.Diagnostics.AddError("Could not read namespace", err.Error())
		return
	}
	m.ID = types.StringValue(out.Name)
	m.DisplayName = strValue(out.DisplayName)
	m.Description = strValue(out.Description)
	m.CreatedAt = types.StringValue(out.CreatedAt)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

// --------------------------------------------------------------------------- shoal_connection

type connectionDataSource struct{ data *providerData }

type connectionDSModel struct {
	Name        types.String  `tfsdk:"name"`
	Namespace   types.String  `tfsdk:"namespace"`
	Integration types.String  `tfsdk:"integration"`
	WebhookURL  types.String  `tfsdk:"webhook_url"`
	Spec        types.Dynamic `tfsdk:"spec"`
}

func NewConnectionDataSource() datasource.DataSource { return &connectionDataSource{} }

func (d *connectionDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_connection"
}

func (d *connectionDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Look up a connection, for example an org-wide one in `shared`, to read its webhook URL and register it in Jira or GitHub with their own Terraform providers.",
		Attributes: map[string]schema.Attribute{
			"name":        schema.StringAttribute{Required: true},
			"namespace":   schema.StringAttribute{Optional: true, Computed: true, MarkdownDescription: "Defaults to the provider's namespace; connections in `shared` are found from any namespace."},
			"integration": schema.StringAttribute{Computed: true, MarkdownDescription: "Integration key, e.g. `github` or `jira`."},
			"webhook_url": schema.StringAttribute{Computed: true, MarkdownDescription: "URL that receives this connection's webhooks."},
			"spec":        schema.DynamicAttribute{Computed: true},
		},
	}
}

func (d *connectionDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	d.data = configureDS(req, resp)
}

func (d *connectionDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var m connectionDSModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	ns := d.data.namespace
	if !m.Namespace.IsNull() {
		ns = m.Namespace.ValueString()
	}
	var out client.Resource
	if _, err := d.data.client.Do(ctx, http.MethodGet, client.Path("v1", "namespaces", ns, "connections", m.Name.ValueString()), nil, &out); err != nil {
		resp.Diagnostics.AddError("Could not read connection", err.Error())
		return
	}
	spec, err := jsonToDynamic(out.Spec)
	if err != nil {
		resp.Diagnostics.AddError("Could not read connection spec", err.Error())
		return
	}
	m.Namespace = types.StringValue(out.Metadata.Namespace)
	m.Spec = spec
	m.WebhookURL = types.StringNull()
	if url, ok := out.Status["webhook_url"].(string); ok {
		m.WebhookURL = types.StringValue(url)
	}
	var fields struct {
		Integration string `json:"integration"`
	}
	_ = decode(out.Spec, &fields)
	m.Integration = types.StringValue(fields.Integration)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}
