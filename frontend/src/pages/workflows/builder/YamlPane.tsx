import { CheckCircle2, CircleAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { CodeEditor, CopyButton } from "@/components/ui";
import type { ParsedWorkflow } from "@/features/workflow/document";

/**
 * The YAML view. Two-way: canvas edits re-render the text unless you are
 * typing here; your edits are parsed as you type and applied to the canvas
 * as soon as they form a valid workflow.
 */
export function YamlPane({ text, onApply, readOnly }: { text: string; onApply: (text: string) => ParsedWorkflow; readOnly?: boolean }) {
  const [draft, setDraft] = useState(text);
  const [error, setError] = useState<string | null>(null);
  const focused = useRef(false);
  const lastApplied = useRef(text);

  // Follow external changes, but never clobber what the user is typing.
  useEffect(() => {
    if (!focused.current && text !== lastApplied.current) {
      setDraft(text);
      lastApplied.current = text;
      setError(null);
    }
  }, [text]);

  useEffect(() => {
    if (draft === lastApplied.current) return;
    const t = setTimeout(() => {
      const res = onApply(draft);
      if (res.ok) {
        setError(null);
        lastApplied.current = draft;
      } else {
        setError(res.line ? `Line ${res.line}: ${res.error}` : res.error);
      }
    }, 450);
    return () => clearTimeout(t);
    // onApply identity changes every render; only re-run on draft changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-border px-3">
        <span className="text-xs font-semibold">workflow.yaml</span>
        <span className="text-[11px] text-faint">{readOnly ? "read-only" : "edits apply live"}</span>
        <div className="flex-1" />
        <CopyButton value={draft} />
      </div>
      <div
        className="min-h-0 flex-1 overflow-auto"
        onFocus={() => (focused.current = true)}
        onBlur={() => {
          focused.current = false;
          // Re-sync with the canonical rendering once the user leaves the editor.
          if (!error && text !== draft) {
            setDraft(text);
            lastApplied.current = text;
          }
        }}
      >
        <CodeEditor value={draft} onChange={setDraft} language="yaml" readOnly={readOnly} height="100%" className="h-full" />
      </div>
      <div className={`flex h-8 shrink-0 items-center gap-1.5 border-t border-border px-3 text-[11.5px] ${error ? "bg-bad-soft text-bad" : "text-faint"}`}>
        {error ? <CircleAlert className="size-3.5" /> : <CheckCircle2 className="size-3.5 text-ok" />}
        <span className="truncate">{error ?? "In sync with the canvas"}</span>
      </div>
    </div>
  );
}
