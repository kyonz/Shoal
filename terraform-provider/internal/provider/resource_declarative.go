package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/mapdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/kyonz/ai-workflow-manager/terraform-provider/internal/client"
)

// declarativeKind describes one declarative resource kind of the API.
type declarativeKind struct {
	TypeName    string // shoal_<TypeName>
	Kind        string // API kind
	Plural      string // URL segment
	TenantWide  bool   // stored in the tenant's "shared" namespace
	Description string
	Example     string // spec example used in the docs
}

var declarativeKinds = []declarativeKind{
	{"workflow", "Workflow", "workflows", false, "A workflow: typed inputs, a graph of steps (agents, integration actions, approvals, waits, scripts, sub-workflows) and outputs.", ""},
	{"agent", "Agent", "agents", false, "An agent: model, instructions, skills, tools, subagents, credentials, sandbox and limits.", ""},
	{"skill", "Skill", "skills", false, "A skill: reusable instructions and files that agents load on demand.", ""},
	{"connection", "Connection", "connections", false, "A configured integration (GitHub, Jira, webhook, or any installed plugin). `status.webhook_url` is the URL to register with the external system.", ""},
	{"trigger", "Trigger", "triggers", false, "A trigger that starts a workflow from an integration event, a schedule, or a poll, with optional filters and triage.", ""},
	{"campaign", "Campaign", "campaigns", false, "A campaign: a workflow fanned out over many targets with concurrency, rate and failure policies.", ""},
	{"sandbox_profile", "SandboxProfile", "sandbox-profiles", false, "A sandbox profile: backend (local, docker, kubernetes, e2b, space, remote), image, resources, network policy and TTL.", ""},
	{"gateway", "Gateway", "gateways", true, "An LLM gateway: how model requests are sent (provider, base URL, headers). Tenant-wide and managed by tenant admins (`gateways:manage`); credentials are secrets in the `shared` namespace (`api_key_secret`, `secret_headers`), never clear text.", ""},
	{"model_policy", "ModelPolicy", "model-policies", true, "A model policy: which model serves which agent or workflow, with fallbacks and weighted routing. Tenant-wide and managed by tenant admins (`model-policies:manage`).", ""},
	{"eval", "Eval", "evals", false, "An evaluation suite: cases run against a workflow or agent and graded.", ""},
	{"identity", "Identity", "identities", true, "A person across systems (email, GitHub login, Jira account, Shoal users), used to attribute runs and pull requests to the human who drove them. Tenant-wide.", ""},
	{"blueprint", "Blueprint", "blueprints", true, "A blueprint in the tenant's catalogue: a versioned bundle of templated resources that namespace owners adopt into their namespace. Tenant-wide.", ""},
	{"skill_source", "SkillSource", "skill-sources", true, "A skill source (marketplace): a git repository in the Agent Skills layout, a skill registry, or any installed skill source plugin, from which namespaces import skills. Tenant-wide and managed by tenant admins (`skill-sources:manage`); credentials are secrets in the `shared` namespace, never clear text.", ""},
}

type declarativeModel struct {
	ID          types.String  `tfsdk:"id"`
	Name        types.String  `tfsdk:"name"`
	Namespace   types.String  `tfsdk:"namespace"`
	Description types.String  `tfsdk:"description"`
	Labels      types.Map     `tfsdk:"labels"`
	Spec        types.Dynamic `tfsdk:"spec"`
	Version     types.Int64   `tfsdk:"version"`
	Status      types.Map     `tfsdk:"status"`
}

type declarativeResource struct {
	kind declarativeKind
	data *providerData
}

var (
	_ resource.ResourceWithConfigure   = (*declarativeResource)(nil)
	_ resource.ResourceWithImportState = (*declarativeResource)(nil)
	_ resource.ResourceWithModifyPlan  = (*declarativeResource)(nil)
)

func newDeclarativeResource(kind declarativeKind) func() resource.Resource {
	return func() resource.Resource { return &declarativeResource{kind: kind} }
}

func (r *declarativeResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_" + r.kind.TypeName
}

