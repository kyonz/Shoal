package provider

import (
	"context"
	"net/http"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/kyonz/shoal/terraform-provider/internal/client"
)

// namedCRUD is the shared lifecycle of objects that people name and the API
// creates or replaces with PUT /v1/<collection>/<name>.
type namedCRUD[M any] struct {
	data       *providerData
	collection string
	label      string
	name       func(*M) string
	put        func(context.Context, *M) (any, error) // request body
	read       func(context.Context, *M, []byte) error
}

func (c *namedCRUD[M]) url(m *M) string { return client.Path("v1", c.collection, c.name(m)) }

func (c *namedCRUD[M]) save(ctx context.Context, m *M) error {
	body, err := c.put(ctx, m)
	if err != nil {
		return err
	}
	var raw jsonRaw
	if _, err := c.data.client.Do(ctx, http.MethodPut, c.url(m), body, &raw); err != nil {
		return err
	}
	return c.read(ctx, m, raw)
}

type jsonRaw []byte

func (j *jsonRaw) UnmarshalJSON(b []byte) error { *j = append((*j)[:0], b...); return nil }

func (c *namedCRUD[M]) create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m M
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if _, err := c.data.client.Do(ctx, http.MethodGet, c.url(&m), nil, nil); err == nil {
		resp.Diagnostics.AddError(c.label+" already exists", "Import it instead of creating it: terraform import <address> "+c.name(&m))
		return
	}
	if err := c.save(ctx, &m); err != nil {
		resp.Diagnostics.AddError("Could not create "+c.label, err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (c *namedCRUD[M]) readState(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m M
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var raw jsonRaw
	_, err := c.data.client.Do(ctx, http.MethodGet, c.url(&m), nil, &raw)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err == nil {
		err = c.read(ctx, &m, raw)
	}
	if err != nil {
		resp.Diagnostics.AddError("Could not read "+c.label, err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (c *namedCRUD[M]) update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m M
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := c.save(ctx, &m); err != nil {
		resp.Diagnostics.AddError("Could not update "+c.label, err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (c *namedCRUD[M]) delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m M
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if _, err := c.data.client.Do(ctx, http.MethodDelete, c.url(&m), nil, nil); err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Could not delete "+c.label, err.Error())
	}
}

func importName(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("name"), req.ID)...)
}

func idState() schema.StringAttribute {
	return schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}}
}

func nameAttr(description string) schema.StringAttribute {
	return schema.StringAttribute{Required: true, MarkdownDescription: description,
		PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}, Validators: []validatorString{nameValidator()}}
}

// --------------------------------------------------------------------------- shoal_role

type roleModel struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	Permissions types.Set    `tfsdk:"permissions"`
}

type roleResource struct{ crud namedCRUD[roleModel] }

func NewRoleResource() resource.Resource {
	r := &roleResource{}
	r.crud = namedCRUD[roleModel]{
		collection: "roles", label: "role",
		name: func(m *roleModel) string { return m.Name.ValueString() },
		put: func(ctx context.Context, m *roleModel) (any, error) {
			var diags diagCollector
			perms := listStrings(ctx, m.Permissions, &diags.d)
			return client.Role{Description: strPtr(m.Description), Permissions: perms}, diags.err()
		},
		read: func(_ context.Context, m *roleModel, raw []byte) error {
			var out client.Role
			if err := decode(raw, &out); err != nil {
				return err
			}
			m.ID = types.StringValue(out.Name)
			m.Description = optStr(m.Description, out.Description)
			m.Permissions = setOf(out.Permissions)
			return nil
		},
	}
	return r
}

func (r *roleResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_role"
}

func (r *roleResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "A custom role: a named set of permissions (`resource:verb`, wildcards allowed, e.g. `runs:*`). " +
			"Built-in roles (`tenant-admin`, `namespace-admin`, `editor`, `operator`, `viewer`) cannot be changed. " +
			"See the `shoal_permissions` data source for the full list. Maps to `/v1/roles/{name}`.",
		Attributes: map[string]schema.Attribute{
			"id":          idState(),
			"name":        nameAttr("Role name."),
			"description": schema.StringAttribute{Optional: true, MarkdownDescription: "What the role is for."},
			"permissions": schema.SetAttribute{Required: true, ElementType: types.StringType,
				MarkdownDescription: "Permission patterns, e.g. `[\"workflows:read\", \"runs:*\"]`."},
		},
	}
}

