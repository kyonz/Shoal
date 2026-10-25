package provider

import (
	"context"
	"net/http"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/kyonz/shoal/terraform-provider/internal/client"
)

func importID(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func replaceString() []planmodifier.String {
	return []planmodifier.String{stringplanmodifier.RequiresReplace()}
}

// --------------------------------------------------------------------------- shoal_user

type userResource struct{ data *providerData }

type userModel struct {
	ID          types.String `tfsdk:"id"`
	Username    types.String `tfsdk:"username"`
	Email       types.String `tfsdk:"email"`
	DisplayName types.String `tfsdk:"display_name"`
	Active      types.Bool   `tfsdk:"active"`
	Groups      types.Set    `tfsdk:"groups"`
}

func NewUserResource() resource.Resource { return &userResource{} }

func (r *userResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_user"
}

func (r *userResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "A person in the tenant directory. With SSO, users are usually created on first sign-in or by SCIM; " +
			"use this resource to pre-provision people or manage break-glass accounts. Maps to `/v1/users/{id}`.",
		Attributes: map[string]schema.Attribute{
			"id":       schema.StringAttribute{Computed: true, MarkdownDescription: "User id, used as `subject_id` in role bindings and group members.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"username": schema.StringAttribute{Required: true, MarkdownDescription: "Username (usually the email address). Changing it recreates the user.", PlanModifiers: replaceString()},
			"email":    schema.StringAttribute{Optional: true, MarkdownDescription: "Email address; matched against verified OIDC emails on first sign-in."},
			"display_name": schema.StringAttribute{Optional: true, Computed: true, MarkdownDescription: "Name shown in the UI. Defaults to `username`.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"active": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true),
				MarkdownDescription: "Set to `false` to block sign-in and revoke the user's API keys."},
			"groups": schema.SetAttribute{Computed: true, ElementType: types.StringType, MarkdownDescription: "Ids of the groups the user belongs to."},
		},
	}
}

func (r *userResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.data = configureData(req.ProviderData, &resp.Diagnostics)
}

func (r *userResource) apply(m *userModel, out *client.User) {
	m.ID = types.StringValue(out.ID)
	m.Username = types.StringValue(out.Username)
	m.Email = strValue(out.Email)
	m.DisplayName = strValue(out.DisplayName)
	if out.Active != nil {
		m.Active = types.BoolValue(*out.Active)
	}
	m.Groups = setOf(out.Groups)
}

func (r *userResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m userModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	active := m.Active.ValueBool()
	body := client.User{Username: m.Username.ValueString(), Email: strPtr(m.Email), DisplayName: strPtr(m.DisplayName), Active: &active}
	var out client.User
	if _, err := r.data.client.Do(ctx, http.MethodPost, "/v1/users", body, &out); err != nil {
		resp.Diagnostics.AddError("Could not create user", err.Error())
		return
	}
	r.apply(&m, &out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *userResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m userModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out client.User
	_, err := r.data.client.Do(ctx, http.MethodGet, client.Path("v1", "users", m.ID.ValueString()), nil, &out)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		resp.Diagnostics.AddError("Could not read user", err.Error())
		return
	}
	r.apply(&m, &out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *userResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m userModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	active := m.Active.ValueBool()
	body := map[string]any{"email": strPtr(m.Email), "active": active}
	if dn := strPtr(m.DisplayName); dn != nil {
		body["display_name"] = *dn
	}
	var out client.User
	if _, err := r.data.client.Do(ctx, http.MethodPatch, client.Path("v1", "users", m.ID.ValueString()), body, &out); err != nil {
		resp.Diagnostics.AddError("Could not update user", err.Error())
		return
	}
	r.apply(&m, &out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *userResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m userModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if _, err := r.data.client.Do(ctx, http.MethodDelete, client.Path("v1", "users", m.ID.ValueString()), nil, nil); err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Could not delete user", err.Error())
	}
}

func (r *userResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	importID(ctx, req, resp)
}

// --------------------------------------------------------------------------- shoal_group

type groupResource struct{ data *providerData }

type groupModel struct {
	ID          types.String `tfsdk:"id"`
	DisplayName types.String `tfsdk:"display_name"`
	Members     types.Set    `tfsdk:"members"`
	Source      types.String `tfsdk:"source"`
}

func NewGroupResource() resource.Resource { return &groupResource{} }

func (r *groupResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_group"
}

func (r *groupResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "A group of users. Bind roles to groups rather than individuals. Leave `members` unset when membership comes from " +
			"SCIM or the OIDC groups claim, so Terraform does not fight the identity provider. Maps to `/v1/groups/{id}`.",
		Attributes: map[string]schema.Attribute{
			"id":           schema.StringAttribute{Computed: true, MarkdownDescription: "Group id, used as `subject_id` in role bindings.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"display_name": schema.StringAttribute{Required: true, MarkdownDescription: "Group name (matched against OIDC group claims and SCIM `displayName`)."},
			"members": schema.SetAttribute{Optional: true, Computed: true, ElementType: types.StringType,
				MarkdownDescription: "User ids. When set, Terraform owns the full membership list.",
				PlanModifiers:       []planmodifier.Set{setplanmodifier.UseStateForUnknown()}},
			"source": schema.StringAttribute{Computed: true, MarkdownDescription: "`local`, `scim` or `oidc`.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
		},
	}
}

func (r *groupResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.data = configureData(req.ProviderData, &resp.Diagnostics)
}

func (r *groupResource) apply(m *groupModel, out *client.Group) {
	m.ID = types.StringValue(out.ID)
	m.DisplayName = types.StringValue(out.DisplayName)
	m.Members = setOf(out.Members)
	m.Source = types.StringValue(out.Source)
}

func (r *groupResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m groupModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := client.Group{DisplayName: m.DisplayName.ValueString(), Members: listStrings(ctx, m.Members, &resp.Diagnostics)}
	var out client.Group
	if _, err := r.data.client.Do(ctx, http.MethodPost, "/v1/groups", body, &out); err != nil {
		resp.Diagnostics.AddError("Could not create group", err.Error())
		return
	}
	r.apply(&m, &out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *groupResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m groupModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out client.Group
	_, err := r.data.client.Do(ctx, http.MethodGet, client.Path("v1", "groups", m.ID.ValueString()), nil, &out)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		resp.Diagnostics.AddError("Could not read group", err.Error())
		return
	}
	r.apply(&m, &out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *groupResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m groupModel
	var configMembers types.Set
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	resp.Diagnostics.Append(req.Config.GetAttribute(ctx, path.Root("members"), &configMembers)...)
	if resp.Diagnostics.HasError() {
		return
	}
	groupPath := client.Path("v1", "groups", m.ID.ValueString())
	var out client.Group
	if _, err := r.data.client.Do(ctx, http.MethodPatch, groupPath, map[string]string{"display_name": m.DisplayName.ValueString()}, &out); err != nil {
		resp.Diagnostics.AddError("Could not update group", err.Error())
		return
	}
	if !configMembers.IsNull() {
		members := listStrings(ctx, m.Members, &resp.Diagnostics)
		if _, err := r.data.client.Do(ctx, http.MethodPut, groupPath+"/members", map[string][]string{"members": members}, &out); err != nil {
			resp.Diagnostics.AddError("Could not set group members", err.Error())
			return
		}
	}
	r.apply(&m, &out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *groupResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m groupModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if _, err := r.data.client.Do(ctx, http.MethodDelete, client.Path("v1", "groups", m.ID.ValueString()), nil, nil); err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Could not delete group", err.Error())
	}
}

func (r *groupResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	importID(ctx, req, resp)
}

// --------------------------------------------------------------------------- shoal_role_binding

type roleBindingResource struct{ data *providerData }

type roleBindingModel struct {
	ID          types.String `tfsdk:"id"`
	Role        types.String `tfsdk:"role"`
	SubjectKind types.String `tfsdk:"subject_kind"`
	SubjectID   types.String `tfsdk:"subject_id"`
	Namespace   types.String `tfsdk:"namespace"`
}

func NewRoleBindingResource() resource.Resource { return &roleBindingResource{} }

func (r *roleBindingResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_role_binding"
}

func (r *roleBindingResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Grants a role to a user, service account or group, either in one namespace or tenant-wide. " +
			"You can only grant permissions you hold yourself. Bindings are immutable: any change replaces the binding. Maps to `/v1/role-bindings/{id}`.",
		Attributes: map[string]schema.Attribute{
			"id":   schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"role": schema.StringAttribute{Required: true, MarkdownDescription: "Built-in (`tenant-admin`, `namespace-admin`, `editor`, `operator`, `viewer`) or custom role name.", PlanModifiers: replaceString()},
			"subject_kind": schema.StringAttribute{Required: true, PlanModifiers: replaceString(), Validators: []validatorString{oneOf("user", "group")},
				MarkdownDescription: "`user` (people and service accounts) or `group`."},
			"subject_id": schema.StringAttribute{Required: true, PlanModifiers: replaceString(),
				MarkdownDescription: "Id of the user, service account or group, e.g. `shoal_group.sre.id`."},
			"namespace": schema.StringAttribute{Optional: true, PlanModifiers: replaceString(),
				MarkdownDescription: "Namespace the role applies in. Omit for a tenant-wide binding (needed for tenant permissions such as `users:manage`). This does not default to the provider's namespace."},
		},
	}
}

func (r *roleBindingResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.data = configureData(req.ProviderData, &resp.Diagnostics)
}

func (r *roleBindingResource) apply(m *roleBindingModel, out *client.RoleBinding) {
	m.ID = types.StringValue(out.ID)
	m.Role = types.StringValue(out.Role)
	m.SubjectKind = types.StringValue(out.SubjectKind)
	m.SubjectID = types.StringValue(out.SubjectID)
	m.Namespace = strValue(out.Namespace)
}

func (r *roleBindingResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m roleBindingModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := client.RoleBinding{Role: m.Role.ValueString(), SubjectKind: m.SubjectKind.ValueString(), SubjectID: m.SubjectID.ValueString(), Namespace: strPtr(m.Namespace)}
	var out client.RoleBinding
	if _, err := r.data.client.Do(ctx, http.MethodPost, "/v1/role-bindings", body, &out); err != nil {
		resp.Diagnostics.AddError("Could not create role binding", err.Error())
		return
	}
	r.apply(&m, &out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *roleBindingResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m roleBindingModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out client.RoleBinding
	_, err := r.data.client.Do(ctx, http.MethodGet, client.Path("v1", "role-bindings", m.ID.ValueString()), nil, &out)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		resp.Diagnostics.AddError("Could not read role binding", err.Error())
		return
	}
	r.apply(&m, &out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *roleBindingResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	// Every attribute forces replacement; nothing to update in place.
	var m roleBindingModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *roleBindingResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m roleBindingModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if _, err := r.data.client.Do(ctx, http.MethodDelete, client.Path("v1", "role-bindings", m.ID.ValueString()), nil, nil); err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Could not delete role binding", err.Error())
	}
}

func (r *roleBindingResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	importID(ctx, req, resp)
}

// --------------------------------------------------------------------------- shoal_api_key

type apiKeyResource struct{ data *providerData }

type apiKeyModel struct {
	ID             types.String `tfsdk:"id"`
	Name           types.String `tfsdk:"name"`
	ServiceAccount types.String `tfsdk:"service_account"`
	Scopes         types.List   `tfsdk:"scopes"`
	Namespaces     types.List   `tfsdk:"namespaces"`
	ExpiresAt      types.String `tfsdk:"expires_at"`
	Token          types.String `tfsdk:"token"`
	Prefix         types.String `tfsdk:"prefix"`
	OwnerID        types.String `tfsdk:"owner_id"`
}

func NewAPIKeyResource() resource.Resource { return &apiKeyResource{} }

func (r *apiKeyResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_api_key"
}

func (r *apiKeyResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replaceList := []planmodifier.List{listplanmodifier.RequiresReplace()}
	resp.Schema = schema.Schema{
		MarkdownDescription: "An API key for a service account (or for the caller). The key acts with its owner's roles, " +
			"optionally narrowed by `scopes` and `namespaces`. The token is only available at creation and is stored in state; " +
			"treat state as secret. Any change replaces the key; destroying it revokes the key. Maps to `/v1/api-keys/{id}`.",
		Attributes: map[string]schema.Attribute{
			"id":   schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name": schema.StringAttribute{Required: true, MarkdownDescription: "Label for the key.", PlanModifiers: replaceString()},
			"service_account": schema.StringAttribute{Optional: true, PlanModifiers: replaceString(),
				MarkdownDescription: "Service account name that owns the key (needs `api-keys:manage`). Omit to create a key for the caller."},
			"scopes": schema.ListAttribute{Optional: true, Computed: true, ElementType: types.StringType, PlanModifiers: replaceList,
				Default:             listdefault.StaticValue(listOf([]string{"*"})),
				MarkdownDescription: "Permission patterns the key is limited to. Defaults to `[\"*\"]` (everything the owner can do)."},
			"namespaces": schema.ListAttribute{Optional: true, Computed: true, ElementType: types.StringType, PlanModifiers: replaceList,
				Default:             listdefault.StaticValue(listOf(nil)),
				MarkdownDescription: "Namespaces the key is limited to. Empty means all namespaces the owner can access."},
			"expires_at": schema.StringAttribute{Optional: true, PlanModifiers: replaceString(), MarkdownDescription: "Expiry time (RFC 3339)."},
			"token": schema.StringAttribute{Computed: true, Sensitive: true, MarkdownDescription: "The key. Use it as `Authorization: Bearer <token>`.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"prefix": schema.StringAttribute{Computed: true, MarkdownDescription: "Non-secret prefix shown in the UI and audit log.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"owner_id": schema.StringAttribute{Computed: true, MarkdownDescription: "Id of the owning user or service account.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
		},
	}
}

func (r *apiKeyResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.data = configureData(req.ProviderData, &resp.Diagnostics)
}

func (r *apiKeyResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m apiKeyModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := client.APIKeyIn{
		Name: m.Name.ValueString(), ServiceAccount: strPtr(m.ServiceAccount),
		Scopes: listStrings(ctx, m.Scopes, &resp.Diagnostics), Namespaces: listStrings(ctx, m.Namespaces, &resp.Diagnostics),
		ExpiresAt: strPtr(m.ExpiresAt),
	}
	var out client.APIKeyCreated
	if _, err := r.data.client.Do(ctx, http.MethodPost, "/v1/api-keys", body, &out); err != nil {
		resp.Diagnostics.AddError("Could not create API key", err.Error())
		return
	}
	m.ID = types.StringValue(out.Key.ID)
	m.Token = types.StringValue(out.Token)
	m.Prefix = types.StringValue(out.Key.Prefix)
	m.OwnerID = types.StringValue(out.Key.OwnerID)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *apiKeyResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m apiKeyModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out client.APIKey
	_, err := r.data.client.Do(ctx, http.MethodGet, client.Path("v1", "api-keys", m.ID.ValueString()), nil, &out)
	if client.IsNotFound(err) || (err == nil && out.RevokedAt != nil) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		resp.Diagnostics.AddError("Could not read API key", err.Error())
		return
	}
	m.Name = types.StringValue(out.Name)
	m.Scopes = listOf(out.Scopes)
	m.Namespaces = listOf(out.Namespaces)
	m.Prefix = types.StringValue(out.Prefix)
	m.OwnerID = types.StringValue(out.OwnerID)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *apiKeyResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m apiKeyModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *apiKeyResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m apiKeyModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if _, err := r.data.client.Do(ctx, http.MethodDelete, client.Path("v1", "api-keys", m.ID.ValueString()), nil, nil); err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Could not revoke API key", err.Error())
	}
}
