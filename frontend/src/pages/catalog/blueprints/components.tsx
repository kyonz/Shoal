import { useQuery } from "@tanstack/react-query";
import { ArrowRight, ArrowUpCircle, Check, ExternalLink, Package, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { rawJson, type Resource } from "@/api/client";
import { useCan } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { relTime } from "@/lib/format";
import { nsPath } from "@/lib/ns";
import { Badge, Button, Dialog, EmptyState, ErrorNotice, Field, Input, NativeSelect, Switch, Table, Td, Th, Tr, useConfirm } from "@/components/ui";
import { SecretPicker } from "../extensions";
import {
  PLURAL_BY_KIND,
  isFeatured,
  paramErrors,
  resourceRef,
  useAdopt,
  useRemoveAdoption,
  useUpdateAdoption,
  type Adoption,
  type AdoptResult,
  type AppliedResource,
  type BlueprintParam,
  type BlueprintSpec,
} from "./api";
import { isMissingEndpoint } from "../evals/api";

const STATUS_TONE = { draft: "neutral", published: "info", featured: "accent", promoted: "accent", deprecated: "warn" } as const;

export function BlueprintStatus({ status }: { status?: string }) {
  const s = (status ?? "draft") as keyof typeof STATUS_TONE;
  return <Badge tone={STATUS_TONE[s] ?? "neutral"}>{s === "promoted" ? "featured" : s}</Badge>;
}

export function BlueprintIcon({ spec, className }: { spec: BlueprintSpec; className?: string }) {
  const icon = spec.icon?.trim();
  const base = cn("flex shrink-0 items-center justify-center overflow-hidden rounded-xl border border-border bg-gradient-to-br from-accent-soft to-info-soft", className ?? "size-11");
  if (icon && /^(https?:|\/|data:)/.test(icon)) return <img src={icon} alt="" className={cn(base, "object-cover")} />;
  if (icon && [...icon].length <= 3) return <div className={cn(base, "text-xl")}>{icon}</div>;
  return (
    <div className={cn(base, "text-sm font-semibold text-accent")}>
      {spec.title?.trim()?.[0]?.toUpperCase() ?? <Package className="size-5" />}
    </div>
  );
}

// --------------------------------------------------------------------------- adopt wizard

function useConnections(ns: string) {
  return useQuery({
    queryKey: ["adopt-connections", ns],
    queryFn: async () => {
      const own = await rawJson<Resource[]>(`/v1/namespaces/${ns}/connections`).catch(() => [] as Resource[]);
      const shared = ns === "shared" ? [] : await rawJson<Resource[]>(`/v1/namespaces/shared/connections`).catch(() => [] as Resource[]);
      return [...own.map((r) => ({ r, shared: false })), ...shared.map((r) => ({ r, shared: true }))];
    },
  });
}

function ParamInput({ name, param, ns, value, onChange, connections }: {
  name: string;
  param: BlueprintParam;
  ns: string;
  value: unknown;
  onChange: (v: unknown) => void;
  connections: { r: Resource; shared: boolean }[];
}) {
  switch (param.type ?? "string") {
    case "connection": {
      const list = connections.filter(({ r }) => !param.integration || r.spec.integration === param.integration);
      return (
        <div>
          <NativeSelect value={(value as string) ?? ""} onChange={(e) => onChange(e.target.value || undefined)}>
            <option value="">{list.length ? `Choose a ${param.integration ?? ""} connection…` : `No ${param.integration ?? ""} connections`}</option>
            {list.map(({ r, shared }) => (
              <option key={`${shared}-${r.metadata.name}`} value={r.metadata.name}>
                {r.metadata.name}
                {shared ? " (shared)" : ""}
              </option>
            ))}
          </NativeSelect>
          {!list.length && (
            <Link to={nsPath(ns, "connections")} className="mt-1 inline-flex items-center gap-1 text-xs text-accent hover:underline">
              Create a {param.integration} connection <ExternalLink className="size-3" />
            </Link>
          )}
        </div>
      );
    }
    case "secret":
      return <SecretPicker ns={ns} value={value} onChange={onChange} />;
    case "choice":
      return (
        <NativeSelect value={value == null ? "" : String(value)} onChange={(e) => onChange(e.target.value || undefined)}>
          <option value="">Choose…</option>
          {(param.options ?? []).map((o) => (
            <option key={o} value={o}>{o}</option>
          ))}
        </NativeSelect>
      );
    case "boolean":
      return <Switch checked={value === undefined ? param.default === true : !!value} onCheckedChange={onChange} label={<span className="text-muted">{value ? "Yes" : "No"}</span>} />;
    case "number":
      return <Input type="number" step="any" value={value == null ? "" : String(value)} placeholder={param.default != null ? String(param.default) : undefined} onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))} />;
    default:
      return <Input value={(value as string) ?? ""} placeholder={param.default != null ? String(param.default) : name} onChange={(e) => onChange(e.target.value || undefined)} />;
  }
}

