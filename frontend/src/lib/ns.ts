import { useLocation, useNavigate, useParams } from "react-router";
import { createContext, createElement, useCallback, useContext, type ReactNode } from "react";

const LAST_NS = "shoal.namespace";

/** The tenant-wide namespace that holds organisation-level connections and secrets. */
export const SHARED_NS = "shared";
/** Sections of the shared namespace that the admin console owns (org-level configuration). */
const ORG_SECTIONS = new Set(["connections", "secrets"]);

const NsOverride = createContext<string | null>(null);

/** Render namespaced pages (connections, secrets) for the organisation, inside the admin console. */
export function OrgScope({ children }: { children: ReactNode }) {
  return createElement(NsOverride.Provider, { value: SHARED_NS }, children);
}

/** True inside the admin console's organisation pages. */
export function useOrgScope(): boolean {
  return useContext(NsOverride) === SHARED_NS;
}

export function rememberNamespace(ns: string) {
  try {
    localStorage.setItem(LAST_NS, ns);
  } catch {
    /* ignore */
  }
}

export function lastNamespace(): string {
  try {
    return localStorage.getItem(LAST_NS) || "default";
  } catch {
    return "default";
  }
}

/** The namespace in the URL (`/ns/:ns/...`), else the last one used. */
export function useNs(): string {
  const { ns } = useParams();
  const override = useContext(NsOverride);
  return override ?? ns ?? lastNamespace();
}

/**
 * Build a namespaced path: nsPath("acme", "workflows") -> /ns/acme/workflows.
 * Organisation connections and secrets (the shared namespace) live in the admin console.
 */
export function nsPath(ns: string, ...parts: string[]) {
  const tail = parts.map(encodeURIComponent).join("/");
  if (ns === SHARED_NS && ORG_SECTIONS.has(parts[0] ?? "")) return `/admin/${tail}`;
  return `/ns/${encodeURIComponent(ns)}/${tail}`.replace(/\/$/, "");
}

/** Switch namespace while keeping the current section (workflows, runs, ...). */
export function useSwitchNamespace() {
  const navigate = useNavigate();
  const location = useLocation();
  return useCallback(
    (ns: string) => {
      rememberNamespace(ns);
      const m = location.pathname.match(/^\/ns\/[^/]+\/([^/]+)/);
      navigate(m ? nsPath(ns, m[1]) : nsPath(ns, "workflows"));
    },
    [location.pathname, navigate],
  );
}
