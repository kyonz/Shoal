/** Who did something: `user:<username>`, `sa:<name>`, `account:<email>`, `hub_user:<id>`, `system:<name>`. */
import { Bot, Globe, Server, ShieldCheck, UserRound } from "lucide-react";
import type { ComponentType } from "react";
import { cn } from "@/lib/cn";

export type ActorKind = "user" | "service" | "account" | "hub" | "system" | "other";

export interface ParsedActor {
  kind: ActorKind;
  name: string;
  /** Short label for the kind, shown as a tag next to the name (none for plain users). */
  tag?: string;
  /** Longer explanation, for a tooltip. */
  title: string;
}

const PREFIXES: Record<string, Omit<ParsedActor, "name" | "title"> & { describe: (name: string) => string }> = {
  user: { kind: "user", describe: (n) => `Person ${n}` },
  sa: { kind: "service", tag: "Service", describe: (n) => `Service account ${n}` },
  account: {
    kind: "account",
    tag: "Instance",
    describe: (n) => `${n}, acting as an instance admin or auditor in a tenant they aren't a member of`,
  },
  hub_user: { kind: "hub", tag: "Hub", describe: (n) => `${n}, through the federation hub` },
  system: { kind: "system", tag: "System", describe: (n) => (n === "admin" ? "The instance's break-glass admin token" : `System (${n})`) },
};

export function parseActor(actor: string | null | undefined): ParsedActor | null {
  if (!actor) return null;
  const i = actor.indexOf(":");
  const prefix = i > 0 ? actor.slice(0, i) : "";
  const name = i > 0 ? actor.slice(i + 1) : actor;
  const known = PREFIXES[prefix];
  if (!known) return { kind: actor === "system" ? "system" : "other", name: actor, title: actor };
  const { describe, ...rest } = known;
  return { ...rest, name: prefix === "system" && name === "admin" ? "Admin token" : name, title: describe(name) };
}

const ICONS: Record<ActorKind, ComponentType<{ className?: string }>> = {
  user: UserRound,
  service: Bot,
  account: ShieldCheck,
  hub: Globe,
  system: Server,
  other: UserRound,
};

const TAG_TONE: Partial<Record<ActorKind, string>> = {
  account: "border-accent/20 bg-accent-soft text-accent",
  hub: "border-info/20 bg-info-soft text-info",
};

/** An actor with an icon and, for anyone but a plain user, a tag saying what kind of actor it is. */
export function Actor({ actor, className, fallback = "—" }: { actor: string | null | undefined; className?: string; fallback?: string }) {
  const a = parseActor(actor);
  if (!a) return <span className="text-faint">{fallback}</span>;
  const Icon = ICONS[a.kind];
  return (
    <span className={cn("inline-flex max-w-full min-w-0 items-center gap-1.5", className)} title={`${a.title}\n${actor}`}>
      <Icon className="size-3.5 shrink-0 text-faint" />
      <span className="truncate font-mono text-xs">{a.name}</span>
      {a.tag && (
        <span className={cn("shrink-0 rounded border border-border px-1 text-[10px] leading-4 font-medium text-muted", TAG_TONE[a.kind])}>
          {a.tag}
        </span>
      )}
    </span>
  );
}

/** An actor as plain text, for sentences: `trevor@acme.dev`, `deploy-bot (service account)`, `ana@acme.dev (instance)`. */
export function actorName(actor: string | null | undefined, fallback = "unknown"): string {
  const a = parseActor(actor);
  if (!a) return fallback;
  const suffix: Partial<Record<ActorKind, string>> = { service: "service account", account: "instance", hub: "hub" };
  return suffix[a.kind] ? `${a.name} (${suffix[a.kind]})` : a.name;
}
