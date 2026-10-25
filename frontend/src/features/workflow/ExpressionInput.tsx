import { autocompletion, startCompletion } from "@codemirror/autocomplete";
import { Prec } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { Braces } from "lucide-react";
import { createContext, useContext, useMemo, useRef } from "react";
import type { ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { cn } from "@/lib/cn";
import { CodeEditor, Tooltip } from "@/components/ui";
import { expressionCompletions, type ExprVar } from "./expressions";

/** Variables available to expressions in the field being edited. */
export const ExprVarsContext = createContext<() => ExprVar[]>(() => []);

const singleLine = [
  Prec.highest(keymap.of([{ key: "Enter", run: () => true }])),
  EditorView.contentAttributes.of({ "aria-multiline": "false" }),
];

/**
 * A text field that understands `{{ }}`: highlights expressions and
 * autocompletes inputs, upstream step outputs, run context and secrets.
 */
export function ExpressionInput({ value, onChange, multiline, placeholder, minHeight, className, mono }: {
  value: string;
  onChange: (v: string) => void;
  multiline?: boolean;
  placeholder?: string;
  minHeight?: string;
  className?: string;
  mono?: boolean;
}) {
  const getVars = useContext(ExprVarsContext);
  const ref = useRef<ReactCodeMirrorRef>(null);
  const getRef = useRef(getVars);
  getRef.current = getVars;
  const extensions = useMemo(
    () => [
      autocompletion({ override: [expressionCompletions(() => getRef.current())], icons: false }),
      ...(multiline ? [] : singleLine),
      EditorView.theme({
        ".cm-content": { fontFamily: mono || multiline ? "var(--font-mono)" : "var(--font-sans)", fontSize: mono || multiline ? "12px" : "13px", padding: multiline ? "6px 0" : "5px 0" },
        ".cm-line": { padding: "0 10px" },
        "&": { minHeight: minHeight ?? (multiline ? "88px" : "30px") },
      }),
    ],
    [multiline, mono, minHeight],
  );

  const insert = () => {
    const view = ref.current?.view;
    if (!view) return;
    const { from, to } = view.state.selection.main;
    view.dispatch({ changes: { from, to, insert: "{{  }}" }, selection: { anchor: from + 3 } });
    view.focus();
    startCompletion(view);
  };

  return (
    <div
      className={cn(
        "group/expr relative rounded-lg border border-border bg-panel shadow-card transition-colors hover:border-border-strong focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/25",
        className,
      )}
    >
      <CodeEditor ref={ref} value={value} onChange={onChange} language="text" lineNumbers={false} placeholder={placeholder} extensions={extensions} defaultCompletions={false} maxHeight={multiline ? "420px" : undefined} />
      <Tooltip content="Insert expression ({{ }})">
        <button
          type="button"
          onClick={insert}
          className="absolute top-1 right-1 flex size-6 items-center justify-center rounded-md text-faint opacity-0 transition-opacity group-focus-within/expr:opacity-100 group-hover/expr:opacity-100 hover:bg-subtle hover:text-accent"
          aria-label="Insert expression"
        >
          <Braces className="size-3.5" />
        </button>
      </Tooltip>
    </div>
  );
}
