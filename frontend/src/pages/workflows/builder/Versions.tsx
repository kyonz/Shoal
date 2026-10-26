import { diffLines } from "diff";
import { RotateCcw } from "lucide-react";
import { useMemo, useState } from "react";
import { parse } from "yaml";
import { useResourceVersions } from "@/api/hooks";
import { cn } from "@/lib/cn";
import { relTime } from "@/lib/format";
import { Badge, Button, Dialog, EmptyState, Spinner } from "@/components/ui";
import { alignKeys, stripServerDefaults, toDocument, toYaml } from "@/features/workflow/document";
import type { Schemas } from "@/api/client";
import type { WorkflowSpecDoc } from "@/features/workflow/types";
import { useBuilder } from "./store";
import { actorName } from "@/lib/actor";

type Version = Schemas["ResourceVersionOut"];

/** A version as its author wrote it: the kept source, else the stored document minus server-filled step defaults. */
function asWritten(v: Version) {
  const doc = v.document as { metadata?: { description?: string | null; labels?: Record<string, string> }; spec?: WorkflowSpecDoc };
  const spec = (v.source as WorkflowSpecDoc | null | undefined) ?? stripServerDefaults(doc.spec ?? { steps: [] });
  return { spec, description: doc.metadata?.description ?? "", labels: doc.metadata?.labels ?? {} };
}

/** Version history with a line diff against the current editor state, and restore. */
export function VersionsDialog({ open, onOpenChange, ns, name, currentYaml }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  ns: string;
  name: string;
  currentYaml: string;
}) {
  const { data, isLoading } = useResourceVersions("workflows", ns, open ? name : undefined);
  const versions = useMemo(() => [...(data ?? [])].sort((a, b) => b.version - a.version), [data]);
  const [picked, setPicked] = useState<number | null>(null);
  const active = versions.find((v) => v.version === picked) ?? versions[0];

  const parts = useMemo(() => {
    if (!active) return [];
    const v = asWritten(active);
    let current: unknown = {};
    try {
      current = parse(currentYaml);
    } catch {
      /* keep empty */
    }
    const left = toYaml(alignKeys(toDocument(name, v.description, v.labels, v.spec), current));
    return diffLines(left, toYaml(current));
  }, [active, currentYaml, name]);

  const added = parts.filter((p) => p.added).reduce((n, p) => n + (p.count ?? 0), 0);
  const removed = parts.filter((p) => p.removed).reduce((n, p) => n + (p.count ?? 0), 0);

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="Version history" description="Compare a saved version with what is in the editor, or restore it." size="xl">
      {isLoading ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : !versions.length ? (
        <EmptyState title="No versions yet" />
      ) : (
        <div className="flex h-[56vh] gap-4">
          <div className="w-52 shrink-0 space-y-1 overflow-y-auto">
            {versions.map((v, i) => (
              <button
                key={v.version}
                onClick={() => setPicked(v.version)}
                className={cn(
                  "w-full rounded-lg border px-3 py-2 text-left transition-colors",
                  active?.version === v.version ? "border-accent/40 bg-accent-soft" : "border-transparent hover:bg-subtle",
                )}
              >
                <div className="flex items-center gap-2 text-[13px] font-medium">
                  Version {v.version}
                  {i === 0 && <Badge tone="ok">latest</Badge>}
                </div>
                <div className="mt-0.5 truncate text-[11px] text-faint">
                  {actorName(v.created_by)} · {relTime(v.created_at)}
                </div>
                {v.message && <div className="mt-0.5 line-clamp-2 text-[11.5px] text-muted">{v.message}</div>}
              </button>
            ))}
          </div>
          <div className="flex min-w-0 flex-1 flex-col rounded-xl border border-border">
            <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs">
              <span className="font-medium">v{active?.version}</span>
              <span className="text-faint">→ editor</span>
              <span className="text-ok">+{added}</span>
              <span className="text-bad">−{removed}</span>
              <div className="flex-1" />
              <Button
                size="sm"
                icon={<RotateCcw className="size-3.5" />}
                onClick={() => {
                  if (active) {
                    const v = asWritten(active);
                    // One undo step: commits with the same key close together are merged.
                    useBuilder.getState().setSpec(structuredClone(v.spec), "restore");
                    useBuilder.getState().setMeta({ description: v.description, labels: v.labels }, "restore");
                  }
                  onOpenChange(false);
                }}
              >
                Restore into editor
              </Button>
            </div>
            <pre className="min-h-0 flex-1 overflow-auto py-2 font-mono text-[12px] leading-[1.55]">
              {parts.map((p, i) =>
                p.value
                  .replace(/\n$/, "")
                  .split("\n")
                  .map((line, j) => (
                    <div
                      key={`${i}-${j}`}
                      className={cn("px-3 whitespace-pre-wrap", p.added && "bg-ok-soft text-ok", p.removed && "bg-bad-soft text-bad", !p.added && !p.removed && "text-muted")}
                    >
                      <span className="mr-3 inline-block w-3 select-none opacity-60">{p.added ? "+" : p.removed ? "−" : " "}</span>
                      {line}
                    </div>
                  )),
              )}
              {added === 0 && removed === 0 && <div className="px-3 py-4 text-center font-sans text-xs text-faint">Identical to the editor.</div>}
            </pre>
          </div>
        </div>
      )}
    </Dialog>
  );
}
