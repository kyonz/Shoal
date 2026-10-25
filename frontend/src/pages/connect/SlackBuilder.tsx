import { Calendar, ChevronDown, Clock, ExternalLink, MessagesSquare, Plus, Zap } from "lucide-react";
import { Fragment, useMemo, useState, type ReactNode } from "react";
import YAML from "yaml";
import { Badge, Button, Card, CardHeader, CodeEditor, CopyField, Field, Input, Page, PageHeader } from "@/components/ui";
import { cn } from "@/lib/cn";
import { nsPath, useNs } from "@/lib/ns";
import { Crumb } from "./shared";

type Json = Record<string, unknown>;
type Block = Json & { type: string };

const plain = (text: string) => ({ type: "plain_text", text });
const option = (text: string, value: string) => ({ text: plain(text), value });

/** Snippets the palette inserts. Interactive elements carry the Shoal-only `shoal:` key that wires them to the run. */
const PALETTE: { label: string; make: () => Block }[] = [
  { label: "Header", make: () => ({ type: "header", text: plain("Release {{ inputs.version }}") }) },
  { label: "Text", make: () => ({ type: "section", text: { type: "mrkdwn", text: "*{{ inputs.service }}* is ready to ship." } }) },
  { label: "Fields", make: () => ({ type: "section", fields: [{ type: "mrkdwn", text: "*Service*\n{{ inputs.service }}" }, { type: "mrkdwn", text: "*Owner*\n{{ run.initiator.display_name }}" }] }) },
  { label: "Divider", make: () => ({ type: "divider" }) },
  { label: "Context", make: () => ({ type: "context", elements: [{ type: "mrkdwn", text: "Started by Shoal" }] }) },
  { label: "Image", make: () => ({ type: "image", image_url: "https://placehold.co/600x200/png", alt_text: "chart" }) },
  {
    label: "Buttons",
    make: () => ({
      type: "actions",
      elements: [
        { type: "button", action_id: "promote", style: "primary", text: plain("Promote"), value: "promote", shoal: { signal: "choice" } },
        { type: "button", action_id: "rollback", style: "danger", text: plain("Roll back"), value: "rollback", shoal: { signal: "choice" } },
      ],
    }),
  },
  {
    label: "Select",
    make: () => ({
      type: "section",
      text: { type: "mrkdwn", text: "Where should it go?" },
      accessory: { type: "static_select", action_id: "env", placeholder: plain("Environment"), options: [option("Staging", "staging"), option("Production", "prod")], shoal: { signal: "env" } },
    }),
  },
  {
    label: "Date & time",
    make: () => ({ type: "actions", elements: [{ type: "datepicker", action_id: "day", shoal: { signal: "day", after: "keep" } }, { type: "timepicker", action_id: "time", shoal: { signal: "time", after: "keep" } }] }),
  },
  {
    label: "Checkboxes",
    make: () => ({
      type: "section",
      text: { type: "mrkdwn", text: "Checklist" },
      accessory: { type: "checkboxes", action_id: "checks", options: [option("Tests pass", "tests"), option("Docs updated", "docs")], shoal: { signal: "checks", after: "keep" } },
    }),
  },
  {
    label: "Form button",
    make: () => ({
      type: "actions",
      elements: [{
        type: "button", action_id: "details", text: plain("Add details"),
        shoal: { signal: "details", modal: { title: plain("Details"), blocks: [{ type: "input", block_id: "reason", label: plain("Reason"), element: { type: "plain_text_input", action_id: "reason", multiline: true } }] } },
      }],
    }),
  },
  { label: "Trigger button", make: () => ({ type: "actions", elements: [{ type: "button", action_id: "page_oncall", text: plain("Page on-call"), shoal: { event: true } }] }) },
];

const START: Block[] = [PALETTE[0].make(), PALETTE[1].make(), PALETTE[6].make()];

