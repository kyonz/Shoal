package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type validatorString = validator.String

var namePattern = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)

type nameCheck struct{}

func nameValidator() validator.String { return nameCheck{} }

func (nameCheck) Description(context.Context) string {
	return "lowercase letters, digits and '-', 1-63 characters, starting and ending with a letter or digit"
}

func (v nameCheck) MarkdownDescription(ctx context.Context) string { return v.Description(ctx) }

func (v nameCheck) ValidateString(ctx context.Context, req validator.StringRequest, resp *validator.StringResponse) {
	if req.ConfigValue.IsNull() || req.ConfigValue.IsUnknown() {
		return
	}
	if !namePattern.MatchString(req.ConfigValue.ValueString()) {
		resp.Diagnostics.AddAttributeError(req.Path, "Invalid name",
			fmt.Sprintf("%q must be %s.", req.ConfigValue.ValueString(), v.Description(ctx)))
	}
}

type oneOfCheck struct{ values []string }

func oneOf(values ...string) validator.String { return oneOfCheck{values} }

func (v oneOfCheck) Description(context.Context) string { return fmt.Sprintf("one of %q", v.values) }

func (v oneOfCheck) MarkdownDescription(ctx context.Context) string { return v.Description(ctx) }

func (v oneOfCheck) ValidateString(_ context.Context, req validator.StringRequest, resp *validator.StringResponse) {
	if req.ConfigValue.IsNull() || req.ConfigValue.IsUnknown() {
		return
	}
	for _, allowed := range v.values {
		if req.ConfigValue.ValueString() == allowed {
			return
		}
	}
	resp.Diagnostics.AddAttributeError(req.Path, "Invalid value", fmt.Sprintf("must be one of %q", v.values))
}

// strPtr returns nil for null/unknown strings.
func strPtr(v types.String) *string {
	if v.IsNull() || v.IsUnknown() {
		return nil
	}
	s := v.ValueString()
	return &s
}

// strValue maps a nil pointer to null.
func strValue(p *string) types.String {
	if p == nil {
		return types.StringNull()
	}
	return types.StringValue(*p)
}

// optStr keeps a null in state when the server returns the default empty value
// for a field the configuration left unset.
func optStr(prior types.String, p *string) types.String {
	if p == nil || (*p == "" && prior.IsNull()) {
		return types.StringNull()
	}
	return types.StringValue(*p)
}

// listStrings reads a list or set of strings; null and unknown read as empty.
func listStrings(ctx context.Context, v interface {
	IsNull() bool
	IsUnknown() bool
	ElementsAs(context.Context, any, bool) diag.Diagnostics
}, diags *diag.Diagnostics) []string {
	out := []string{}
	if v.IsNull() || v.IsUnknown() {
		return out
	}
	diags.Append(v.ElementsAs(ctx, &out, false)...)
	return out
}

func setOf(items []string) types.Set {
	values := make([]attr.Value, 0, len(items))
	for _, s := range items {
		values = append(values, types.StringValue(s))
	}
	return types.SetValueMust(types.StringType, values)
}

func listOf(items []string) types.List {
	values := make([]attr.Value, 0, len(items))
	for _, s := range items {
		values = append(values, types.StringValue(s))
	}
	return types.ListValueMust(types.StringType, values)
}

// diagCollector gathers diagnostics in helpers that return plain errors.
type diagCollector struct{ d diag.Diagnostics }

func (c *diagCollector) err() error {
	if c.d.HasError() {
		for _, e := range c.d.Errors() {
			return fmt.Errorf("%s: %s", e.Summary(), e.Detail())
		}
	}
	return nil
}

func decode(raw []byte, out any) error { return json.Unmarshal(raw, out) }
