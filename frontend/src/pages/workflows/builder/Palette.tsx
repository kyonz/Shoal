import { ChevronRight, GripVertical, Puzzle, Search } from "lucide-react";
import { useState, type DragEvent } from "react";
import { useIntegrations, useResources } from "@/api/hooks";
import { cn } from "@/lib/cn";
import { useNs } from "@/lib/ns";
import { Badge, Tooltip } from "@/components/ui";
import { STEP_TYPES, stepType } from "@/features/workflow/stepTypes";
import type { StepDoc } from "@/features/workflow/types";

export const DND_MIME = "application/x-shoal-step";

export interface PaletteItem {
  type: string;
  preset?: Partial<StepDoc>;
}

function onDragStart(e: DragEvent, item: PaletteItem) {
  e.dataTransfer.setData(DND_MIME, JSON.stringify(item));
  e.dataTransfer.effectAllowed = "move";
}

/** Left rail: step types, integration actions and workflows to drag onto the canvas. */
export function Palette({ onAdd }: { onAdd: (item: PaletteItem) => void }) {
  const ns = useNs();
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const { data: integrations = [] } = useIntegrations();
  const { data: workflows = [] } = useResources("workflows", ns);
  const match = (...parts: (string | undefined)[]) => !q || parts.join(" ").toLowerCase().includes(q.toLowerCase());

  const types = STEP_TYPES.filter((t) => match(t.label, t.type, t.description));

  // Enter adds the first match, in the order the palette lists them.
  const firstMatch = (): PaletteItem | undefined => {
    if (types[0]) return { type: types[0].type };
    for (const i of integrations) {
      const a = i.actions.find((a) => match(i.key, i.title, a.name, a.description));
      if (a) return { type: "action", preset: { action: `${i.key}.${a.name}`, with: {} } };
    }
    const w = workflows.find((w) => match(w.metadata.name, w.metadata.description ?? ""));
    if (w) return { type: "workflow", preset: { workflow: w.metadata.name, with: {}, id: w.metadata.name.replace(/-/g, "_") } };
  };

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border p-2.5">
        <div className="flex h-8 items-center gap-2 rounded-lg border border-border bg-subtle/60 px-2.5 focus-within:border-ring">
          <Search className="size-3.5 text-faint" />
          <input value={q} onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") setQ("");
              if (e.key !== "Enter" || !q) return;
              const item = firstMatch();
              if (item) {
                onAdd(item);
                setQ("");
              }
            }}
            placeholder="Search steps & actions" className="min-w-0 flex-1 bg-transparent text-[13px] outline-none placeholder:text-faint" />
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
        {types.length > 0 && <GroupTitle>Steps</GroupTitle>}
        <div className="space-y-0.5">
          {types.map((t) => (
            <Tile key={t.type} item={{ type: t.type }} title={t.label} description={t.description} onAdd={onAdd} />
          ))}
        </div>

        {integrations.map((i) => {
          const actions = i.actions.filter((a) => match(i.key, i.title, a.name, a.description));
          if (!actions.length) return null;
          const expanded = open[i.key] ?? !!q;
          return (
            <div key={i.key} className="mt-3">
              <button onClick={() => setOpen({ ...open, [i.key]: !expanded })} className="flex w-full items-center gap-1 px-1.5 py-1 text-left">
                <ChevronRight className={cn("size-3.5 text-faint transition-transform", expanded && "rotate-90")} />
                <span className="flex-1 text-[11px] font-semibold tracking-wider text-faint uppercase">{i.title}</span>
                <span className="text-[11px] text-faint">{actions.length}</span>
              </button>
              {expanded && (
                <div className="space-y-0.5">
                  {actions.map((a) => (
                    <Tile
                      key={a.name}
                      item={{ type: "action", preset: { action: `${i.key}.${a.name}`, with: {} } }}
                      title={a.name.replace(/_/g, " ")}
                      mono={`${i.key}.${a.name}`}
                      description={a.description}
                      badge={a.mutating ? <Badge tone="warn">writes</Badge> : undefined}
                      onAdd={onAdd}
                    />
                  ))}
                </div>
              )}
            </div>
          );
        })}

        {workflows.filter((w) => match(w.metadata.name, w.metadata.description ?? "")).length > 0 && (
          <div className="mt-3">
            <button onClick={() => setOpen({ ...open, _wf: !(open._wf ?? !!q) })} className="flex w-full items-center gap-1 px-1.5 py-1 text-left">
              <ChevronRight className={cn("size-3.5 text-faint transition-transform", (open._wf ?? !!q) && "rotate-90")} />
              <span className="flex-1 text-[11px] font-semibold tracking-wider text-faint uppercase">Workflows</span>
            </button>
            {(open._wf ?? !!q) &&
              workflows
                .filter((w) => match(w.metadata.name, w.metadata.description ?? ""))
                .map((w) => (
                  <Tile
                    key={w.metadata.name}
                    item={{ type: "workflow", preset: { workflow: w.metadata.name, with: {}, id: w.metadata.name.replace(/-/g, "_") } }}
                    title={w.metadata.name}
                    description={w.metadata.description ?? "Run as a sub-workflow"}
                    icon={<Puzzle className="size-3.5" />}
                    onAdd={onAdd}
                  />
                ))}
          </div>
        )}
      </div>
      <div className="border-t border-border px-3 py-2 text-[11px] leading-snug text-faint">Drag onto the canvas, or click to add after the selected step. Enter adds the first match.</div>
    </div>
  );
}

function GroupTitle({ children }: { children: React.ReactNode }) {
  return <div className="px-1.5 pt-0.5 pb-1 text-[11px] font-semibold tracking-wider text-faint uppercase">{children}</div>;
}

function Tile({ item, title, description, onAdd, mono, badge, icon }: {
  item: PaletteItem;
  title: string;
  description: string;
  onAdd: (item: PaletteItem) => void;
  mono?: string;
  badge?: React.ReactNode;
  icon?: React.ReactNode;
}) {
  const info = stepType(item.type);
  const Icon = info.icon;
  return (
    <Tooltip content={description} side="right">
      <div
        draggable
        onDragStart={(e) => onDragStart(e, item)}
        onClick={() => onAdd(item)}
        className="group flex cursor-grab items-center gap-2 rounded-lg border border-transparent px-1.5 py-1.5 transition-colors select-none hover:border-border hover:bg-panel hover:shadow-card active:cursor-grabbing"
      >
        <div className="flex size-6 shrink-0 items-center justify-center rounded-md text-white" style={{ background: info.color }}>
          {icon ?? <Icon className="size-3.5" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[12.5px] font-medium first-letter:uppercase">{title}</div>
          {mono && <div className="truncate font-mono text-[10.5px] text-faint">{mono}</div>}
        </div>
        {badge}
        <GripVertical className="size-3.5 text-faint opacity-0 group-hover:opacity-100" />
      </div>
    </Tooltip>
  );
}
