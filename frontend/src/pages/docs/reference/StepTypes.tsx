import { useStepSchema, type JsonSchema } from "@/api/hooks";
import { ErrorNotice, Spinner } from "@/components/ui";
import { deref, FieldsTable, makeLookup, SectionAnchor } from "./SchemaFields";

const COMMON = ["id", "name", "needs", "if", "retry", "timeout", "on_error"];

export default function StepTypes() {
  const { data, error, isLoading } = useStepSchema();
  if (isLoading) return <Spinner />;
  if (error) return <ErrorNotice error={error} />;
  const root = data ?? {};
  const lookup = makeLookup(root);
  const variants = (root.oneOf ?? root.anyOf ?? []).map((v) => deref(v, lookup).schema);
  const typeOf = (s: JsonSchema) => {
    const t = s.properties?.type;
    return String(t?.const ?? t?.default ?? t?.enum?.[0] ?? s.title ?? "?");
  };
  const first = variants[0];
  const commonSchema: JsonSchema | undefined = first && {
    type: "object",
    properties: Object.fromEntries(Object.entries(first.properties ?? {}).filter(([k]) => COMMON.includes(k))),
    required: (first.required ?? []).filter((k) => COMMON.includes(k)),
  };
  return (
    <div>
      <p className="mb-4 text-[13px] leading-relaxed text-muted">
        A workflow's <code className="font-mono text-[12px]">spec.steps</code> is a list of steps discriminated by{" "}
        <code className="font-mono text-[12px]">type</code>. Steps without <code className="font-mono text-[12px]">needs</code> run after the
        previous step in the list; plugin step types appear here automatically.
      </p>
      <div className="mb-2 flex flex-wrap gap-1.5">
        {variants.map((v) => (
          <a key={typeOf(v)} href={`#step-${typeOf(v)}`} className="rounded-md border border-border bg-panel px-2 py-0.5 font-mono text-[12px] text-muted hover:border-accent/50 hover:text-accent">
            {typeOf(v)}
          </a>
        ))}
      </div>
      {commonSchema && (
        <section>
          <SectionAnchor id="common-fields">Common fields</SectionAnchor>
          <p className="mb-3 text-[13px] text-muted">Every step type accepts these.</p>
          <FieldsTable schema={commonSchema} lookup={lookup} />
        </section>
      )}
      {variants.map((v) => (
        <section key={typeOf(v)}>
          <SectionAnchor id={`step-${typeOf(v)}`} sub={v.title} label={typeOf(v)}>
            <code className="font-mono">{typeOf(v)}</code>
          </SectionAnchor>
          {v.description && <p className="mb-3 text-[13px] text-muted">{v.description}</p>}
          <FieldsTable schema={v} lookup={lookup} exclude={["type", ...COMMON]} />
        </section>
      ))}
    </div>
  );
}