function elementsOf(block: Block): Json[] {
  if (block.type === "actions") return (block.elements as Json[]) ?? [];
  if (block.type === "section" && block.accessory) return [block.accessory as Json];
  return [];
}

/** Signals the message sends: what the workflow should wait for. */
function hooksOf(blocks: Block[], messageSignal: string): { signal: string; elements: string[]; modal: boolean }[] {
  const out = new Map<string, { signal: string; elements: string[]; modal: boolean }>();
  for (const block of blocks) {
    for (const el of elementsOf(block)) {
      const hook = el.shoal as Json | string | undefined;
      const h = typeof hook === "string" ? { signal: hook } : hook;
      if (h?.event || (!h && (!messageSignal || el.url))) continue;
      const signal = String(h?.signal ?? messageSignal);
      const entry = out.get(signal) ?? { signal, elements: [], modal: false };
      entry.elements.push(String(el.action_id ?? el.type));
      entry.modal ||= !!h?.modal;
      out.set(signal, entry);
    }
  }
  return [...out.values()];
}

function stripShoal(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripShoal);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Json).filter(([k]) => k !== "shoal").map(([k, v]) => [k, stripShoal(v)]));
  }
  return value;
}

// --------------------------------------------------------------------------- mrkdwn

function mrkdwn(text: string): ReactNode {
  const parts = text.split(/(\{\{[^}]*\}\}|<[^>]+>|\*[^*\n]+\*|_[^_\n]+_|`[^`\n]+`|~[^~\n]+~|\n)/g);
  return parts.map((p, i) => {
    if (!p) return null;
    if (p === "\n") return <br key={i} />;
    if (p.startsWith("{{")) return <code key={i} className="rounded bg-accent-soft px-1 font-mono text-[12px] text-accent">{p}</code>;
    if (p.startsWith("<") && p.endsWith(">")) {
      const [target, label] = p.slice(1, -1).split("|");
      if (target.startsWith("@") || target.startsWith("#") || target.startsWith("!")) return <span key={i} className="rounded bg-info-soft px-0.5 text-info">{label ?? target}</span>;
      return <a key={i} className="text-[#1264a3] hover:underline" href={target} target="_blank" rel="noreferrer">{label ?? target}</a>;
    }
    if (p.length > 2 && p.startsWith("*") && p.endsWith("*")) return <strong key={i}>{p.slice(1, -1)}</strong>;
    if (p.length > 2 && p.startsWith("_") && p.endsWith("_")) return <em key={i}>{p.slice(1, -1)}</em>;
    if (p.length > 2 && p.startsWith("~") && p.endsWith("~")) return <s key={i}>{p.slice(1, -1)}</s>;
    if (p.length > 2 && p.startsWith("`") && p.endsWith("`")) return <code key={i} className="rounded border border-border bg-subtle px-1 font-mono text-[12px] text-[#e01e5a]">{p.slice(1, -1)}</code>;
    return <Fragment key={i}>{p}</Fragment>;
  });
}

function textOf(t: unknown): string {
  return t && typeof t === "object" ? String((t as Json).text ?? "") : "";
}

// --------------------------------------------------------------------------- preview

export function SlackPreview({ blocks, messageSignal = "", onUse, appName = "Shoal" }: {
  blocks: Block[];
  messageSignal?: string;
  onUse?: (what: string) => void;
  appName?: string;
}) {
  return (
    <div className="flex gap-2.5 rounded-lg bg-white p-3 text-[15px] leading-[1.46] text-[#1d1c1d] dark:bg-[#1a1d21] dark:text-[#d1d2d3]">
      <div className="flex size-9 shrink-0 items-center justify-center rounded-md bg-[#4a154b] text-white">
        <MessagesSquare className="size-4.5" />
      </div>
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex items-center gap-1.5">
          <span className="font-bold">{appName}</span>
          <span className="rounded bg-black/10 px-1 text-[10px] font-semibold uppercase dark:bg-white/10">App</span>
          <span className="text-xs text-[#616061]">12:00</span>
        </div>
        {blocks.map((b, i) => (
          <PreviewBlock key={i} block={b} messageSignal={messageSignal} onUse={onUse} />
        ))}
      </div>
    </div>
  );
}

function hookLabel(el: Json, messageSignal: string): { label: string; tone: "info" | "warn" | "neutral" } | null {
  const hook = el.shoal as Json | string | undefined;
  const h = typeof hook === "string" ? { signal: hook } : hook;
  if (h?.event) return { label: "trigger event", tone: "warn" };
  if (el.url && !h) return null;
  const signal = h?.signal ?? (messageSignal || undefined);
  if (!signal) return { label: "trigger event", tone: "warn" };
  return { label: `${h?.modal ? "form → " : "→ "}${signal}`, tone: "info" };
}

function PreviewElement({ el, messageSignal, onUse }: { el: Json; messageSignal: string; onUse?: (what: string) => void }) {
  const hook = hookLabel(el, messageSignal);
  const use = (value: unknown) => onUse?.(hook ? `${String(el.action_id ?? el.type)}: ${hook.label}${hook.tone === "info" ? ` with value ${JSON.stringify(value)}` : ""}` : `opens ${String(el.url)}`);
  const type = String(el.type);
  let control: ReactNode;
  if (type === "button") {
    const style = el.style === "primary" ? "border-[#007a5a] bg-[#007a5a] text-white" : el.style === "danger" ? "border-[#e01e5a] text-[#e01e5a]" : "border-black/25 dark:border-white/25";
    control = (
      <button type="button" onClick={() => use(el.value ?? textOf(el.text))} className={cn("inline-flex h-8 items-center gap-1 rounded border px-3 text-[13px] font-bold", style)}>
        {textOf(el.text)} {!!el.url && <ExternalLink className="size-3" />}
      </button>
    );
  } else if (type.includes("select") || type === "overflow") {
    const options = (el.options as Json[]) ?? [];
    control = (
      <select
        className="h-8 rounded border border-black/25 bg-transparent px-2 text-[13px] dark:border-white/25"
        defaultValue=""
        onChange={(e) => use(e.target.value)}
      >
        <option value="" disabled>{textOf(el.placeholder) || (type === "overflow" ? "⋯" : "Select an item")}</option>
        {options.map((o, i) => (
          <option key={i} value={String(o.value)}>{textOf(o.text)}</option>
        ))}
      </select>
    );
  } else if (type === "datepicker" || type === "timepicker" || type === "datetimepicker") {
    const Icon = type === "timepicker" ? Clock : Calendar;
    control = (
      <button type="button" onClick={() => use(type === "timepicker" ? "09:30" : "2026-11-02")} className="inline-flex h-8 items-center gap-2 rounded border border-black/25 px-2 text-[13px] dark:border-white/25">
        <Icon className="size-3.5" /> {type === "timepicker" ? "Select time" : "Select a date"} <ChevronDown className="size-3" />
      </button>
    );
  } else if (type === "checkboxes" || type === "radio_buttons") {
    const options = (el.options as Json[]) ?? [];
    control = (
      <div className="space-y-1">
        {options.map((o, i) => (
          <label key={i} className="flex items-center gap-2 text-[14px]">
            <input type={type === "checkboxes" ? "checkbox" : "radio"} name={String(el.action_id)} onChange={() => use(type === "checkboxes" ? [o.value] : o.value)} />
            {textOf(o.text)}
          </label>
        ))}
      </div>
    );
  } else {
    control = <span className="text-xs text-[#616061]">{type}</span>;
  }
  return (
    <div className="inline-flex flex-col items-start gap-1">
      {control}
      {hook && <Badge tone={hook.tone}>{hook.label}</Badge>}
    </div>
  );
}

function PreviewBlock({ block, messageSignal, onUse }: { block: Block; messageSignal: string; onUse?: (what: string) => void }) {
  switch (block.type) {
    case "header":
      return <div className="text-[18px] font-black">{textOf(block.text)}</div>;
    case "divider":
      return <hr className="border-black/10 dark:border-white/10" />;
    case "context":
      return (
        <div className="flex flex-wrap items-center gap-1.5 text-[12px] text-[#616061]">
          {((block.elements as Json[]) ?? []).map((e, i) => (e.type === "image" ? <img key={i} src={String(e.image_url)} alt={String(e.alt_text)} className="size-4 rounded" /> : <span key={i}>{mrkdwn(textOf(e))}</span>))}
        </div>
      );
    case "image":
      return <img src={String(block.image_url)} alt={String(block.alt_text ?? "")} className="max-h-64 rounded-lg border border-black/10" />;
    case "actions":
      return (
        <div className="flex flex-wrap items-start gap-2">
          {((block.elements as Json[]) ?? []).map((el, i) => <PreviewElement key={i} el={el} messageSignal={messageSignal} onUse={onUse} />)}
        </div>
      );
    case "section": {
      const fields = (block.fields as Json[]) ?? [];
      return (
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 space-y-2">
            {!!block.text && <div className="whitespace-pre-wrap">{mrkdwn(textOf(block.text))}</div>}
            {fields.length > 0 && (
              <div className="grid grid-cols-2 gap-x-6 gap-y-2">
                {fields.map((f, i) => <div key={i} className="whitespace-pre-wrap">{mrkdwn(textOf(f))}</div>)}
              </div>
            )}
          </div>
          {!!block.accessory && <PreviewElement el={block.accessory as Json} messageSignal={messageSignal} onUse={onUse} />}
        </div>
      );
    }
    case "input":
      return (
        <div className="space-y-1">
          <div className="text-[14px] font-bold">{textOf(block.label)}</div>
          <div className="h-8 rounded border border-black/25 dark:border-white/25" />
        </div>
      );
    default:
      return <div className="text-xs text-[#616061]">Unsupported in preview: {block.type}</div>;
  }
}

// --------------------------------------------------------------------------- the page

export default function SlackBuilder() {
  const ns = useNs();
  const [source, setSource] = useState(() => JSON.stringify({ blocks: START }, null, 2));
  const [text, setText] = useState("{{ inputs.service }} is ready to ship");
  const [channel, setChannel] = useState("#deploys");
  const [messageSignal, setMessageSignal] = useState("");
  const [used, setUsed] = useState<string | null>(null);

  const parsed = useMemo(() => {
    try {
      const data = JSON.parse(source) as unknown;
      const blocks = Array.isArray(data) ? data : (data as Json).blocks;
      if (!Array.isArray(blocks)) return { error: "Expected {\"blocks\": [...]} or a list of blocks" };
      return { blocks: blocks as Block[] };
    } catch (e) {
      return { error: (e as Error).message };
    }
  }, [source]);
  const blocks = parsed.blocks ?? [];
  const hooks = hooksOf(blocks, messageSignal);

  const add = (make: () => Block) => setSource(JSON.stringify({ blocks: [...blocks, make()] }, null, 2));

  const steps = useMemo(() => {
    const post: Json = { id: "post", type: "action", action: "slack.post_message", with: { channel, text, ...(messageSignal ? { signal: messageSignal } : {}), blocks } };
    const waits = hooks.map((h) => ({ id: `on_${h.signal.replace(/[^A-Za-z0-9_]/g, "_")}`, type: "wait", signal: { name: h.signal }, max_wait: "1d" }));
    return YAML.stringify([post, ...waits], { lineWidth: 0 });
  }, [blocks, channel, text, messageSignal, hooks]);

  const builderUrl = `https://app.slack.com/block-kit-builder/#${encodeURIComponent(JSON.stringify({ blocks: stripShoal(blocks) }))}`;

  return (
    <Page>
      <PageHeader
        breadcrumb={<Crumb to={nsPath(ns, "connections")}>Connections</Crumb>}
        title="Slack message builder"
        description="Design a Block Kit message, wire its buttons and menus back into the workflow, and copy the steps."
        actions={
          <a href={builderUrl} target="_blank" rel="noreferrer">
            <Button icon={<ExternalLink className="size-4" />}>Open in Block Kit Builder</Button>
          </a>
        }
      />
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="space-y-4">
          <Card>
            <CardHeader title="Blocks" description={<>Expressions like <code className="font-mono">{"{{ inputs.x }}"}</code> are filled in when the step runs. Add <code className="font-mono">shoal: {"{signal: name}"}</code> to an element to send the run that signal with the chosen value.</>} />
            <div className="space-y-3 p-4">
              <div className="flex flex-wrap gap-1.5">
                {PALETTE.map((p) => (
                  <Button key={p.label} size="sm" icon={<Plus className="size-3.5" />} onClick={() => add(p.make)}>
                    {p.label}
                  </Button>
                ))}
              </div>
              <CodeEditor value={source} onChange={setSource} language="json" minHeight="360px" maxHeight="560px" />
              {parsed.error && <p className="text-xs text-bad">{parsed.error}</p>}
            </div>
          </Card>
          <Card>
            <CardHeader title="Message" />
            <div className="grid gap-3.5 p-4 sm:grid-cols-3">
              <Field label="Channel" description="#name, an id, a user id or an email">
                <Input mono value={channel} onChange={(e) => setChannel(e.target.value)} />
              </Field>
              <Field label="Fallback text" description="Shown in notifications">
                <Input value={text} onChange={(e) => setText(e.target.value)} />
              </Field>
              <Field label="Default signal" description="For elements without their own">
                <Input mono value={messageSignal} onChange={(e) => setMessageSignal(e.target.value)} placeholder="choice" />
              </Field>
            </div>
          </Card>
        </div>
        <div className="space-y-4">
          <Card>
            <CardHeader title="Preview" description="Click the controls to see what the workflow receives." />
            <div className="space-y-3 p-4">
              <div className="rounded-xl border border-border p-1">
                <SlackPreview blocks={blocks} messageSignal={messageSignal} onUse={setUsed} />
              </div>
              {used && (
                <div className="flex items-center gap-2 rounded-lg border border-info/25 bg-info-soft px-3 py-2 text-[13px] text-info">
                  <Zap className="size-3.5 shrink-0" /> {used}
                </div>
              )}
            </div>
          </Card>
          <Card>
            <CardHeader title="Hooks" description="What this message sends back to the run." />
            <div className="space-y-2 p-4 text-[13px]">
              {hooks.length === 0 && <p className="text-muted">No element is wired to the run; clicks reach triggers as block_actions events.</p>}
              {hooks.map((h) => (
                <div key={h.signal} className="flex flex-wrap items-center gap-2">
                  <Badge tone="info">{h.signal}</Badge>
                  <span className="text-muted">from {h.elements.join(", ")}{h.modal ? " (form answers in payload.values)" : " (choice in payload.value)"}</span>
                </div>
              ))}
            </div>
          </Card>
          <Card>
            <CardHeader title="Workflow steps" description="Paste into a workflow's steps. Branch with if: on the wait step's payload." />
            <div className="space-y-2 p-4">
              <CodeEditor value={steps} language="yaml" readOnly minHeight="200px" maxHeight="420px" />
              <CopyField value={`if: "{{ steps.${hooks[0] ? `on_${hooks[0].signal.replace(/[^A-Za-z0-9_]/g, "_")}` : "on_choice"}.output.payload.value == 'promote' }}"`} />
            </div>
          </Card>
        </div>
      </div>
    </Page>
  );
}