func (r *roleResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.crud.data = configureData(req.ProviderData, &resp.Diagnostics)
}
func (r *roleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	r.crud.create(ctx, req, resp)
}
func (r *roleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	r.crud.readState(ctx, req, resp)
}
func (r *roleResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	r.crud.update(ctx, req, resp)
}
func (r *roleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	r.crud.delete(ctx, req, resp)
}
func (r *roleResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	importName(ctx, req, resp)
}

// --------------------------------------------------------------------------- shoal_service_account

type serviceAccountModel struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	DisplayName types.String `tfsdk:"display_name"`
	Description types.String `tfsdk:"description"`
}

type serviceAccountResource struct {
	crud namedCRUD[serviceAccountModel]
}

func NewServiceAccountResource() resource.Resource {
	r := &serviceAccountResource{}
	r.crud = namedCRUD[serviceAccountModel]{
		collection: "service-accounts", label: "service account",
		name: func(m *serviceAccountModel) string { return m.Name.ValueString() },
		put: func(_ context.Context, m *serviceAccountModel) (any, error) {
			return client.ServiceAccount{DisplayName: strPtr(m.DisplayName), Description: strPtr(m.Description)}, nil
		},
		read: func(_ context.Context, m *serviceAccountModel, raw []byte) error {
			var out client.ServiceAccount
			if err := decode(raw, &out); err != nil {
				return err
			}
			m.ID = types.StringValue(out.ID)
			m.DisplayName = strValue(out.DisplayName)
			m.Description = optStr(m.Description, out.Description)
			return nil
		},
	}
	return r
}

func (r *serviceAccountResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_service_account"
}

