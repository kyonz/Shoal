import { useSchemas } from "@/api/hooks";
import { ErrorNotice, Spinner } from "@/components/ui";
import { slugify } from "../registry";
import { FieldsTable, makeLookup, SectionAnchor } from "./SchemaFields";

export default function ResourceSchemas() {
  const { data, error, isLoading } = useSchemas();
  if (isLoading) return <Spinner />;
  if (error) return <ErrorNotice error={error} />;
  const kinds = Object.entries(data ?? {}).sort(([, a], [, b]) => a.kind.localeCompare(b.kind));
  return (
    <div>
      <p className="mb-4 text-[13px] leading-relaxed text-muted">
        Every resource uses the same envelope: <code className="font-mono text-[12px]">apiVersion: awm.dev/v1</code>,{" "}
        <code className="font-mono text-[12px]">kind</code>, <code className="font-mono text-[12px]">metadata</code> (name, description, labels,
        annotations) and the <code className="font-mono text-[12px]">spec</code> described below. Namespaced kinds live at{" "}
        <code className="font-mono text-[12px]">/v1/namespaces/{"{ns}"}/{"{plural}"}/{"{name}"}</code>; tenant kinds at{" "}
        <code className="font-mono text-[12px]">/v1/{"{plural}"}/{"{name}"}</code>.
      </p>
      <div className="mb-2 flex flex-wrap gap-1.5">
        {kinds.map(([plural, k]) => (
          <a key={plural} href={`#${slugify(k.kind)}`} className="rounded-md border border-border bg-panel px-2 py-0.5 font-mono text-[12px] text-muted hover:border-accent/50 hover:text-accent">
            {k.kind}
          </a>
        ))}
      </div>
      {kinds.map(([plural, k]) => {
        const lookup = makeLookup(k.spec_schema);
        const path = k.scope === "tenant" ? `/v1/${plural}/{name}` : `/v1/namespaces/{ns}/${plural}/{name}`;
        return (
          <section key={plural}>
            <SectionAnchor id={slugify(k.kind)} label={k.kind} sub={<span className="font-mono">{path}</span>}>
              {k.kind}
              <span className="rounded-md border border-border bg-subtle px-1.5 py-0.5 text-[11px] font-medium tracking-normal text-muted">
                {k.scope === "tenant" ? "tenant-wide" : "namespaced"}
              </span>
            </SectionAnchor>
            {k.spec_schema.description && <p className="mb-3 text-[13px] text-muted">{k.spec_schema.description}</p>}
            <FieldsTable schema={k.spec_schema} lookup={lookup} />
          </section>
        );
      })}
    </div>
  );
}
