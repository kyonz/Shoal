package provider

import (
	"context"
	"net/http"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/kyonz/ai-workflow-manager/terraform-provider/internal/client"
)

type federationLink struct {
	ID            string   `json:"id"`
	Tenant        string   `json:"tenant"`
	Status        string   `json:"status"`
	Connected     bool     `json:"connected"`
	RequestedMode string   `json:"requested_mode"`
	MemberMode    *string  `json:"member_mode"`
	EffectiveMode string   `json:"effective_mode"`
	Namespaces    []string `json:"namespaces"`
	Visibility    string   `json:"visibility"`
	LastSeenAt    *string  `json:"last_seen_at"`
}

type federationLinkCreated struct {
	Link                federationLink `json:"link"`
	EnrollmentToken     string         `json:"enrollment_token"`
	EnrollmentExpiresAt string         `json:"enrollment_expires_at"`
}

type federationLinkResource struct{ data *providerData }

type federationLinkModel struct {
	ID                  types.String `tfsdk:"id"`
	Tenant              types.String `tfsdk:"tenant"`
	Mode                types.String `tfsdk:"mode"`
	Status              types.String `tfsdk:"status"`
	Connected           types.Bool   `tfsdk:"connected"`
	MemberMode          types.String `tfsdk:"member_mode"`
	EffectiveMode       types.String `tfsdk:"effective_mode"`
	Namespaces          types.List   `tfsdk:"namespaces"`
	Visibility          types.String `tfsdk:"visibility"`
	LastSeenAt          types.String `tfsdk:"last_seen_at"`
	EnrollmentToken     types.String `tfsdk:"enrollment_token"`
	EnrollmentExpiresAt types.String `tfsdk:"enrollment_expires_at"`
}

var (
	_ resource.ResourceWithConfigure   = (*federationLinkResource)(nil)
	_ resource.ResourceWithImportState = (*federationLinkResource)(nil)
)

func NewFederationLinkResource() resource.Resource { return &federationLinkResource{} }

func (r *federationLinkResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_federation_link"
}

func (r *federationLinkResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Federates a tenant: its data lives in a member deployment that the hub reaches through the member's tenant connector. " +
			"Operator only (the provider's `api_key` must be the admin token or a key with `system:admin`). " +
			"Give `enrollment_token` to the member's admin for `shoal federation join`. Destroying the resource revokes the link; the member keeps running on its own. " +
			"Maps to `/v1/federation/links/{tenant}`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, MarkdownDescription: "Link ID (`fed_...`).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"tenant": schema.StringAttribute{Required: true, Validators: []validatorString{nameValidator()},
				MarkdownDescription: "Slug of the tenant to federate. It must be empty: its data lives in the member deployment.",
				PlanModifiers:       []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"mode": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("read_write"),
				Validators:          []validatorString{oneOf("read", "read_write")},
				MarkdownDescription: "Access the hub asks for: `read` or `read_write` (default). The member's ceiling can lower it; see `effective_mode`."},
			"status":         schema.StringAttribute{Computed: true, MarkdownDescription: "`pending` (waiting for enrollment) or `active`."},
			"connected":      schema.BoolAttribute{Computed: true, MarkdownDescription: "Whether the member's connector polled recently."},
			"member_mode":    schema.StringAttribute{Computed: true, MarkdownDescription: "The member's ceiling, once enrolled."},
			"effective_mode": schema.StringAttribute{Computed: true, MarkdownDescription: "The lower of `mode` and `member_mode`; `read` until the member enrolls."},
			"namespaces": schema.ListAttribute{Computed: true, ElementType: types.StringType,
				MarkdownDescription: "Namespaces the member exposes (empty: all)."},
			"visibility":   schema.StringAttribute{Computed: true, MarkdownDescription: "`full`, or `metadata` when the member hides run data."},
			"last_seen_at": schema.StringAttribute{Computed: true, MarkdownDescription: "When the member's connector last polled."},
			"enrollment_token": schema.StringAttribute{Computed: true, Sensitive: true,
				MarkdownDescription: "One-time enrollment token, set on create. Replace the resource to issue a new one.",
				PlanModifiers:       []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"enrollment_expires_at": schema.StringAttribute{Computed: true, MarkdownDescription: "When `enrollment_token` expires.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
		},
	}
}

func (r *federationLinkResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.data = configureData(req.ProviderData, &resp.Diagnostics)
}

func (r *federationLinkResource) path(tenant string) string {
	return client.Path("v1", "federation", "links", tenant)
}

func (m *federationLinkModel) fill(link federationLink) {
	m.ID = types.StringValue(link.ID)
	m.Tenant = types.StringValue(link.Tenant)
	m.Mode = types.StringValue(link.RequestedMode)
	m.Status = types.StringValue(link.Status)
	m.Connected = types.BoolValue(link.Connected)
	m.MemberMode = strValue(link.MemberMode)
	m.EffectiveMode = types.StringValue(link.EffectiveMode)
	m.Namespaces = listOf(link.Namespaces)
	m.Visibility = types.StringValue(link.Visibility)
	m.LastSeenAt = strValue(link.LastSeenAt)
}

func (r *federationLinkResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m federationLinkModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out federationLinkCreated
	body := map[string]string{"tenant": m.Tenant.ValueString(), "mode": m.Mode.ValueString()}
	if _, err := r.data.client.Do(ctx, http.MethodPost, client.Path("v1", "federation", "links"), body, &out); err != nil {
		resp.Diagnostics.AddError("Could not federate tenant", err.Error())
		return
	}
	m.fill(out.Link)
	m.EnrollmentToken = types.StringValue(out.EnrollmentToken)
	m.EnrollmentExpiresAt = types.StringValue(out.EnrollmentExpiresAt)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *federationLinkResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m federationLinkModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out federationLink
	_, err := r.data.client.Do(ctx, http.MethodGet, r.path(m.Tenant.ValueString()), nil, &out)
	if client.IsNotFound(err) || (err == nil && out.Status == "revoked") {
		resp.State.RemoveResource(ctx) // revoked outside Terraform: the next apply federates it again
		return
	}
	if err != nil {
		resp.Diagnostics.AddError("Could not read federation link", err.Error())
		return
	}
	m.fill(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *federationLinkResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m federationLinkModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out federationLink
	if _, err := r.data.client.Do(ctx, http.MethodPatch, r.path(m.Tenant.ValueString()), map[string]string{"mode": m.Mode.ValueString()}, &out); err != nil {
		resp.Diagnostics.AddError("Could not update federation link", err.Error())
		return
	}
	m.fill(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *federationLinkResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m federationLinkModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if _, err := r.data.client.Do(ctx, http.MethodDelete, r.path(m.Tenant.ValueString()), nil, nil); err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Could not revoke federation link", err.Error())
	}
}

// ImportState takes the tenant slug. The enrollment token is not returned by the API, so it stays null after import.
func (r *federationLinkResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("tenant"), req.ID)...)
}
