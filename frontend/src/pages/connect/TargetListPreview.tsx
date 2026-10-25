/** Resolving target lists: what a saved (or unsaved) list stands for right now. */
import { rawJson } from "@/api/client";

export interface TargetListResolution {
  total: number;
  targets: ({ key: string } & Record<string, unknown>)[];
  truncated: boolean;
  /** Listed targets the lookup query did not return (renamed, archived, no access). */
  missing: string[];
}

export const resolveTargetList = (ns: string, name: string) =>
  rawJson<TargetListResolution>(`/v1/namespaces/${ns}/target-lists/${encodeURIComponent(name)}/resolve`);

export const previewTargetList = (ns: string, spec: Record<string, unknown>) =>
  rawJson<TargetListResolution>(`/v1/namespaces/${ns}/target-lists/preview`, { method: "POST", body: JSON.stringify(spec) });

export function TargetListResult({ result, limit }: { result: TargetListResolution; limit?: number }) {
  const shown = limit ? result.targets.slice(0, limit) : result.targets;
  return (
    <div className="animate-fade-in space-y-2">
      <div className="text-xs text-muted">
        {result.total} target{result.total === 1 ? "" : "s"}
        {result.missing.length > 0 && <span className="text-warn"> · {result.missing.length} not found</span>}
      </div>
      <ul className="max-h-80 divide-y divide-border overflow-auto rounded-lg border border-border">
        {shown.map((t) => (
          <li key={t.key} className="truncate px-3 py-1.5 font-mono text-xs">
            {t.key}
          </li>
        ))}
        {result.targets.length > shown.length && (
          <li className="px-3 py-1.5 text-xs text-faint">and {result.total - shown.length} more</li>
        )}
        {!result.total && <li className="px-3 py-3 text-center text-xs text-faint">The list is empty.</li>}
      </ul>
      {result.missing.length > 0 && (
        <div className="rounded-lg border border-warn/25 bg-warn-soft p-2.5 text-xs">
          <div className="mb-1 font-medium text-fg">Not found by the lookup, so campaigns skip them:</div>
          <div className="font-mono text-muted">{result.missing.join(", ")}</div>
        </div>
      )}
    </div>
  );
}
