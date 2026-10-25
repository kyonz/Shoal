import { useLocation, useNavigate, useParams } from "react-router";
import { useCallback } from "react";

const LAST_NS = "shoal.namespace";

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
  return ns ?? lastNamespace();
}

/** Build a namespaced path: nsPath("acme", "workflows") -> /ns/acme/workflows */
export function nsPath(ns: string, ...parts: string[]) {
  return `/ns/${encodeURIComponent(ns)}/${parts.map(encodeURIComponent).join("/")}`.replace(/\/$/, "");
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
