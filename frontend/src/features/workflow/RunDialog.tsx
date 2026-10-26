import { Play } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { ApiError, client, unwrap } from "@/api/client";
import { nsPath } from "@/lib/ns";
import { Button, Dialog, ErrorNotice, Field, Input, NativeSelect, Switch } from "@/components/ui";
import { JsonField } from "@/components/forms/SchemaForm";
import type { ParamDoc } from "./types";

function initial(inputs: Record<string, ParamDoc>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(inputs).map(([k, p]) => [k, p?.default ?? (p?.type === "boolean" ? false : undefined)]));
}

/** "Run workflow" with a form generated from the workflow's declared inputs. */
export function RunDialog({ open, onOpenChange, ns, workflow, inputs, version }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  ns: string;
  workflow: string;
  inputs: Record<string, ParamDoc>;
  version?: number;
}) {
  const navigate = useNavigate();
  const [values, setValues] = useState<Record<string, unknown>>(() => initial(inputs));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (open) {
      setValues(initial(inputs));
      setError(null);
    }
  }, [open, inputs]);

  const missing = Object.entries(inputs).filter(([k, p]) => (p?.required ?? p?.default == null) && (values[k] == null || values[k] === ""));

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const clean = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== undefined && v !== ""));
      const run = await unwrap(
        client.POST("/v1/namespaces/{namespace}/workflows/{name}/runs", {
          params: { path: { namespace: ns, name: workflow } },
          body: { inputs: clean, version: version ?? null, labels: {} },
        }),
      );
      toast.success("Run started", { description: `${workflow} · ${run.id.slice(-8)}` });
      onOpenChange(false);
      navigate(nsPath(ns, "runs", run.id));
    } catch (e) {
      setError(e instanceof ApiError ? e : new Error(String(e)));
    } finally {
      setBusy(false);
    }
  };

  const entries = Object.entries(inputs);
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Run ${workflow}`}
      description={entries.length ? "Fill in the inputs for this run." : "This workflow takes no inputs."}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} disabled={missing.length > 0} onClick={() => void start()} icon={<Play className="size-3.5" />}>
            Start run
          </Button>
        </>
      }
    >
      <form
        className="space-y-3.5"
        onSubmit={(e) => {
          e.preventDefault();
          if (!missing.length) void start();
        }}
      >
        {entries.map(([k, param], i) => {
          const p: ParamDoc = param ?? {};
          const required = p.required ?? p.default == null;
          const v = values[k];
          const set = (nv: unknown) => setValues((cur) => ({ ...cur, [k]: nv }));
          let control: React.ReactNode;
          if (p.enum?.length) {
            control = (
              <NativeSelect value={v == null ? "" : String(v)} onChange={(e) => set(p.enum!.find((o) => String(o) === e.target.value) ?? e.target.value)}>
                {!required && <option value="">—</option>}
                {p.enum.map((o) => (
                  <option key={String(o)}>{String(o)}</option>
                ))}
              </NativeSelect>
            );
          } else if (p.type === "boolean") {
            control = <Switch checked={!!v} onCheckedChange={set} label={<span className="text-xs text-muted">{v ? "true" : "false"}</span>} />;
          } else if (p.type === "integer" || p.type === "number") {
            control = <Input type="number" value={v == null ? "" : String(v)} onChange={(e) => set(e.target.value === "" ? undefined : Number(e.target.value))} />;
          } else if (p.type === "object" || p.type === "array" || p.type === "any") {
            control = <JsonField value={v} onChange={set} />;
          } else {
            control = <Input autoFocus={i === 0} value={v == null ? "" : String(v)} onChange={(e) => set(e.target.value)} />;
          }
          return (
            <Field key={k} label={<span className="font-mono">{k}</span>} hint={p.type ?? "string"} required={required} description={p.description}>
              {control}
            </Field>
          );
        })}
        <button type="submit" hidden />
        {error ? <ErrorNotice error={error} /> : null}
      </form>
    </Dialog>
  );
}
