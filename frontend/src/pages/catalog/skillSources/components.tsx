import { diffLines } from "diff";
import { ArrowUpCircle, Download, ExternalLink, FileText, GitCommit } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";
import { useCan } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { nsPath } from "@/lib/ns";
import { Badge, Button, Dialog, EmptyState, ErrorNotice, Field, Input, Skeleton } from "@/components/ui";
import { NAME_RE, kindPath } from "../kinds";
import { Markdown } from "../widgets";
import { readablePath, useImportSkill, useSourceSkill, useUpgradeSkill, type Change, type ImportResult } from "./api";

const OP_TONE = { add: "ok", remove: "bad", replace: "info", reorder: "neutral" } as const;

function short(value: unknown) {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 80 ? `${text.slice(0, 77)}…` : text;
}

/** The diff an import or upgrade would make, as returned by the API. */
export function ChangeList({ changes }: { changes: Change[] }) {
  if (!changes.length) return <p className="text-[13px] text-muted">No changes: the namespace already has this version.</p>;
  return (
    <ul className="divide-y divide-border rounded-lg border border-border">
      {changes.map((c) => {
        const multiline = c.op === "replace" && typeof c.from === "string" && typeof c.to === "string" && (c.from.includes("\n") || c.to.includes("\n"));
        return (
          <li key={`${c.op}${c.path}`} className="px-3 py-2 text-[13px]">
            <div className="flex items-center gap-2">
              <Badge tone={OP_TONE[c.op]} className="w-16 justify-center">{c.op}</Badge>
              <span className="truncate font-mono text-xs">{readablePath(c.path)}</span>
              {c.op === "replace" && !multiline && (
                <span className="ml-auto truncate font-mono text-xs text-muted">
                  {short(c.from)} → <span className="text-fg">{short(c.to)}</span>
                </span>
              )}
            </div>
            {multiline && (
              <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-subtle p-2 font-mono text-[11px] leading-relaxed">
                {diffLines(`${c.from as string}\n`, `${c.to as string}\n`).map((part, i) => (
                  <span key={i} className={cn(part.added && "bg-ok-soft text-ok", part.removed && "bg-bad-soft text-bad line-through decoration-bad/40", !part.added && !part.removed && "text-faint")}>
                    {part.value}
                  </span>
                ))}
              </pre>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function ImportSummary({ result, ns }: { result: ImportResult; ns: string }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[13px]">
        <Badge tone={result.outcome === "created" ? "ok" : result.outcome === "updated" ? "info" : "neutral"}>
          {result.dry_run ? `would be ${result.outcome}` : result.outcome}
        </Badge>
        {result.dry_run ? (
          <span className="font-mono text-xs">{result.name}</span>
        ) : (
          <Link to={nsPath(ns, "skills", result.name)} className="font-mono text-xs text-accent hover:underline">{result.name}</Link>
        )}
        <span className="text-muted">
          {result.previous_version && result.previous_version !== result.version ? `${result.previous_version} → ` : ""}
          {result.version}
        </span>
        {result.ref && <span className="font-mono text-[11px] text-faint">{result.ref.slice(0, 12)}</span>}
      </div>
      <ChangeList changes={result.changes} />
      {result.skipped_files.length > 0 && (
        <p className="text-xs text-warn">Left out (binary or too large): {result.skipped_files.join(", ")}</p>
      )}
      {!result.dry_run && result.resource_version && result.resource_version > 1 && (
        <p className="text-xs text-faint">Saved as version {result.resource_version} of the skill; roll back from the Imported tab or the skill's history.</p>
      )}
    </div>
  );
}

/** A skill from a source: its SKILL.md, files and version, and importing it. */
export function SkillDialog({ ns, source, id, onOpenChange }: { ns: string; source: string; id: string | null; onOpenChange: (open: boolean) => void }) {
  const detail = useSourceSkill(ns, source, id ?? undefined);
  const importer = useImportSkill(ns);
  const mayImport = useCan("skills:import", ns);
  const mayWrite = useCan("skills:write", ns);
  const canImport = mayImport && mayWrite;
  const [name, setName] = useState("");
  const [result, setResult] = useState<ImportResult | null>(null);

  useEffect(() => {
    setResult(null);
    setName("");
    importer.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const d = detail.data;
  const installed = d?.installed ?? [];
  const target = name || installed[0]?.name || "";
  const nameError = name && !NAME_RE.test(name) ? "Lowercase letters, digits and dashes" : undefined;

  const run = async (dryRun: boolean) => {
    if (!d) return;
    try {
      const r = await importer.mutateAsync({ source, skill: d.id, name: target || undefined, dry_run: dryRun });
      setResult(r);
      if (!dryRun) toast.success(r.outcome === "unchanged" ? `${r.name} is already up to date` : `Imported ${r.name} (${r.version})`);
    } catch {
      /* shown below */
    }
  };

  return (
    <Dialog
      open={!!id}
      onOpenChange={onOpenChange}
      size="xl"
      title={d ? d.name : "Skill"}
      description={d?.description}
      footer={
        d && canImport ? (
          result && !result.dry_run ? (
            <Button onClick={() => onOpenChange(false)}>Done</Button>
          ) : (
            <>
              <Button disabled={!!nameError} loading={importer.isPending && !result} onClick={() => run(true)}>Preview</Button>
              <Button variant="primary" icon={<Download className="size-4" />} disabled={!!nameError} loading={importer.isPending && !!result} onClick={() => run(false)}>
                {installed.length ? "Update" : `Import into ${ns}`}
              </Button>
            </>
          )
        ) : undefined
      }
    >
      {detail.isLoading ? (
        <Skeleton className="h-64" />
      ) : detail.error || !d ? (
        <ErrorNotice error={detail.error} />
      ) : result ? (
        <>
          <ImportSummary result={result} ns={ns} />
          {importer.error ? <ErrorNotice className="mt-3" error={importer.error} /> : null}
        </>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_240px]">
          <div className="min-w-0 rounded-lg border border-border p-4">
            {d.instructions.trim() ? <Markdown>{d.instructions}</Markdown> : <p className="text-[13px] text-faint">This skill's SKILL.md has no body.</p>}
          </div>
          <div className="space-y-4 text-[13px]">
            <div className="space-y-1">
              <div className="text-xs text-faint">Version</div>
              <div className="flex items-center gap-2">
                <Badge className="font-mono">{d.version}</Badge>
                {d.ref && <span className="inline-flex items-center gap-1 font-mono text-[11px] text-faint"><GitCommit className="size-3" />{d.ref.slice(0, 12)}</span>}
              </div>
              <div className="font-mono text-[11px] break-all text-faint">{d.source}/{d.id}</div>
              {d.homepage && (
                <a href={d.homepage} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
                  Homepage <ExternalLink className="size-3" />
                </a>
              )}
            </div>
            <div>
              <div className="mb-1 text-xs text-faint">Files</div>
              {Object.keys(d.files).length ? (
                <ul className="space-y-0.5">
                  {Object.keys(d.files).sort().map((f) => (
                    <li key={f} className="flex items-center gap-1.5 font-mono text-xs"><FileText className="size-3 text-faint" />{f}</li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-faint">Only SKILL.md.</p>
              )}
              {d.skipped.length > 0 && <p className="mt-1 text-xs text-warn">Not imported: {d.skipped.join(", ")}</p>}
            </div>
            {installed.length > 0 && (
              <div>
                <div className="mb-1 text-xs text-faint">In {ns}</div>
                {installed.map((i) => (
                  <Link key={i.name} to={nsPath(ns, "skills", i.name)} className="flex items-center gap-1.5 text-xs text-accent hover:underline">
                    {i.name} <span className="text-faint">{i.version}</span>
                  </Link>
                ))}
              </div>
            )}
            {canImport && (
              <Field label="Skill name" hint="optional" error={nameError} description={installed.length ? "Updates the existing import; enter another name to import a second copy." : "Defaults to the skill's own name."}>
                <Input mono value={name} placeholder={installed[0]?.name ?? d.name.toLowerCase().replace(/[^a-z0-9]+/g, "-")} onChange={(e) => setName(e.target.value)} />
              </Field>
            )}
            {importer.error ? <ErrorNotice error={importer.error} /> : null}
          </div>
        </div>
      )}
    </Dialog>
  );
}

/** Preview an upgrade's diff, then apply it. */
export function UpgradeDialog({ ns, name, onOpenChange }: { ns: string; name: string | null; onOpenChange: (open: boolean) => void }) {
  const upgrade = useUpgradeSkill(ns);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [version, setVersion] = useState("");

  useEffect(() => {
    setResult(null);
    setVersion("");
    upgrade.reset();
    if (name) void upgrade.mutateAsync({ name, dry_run: true }).then(setResult).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name]);

  const preview = async () => {
    if (!name) return;
    setResult(null);
    await upgrade.mutateAsync({ name, version: version || undefined, dry_run: true }).then(setResult).catch(() => undefined);
  };
  const apply = async () => {
    if (!name) return;
    try {
      const r = await upgrade.mutateAsync({ name, version: version || undefined, dry_run: false });
      setResult(r);
      toast.success(`Upgraded ${name} to ${r.version}`);
    } catch {
      /* shown below */
    }
  };

  const done = result && !result.dry_run;
  return (
    <Dialog
      open={!!name}
      onOpenChange={onOpenChange}
      size="lg"
      title={`Upgrade ${name ?? ""}`}
      description="What re-importing from the source changes. The current version stays in the skill's history."
      footer={
        done ? (
          <Button onClick={() => onOpenChange(false)}>Done</Button>
        ) : (
          <Button variant="primary" icon={<ArrowUpCircle className="size-4" />} loading={upgrade.isPending} disabled={!result || result.outcome === "unchanged"} onClick={apply}>
            Upgrade
          </Button>
        )
      }
    >
      {!done && (
        <div className="mb-4 flex items-end gap-2">
          <Field label="Version or ref" hint="optional" className="flex-1" description="Leave empty for what the source is pinned to.">
            <Input mono value={version} placeholder="latest" onChange={(e) => setVersion(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void preview()} />
          </Field>
          <Button onClick={preview} loading={upgrade.isPending && !result}>Preview</Button>
        </div>
      )}
      {upgrade.error ? (
        <ErrorNotice error={upgrade.error} />
      ) : !result ? (
        <Skeleton className="h-32" />
      ) : (
        <ImportSummary result={result} ns={ns} />
      )}
    </Dialog>
  );
}

export function NoSources({ canManage, ns }: { canManage: boolean; ns: string }) {
  const navigate = useNavigate();
  return (
    <EmptyState
      title="No skill sources yet"
      description={
        canManage
          ? "Add a git repository in the Agent Skills layout or a skill registry, and teams can import its skills here."
          : "Ask a tenant admin to add a skill source (a git repository or a skill registry) to browse and import skills."
      }
      action={canManage && <Button variant="primary" onClick={() => navigate(kindPath("skill-sources", ns, "new"))}>Add a skill source</Button>}
    />
  );
}
