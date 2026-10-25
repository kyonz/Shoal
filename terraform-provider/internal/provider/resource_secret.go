package provider

import (
	"context"
	"net/http"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/kyonz/ai-workflow-manager/terraform-provider/internal/client"
)

type secretResource struct{ data *providerData }

type secretModel struct {
	ID        types.String `tfsdk:"id"`
	Namespace types.String `tfsdk:"namespace"`
	Name      types.String `tfsdk:"name"`
	Value     types.String `tfsdk:"value"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}

var (
	_ resource.ResourceWithConfigure   = (*secretResource)(nil)
	_ resource.ResourceWithImportState = (*secretResource)(nil)
	_ resource.ResourceWithModifyPlan  = (*secretResource)(nil)
)

func NewSecretResource() resource.Resource { return &secretResource{} }

func (r *secretResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_secret"
}

func (r *secretResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "A secret in a namespace, encrypted at rest. Connections, agents and gateways refer to secrets by name. " +
			"The API never returns the value, so changes made outside Terraform are not detected. Maps to `/v1/namespaces/{namespace}/secrets/{name}`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, MarkdownDescription: "`<namespace>/<name>`.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"namespace": schema.StringAttribute{Optional: true, Computed: true,
				MarkdownDescription: "Namespace. Defaults to the provider's `namespace`. Secrets are never read across namespaces (not even from `shared`)."},
			"name": schema.StringAttribute{Required: true, MarkdownDescription: "Secret name.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"value":      schema.StringAttribute{Required: true, Sensitive: true, MarkdownDescription: "The secret value (write-only on the server)."},
			"updated_at": schema.StringAttribute{Computed: true, MarkdownDescription: "When the value was last written."},
		},
	}
}

func (r *secretResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.data = configureData(req.ProviderData, &resp.Diagnostics)
}

func (r *secretResource) ModifyPlan(ctx context.Context, req resource.ModifyPlanRequest, resp *resource.ModifyPlanResponse) {
	defaultNamespace(ctx, r.data, req, resp)
}

func (r *secretResource) path(m *secretModel) string {
	return client.Path("v1", "namespaces", m.Namespace.ValueString(), "secrets", m.Name.ValueString())
}

func (r *secretResource) write(ctx context.Context, m *secretModel) error {
	var out client.Secret
	if _, err := r.data.client.Do(ctx, http.MethodPut, r.path(m), map[string]string{"value": m.Value.ValueString()}, &out); err != nil {
		return err
	}
	m.ID = types.StringValue(out.Namespace + "/" + out.Name)
	m.UpdatedAt = types.StringValue(out.UpdatedAt)
	return nil
}

func (r *secretResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m secretModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.write(ctx, &m); err != nil {
		resp.Diagnostics.AddError("Could not write secret", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *secretResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m secretModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out client.Secret
	_, err := r.data.client.Do(ctx, http.MethodGet, r.path(&m), nil, &out)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		resp.Diagnostics.AddError("Could not read secret", err.Error())
		return
	}
	m.ID = types.StringValue(out.Namespace + "/" + out.Name)
	m.UpdatedAt = types.StringValue(out.UpdatedAt)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *secretResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m secretModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.write(ctx, &m); err != nil {
		resp.Diagnostics.AddError("Could not write secret", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *secretResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m secretModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if _, err := r.data.client.Do(ctx, http.MethodDelete, r.path(&m), nil, nil); err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Could not delete secret", err.Error())
	}
}

// ImportState takes "<namespace>/<name>"; the value must then be set in configuration.
func (r *secretResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	ns, name, ok := strings.Cut(req.ID, "/")
	if !ok {
		ns, name = r.data.namespace, req.ID
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("namespace"), ns)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("name"), name)...)
}

// defaultNamespace fills a namespace attribute left unset from the provider's
// namespace and replaces the resource when it changes.
func defaultNamespace(ctx context.Context, data *providerData, req resource.ModifyPlanRequest, resp *resource.ModifyPlanResponse) {
	if req.Plan.Raw.IsNull() || data == nil {
		return
	}
	var planNS, configNS types.String
	resp.Diagnostics.Append(req.Plan.GetAttribute(ctx, path.Root("namespace"), &planNS)...)
	resp.Diagnostics.Append(req.Config.GetAttribute(ctx, path.Root("namespace"), &configNS)...)
	if configNS.IsNull() {
		planNS = types.StringValue(data.namespace)
		if !req.State.Raw.IsNull() {
			var stateNS types.String
			resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("namespace"), &stateNS)...)
			if !stateNS.IsNull() {
				planNS = stateNS // keep where it is unless the configuration says otherwise
			}
		}
		resp.Diagnostics.Append(resp.Plan.SetAttribute(ctx, path.Root("namespace"), planNS)...)
	}
	if req.State.Raw.IsNull() || planNS.IsUnknown() {
		return
	}
	var stateNS types.String
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("namespace"), &stateNS)...)
	if !stateNS.Equal(planNS) {
		resp.RequiresReplace = append(resp.RequiresReplace, path.Root("namespace"))
	}
}
