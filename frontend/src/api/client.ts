/**
 * The typed API client. Types come from the server's OpenAPI document
 * (`npm run gen:api` regenerates `schema.d.ts`), so the UI uses exactly the
 * public API that integrators use.
 *
 * Credentials: a browser session (HttpOnly cookie from OIDC sign-in) is sent
 * automatically; an API key or the bootstrap admin token pasted on the sign-in
 * page is kept in localStorage and sent as a bearer token.
 */
import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Resource = Schemas["ResourceOut"];
export type Run = Schemas["RunOut"];
export type RunDetail = Schemas["RunDetail"];
export type Step = Schemas["StepOut"];
/** `GET /v1/runs/{id}/events` (and each SSE message's data). */
export interface RunEvent {
  id: number;
  run_id: string;
  step_id: string | null;
  type: string;
  data: Record<string, unknown>;
  created_at: string;
}

const TOKEN_KEY = "shoal.token";
const TENANT_KEY = "shoal.tenant";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null) {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    /* private mode */
  }
}

export const credentials = {
  get token() {
    return read(TOKEN_KEY);
  },
  set token(value: string | null) {
    write(TOKEN_KEY, value);
  },
  /** Tenant slug sent as X-AWM-Tenant (needed with the system admin token). */
  get tenant() {
    return read(TENANT_KEY);
  },
  set tenant(value: string | null) {
    write(TENANT_KEY, value);
  },
  headers(): Record<string, string> {
    // Tags audit entries with source=ui.
    const h: Record<string, string> = { "X-AWM-Client": "ui" };
    const token = this.token;
    if (token) h.Authorization = `Bearer ${token}`;
    const tenant = this.tenant;
    if (tenant) h["X-AWM-Tenant"] = tenant;
    return h;
  },
};

export class ApiError extends Error {
  status: number;
  code: string;
  detail: string;
  body: unknown;

  constructor(status: number, body: unknown) {
    const b = (body ?? {}) as { code?: string; detail?: unknown; title?: string };
    const detail =
      typeof b.detail === "string" ? b.detail : b.title ?? (status === 0 ? "network error" : `HTTP ${status}`);
    super(detail);
    this.status = status;
    this.code = b.code ?? `http_${status}`;
    this.detail = detail;
    this.body = body;
  }

  /** Field errors from a 422 problem document (top-level `errors` or `details.errors`). */
  get fieldErrors(): { loc: string[]; msg: string }[] {
    const b = this.body as { errors?: unknown; details?: { errors?: unknown } } | undefined;
    const list = (Array.isArray(b?.errors) ? b.errors : Array.isArray(b?.details?.errors) ? b.details.errors : []) as {
      loc: string[] | string;
      msg: string;
    }[];
    return list.map((e) => ({ loc: Array.isArray(e.loc) ? e.loc.map(String) : String(e.loc ?? "").split("."), msg: e.msg }));
  }

  /** Per-document results of a failed `/v1/apply`. */
  get applyResults(): Schemas["ApplyResult"][] {
    const b = this.body as { details?: { results?: Schemas["ApplyResult"][] } } | undefined;
    return b?.details?.results ?? [];
  }
}

const auth: Middleware = {
  onRequest({ request }) {
    for (const [k, v] of Object.entries(credentials.headers())) request.headers.set(k, v);
    return request;
  },
};

export const client = createClient<paths>({ baseUrl: "", credentials: "include" });
client.use(auth);

/** Unwrap an openapi-fetch result, throwing ApiError on failure. */
export async function unwrap<T>(
  promise: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await promise;
  if (!response.ok) throw new ApiError(response.status, error);
  return data as T;
}

/** For the few calls that are not JSON in/JSON out (YAML apply, SSE). */
export async function raw(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  for (const [k, v] of Object.entries(credentials.headers())) headers.set(k, v);
  const response = await fetch(path, { ...init, headers, credentials: "include" });
  if (!response.ok) {
    let body: unknown = undefined;
    try {
      body = await response.json();
    } catch {
      /* not json */
    }
    throw new ApiError(response.status, body);
  }
  return response;
}

export async function rawJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await raw(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers as Record<string, string>) },
  });
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Apply multi-document YAML or JSON (`POST /v1/apply`). */
export async function applyYaml(
  body: string,
  { namespace, dryRun = false }: { namespace: string; dryRun?: boolean },
): Promise<Schemas["ApplyResult"][]> {
  const q = new URLSearchParams({ namespace, dry_run: String(dryRun) });
  const response = await raw(`/v1/apply?${q}`, {
    method: "POST",
    headers: { "Content-Type": "application/yaml" },
    body,
  });
  return response.json();
}
