/**
 * Federated tenants are served by a member deployment through the hub. When
 * that member is offline the hub answers 503 `federation_disconnected` (or
 * `federation_revoked` once an operator revoked the link), and a read-only link
 * refuses writes with 403 `federation_read_only`. Both are
 * states of the whole tenant, so they show as one banner, not per-screen errors.
 */
import { create } from "zustand";
import { useQuery } from "@tanstack/react-query";
import { ApiError, client, unwrap } from "@/api/client";

export type FederationMode = "read" | "read_write";
export const MODE_LABEL: Record<FederationMode, string> = { read: "Read only", read_write: "Read and write" };
export const MODE_OPTIONS = [
  { value: "read" as FederationMode, label: "Read only" },
  { value: "read_write" as FederationMode, label: "Read and write" },
];

export const FEDERATION_DISCONNECTED = "federation_disconnected";
export const FEDERATION_READ_ONLY = "federation_read_only";
export const FEDERATION_REVOKED = "federation_revoked";
/** The hub still counts the member as connected but it didn't answer: the same thing to the person. */
export const FEDERATION_TIMEOUT = "federation_timeout";

interface FederationState {
  disconnected: boolean;
  revoked: boolean;
  /** A call was refused with a plain 403; on a federated tenant that is the member's RBAC speaking. */
  forbidden: boolean;
  readOnly: boolean;
  dismissedReadOnly: boolean;
  dismissReadOnly: () => void;
}

export const useFederationState = create<FederationState>((set) => ({
  disconnected: false,
  revoked: false,
  forbidden: false,
  readOnly: false,
  dismissedReadOnly: false,
  dismissReadOnly: () => set({ dismissedReadOnly: true }),
}));

export function federationCode(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  if (error.code === FEDERATION_TIMEOUT) return FEDERATION_DISCONNECTED;
  return [FEDERATION_DISCONNECTED, FEDERATION_READ_ONLY, FEDERATION_REVOKED].includes(error.code) ? error.code : null;
}

/** Record a failed call (wired into the query and mutation caches). */
export function noteFederationError(error: unknown) {
  const code = federationCode(error);
  if (code === FEDERATION_DISCONNECTED) useFederationState.setState({ disconnected: true });
  if (code === FEDERATION_REVOKED) useFederationState.setState({ revoked: true });
  if (code === FEDERATION_READ_ONLY) useFederationState.setState({ readOnly: true, dismissedReadOnly: false });
  if (error instanceof ApiError && error.status === 403 && !code && !useFederationState.getState().forbidden)
    useFederationState.setState({ forbidden: true });
}

/**
 * The hub link for the tenant being viewed, when the viewer is an instance
 * admin or auditor (only they can list links). Members grant hub people access
 * themselves; the operator token gets no automatic rights there.
 */
export function useFederatedLink(isInstanceReader: boolean, tenantSlug: string | undefined) {
  const links = useQuery({
    queryKey: ["admin", "federation", "links"],
    queryFn: () => unwrap(client.GET("/v1/federation/links")),
    enabled: isInstanceReader && !!tenantSlug,
    staleTime: 60_000,
  });
  return links.data?.find((l) => l.tenant === tenantSlug && l.status !== "revoked" && l.status !== "left") ?? null;
}

/** Any successful read means the member is reachable again. */
export function noteSuccess() {
  const { disconnected, revoked } = useFederationState.getState();
  if (disconnected || revoked) useFederationState.setState({ disconnected: false, revoked: false });
}

export function federationMessage(code: string): string {
  if (code === FEDERATION_REVOKED)
    return "The hub no longer forwards to this organisation's deployment, which keeps running on its own with all of its data. An operator can federate it again.";
  return code === FEDERATION_DISCONNECTED
    ? "This organisation's deployment is offline, so the hub can't reach its data right now. It will appear again as soon as it reconnects."
    : "This organisation is linked to the hub read-only. You can browse everything, but changes have to be made in its own deployment.";
}