export function OutcomeList({ items, ns, links }: { items: AppliedResource[]; ns: string; links?: boolean }) {
  const tone = (o: string) => (o === "created" ? "ok" : o === "updated" ? "info" : o === "invalid" || o === "removed" ? "bad" : "neutral");
  return (
    <ul className="divide-y divide-border rounded-lg border border-border">
      {items.map((r) => {
        const plural = PLURAL_BY_KIND[r.kind];
        return (
          <li key={`${r.kind}/${r.name}`} className="flex items-center gap-2 px-3 py-2 text-[13px]">
            <Badge className="w-28 justify-center">{r.kind || "resource"}</Badge>
            {links && plural ? (
              <Link to={nsPath(ns, plural, r.name)} className="font-mono text-xs text-accent hover:underline">{r.name}</Link>
            ) : (
              <span className="font-mono text-xs">{r.name}</span>
            )}
            {r.outcome && <Badge tone={tone(r.outcome)} className="ml-auto">{r.outcome}</Badge>}
          </li>
        );
      })}
    </ul>
  );
}

export function AdoptWizard({ open, onOpenChange, name, spec: s, ns }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  name: string;
  spec: BlueprintSpec;
  ns: string;
}) {
  const params = s.params ?? {};
  const adopt = useAdopt(ns, name);
  const connections = useConnections(ns);
  const [step, setStep] = useState<"params" | "preview" | "done">("params");
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [prefix, setPrefix] = useState("");
  const [preview, setPreview] = useState<AdoptResult | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (!open) return;
    setStep("params");
    setPreview(null);
    setError(null);
    setValues(Object.fromEntries(Object.entries(params).filter(([, p]) => p.default !== undefined && p.default !== null).map(([k, p]) => [k, p.default])));
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const errs = paramErrors(error);
  const missing = Object.entries(params).filter(([k, p]) => p.required !== false && (values[k] === undefined || values[k] === "")).map(([k]) => k);

  const run = async (dry: boolean) => {
    setError(null);
    try {
      const res = await adopt.mutateAsync({ params: values, prefix: prefix.replace(/-+$/, "") || undefined, version: s.version, dry_run: dry });
      setPreview(res);
      setStep(dry ? "preview" : "done");
      if (!dry) toast.success(`Adopted ${s.title} into ${ns}`);
    } catch (e) {
      setError(e);
      if (Object.keys(paramErrors(e)).length) setStep("params");
    }
  };

  const missingApi = isMissingEndpoint(error);
  const steps = ["params", "preview", "done"] as const;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title={`Adopt ${s.title}`}
      description={`Creates the blueprint's resources in ${ns}, wired to your connections. You can customise them afterwards.`}
      footer={
        step === "params" ? (
          <>
            <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button variant="primary" loading={adopt.isPending} disabled={missing.length > 0 || missingApi} onClick={() => void run(true)} icon={<ArrowRight className="size-4" />}>
              Preview changes
            </Button>
          </>
        ) : step === "preview" ? (
          <>
            <Button variant="ghost" onClick={() => setStep("params")}>Back</Button>
            <Button variant="primary" loading={adopt.isPending} onClick={() => void run(false)} icon={<Check className="size-4" />}>
              Adopt into {ns}
            </Button>
          </>
        ) : (
          <Button variant="primary" onClick={() => onOpenChange(false)}>Done</Button>
        )
      }
    >
      <ol className="mb-4 flex items-center gap-2 text-xs">
        {steps.map((st, i) => (
          <li key={st} className="flex items-center gap-2">
            <span className={cn("flex size-5 items-center justify-center rounded-full border text-[10px] font-semibold", steps.indexOf(step) >= i ? "border-accent bg-accent text-accent-fg" : "border-border text-faint")}>
              {steps.indexOf(step) > i ? <Check className="size-3" /> : i + 1}
            </span>
            <span className={steps.indexOf(step) >= i ? "text-fg" : "text-faint"}>{["Configure", "Preview", "Done"][i]}</span>
            {i < 2 && <span className="h-px w-8 bg-border" />}
          </li>
        ))}
      </ol>
      {missingApi ? (
        <EmptyState title="Adoption needs a newer server" description="This Shoal API does not support adopting blueprints yet." />
      ) : step === "params" ? (
        <div className="space-y-3.5">
          {Object.entries(params).map(([k, p]) => (
            <Field key={k} label={k} required={p.required !== false} description={p.description} error={errs[k]} hint={p.type === "connection" ? `${p.integration ?? ""} connection` : p.type}>
              <ParamInput name={k} param={p} ns={ns} value={values[k]} connections={connections.data ?? []} onChange={(v) => setValues((cur) => {
                const next = { ...cur };
                if (v === undefined) delete next[k];
                else next[k] = v;
                return next;
              })} />
            </Field>
          ))}
          {!Object.keys(params).length && <p className="text-[13px] text-muted">This blueprint has no parameters.</p>}
          <Field label="Name prefix" hint="optional" description={prefix ? `Resources are named ${prefix.replace(/-+$/, "")}-<name>, so several adoptions can live side by side.` : "Prepended (with a dash) to every resource name so several adoptions can live side by side."} error={errs.prefix}>
            <Input mono value={prefix} placeholder="team-a" onChange={(e) => setPrefix(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/^-+/, ""))} />
          </Field>
          {error && !Object.keys(errs).length ? <ErrorNotice error={error} /> : null}
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-[13px] text-muted">{step === "preview" ? "These resources will be created or updated:" : "Created in this namespace:"}</p>
          <OutcomeList items={preview?.resources ?? []} ns={ns} links={step === "done"} />
          {error ? <ErrorNotice error={error} /> : null}
        </div>
      )}
    </Dialog>
  );
}

