import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, KeyRound, LogIn, Plus, RefreshCw, Trash2, Users } from "lucide-react";
import { useEffect, useState } from "react";
import { client, unwrap, type Schemas } from "@/api/client";
import { useNamespaces } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { TagInput } from "@/components/forms/SchemaForm";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  CopyField,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Page,
  PageHeader,
  Sheet,
  Skeleton,
  Switch,
  useConfirm,
} from "@/components/ui";
import { adminKeys, RoleSelect, useAction, useServiceAccounts } from "./shared";
import { TokenDialog } from "./TokenDialog";

type Provider = Schemas["OIDCProviderOut"];

const PRESETS: { label: string; issuer: string; groups?: string }[] = [
  { label: "Okta", issuer: "https://YOUR_ORG.okta.com", groups: "groups" },
  { label: "Microsoft Entra ID", issuer: "https://login.microsoftonline.com/TENANT_ID/v2.0", groups: "groups" },
  { label: "Google", issuer: "https://accounts.google.com" },
  { label: "Keycloak", issuer: "https://keycloak.example.com/realms/REALM", groups: "groups" },
  { label: "Auth0", issuer: "https://YOUR_TENANT.auth0.com/" },
  { label: "Dex", issuer: "https://dex.example.com", groups: "groups" },
];

export default function AuthSettings() {
  const origin = window.location.origin;
  const providers = useQuery({ queryKey: adminKeys.oidc, queryFn: () => unwrap(client.GET("/v1/oidc-providers")) });
  const [editing, setEditing] = useState<Provider | "new" | null>(null);
  const { me } = useAuth();

  return (
    <Page>
      <PageHeader
        title="Sign-in & SCIM"
        description="Let people sign in with your identity provider over OpenID Connect, and keep users and groups in sync with SCIM 2.0."
      />
      <div className="space-y-5">
        <Card>
          <CardHeader
            title="OIDC providers"
            description="Authorization-code flow with PKCE. ID tokens are verified against the provider's JWKS."
            actions={
              <Button variant="primary" size="sm" icon={<Plus className="size-3.5" />} onClick={() => setEditing("new")}>
                Add provider
              </Button>
            }
          />
          <div className="border-b border-border px-4 py-3">
            <Field label="Redirect URI" description="Register this as the allowed redirect / callback URL in your identity provider.">
              <CopyField value={`${origin}/v1/auth/callback`} />
            </Field>
          </div>
          {providers.isLoading ? (
            <div className="p-4">
              <Skeleton className="h-16" />
            </div>
          ) : !providers.data?.length ? (
            <EmptyState
              icon={<LogIn className="size-5" />}
              title="No identity providers"
              description="Add Okta, Entra ID, Google, Keycloak or any OpenID Connect provider."
            />
          ) : (
            <div className="divide-y divide-border">
              {providers.data.map((p) => (
                <button key={p.name} onClick={() => setEditing(p)} className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-subtle/60">
                  <span className="flex size-9 items-center justify-center rounded-lg border border-border bg-subtle text-accent">
                    <LogIn className="size-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 text-[13px] font-medium">
                      {p.display_name}
                      <span className="font-mono text-[11px] font-normal text-faint">{p.name}</span>
                    </div>
                    <div className="truncate font-mono text-[11px] text-muted">{p.issuer}</div>
                  </div>
                  <div className="flex items-center gap-1.5">
                    {p.groups_claim && <Badge>groups → {p.groups_claim}</Badge>}
                    {p.auto_provision && <Badge tone="info">auto-provision</Badge>}
                    {p.enabled ? (
                      <Badge tone="ok" dot>
                        Enabled
                      </Badge>
                    ) : (
                      <Badge dot>Disabled</Badge>
                    )}
                  </div>
                </button>
              ))}
            </div>
          )}
          {me?.tenant && (providers.data?.length ?? 0) > 0 && (
            <div className="border-t border-border px-4 py-3 text-xs text-muted">
              People sign in at <span className="font-mono">{origin}/login</span> with organisation{" "}
              <b className="font-mono text-fg">{me.tenant.slug}</b>.
            </div>
          )}
        </Card>
        <ScimCard />
      </div>
      <ProviderSheet provider={editing} onClose={() => setEditing(null)} />
    </Page>
  );
}