func (r *serviceAccountResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "A service account: a non-human principal for CI, integrations and automation. Bind roles to it with " +
			"`shoal_role_binding` (`subject_kind = \"user\"`, `subject_id = shoal_service_account.x.id`) and give it keys with `shoal_api_key`. " +
			"Maps to `/v1/service-accounts/{name}`.",
		Attributes: map[string]schema.Attribute{
			"id":   schema.StringAttribute{Computed: true, MarkdownDescription: "Principal id, used as `subject_id` in role bindings.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name": nameAttr("Service account name; it acts as `sa:<name>` in audit logs and run history."),
			"display_name": schema.StringAttribute{Optional: true, Computed: true, MarkdownDescription: "Name shown in the UI. Defaults to `name`.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"description": schema.StringAttribute{Optional: true, MarkdownDescription: "What it is used for."},
		},
	}
}

func (r *serviceAccountResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.crud.data = configureData(req.ProviderData, &resp.Diagnostics)
}
func (r *serviceAccountResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	r.crud.create(ctx, req, resp)
}
func (r *serviceAccountResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	r.crud.readState(ctx, req, resp)
}
func (r *serviceAccountResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	r.crud.update(ctx, req, resp)
}
func (r *serviceAccountResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	r.crud.delete(ctx, req, resp)
}
func (r *serviceAccountResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	importName(ctx, req, resp)
}

// --------------------------------------------------------------------------- shoal_oidc_provider

type oidcModel struct {
	ID               types.String `tfsdk:"id"`
	Name             types.String `tfsdk:"name"`
	DisplayName      types.String `tfsdk:"display_name"`
	Issuer           types.String `tfsdk:"issuer"`
	ClientID         types.String `tfsdk:"client_id"`
	ClientSecret     types.String `tfsdk:"client_secret"`
	Scopes           types.List   `tfsdk:"scopes"`
	GroupsClaim      types.String `tfsdk:"groups_claim"`
	AllowedDomains   types.Set    `tfsdk:"allowed_domains"`
	AutoProvision    types.Bool   `tfsdk:"auto_provision"`
	DefaultRole      types.String `tfsdk:"default_role"`
	DefaultNamespace types.String `tfsdk:"default_namespace"`
	Enabled          types.Bool   `tfsdk:"enabled"`
	LoginURL         types.String `tfsdk:"login_url"`
}

type oidcResource struct{ crud namedCRUD[oidcModel] }

func NewOIDCProviderResource() resource.Resource {
	r := &oidcResource{}
	r.crud = namedCRUD[oidcModel]{
		collection: "oidc-providers", label: "OIDC provider",
		name: func(m *oidcModel) string { return m.Name.ValueString() },
		put: func(ctx context.Context, m *oidcModel) (any, error) {
			var diags diagCollector
			body := client.OIDCProvider{
				DisplayName: strPtr(m.DisplayName), Issuer: m.Issuer.ValueString(), ClientID: m.ClientID.ValueString(),
				ClientSecret: strPtr(m.ClientSecret), Scopes: listStrings(ctx, m.Scopes, &diags.d),
				GroupsClaim: strPtr(m.GroupsClaim), AllowedDomains: listStrings(ctx, m.AllowedDomains, &diags.d),
				AutoProvision: m.AutoProvision.ValueBool(), DefaultRole: strPtr(m.DefaultRole),
				DefaultNamespace: strPtr(m.DefaultNamespace), Enabled: m.Enabled.ValueBool(),
			}
			return body, diags.err()
		},
		read: nil,
	}
	r.crud.read = func(_ context.Context, m *oidcModel, raw []byte) error {
		var out client.OIDCProvider
		if err := decode(raw, &out); err != nil {
			return err
		}
		m.ID = types.StringValue(out.Name)
		m.DisplayName = strValue(out.DisplayName)
		m.Issuer = types.StringValue(out.Issuer)
		m.ClientID = types.StringValue(out.ClientID)
		m.Scopes = listOf(out.Scopes)
		m.GroupsClaim = strValue(out.GroupsClaim)
		m.AllowedDomains = setOf(out.AllowedDomains)
		m.AutoProvision = types.BoolValue(out.AutoProvision)
		m.DefaultRole = strValue(out.DefaultRole)
		m.DefaultNamespace = strValue(out.DefaultNamespace)
		m.Enabled = types.BoolValue(out.Enabled)
		if r.crud.data != nil {
			m.LoginURL = types.StringValue(r.crud.data.client.Endpoint + "/v1/auth/login/{tenant}/" + out.Name)
		}
		return nil
	}
	return r
}

func (r *oidcResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_oidc_provider"
}

func (r *oidcResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Single sign-on through an OpenID Connect provider (Okta, Entra ID, Google, Auth0, Keycloak, ...). " +
			"Register `<endpoint>/v1/auth/callback` as the redirect URI in the provider. Maps to `/v1/oidc-providers/{name}`.",
		Attributes: map[string]schema.Attribute{
			"id":           idState(),
			"name":         nameAttr("Provider name, part of the login URL."),
			"display_name": schema.StringAttribute{Optional: true, Computed: true, MarkdownDescription: "Button label on the sign-in page. Defaults to `name`.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"issuer":       schema.StringAttribute{Required: true, MarkdownDescription: "Issuer URL; discovery is read from `<issuer>/.well-known/openid-configuration`."},
			"client_id":    schema.StringAttribute{Required: true, MarkdownDescription: "OAuth client id."},
			"client_secret": schema.StringAttribute{Optional: true, Sensitive: true,
				MarkdownDescription: "OAuth client secret (write-only on the server). Omit for public clients; PKCE is always used."},
			"scopes": schema.ListAttribute{Optional: true, Computed: true, ElementType: types.StringType,
				Default:             listdefault.StaticValue(listOf([]string{"openid", "email", "profile"})),
				MarkdownDescription: "Scopes to request. Defaults to `[\"openid\", \"email\", \"profile\"]`."},
			"groups_claim": schema.StringAttribute{Optional: true, MarkdownDescription: "ID token claim holding group names (e.g. `groups`); groups are synced on each sign-in."},
			"allowed_domains": schema.SetAttribute{Optional: true, Computed: true, ElementType: types.StringType,
				Default: setdefault.StaticValue(setOf(nil)), MarkdownDescription: "Only allow verified emails in these domains (empty: any)."},
			"auto_provision": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true),
				MarkdownDescription: "Create users on first sign-in. Defaults to `true`."},
			"default_role": schema.StringAttribute{Optional: true, MarkdownDescription: "Role bound to newly provisioned users."},
			"default_namespace": schema.StringAttribute{Optional: true,
				MarkdownDescription: "Namespace for `default_role` (omit for a tenant-wide binding)."},
			"enabled": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true), MarkdownDescription: "Show on the sign-in page. Defaults to `true`."},
			"login_url": schema.StringAttribute{Computed: true, MarkdownDescription: "Sign-in URL template (`{tenant}` is the tenant slug).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
		},
	}
}

func (r *oidcResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.crud.data = configureData(req.ProviderData, &resp.Diagnostics)
}
func (r *oidcResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	r.crud.create(ctx, req, resp)
}
func (r *oidcResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	r.crud.readState(ctx, req, resp)
}
func (r *oidcResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	r.crud.update(ctx, req, resp)
}
func (r *oidcResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	r.crud.delete(ctx, req, resp)
}
func (r *oidcResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	importName(ctx, req, resp)
}
