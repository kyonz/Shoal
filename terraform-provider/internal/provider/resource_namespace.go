package provider

import (
	"context"
	"net/http"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/kyonz/ai-workflow-manager/terraform-provider/internal/client"
)

type namespaceResource struct{ data *providerData }

type namespaceModel struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	DisplayName types.String `tfsdk:"display_name"`
	Description types.String `tfsdk:"description"`
	CreatedAt   types.String `tfsdk:"created_at"`
}

var (
	_ resource.ResourceWithConfigure   = (*namespaceResource)(nil)
	_ resource.ResourceWithImportState = (*namespaceResource)(nil)
)

func NewNamespaceResource() resource.Resource { return &namespaceResource{} }

func (r *namespaceResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_namespace"
}

func (r *namespaceResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "A namespace inside the tenant. Workflows, agents, connections, secrets and runs live in namespaces, and people get access through role bindings on them. Maps to `/v1/namespaces/{name}`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, MarkdownDescription: "Same as `name`.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name": schema.StringAttribute{Required: true, MarkdownDescription: "Namespace name. Changing it recreates the namespace.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}, Validators: []validatorString{nameValidator()}},
			"display_name": schema.StringAttribute{Optional: true, Computed: true, MarkdownDescription: "Name shown in the UI. Defaults to `name`.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"description": schema.StringAttribute{Optional: true, MarkdownDescription: "What the namespace is for."},
			"created_at": schema.StringAttribute{Computed: true, MarkdownDescription: "Creation time (RFC 3339).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
		},
	}
}

func (r *namespaceResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.data = configureData(req.ProviderData, &resp.Diagnostics)
}

func (r *namespaceResource) save(ctx context.Context, m *namespaceModel, create bool) error {
	body := client.Namespace{DisplayName: strPtr(m.DisplayName), Description: strPtr(m.Description)}
	if !create && body.Description == nil {
		empty := ""
		body.Description = &empty // clear it
	}
	var out client.Namespace
	if _, err := r.data.client.Do(ctx, http.MethodPut, client.Path("v1", "namespaces", m.Name.ValueString()), body, &out); err != nil {
		return err
	}
	r.apply(m, &out)
	return nil
}

func (r *namespaceResource) apply(m *namespaceModel, out *client.Namespace) {
	m.ID = types.StringValue(out.Name)
	m.Name = types.StringValue(out.Name)
	m.DisplayName = strValue(out.DisplayName)
	m.Description = optStr(m.Description, out.Description)
	m.CreatedAt = types.StringValue(out.CreatedAt)
}

func (r *namespaceResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m namespaceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if _, err := r.data.client.Do(ctx, http.MethodGet, client.Path("v1", "namespaces", m.Name.ValueString()), nil, nil); err == nil {
		resp.Diagnostics.AddError("Namespace already exists", "Import it instead: terraform import shoal_namespace.<name> "+m.Name.ValueString())
		return
	}
	if err := r.save(ctx, &m, true); err != nil {
		resp.Diagnostics.AddError("Could not create namespace", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *namespaceResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m namespaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out client.Namespace
	_, err := r.data.client.Do(ctx, http.MethodGet, client.Path("v1", "namespaces", m.Name.ValueString()), nil, &out)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		resp.Diagnostics.AddError("Could not read namespace", err.Error())
		return
	}
	r.apply(&m, &out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *namespaceResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m namespaceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.save(ctx, &m, false); err != nil {
		resp.Diagnostics.AddError("Could not update namespace", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *namespaceResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m namespaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	_, err := r.data.client.Do(ctx, http.MethodDelete, client.Path("v1", "namespaces", m.Name.ValueString()), nil, nil)
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Could not delete namespace", err.Error())
	}
}

func (r *namespaceResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("name"), req.ID)...)
}