// --------------------------------------------------------------------------- adoptions

export function AdoptionsTable({ ns, adoptions, latest }: { ns: string; adoptions: Adoption[]; latest?: Record<string, string> }) {
  const canWrite = useCan("blueprints:adopt", ns);
  const remove = useRemoveAdoption(ns);
  const update = useUpdateAdoption(ns);
  const confirm = useConfirm();
  const [upgrade, setUpgrade] = useState<{ a: Adoption; version: string; result: AdoptResult } | null>(null);

  const startUpgrade = async (a: Adoption, version: string) => {
    try {
      const result = await update.mutateAsync({ id: a.id, version, dry_run: true });
      setUpgrade({ a, version, result });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not preview upgrade");
    }
  };
  const del = async (a: Adoption) => {
    const ok = await confirm({
      title: `Remove ${a.blueprint} from ${ns}?`,
      description: `This deletes the ${a.resources.length} resources the adoption created.`,
      confirmLabel: "Remove",
      danger: true,
    });
    if (!ok) return;
    try {
      await remove.mutateAsync(a.id);
      toast.success(`Removed ${a.blueprint}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Remove failed");
    }
  };

  return (
    <>
      <Table>
        <thead>
          <tr>
            <Th>Blueprint</Th>
            <Th>Version</Th>
            <Th>Prefix</Th>
            <Th>Resources</Th>
            <Th className="text-right">Adopted</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {adoptions.map((a) => {
            const newest = latest?.[a.blueprint];
            const outdated = newest && newest !== a.blueprint_version;
            return (
              <Tr key={a.id}>
                <Td>
                  <Link to={nsPath(ns, "blueprints", a.blueprint)} className="font-medium hover:text-accent">{a.blueprint}</Link>
                </Td>
                <Td className="font-mono text-xs">
                  {a.blueprint_version}
                  {outdated && <Badge tone="info" className="ml-1.5">v{newest} available</Badge>}
                </Td>
                <Td className="font-mono text-xs text-muted">{a.prefix || "—"}</Td>
                <Td>
                  <div className="flex flex-wrap gap-1">
                    {a.resources.slice(0, 4).map((x) => {
                      const r = resourceRef(x);
                      const plural = PLURAL_BY_KIND[r.kind];
                      return plural ? (
                        <Link key={`${r.kind}${r.name}`} to={nsPath(ns, plural, r.name)}>
                          <Badge className="font-mono hover:border-accent">{r.name}</Badge>
                        </Link>
                      ) : (
                        <Badge key={`${r.kind}${r.name}`} className="font-mono">{r.name}</Badge>
                      );
                    })}
                    {a.resources.length > 4 && <Badge>+{a.resources.length - 4}</Badge>}
                  </div>
                </Td>
                <Td className="text-right text-xs text-muted">
                  {relTime(a.created_at)}
                  {a.created_by && <div className="text-[11px] text-faint">{a.created_by}</div>}
                </Td>
                <Td className="text-right whitespace-nowrap">
                  {canWrite && outdated && (
                    <Button size="sm" variant="secondary" icon={<ArrowUpCircle className="size-3.5" />} loading={update.isPending && update.variables?.id === a.id} onClick={() => void startUpgrade(a, newest!)}>
                      Upgrade
                    </Button>
                  )}
                  {canWrite && (
                    <Button size="icon-sm" variant="ghost" aria-label="Remove adoption" onClick={() => void del(a)}>
                      <Trash2 className="size-3.5" />
                    </Button>
                  )}
                </Td>
              </Tr>
            );
          })}
        </tbody>
      </Table>
      <Dialog
        open={!!upgrade}
        onOpenChange={(o) => !o && setUpgrade(null)}
        title={upgrade ? `Upgrade ${upgrade.a.blueprint} to v${upgrade.version}` : ""}
        description="Re-renders the templates with the new version, keeping your parameters."
        footer={
          <>
            <Button variant="ghost" onClick={() => setUpgrade(null)}>Cancel</Button>
            <Button
              variant="primary"
              loading={update.isPending}
              onClick={async () => {
                if (!upgrade) return;
                try {
                  await update.mutateAsync({ id: upgrade.a.id, version: upgrade.version, dry_run: false });
                  toast.success(`Upgraded to v${upgrade.version}`);
                  setUpgrade(null);
                } catch (e) {
                  toast.error(e instanceof Error ? e.message : "Upgrade failed");
                }
              }}
            >
              Upgrade
            </Button>
          </>
        }
      >
        {upgrade && (
          <div className="space-y-3">
            <OutcomeList items={upgrade.result.resources} ns={ns} />
            {(upgrade.result.removed?.length ?? 0) > 0 && (
              <>
                <p className="text-[13px] text-muted">These will be removed:</p>
                <OutcomeList items={(upgrade.result.removed ?? []).map((x) => ({ ...resourceRef(x), outcome: "removed" }))} ns={ns} />
              </>
            )}
          </div>
        )}
      </Dialog>
    </>
  );
}

export { isFeatured };