func (r *declarativeResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	nsDescription := "Namespace. Defaults to the provider's `namespace`. Changing it recreates the resource."
	if r.kind.TenantWide {
		nsDescription = "Always `shared`: " + r.kind.Kind + " resources are tenant-wide."
	}
	resp.Schema = schema.Schema{
		MarkdownDescription: r.kind.Description + "\n\nMaps to `/v1/" + r.apiBase("{namespace}") + "/{name}`. " +
			"`spec` is written as a plain object (HCL, or `yamldecode(file(...))`) and uses the same fields as the `spec` of the YAML resource.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:            true,
				MarkdownDescription: "`<namespace>/<name>`.",
				PlanModifiers:       []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"name": schema.StringAttribute{
				Required:            true,
				MarkdownDescription: "Name, unique per namespace (lowercase letters, digits and `-`). Changing it recreates the resource.",
				PlanModifiers:       []planmodifier.String{stringplanmodifier.RequiresReplace()},
				Validators:          []validatorString{nameValidator()},
			},
			"namespace": schema.StringAttribute{
				Optional:            !r.kind.TenantWide,
				Computed:            true,
				MarkdownDescription: nsDescription,
			},
			"description": schema.StringAttribute{
				Optional:            true,
				MarkdownDescription: "Human description shown in the UI.",
			},
			"labels": schema.MapAttribute{
				ElementType:         types.StringType,
				Optional:            true,
				Computed:            true,
				Default:             mapdefault.StaticValue(types.MapValueMust(types.StringType, map[string]attr.Value{})),
				MarkdownDescription: "Labels for filtering and selection.",
			},
			"spec": schema.DynamicAttribute{
				Required:            true,
				MarkdownDescription: "The " + r.kind.Kind + " spec as an object. See the " + r.kind.Kind + " reference for its fields.",
			},
			"version": schema.Int64Attribute{
				Computed:            true,
				MarkdownDescription: "Server version, incremented on every change.",
			},
			"status": schema.MapAttribute{
				ElementType:         types.StringType,
				Computed:            true,
				MarkdownDescription: "Server-computed details, e.g. `webhook_url` for connections or `next_fire_at` for scheduled triggers.",
			},
		},
	}
}

func (r *declarativeResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.data = configureData(req.ProviderData, &resp.Diagnostics)
}

func (r *declarativeResource) apiBase(namespace string) string {
	if r.kind.TenantWide {
		return r.kind.Plural
	}
	return "namespaces/" + namespace + "/" + r.kind.Plural
}

func (r *declarativeResource) path(namespace, name string) string {
	if r.kind.TenantWide {
		return client.Path("v1", r.kind.Plural, name)
	}
	return client.Path("v1", "namespaces", namespace, r.kind.Plural, name)
}

// ModifyPlan fills in the default namespace and recreates on namespace changes.
func (r *declarativeResource) ModifyPlan(ctx context.Context, req resource.ModifyPlanRequest, resp *resource.ModifyPlanResponse) {
	if r.kind.TenantWide {
		if !req.Plan.Raw.IsNull() {
			resp.Diagnostics.Append(resp.Plan.SetAttribute(ctx, path.Root("namespace"), "shared")...)
		}
		return
	}
	defaultNamespace(ctx, r.data, req, resp)
}

func (r *declarativeResource) body(ctx context.Context, m *declarativeModel) (map[string]any, diag.Diagnostics) {
	var diags diag.Diagnostics
	spec, err := dynamicToJSON(m.Spec)
	if err != nil {
		diags.AddAttributeError(path.Root("spec"), "Invalid spec", err.Error())
		return nil, diags
	}
	if _, ok := spec.(map[string]any); !ok {
		diags.AddAttributeError(path.Root("spec"), "Invalid spec", "spec must be an object")
		return nil, diags
	}
	labels := map[string]string{}
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		diags.Append(m.Labels.ElementsAs(ctx, &labels, false)...)
	}
	metadata := map[string]any{"labels": labels}
	if !m.Description.IsNull() {
		metadata["description"] = m.Description.ValueString()
	}
	return map[string]any{"metadata": metadata, "spec": spec}, diags
}

func (r *declarativeResource) put(ctx context.Context, m *declarativeModel, diags *diag.Diagnostics) {
	body, d := r.body(ctx, m)
	diags.Append(d...)
	if diags.HasError() {
		return
	}
	var out client.Resource
	if _, err := r.data.client.Do(ctx, http.MethodPut, r.path(m.Namespace.ValueString(), m.Name.ValueString()), body, &out); err != nil {
		diags.AddError(fmt.Sprintf("Could not save %s %q", r.kind.Kind, m.Name.ValueString()), err.Error())
		return
	}
	r.apply(ctx, m, &out, true, diags)
}

