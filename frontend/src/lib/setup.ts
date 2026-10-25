/**
 * Missing configuration: errors that mean "set something up first" rather than
 * "something broke". The server sends them as `missing_configuration` problems
 * (and `credential_required` for a person's own account), naming what is
 * missing and where in the UI to fix it; a failed step or run carries the same
 * fields in its error.
 */
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { useAuth } from "@/lib/auth";
import { ApiError, client, unwrap, type Schemas } from "@/api/client";

export interface SetupFix {
  /** A path in this UI. */
  href: string;
  label: string;
  /** The permission fixing it takes; null when the caller fixes it themselves. */
  permission?: string | null;
  namespace?: string | null;
}

export interface SetupProblem {
  code: "missing_configuration" | "credential_required";
  detail: string;
  missing: { kind?: string; name?: string; namespace?: string; field?: string };
  fix: SetupFix | null;
}

export const MISSING_CONFIGURATION = "missing_configuration";
export const CREDENTIAL_REQUIRED = "credential_required";

type Loose = Record<string, unknown> | null | undefined;

function asFix(value: unknown): SetupFix | null {
  const f = value as Loose;
  return f && typeof f.href === "string" ? (f as unknown as SetupFix) : null;
}

/** From a problem document, a step/run error or a `SetupProblem`-shaped object. */
export function problemFrom(value: unknown, detail?: string): SetupProblem | null {
  const v = value as Loose;
  if (!v || typeof v !== "object") return null;
  const details = (v.details as Loose) ?? v;
  const code = (v.code ?? details?.code) as string | undefined;
  if (code !== MISSING_CONFIGURATION && code !== CREDENTIAL_REQUIRED) return null;
  const message = detail ?? (typeof v.detail === "string" ? v.detail : typeof v.message === "string" ? v.message : "");
  const missing =
    code === CREDENTIAL_REQUIRED
      ? { kind: "Credential", name: String(details?.connection ?? ""), namespace: String(details?.namespace ?? "") }
      : ((details?.missing as SetupProblem["missing"]) ?? {});
  return { code, detail: message, missing, fix: asFix(details?.fix) };
}

/** The setup problem behind an error, if that is what it is. */
export function setupProblem(error: unknown): SetupProblem | null {
  if (error instanceof ApiError) return problemFrom(error.body, error.detail);
  return problemFrom(error);
}

const KIND_WORDS: Record<string, string> = {
  Gateway: "LLM gateway",
  ModelPolicy: "model policy",
  SandboxProfile: "sandbox profile",
  CloudIdentity: "cloud identity",
  SkillSource: "skill source",
  Credential: "connected account",
};

export function kindWords(kind: string | undefined): string {
  if (!kind) return "configuration";
  return KIND_WORDS[kind] ?? kind.replace(/(?<!^)([A-Z])/g, " $1").toLowerCase();
}

/** Server messages are lower-case clauses; show them as sentences. */
export function sentence(text: string): string {
  const t = text.trim();
  if (!t) return t;
  return `${t[0].toUpperCase()}${t.slice(1)}${/[.!?]$/.test(t) ? "" : "."}`;
}

/** A short heading: "LLM gateway needed", "Secret not set"... */
export function problemTitle(p: SetupProblem): string {
  const kind = p.missing.kind;
  if (p.code === CREDENTIAL_REQUIRED) return "Connect your account";
  if (kind === "Secret") return "A secret is not set";
  if (kind === "Setting") return "Not enabled on this server";
  const what = kindWords(kind);
  // A fix that edits an existing resource (rather than adding one) means it exists but needs a change.
  const exists = !!p.fix && !/\/new(\?|$)/.test(p.fix.href) && /^\/(admin|ns)\//.test(p.fix.href);
  return exists ? `Check the ${what}` : `${what[0].toUpperCase()}${what.slice(1)} needed`;
}

/** Who can fix it: the caller (`self`), the caller because they hold the permission (`you`), or someone else (`admin`). */
export function whoFixes(p: SetupProblem, can: (permission: string, namespace?: string) => boolean): "self" | "you" | "admin" {
  if (!p.fix) return "admin";
  if (!p.fix.permission) return p.missing.kind === "Setting" ? "admin" : "self";
  return can(p.fix.permission, p.fix.namespace ?? undefined) ? "you" : "admin";
}

/** "Ask a tenant admin to add an LLM gateway." for people who cannot fix it. */
export function askAdmin(p: SetupProblem): string {
  if (p.missing.kind === "Setting") return "Ask whoever runs this Shoal server to change its configuration.";
  const tenantWide = p.fix?.href.startsWith("/admin/");
  const who = tenantWide ? "a tenant admin" : `an admin of the ${p.fix?.namespace ?? p.missing.namespace ?? ""} namespace`.replace("  ", " ");
  const label = p.fix?.label ? p.fix.label[0].toLowerCase() + p.fix.label.slice(1) : `set up the ${kindWords(p.missing.kind)}`;
  const name = p.missing.name && !label.includes(p.missing.name) ? ` (${p.missing.name})` : "";
  return `Ask ${who} to ${label}${name}.`;
}

export type SetupStatus = Schemas["SetupStatus"];

/** `GET /v1/setup`: what still needs setting up (model access, the assistant, sandboxes) and each gateway's state. */
export function useSetupStatus(namespace: string, enabled = true) {
  return useQuery({
    queryKey: ["setup-status", namespace],
    queryFn: () => unwrap(client.GET("/v1/setup", { params: { query: { namespace } } })),
    enabled,
    staleTime: 30_000,
    retry: false,
  });
}

// --------------------------------------------------------------------------- toasts

let navigateTo: ((href: string) => void) | null = null;
let canDo: (permission: string, namespace?: string) => boolean = () => false;

/** Mounted once in the app shell so `toastError` can link to the fix and knows who can make it. */
export function useSetupToasts() {
  const navigate = useNavigate();
  const { can } = useAuth();
  useEffect(() => {
    navigateTo = navigate;
    canDo = can;
  }, [navigate, can]);
}

/** Toast an error; a missing configuration gets its explanation and a link to fix it (or who to ask). */
export function toastError(error: unknown, fallback = "Something went wrong") {
  const problem = setupProblem(error);
  if (!problem) {
    toast.error(error instanceof ApiError ? error.detail : error instanceof Error ? error.message || fallback : fallback);
    return;
  }
  const who = whoFixes(problem, canDo);
  const fix = problem.fix;
  toast.warning(problemTitle(problem), {
    description: who === "admin" ? `${sentence(problem.detail)} ${askAdmin(problem)}` : sentence(problem.detail),
    duration: 10_000,
    action: who !== "admin" && fix && navigateTo ? { label: fix.label, onClick: () => navigateTo?.(fix.href) } : undefined,
  });
}
