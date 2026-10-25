import { useQuery } from "@tanstack/react-query";
import { ArrowRight, KeyRound, LogIn, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { client, unwrap } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { Button, ErrorNotice, Field, Input, Spinner } from "@/components/ui";
import { ShoalLogo } from "@/components/layout/AppShell";

const LAST_TENANT = "shoal.loginTenant";

function lastTenant() {
  try {
    return localStorage.getItem(LAST_TENANT) ?? "";
  } catch {
    return "";
  }
}

export default function Login() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { signedIn, signInWithToken } = useAuth();
  const [tenant, setTenant] = useState(lastTenant);
  const [submitted, setSubmitted] = useState(lastTenant());
  const [mode, setMode] = useState<"sso" | "token">("sso");
  const [token, setToken] = useState("");
  const [tokenTenant, setTokenTenant] = useState("");
  const [tokenError, setTokenError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const next = params.get("next") || "/";
  const callbackError = params.get("error");

  const providers = useQuery({
    queryKey: ["login-providers", submitted],
    queryFn: () => unwrap(client.GET("/v1/auth/providers", { params: { query: { tenant: submitted } } })),
    enabled: !!submitted,
    retry: false,
  });

  if (signedIn) return <Navigate to={next} replace />;

  const startSso = (loginUrl: string) => {
    const url = new URL(loginUrl, window.location.origin);
    url.searchParams.set("redirect_to", next);
    // Same-origin in production and behind the dev proxy, so the session cookie lands here.
    window.location.assign(url.pathname + url.search);
  };

  const submitToken = async () => {
    setBusy(true);
    setTokenError(null);
    try {
      await signInWithToken(token, tokenTenant || null);
      navigate(next, { replace: true });
    } catch (e) {
      setTokenError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative flex min-h-full items-center justify-center overflow-hidden bg-bg px-4 py-10">
      <div aria-hidden className="pointer-events-none absolute inset-0">
        <div className="absolute -top-40 left-1/2 h-[520px] w-[900px] -translate-x-1/2 rounded-full bg-accent/10 blur-3xl" />
        <div className="absolute right-[-10%] bottom-[-20%] h-[420px] w-[620px] rounded-full bg-info/10 blur-3xl" />
      </div>
      <div className="relative w-full max-w-[400px]">
        <div className="mb-7 flex flex-col items-center text-center">
          <div className="mb-5"><ShoalLogo className="h-12" /></div>
          <h1 className="text-xl font-semibold tracking-tight">Sign in to your workspace</h1>
          <p className="mt-1 text-[13px] text-muted">Agent workflows for your whole organisation.</p>
        </div>
        <div className="rounded-2xl border border-border bg-panel p-6 shadow-pop">
          {callbackError && <ErrorNotice className="mb-4" error={`Sign-in failed: ${callbackError}`} />}
          {mode === "sso" ? (
            <div className="space-y-4">
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  const slug = tenant.trim().toLowerCase();
                  try {
                    localStorage.setItem(LAST_TENANT, slug);
                  } catch {
                    /* ignore */
                  }
                  setSubmitted(slug);
                }}
              >
                <Field label="Organisation" description="Your tenant's short name, e.g. acme.">
                  <div className="flex gap-2">
                    <Input autoFocus value={tenant} onChange={(e) => setTenant(e.target.value)} placeholder="acme" autoComplete="organization" />
                    <Button type="submit" variant="secondary" icon={<ArrowRight className="size-4" />} aria-label="Continue" />
                  </div>
                </Field>
              </form>
              {providers.isFetching && (
                <div className="flex justify-center py-2">
                  <Spinner />
                </div>
              )}
              {providers.error && <ErrorNotice error={providers.error} />}
              {providers.data && (
                <div className="space-y-2">
                  {providers.data.length === 0 && (
                    <p className="rounded-lg bg-subtle px-3 py-2.5 text-[13px] text-muted">
                      No single sign-on is set up for <b className="text-fg">{submitted}</b> yet. Sign in with an API key, then add an OIDC provider under Admin › Sign-in & SCIM.
                    </p>
                  )}
                  {providers.data.map((p) => (
                    <Button key={p.name} variant="primary" className="w-full justify-center" size="md" icon={<LogIn className="size-4" />} onClick={() => startSso(p.login_url)}>
                      Continue with {p.display_name}
                    </Button>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <form
              className="space-y-4"
              onSubmit={(e) => {
                e.preventDefault();
                void submitToken();
              }}
            >
              <Field label="API key or admin token" description="Keys look like awm_xxxx_…. Stored in this browser only.">
                <Input autoFocus mono type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="awm_…" />
              </Field>
              <Field label="Tenant" hint="optional" description="Only needed with the system admin token.">
                <Input value={tokenTenant} onChange={(e) => setTokenTenant(e.target.value)} placeholder="acme" />
              </Field>
              {tokenError ? <ErrorNotice error={tokenError} /> : null}
              <Button type="submit" variant="primary" className="w-full justify-center" loading={busy} disabled={!token.trim()} icon={<KeyRound className="size-4" />}>
                Sign in with key
              </Button>
            </form>
          )}
          <div className="mt-5 border-t border-border pt-4 text-center">
            <button className="text-[13px] text-muted hover:text-fg" onClick={() => setMode(mode === "sso" ? "token" : "sso")}>
              {mode === "sso" ? "Use an API key instead" : "Use single sign-on instead"}
            </button>
          </div>
        </div>
        <p className="mt-6 flex items-center justify-center gap-1.5 text-xs text-faint">
          <ShieldCheck className="size-3.5" /> OpenID Connect with PKCE · SCIM provisioning
        </p>
      </div>
    </div>
  );
}
