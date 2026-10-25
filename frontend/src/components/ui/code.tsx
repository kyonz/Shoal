import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { yaml } from "@codemirror/lang-yaml";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import type { Extension } from "@codemirror/state";
import { Decoration, EditorView, MatchDecorator, ViewPlugin, placeholder as cmPlaceholder, type DecorationSet, type ViewUpdate } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";
import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { Check, Copy } from "lucide-react";
import { createContext, forwardRef, useContext, useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { Button } from "./primitives";
import { Tooltip } from "./overlays";

const highlight = HighlightStyle.define([
  { tag: [t.keyword, t.bool, t.null], color: "var(--accent)" },
  { tag: [t.propertyName, t.definition(t.propertyName)], color: "var(--info)" },
  { tag: [t.string, t.special(t.string)], color: "var(--ok)" },
  { tag: [t.number], color: "var(--warn)" },
  { tag: [t.comment, t.lineComment, t.blockComment], color: "var(--faint)", fontStyle: "italic" },
  { tag: [t.meta, t.typeName], color: "var(--muted)" },
  { tag: [t.punctuation, t.separator, t.bracket], color: "var(--muted)" },
]);

const baseTheme = EditorView.theme({
  "&": { color: "var(--fg)" },
  ".cm-content": { caretColor: "var(--accent)", padding: "8px 0" },
  ".cm-cursor": { borderLeftColor: "var(--accent)" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": {
    background: "color-mix(in oklab, var(--accent) 22%, transparent) !important",
  },
  ".cm-placeholder": { color: "var(--faint)" },
});

/** Highlight `{{ ... }}` expressions inside any text. */
const exprMatcher = new MatchDecorator({
  regexp: /\{\{[^}]*\}\}/g,
  decoration: Decoration.mark({ class: "cm-expr" }),
});

export const expressionHighlight = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = exprMatcher.createDeco(view);
    }
    update(u: ViewUpdate) {
      this.decorations = exprMatcher.updateDeco(u, this.decorations);
    }
  },
  { decorations: (v) => v.decorations },
);

export interface CodeEditorProps {
  value: string;
  onChange?: (v: string) => void;
  language?: "yaml" | "json" | "python" | "javascript" | "text";
  readOnly?: boolean;
  className?: string;
  height?: string;
  minHeight?: string;
  maxHeight?: string;
  lineNumbers?: boolean;
  extensions?: Extension[];
  placeholder?: string;
  autoFocus?: boolean;
  /** Turn off the default completions (when supplying your own). */
  defaultCompletions?: boolean;
}

/** Makes every CodeEditor below it read-only unless it says otherwise (e.g. a form the user may only view). */
export const ReadOnlyContext = createContext(false);

export const CodeEditor = forwardRef<ReactCodeMirrorRef, CodeEditorProps>(function CodeEditor(
  { value, onChange, language = "yaml", readOnly: readOnlyProp, className, height, minHeight, maxHeight, lineNumbers = true, extensions = [], placeholder, autoFocus, defaultCompletions = true },
  ref,
) {
  const inherited = useContext(ReadOnlyContext);
  const readOnly = readOnlyProp ?? inherited;
  const exts = useMemo(() => {
    const list: Extension[] = [baseTheme, syntaxHighlighting(highlight), expressionHighlight, EditorView.lineWrapping, ...extensions];
    if (language === "yaml") list.push(yaml());
    if (language === "json") list.push(json());
    if (language === "python") list.push(python());
    if (language === "javascript") list.push(javascript());
    if (placeholder) list.push(cmPlaceholder(placeholder));
    return list;
  }, [language, extensions, placeholder]);
  return (
    <CodeMirror
      ref={ref}
      value={value}
      onChange={onChange}
      readOnly={readOnly}
      editable={!readOnly}
      theme="none"
      height={height}
      minHeight={minHeight}
      maxHeight={maxHeight}
      autoFocus={autoFocus}
      basicSetup={{
        lineNumbers,
        foldGutter: lineNumbers,
        highlightActiveLine: lineNumbers && !readOnly,
        highlightActiveLineGutter: lineNumbers && !readOnly,
        autocompletion: defaultCompletions,
        bracketMatching: true,
        closeBrackets: true,
        indentOnInput: true,
        tabSize: 2,
      }}
      extensions={exts}
      className={cn("text-fg", className)}
    />
  );
});

export function CopyButton({ value, className, label }: { value: string; className?: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <Tooltip content={done ? "Copied" : label ?? "Copy"}>
      <Button
        variant="ghost"
        size={label ? "sm" : "icon-sm"}
        className={className}
        onClick={(e) => {
          e.stopPropagation();
          void navigator.clipboard?.writeText(value);
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        }}
        aria-label={label ?? "Copy"}
        icon={done ? <Check className="size-3.5 text-ok" /> : <Copy className="size-3.5" />}
      >
        {label}
      </Button>
    </Tooltip>
  );
}

/** Read-only, highlighted JSON with a copy button. */
export function JsonBlock({ value, className, maxHeight = "420px" }: { value: unknown; className?: string; maxHeight?: string }) {
  const text = useMemo(() => (typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "null"), [value]);
  return (
    <div className={cn("group relative rounded-lg border border-border bg-subtle/60", className)}>
      <div className="absolute top-1 right-1 z-10 opacity-0 transition-opacity group-hover:opacity-100">
        <CopyButton value={text} />
      </div>
      <CodeEditor value={text} language="json" readOnly lineNumbers={false} maxHeight={maxHeight} />
    </div>
  );
}

/** Inline code chip with copy (webhook URLs, ids, tokens). */
export function CopyField({ value, className, secret }: { value: string; className?: string; secret?: boolean }) {
  const [shown, setShown] = useState(!secret);
  return (
    <div className={cn("flex h-8 items-center gap-1 rounded-lg border border-border bg-subtle pr-1 pl-2.5", className)}>
      <code className="min-w-0 flex-1 truncate font-mono text-xs">{shown ? value : "•".repeat(Math.min(32, value.length))}</code>
      {secret && (
        <Button variant="ghost" size="xs" onClick={() => setShown((s) => !s)}>
          {shown ? "Hide" : "Show"}
        </Button>
      )}
      <CopyButton value={value} />
    </div>
  );
}
