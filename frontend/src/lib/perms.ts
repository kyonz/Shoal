/**
 * Client-side permission checks, mirroring the server's `authorize()` so the
 * UI hides what the caller cannot do. The server stays the authority: every
 * call is checked again there.
 */
export interface PermissionSummary {
  tenant: string[];
  namespaces: Record<string, string[]>;
}

function glob(pattern: string, value: string): boolean {
  const re = new RegExp(`^${pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`);
  return re.test(value);
}

/** Same rules as the server: `*`, or `resource:verb` with fnmatch globs on each half. */
export function matches(pattern: string, permission: string): boolean {
  if (pattern === "*" || pattern === permission) return true;
  const [pRes, pVerb = ""] = pattern.split(":", 2);
  const [res, verb = ""] = permission.split(":", 2);
  return glob(pRes || "*", res) && glob(pVerb || "*", verb);
}

export function can(summary: PermissionSummary | undefined, permission: string, namespace?: string): boolean {
  if (!summary) return false;
  if (summary.tenant.some((p) => matches(p, permission))) return true;
  if (namespace) return (summary.namespaces[namespace] ?? []).some((p) => matches(p, permission));
  return Object.values(summary.namespaces).some((list) => list.some((p) => matches(p, permission)));
}