// apply copies a server response into the model. With keepSpec the planned
// spec is kept (the server returns it as written); otherwise the server's spec
// replaces the state only when it differs in meaning.
func (r *declarativeResource) apply(ctx context.Context, m *declarativeModel, out *client.Resource, keepSpec bool, diags *diag.Diagnostics) {
	m.Namespace = types.StringValue(out.Metadata.Namespace)
	m.ID = types.StringValue(out.Metadata.Namespace + "/" + out.Metadata.Name)
	m.Name = types.StringValue(out.Metadata.Name)
	if out.Metadata.Description != nil {
		m.Description = types.StringValue(*out.Metadata.Description)
	} else {
		m.Description = types.StringNull()
	}
	labels, d := types.MapValueFrom(ctx, types.StringType, nonNilLabels(out.Metadata.Labels))
	diags.Append(d...)
	m.Labels = labels
	m.Version = types.Int64Value(out.Metadata.Version)
	m.Status = statusMap(out.Status)
	if keepSpec {
		return
	}
	var server any
	if err := json.Unmarshal(out.Spec, &server); err != nil {
		diags.AddError("Could not read spec", err.Error())
		return
	}
	if !m.Spec.IsNull() && !m.Spec.IsUnknown() {
		if current, err := dynamicToJSON(m.Spec); err == nil && sameJSON(current, server) {
			return
		}
	}
	spec, err := jsonToDynamic(out.Spec)
	if err != nil {
		diags.AddError("Could not read spec", err.Error())
		return
	}
	m.Spec = spec
}

func nonNilLabels(l map[string]string) map[string]string {
	if l == nil {
		return map[string]string{}
	}
	return l
}

func statusMap(status map[string]any) types.Map {
	values := map[string]attr.Value{}
	for k, v := range status {
		switch val := v.(type) {
		case nil:
			continue
		case string:
			values[k] = types.StringValue(val)
		default:
			b, _ := json.Marshal(val)
			values[k] = types.StringValue(string(b))
		}
	}
	return types.MapValueMust(types.StringType, values)
}

func (r *declarativeResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m declarativeModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if !r.kind.TenantWide {
		// Refuse to silently adopt an object that already exists.
		var existing client.Resource
		_, err := r.data.client.Do(ctx, http.MethodGet, r.path(m.Namespace.ValueString(), m.Name.ValueString()), nil, &existing)
		if err == nil {
			resp.Diagnostics.AddError(fmt.Sprintf("%s %q already exists in namespace %q", r.kind.Kind, m.Name.ValueString(), m.Namespace.ValueString()),
				fmt.Sprintf("Import it instead: terraform import shoal_%s.<name> %s/%s", r.kind.TypeName, m.Namespace.ValueString(), m.Name.ValueString()))
			return
		} else if !client.IsNotFound(err) {
			resp.Diagnostics.AddError("Could not check for an existing "+r.kind.Kind, err.Error())
			return
		}
	}
	r.put(ctx, &m, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *declarativeResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m declarativeModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out client.Resource
	_, err := r.data.client.Do(ctx, http.MethodGet, r.path(m.Namespace.ValueString(), m.Name.ValueString()), nil, &out)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		resp.Diagnostics.AddError(fmt.Sprintf("Could not read %s %q", r.kind.Kind, m.ID.ValueString()), err.Error())
		return
	}
	r.apply(ctx, &m, &out, false, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *declarativeResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m declarativeModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &m, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *declarativeResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m declarativeModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	_, err := r.data.client.Do(ctx, http.MethodDelete, r.path(m.Namespace.ValueString(), m.Name.ValueString()), nil, nil)
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError(fmt.Sprintf("Could not delete %s %q", r.kind.Kind, m.ID.ValueString()), err.Error())
	}
}

// ImportState accepts "<namespace>/<name>" or "<name>" (provider namespace).
func (r *declarativeResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	namespace, name := r.data.namespace, req.ID
	if r.kind.TenantWide {
		namespace = "shared"
		name = strings.TrimPrefix(req.ID, "shared/")
	} else if ns, n, ok := strings.Cut(req.ID, "/"); ok {
		namespace, name = ns, n
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("namespace"), namespace)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("name"), name)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), namespace+"/"+name)...)
}
