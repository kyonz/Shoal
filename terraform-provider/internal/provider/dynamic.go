package provider

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math/big"
	"reflect"
	"sort"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-framework/types/basetypes"
)

// Specs are Terraform dynamic values so they are written as plain HCL objects
// (or yamldecode(file(...))) and sent to the API as JSON.

// dynamicToJSON converts a known dynamic value into JSON-ready Go values.
// Null object members are dropped (the API treats omitted and null alike).
func dynamicToJSON(v attr.Value) (any, error) {
	if v == nil || v.IsNull() {
		return nil, nil
	}
	if v.IsUnknown() {
		return nil, fmt.Errorf("value is not known yet")
	}
	switch val := v.(type) {
	case basetypes.DynamicValue:
		return dynamicToJSON(val.UnderlyingValue())
	case basetypes.StringValue:
		return val.ValueString(), nil
	case basetypes.BoolValue:
		return val.ValueBool(), nil
	case basetypes.NumberValue:
		return numberJSON(val.ValueBigFloat()), nil
	case basetypes.Int64Value:
		return val.ValueInt64(), nil
	case basetypes.Float64Value:
		return val.ValueFloat64(), nil
	case basetypes.ObjectValue:
		return membersJSON(val.Attributes())
	case basetypes.MapValue:
		return membersJSON(val.Elements())
	case basetypes.TupleValue:
		return elementsJSON(val.Elements())
	case basetypes.ListValue:
		return elementsJSON(val.Elements())
	case basetypes.SetValue:
		return elementsJSON(val.Elements())
	default:
		return nil, fmt.Errorf("unsupported value type %T", v)
	}
}

func membersJSON(members map[string]attr.Value) (map[string]any, error) {
	out := make(map[string]any, len(members))
	for k, m := range members {
		if m == nil || m.IsNull() {
			continue
		}
		j, err := dynamicToJSON(m)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", k, err)
		}
		out[k] = j
	}
	return out, nil
}

func elementsJSON(elements []attr.Value) ([]any, error) {
	out := make([]any, 0, len(elements))
	for i, e := range elements {
		j, err := dynamicToJSON(e)
		if err != nil {
			return nil, fmt.Errorf("[%d]: %w", i, err)
		}
		out = append(out, j)
	}
	return out, nil
}

func numberJSON(f *big.Float) json.Number {
	if f.IsInt() {
		i, _ := f.Int(nil)
		return json.Number(i.String())
	}
	return json.Number(f.Text('g', -1))
}

// jsonToDynamic converts API JSON into a dynamic value made of objects and
// tuples, the same types HCL literals produce, so equal values compare equal.
func jsonToDynamic(raw json.RawMessage) (types.Dynamic, error) {
	var data any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&data); err != nil {
		return types.DynamicNull(), err
	}
	v, err := goToValue(data)
	if err != nil {
		return types.DynamicNull(), err
	}
	return types.DynamicValue(v), nil
}

func goToValue(data any) (attr.Value, error) {
	switch d := data.(type) {
	case nil:
		return types.DynamicNull(), nil
	case string:
		return types.StringValue(d), nil
	case bool:
		return types.BoolValue(d), nil
	case json.Number:
		f, _, err := big.ParseFloat(string(d), 10, 512, big.ToNearestEven)
		if err != nil {
			return nil, err
		}
		return types.NumberValue(f), nil
	case map[string]any:
		keys := make([]string, 0, len(d))
		for k, v := range d {
			if v != nil {
				keys = append(keys, k)
			}
		}
		sort.Strings(keys)
		attrTypes := make(map[string]attr.Type, len(keys))
		values := make(map[string]attr.Value, len(keys))
		for _, k := range keys {
			v, err := goToValue(d[k])
			if err != nil {
				return nil, err
			}
			attrTypes[k] = v.Type(nil)
			values[k] = v
		}
		obj, diags := types.ObjectValue(attrTypes, values)
		if diags.HasError() {
			return nil, fmt.Errorf("building object: %v", diags)
		}
		return obj, nil
	case []any:
		elemTypes := make([]attr.Type, 0, len(d))
		elems := make([]attr.Value, 0, len(d))
		for _, item := range d {
			v, err := goToValue(item)
			if err != nil {
				return nil, err
			}
			elemTypes = append(elemTypes, v.Type(nil))
			elems = append(elems, v)
		}
		tuple, diags := types.TupleValue(elemTypes, elems)
		if diags.HasError() {
			return nil, fmt.Errorf("building tuple: %v", diags)
		}
		return tuple, nil
	default:
		return nil, fmt.Errorf("unsupported JSON value %T", data)
	}
}

// sameJSON reports whether a and b are the same JSON document, ignoring key
// order, null members and number spelling (15 vs 15.0).
func sameJSON(a, b any) bool {
	return reflect.DeepEqual(canonical(a), canonical(b))
}

func canonical(v any) any {
	data, err := json.Marshal(v)
	if err != nil {
		return v
	}
	var out any
	if json.Unmarshal(data, &out) != nil {
		return v
	}
	return stripNulls(out)
}

func stripNulls(v any) any {
	switch d := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(d))
		for k, item := range d {
			if item != nil {
				out[k] = stripNulls(item)
			}
		}
		return out
	case []any:
		out := make([]any, len(d))
		for i, item := range d {
			out[i] = stripNulls(item)
		}
		return out
	default:
		return v
	}
}
