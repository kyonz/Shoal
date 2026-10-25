/**
 * Federated tenants are served by a member deployment through the hub. When
 * that member is offline the hub answers 503 `federation_disconnected`, and a
 * read-only link refuses writes with 403 `federation_read_only`. Both are
 * states of the whole tenant, so they show as one banner, not per-screen errors.
 */
import { create } from "zustand";
import { useQuery } from "@tanstack/react-query";
import { ApiError, client, unwrap } from "@/api/client";

export const FEDERATION_DISCONNECTED = "federation_disconnected";
export const FEDERATION_READ_ONLY = "federation_read_only";
/** The hub still counts the member as connected but it didn't answer: the same thing to the person. */
export const FEDERATION_TIMEOUT = "federation_timeout";

interface FederationState {
  disconnected: boolean;
  /** A call was refused with a plain 403; on a federated tenant that is the member's RBAC speaking. */
  forbidden: boolean;
  readOnly: boolean;
  dismissedReadOnly: boolean;
  dismissReadOnly: () => void;
}

export const useFederationState = create<FederationState>((set) => ({
  disconnected: false,
  forbidden: false,
  readOnly: false,
  dismissedReadOnly: false,
  dismissReadOnly: () => set({ dismissedReadOnly: true }),
}));

export function federationCode(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  if (error.code === FEDERATION_TIMEOUT) return FEDERATION_DISCONNECTED;
  return error.code === FEDERATION_DISCONNECTED || error.code === FEDERATION_READ_ONLY ? error.code : null;
}

/** Record a failed call (wired into the query and mutation caches). */
export function noteFederationError(error: unknown) {
  const code = federationCode(error);
  if (code === FEDERATION_DISCONNECTED) useFederationState.setState({ disconnected: true });
  if (code === FEDERATION_READ_ONLY) useFederationState.setState({ readOnly: true, dismissedReadOnly: false });
  if (error instanceof ApiError && error.status === 403 && !code && !useFederationState.getState().forbidden)
    useFederationState.setState({ forbidden: true });
}

/**
 * The hub link for the tenant being viewed, when the viewer is the hub's
 * operator (only operators can list links). Members grant hub people access
 * themselves; the operator token gets no automatic rights there.
 */
export function useFederatedLink(isOperator: boolean, tenantSlug: string | undefined) {
  const links = useQuery({
    queryKey: ["admin", "federation", "links"],
    queryFn: () => unwrap(client.GET("/v1/federation/links")),
    enabled: isOperator && !!tenantSlug,
    staleTime: 60_000,
  });
  return links.data?.find((l) => l.tenant === tenantSlug && l.status !== "revoked") ?? null;
}

/** Any successful read means the member is reachable again. */
export function noteSuccess() {
  if (useFederationState.getState().disconnected) useFederationState.setState({ disconnected: false });
}

export function federationMessage(code: string): string {
  return code === FEDERATION_DISCONNECTED
    ? "This organisation's deployment is offline, so the hub can't reach its data right now. It will appear again as soon as it reconnects."
    : "This organisation is linked to the hub read-only. You can browse everything, but changes have to be made in its own deployment.";
}