const EMPTY = {
  name: "",
  display_name: "",
  issuer: "",
  client_id: "",
  client_secret: "",
  scopes: ["openid", "email", "profile"],
  groups_claim: "",
  allowed_domains: [] as string[],
  auto_provision: true,
  default_role: "",
  default_namespace: "",
  enabled: true,
};

function ProviderSheet({ provider, onClose }: { provider: Provider | "new" | null; onClose: () => void }) {
  const isNew = provider === "new";
  const p = isNew ? null : provider;
  const [f, setF] = useState(EMPTY);
  const namespaces = useNamespaces();
  const confirm = useConfirm();
  useEffect(() => {
    if (provider === "new") setF(EMPTY);
    else if (provider)
      setF({
        name: provider.name,
        display_name: provider.display_name,
        issuer: provider.issuer,
        client_id: provider.client_id,
        client_secret: "",
        scopes: provider.scopes,
        groups_claim: provider.groups_claim ?? "",
        allowed_domains: provider.allowed_domains,
        auto_provision: provider.auto_provision,
        default_role: provider.default_role ?? "",
        default_namespace: provider.default_namespace ?? "",
        enabled: provider.enabled,
      });
  }, [provider]);

  const save = useAction(
    () =>
      unwrap(
        client.PUT("/v1/oidc-providers/{name}", {
          params: { path: { name: f.name } },
          body: {
            display_name: f.display_name || null,
            issuer: f.issuer.trim(),
            client_id: f.client_id.trim(),
            // Write-only: omit to keep the stored secret.
            client_secret: f.client_secret ? f.client_secret : undefined,
            scopes: f.scopes,
            groups_claim: f.groups_claim || null,
            allowed_domains: f.allowed_domains,
            auto_provision: f.auto_provision,
            default_role: f.default_role || null,
            default_namespace: f.default_namespace || null,
            enabled: f.enabled,
          },
        }),
      ),
    { success: "Identity provider saved", onSuccess: onClose },
  );
  const remove = useAction((name: string) => unwrap(client.DELETE("/v1/oidc-providers/{name}", { params: { path: { name } } })), {
    success: "Identity provider removed",
    onSuccess: onClose,
  });

  const valid = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(f.name) && f.issuer.startsWith("http") && f.client_id;

  return (
    <Sheet
      open={!!provider}
      onOpenChange={(o) => !o && onClose()}
      title={isNew ? "Add identity provider" : p?.display_name}
      description="Any OpenID Connect provider works."
      width="max-w-2xl"
      footer={
        <>
          {p && (
            <Button
              variant="ghost"
              className="mr-auto text-bad"
              icon={<Trash2 className="size-3.5" />}
              onClick={async () => {
                if (
                  await confirm({
                    title: `Remove ${p.display_name}?`,
                    description: "People who sign in only through this provider will no longer be able to sign in.",
                    danger: true,
                    confirmLabel: "Remove",
                    typeToConfirm: p.name,
                  })
                )
                  remove.mutate(p.name);
              }}
            >
              Remove
            </Button>
          )}
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={save.isPending} disabled={!valid} onClick={() => save.mutate(undefined)}>
            Save provider
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        {isNew && (
          <div>
            <div className="mb-1.5 text-xs font-medium">Start from a preset</div>
            <div className="flex flex-wrap gap-1.5">
              {PRESETS.map((pr) => (
                <button
                  key={pr.label}
                  onClick={() =>
                    setF((x) => ({
                      ...x,
                      display_name: pr.label,
                      name: x.name || pr.label.toLowerCase().split(" ")[0],
                      issuer: pr.issuer,
                      groups_claim: pr.groups ?? "",
                    }))
                  }
                  className="h-7 rounded-md border border-border px-2.5 text-xs text-muted hover:border-accent hover:text-fg"
                >
                  {pr.label}
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name" required description="Short id used in the login URL.">
            <Input mono value={f.name} disabled={!isNew} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="okta" />
          </Field>
          <Field label="Button label">
            <Input value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.target.value })} placeholder="Okta" />
          </Field>
        </div>
        <Field label="Issuer URL" required description="The provider's issuer; /.well-known/openid-configuration is discovered from it.">
          <Input mono value={f.issuer} onChange={(e) => setF({ ...f, issuer: e.target.value })} placeholder="https://acme.okta.com" />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Client ID" required>
            <Input mono value={f.client_id} onChange={(e) => setF({ ...f, client_id: e.target.value })} />
          </Field>
          <Field
            label="Client secret"
            hint={p?.has_client_secret ? "stored" : "optional"}
            description={p?.has_client_secret ? "Leave blank to keep the stored secret." : "Omit for public clients (PKCE only)."}
          >
            <Input
              mono
              type="password"
              value={f.client_secret}
              onChange={(e) => setF({ ...f, client_secret: e.target.value })}
              placeholder={p?.has_client_secret ? "••••••••" : ""}
              autoComplete="new-password"
            />
          </Field>
        </div>
        <Field label="Scopes">
          <TagInput value={f.scopes} onChange={(scopes) => setF({ ...f, scopes })} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Groups claim" description="ID token claim with group names; groups are created and kept in sync.">
            <Input mono value={f.groups_claim} onChange={(e) => setF({ ...f, groups_claim: e.target.value })} placeholder="groups" />
          </Field>
          <Field label="Allowed email domains" description="Empty allows any verified email.">
            <TagInput value={f.allowed_domains} onChange={(allowed_domains) => setF({ ...f, allowed_domains })} placeholder="acme.com" />
          </Field>
        </div>
        <div className="space-y-2.5 rounded-lg border border-border p-3">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs font-medium">Create users on first sign-in</div>
              <div className="text-xs text-faint">Otherwise only existing (or SCIM-provisioned) users can sign in.</div>
            </div>
            <Switch checked={f.auto_provision} onCheckedChange={(auto_provision) => setF({ ...f, auto_provision })} />
          </div>
          {f.auto_provision && (
            <div className="grid grid-cols-2 gap-3 pt-1">
              <Field label="Default role" description="Granted to new users.">
                <RoleSelect value={f.default_role} onChange={(default_role) => setF({ ...f, default_role })} />
              </Field>
              <Field label="In namespace" description="Empty grants it tenant-wide.">
                <NativeSelect value={f.default_namespace} onChange={(e) => setF({ ...f, default_namespace: e.target.value })}>
                  <option value="">Tenant-wide</option>
                  {(namespaces.data ?? []).map((n) => (
                    <option key={n.name} value={n.name}>
                      {n.display_name}
                    </option>
                  ))}
                </NativeSelect>
              </Field>
            </div>
          )}
        </div>
        <div className="flex items-center justify-between rounded-lg border border-border px-3 py-2">
          <div className="text-xs font-medium">Enabled</div>
          <Switch checked={f.enabled} onCheckedChange={(enabled) => setF({ ...f, enabled })} />
        </div>
      </div>
    </Sheet>
  );
}

function ScimCard() {
  const origin = window.location.origin;
  const { can } = useAuth();
  const sas = useServiceAccounts();
  const keys = useQuery({
    queryKey: adminKeys.apiKeys(true),
    queryFn: () => unwrap(client.GET("/v1/api-keys", { params: { query: { all: true } } })),
    enabled: can("api-keys:manage"),
  });
  const confirm = useConfirm();
  const [token, setToken] = useState<string | null>(null);
  const scim = sas.data?.find((s) => s.name === "scim");
  const scimKeys = (keys.data ?? []).filter((k) => scim && k.owner_id === scim.id && !k.revoked_at);

  const generate = useAction(
    async () => {
      if (!scim)
        await unwrap(
          client.PUT("/v1/service-accounts/{name}", {
            params: { path: { name: "scim" } },
            body: { display_name: "SCIM provisioning", description: "Used by the identity provider to provision users and groups." },
          }),
        );
      const created = await unwrap(
        client.POST("/v1/api-keys", {
          body: { name: `scim-${new Date().toISOString().slice(0, 10)}`, service_account: "scim", scopes: ["scim:provision"], namespaces: [] },
        }),
      );
      return created.token;
    },
    { success: "SCIM token created", onSuccess: (t) => setToken(t) },
  );
  const revoke = useAction((id: string) => unwrap(client.DELETE("/v1/api-keys/{key_id}", { params: { path: { key_id: id } } })), {
    success: "Token revoked",
  });

  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <Users className="size-4 text-accent" /> SCIM 2.0 provisioning
          </span>
        }
        description="Your identity provider creates, updates and deactivates users and groups. Deactivated users lose sessions and API keys immediately."
        actions={
          <Button
            variant={scimKeys.length ? "secondary" : "primary"}
            size="sm"
            loading={generate.isPending}
            icon={scimKeys.length ? <RefreshCw className="size-3.5" /> : <KeyRound className="size-3.5" />}
            onClick={async () => {
              if (
                scimKeys.length &&
                !(await confirm({
                  title: "Generate another SCIM token?",
                  description: "Existing tokens keep working until you revoke them, so you can rotate without downtime.",
                  confirmLabel: "Generate",
                }))
              )
                return;
              generate.mutate(undefined);
            }}
          >
            {scimKeys.length ? "New token" : "Generate SCIM token"}
          </Button>
        }
      />
      <div className="grid gap-4 p-4 md:grid-cols-2">
        <Field label="SCIM base URL" description="Paste into your IdP's SCIM connector (Okta, Entra ID, OneLogin, JumpCloud…).">
          <CopyField value={`${origin}/scim/v2`} />
        </Field>
        <Field label="Status">
          <div className="flex h-8 items-center gap-2 text-[13px]">
            {scimKeys.length ? (
              <>
                <CheckCircle2 className="size-4 text-ok" /> {scimKeys.length} active token{scimKeys.length > 1 ? "s" : ""}
                {scimKeys[0]?.last_used_at && <span className="text-xs text-faint">· last used {relTime(scimKeys[0].last_used_at)}</span>}
              </>
            ) : (
              <>
                <AlertTriangle className="size-4 text-warn" /> Not configured
              </>
            )}
          </div>
        </Field>
      </div>
      {scimKeys.length > 0 && (
        <div className="divide-y divide-border border-t border-border">
          {scimKeys.map((k) => (
            <div key={k.id} className="flex items-center gap-3 px-4 py-2 text-[13px]">
              <KeyRound className="size-3.5 text-faint" />
              <span className="font-medium">{k.name}</span>
              <span className="font-mono text-xs text-faint">awm_{k.prefix}_…</span>
              <span className="flex-1 text-xs text-faint">created {relTime(k.created_at)}</span>
              <Button
                variant="ghost"
                size="xs"
                className="text-bad"
                onClick={async () => {
                  if (
                    await confirm({
                      title: "Revoke SCIM token?",
                      description: "Provisioning with this token stops immediately.",
                      danger: true,
                      confirmLabel: "Revoke",
                    })
                  )
                    revoke.mutate(k.id);
                }}
              >
                Revoke
              </Button>
            </div>
          ))}
        </div>
      )}
      <TokenDialog
        token={token}
        onClose={() => setToken(null)}
        title="SCIM bearer token"
        description="Paste it as the API token / secret token in your identity provider. It is shown only once."
      />
    </Card>
  );
}
