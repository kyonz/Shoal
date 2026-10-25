/** Per-user connection credentials: the connection's mode, and each person's own token or OAuth grant. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, KeyRound, Link2, Trash2, UserRound, Users } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { ApiError, client, unwrap, type Schemas } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { dateTime, relTime } from "@/lib/format";
import { humanize, KeyValueInput } from "@/components/forms/SchemaForm";
import { Badge, Button, Card, CardHeader, CopyField, Dialog, Field, Input, NativeSelect, Segmented, Switch, useConfirm } from "@/components/ui";
import { SecretPicker } from "./shared";

export type CredentialMode = "shared" | "per_user" | "per_user_or_shared";
export interface OAuthSpec {
  authorize_url: string;
  token_url: string;
  scopes?: string[];
  client_id: string;
  client_secret_secret?: string | null;
  token_field?: string;
  extra_params?: Record<string, string>;
}
export type Credential = Schemas["CredentialOut"];

export interface PersonalSpec {
  credentials?: CredentialMode;
  user_fields?: string[];
  oauth?: OAuthSpec;
}

/** The per-user parts of a connection spec, leaving defaults out so the YAML stays small. */
export function pickPersonal(spec: PersonalSpec): PersonalSpec {
  const out: PersonalSpec = {};
  if (spec.credentials && spec.credentials !== "shared") out.credentials = spec.credentials;
  if (spec.user_fields?.length) out.user_fields = spec.user_fields;
  if (spec.oauth) out.oauth = spec.oauth;
  return out;
}

/** Fields each person supplies, as the engine computes them. */
export function userFields(spec: PersonalSpec & { secrets?: Record<string, string> }): string[] {
  const fields = new Set(spec.user_fields?.length ? spec.user_fields : Object.keys(spec.secrets ?? {}));
  if (spec.oauth) fields.add(spec.oauth.token_field ?? "token");
  return [...fields];
}

export const MODE_LABEL: Record<CredentialMode, string> = {
  shared: "Shared",
  per_user: "Each person's own",
  per_user_or_shared: "Own, else shared",
};

// --------------------------------------------------------------------------- data

export const credentialKeys = { mine: ["me", "credentials"] as const, user: (u: string) => ["user-credentials", u] as const };

/** Only people have their own credentials; service accounts get a 403. */
export function useIsPerson() {
  return useAuth().me?.actor?.startsWith("user:") ?? false;
}

export function useMyCredentials() {
  const person = useIsPerson();
  return useQuery({ queryKey: credentialKeys.mine, queryFn: () => unwrap(client.GET("/v1/me/credentials")), enabled: person });
}

export function useSetCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ns, connection, values }: { ns: string; connection: string; values: Record<string, string> }) =>
      unwrap(client.PUT("/v1/me/credentials/{namespace}/{connection}", { params: { path: { namespace: ns, connection } }, body: { values } })),
    onSuccess: (_r, v) => {
      toast.success(`Saved your credential for ${v.connection}`);
      void qc.invalidateQueries({ queryKey: credentialKeys.mine });
    },
  });
}

export function useRemoveCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ns, connection }: { ns: string; connection: string }) =>
      unwrap(client.DELETE("/v1/me/credentials/{namespace}/{connection}", { params: { path: { namespace: ns, connection } } })),
    onSuccess: (_r, v) => {
      toast.success(`Removed your credential for ${v.connection}`);
      void qc.invalidateQueries({ queryKey: credentialKeys.mine });
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.detail : String(e)),
  });
}

/** Start the OAuth flow: the browser leaves for the provider and comes back to /settings/credentials. */
export function useStartOAuth() {
  return useMutation({
    mutationFn: ({ ns, connection }: { ns: string; connection: string }) =>
      unwrap(client.POST("/v1/me/credentials/{namespace}/{connection}/oauth", { params: { path: { namespace: ns, connection } } })),
    onSuccess: (r) => window.location.assign(r.authorize_url),
    onError: (e) => toast.error(e instanceof ApiError ? e.detail : String(e)),
  });
}

// --------------------------------------------------------------------------- connection editor

/** "Whose credential do calls use?" plus the fields people supply and optional OAuth settings. */
export function WhoConnects({ ns, value, onChange, secretFields, disabled }: {
  ns: string;
  value: PersonalSpec;
  onChange: (v: PersonalSpec) => void;
  secretFields: string[];
  disabled?: boolean;
}) {
  const mode = value.credentials ?? "shared";
  const set = (patch: Partial<PersonalSpec>) => onChange(pickPersonal({ ...value, ...patch }));
  const chosen = value.user_fields?.length ? value.user_fields : secretFields;
  const toggleField = (f: string) => set({ user_fields: chosen.includes(f) ? chosen.filter((x) => x !== f) : [...chosen, f] });
  // Name the person's fields explicitly, so they don't depend on which shared secrets happen to be set.
  const setMode = (m: CredentialMode) => set({ credentials: m, user_fields: m === "shared" ? undefined : value.user_fields?.length ? value.user_fields : secretFields });
  return (
    <Card>
      <CardHeader title="Whose credential" description="Use one credential for everyone, or have each person connect their own account so calls act as them." />
      <fieldset disabled={disabled} className="space-y-4 p-4">
        <Segmented<CredentialMode>
          value={mode}
          onChange={setMode}
          options={[
            { value: "shared", label: "Shared", icon: <Users className="size-3.5" /> },
            { value: "per_user", label: "Each person's own", icon: <UserRound className="size-3.5" /> },
            { value: "per_user_or_shared", label: "Own, else shared", icon: <KeyRound className="size-3.5" /> },
          ]}
        />
        <p className="text-xs text-muted">
          {mode === "shared"
            ? "Every run uses the secrets above, whoever started it."
            : mode === "per_user"
              ? "Calls use the credential of the person the run acts for. If they haven't connected, the step waits and asks them to."
              : "Calls use the person's credential when they have one, and the shared secrets above otherwise."}
        </p>
        {mode !== "shared" && (
          <>
            {secretFields.length > 0 && (
              <Field label="Fields each person supplies" description="Usually just the token. Unchecked fields come from the shared secrets.">
                <div className="flex flex-wrap gap-2">
                  {secretFields.map((f) => {
                    const on = chosen.includes(f);
                    return (
                      <label key={f} className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-border px-2 py-1 font-mono text-xs has-checked:border-accent has-checked:bg-accent-soft has-checked:text-accent">
                        <input type="checkbox" className="accent-[var(--accent)]" checked={on} onChange={() => toggleField(f)} />
                        {f}
                      </label>
                    );
                  })}
                </div>
              </Field>
            )}
            <OAuthSettings ns={ns} value={value.oauth} onChange={(oauth) => set({ oauth })} secretFields={secretFields} />
          </>
        )}
      </fieldset>
    </Card>
  );
}

const EMPTY_OAUTH: OAuthSpec = { authorize_url: "", token_url: "", client_id: "", scopes: [], token_field: "token", extra_params: {} };

function OAuthSettings({ ns, value, onChange, secretFields }: {
  ns: string;
  value: OAuthSpec | undefined;
  onChange: (v: OAuthSpec | undefined) => void;
  secretFields: string[];
}) {
  const o = value;
  const set = (patch: Partial<OAuthSpec>) => onChange({ ...(o ?? EMPTY_OAUTH), ...patch });
  const callback = `${window.location.origin}/v1/oauth/callback`;
  const tokenFields = secretFields.length ? secretFields : ["token"];
  return (
    <div className="rounded-lg border border-border">
      <div className="flex items-center justify-between gap-3 px-3 py-2.5">
        <div>
          <div className="text-[13px] font-medium">Connect with OAuth</div>
          <div className="text-xs text-muted">People sign in with the service instead of pasting a token.</div>
        </div>
        <Switch checked={!!o} onCheckedChange={(on) => onChange(on ? { ...EMPTY_OAUTH, token_field: tokenFields.includes("token") ? "token" : tokenFields[0] } : undefined)} />
      </div>
      {o && (
        <div className="grid gap-3 border-t border-border p-3 sm:grid-cols-2">
          <Field label="Authorize URL" required>
            <Input mono value={o.authorize_url} onChange={(e) => set({ authorize_url: e.target.value })} placeholder="https://github.com/login/oauth/authorize" />
          </Field>
          <Field label="Token URL" required>
            <Input mono value={o.token_url} onChange={(e) => set({ token_url: e.target.value })} placeholder="https://github.com/login/oauth/access_token" />
          </Field>
          <Field label="Client ID" required>
            <Input mono value={o.client_id} onChange={(e) => set({ client_id: e.target.value })} />
          </Field>
          <SecretPicker
            ns={ns}
            field="client_secret_secret"
            description="Secret holding the client secret. Leave unset for public clients (PKCE only)."
            value={o.client_secret_secret ?? undefined}
            onChange={(v) => set({ client_secret_secret: v ?? null })}
          />
          <Field label="Scopes" description="Space or comma separated.">
            <ScopesInput value={o.scopes ?? []} onChange={(scopes) => set({ scopes })} />
          </Field>
          <Field label="Token goes into" description="The connection field the access token fills.">
            <NativeSelect className="font-mono text-xs" value={o.token_field ?? "token"} onChange={(e) => set({ token_field: e.target.value })}>
              {[...new Set([...tokenFields, o.token_field ?? "token"])].map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <Field label="Extra authorize parameters" description="e.g. audience or prompt." className="sm:col-span-2">
            <KeyValueInput value={o.extra_params ?? {}} onChange={(extra_params) => set({ extra_params })} keyPlaceholder="audience" valuePlaceholder="api.example.com" />
          </Field>
          <Field label="Redirect URL" description="Register this as the callback URL in the provider's OAuth app." className="sm:col-span-2">
            <CopyField value={callback} />
          </Field>
        </div>
      )}
    </div>
  );
}

function ScopesInput({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const [text, setText] = useState(value.join(" "));
  return (
    <Input
      mono
      value={text}
      placeholder="repo read:user"
      onChange={(e) => {
        setText(e.target.value);
        onChange(e.target.value.split(/[\s,]+/).filter(Boolean));
      }}
    />
  );
}

/** Sidebar on a per-user connection: whether I've connected, and the buttons to do it. */
export function MyCredentialCard({ ns, connection, spec }: { ns: string; connection: string; spec: PersonalSpec & { secrets?: Record<string, string> } }) {
  const person = useIsPerson();
  const mine = useMyCredentials();
  if (!person) return null;
  const cred = mine.data?.find((c) => c.namespace === ns && c.connection === connection);
  return (
    <Card>
      <CardHeader title="Your account" description={spec.credentials === "per_user" ? "Runs you start use your own credential here." : "Runs you start use yours if you add one, else the shared one."} />
      <div className="space-y-3 p-4">
        <CredentialStatus cred={cred} />
        <CredentialActions ns={ns} connection={connection} spec={spec} cred={cred} />
      </div>
    </Card>
  );
}

export function CredentialStatus({ cred }: { cred: Credential | undefined }) {
  if (!cred) return <Badge tone="warn" dot>Not connected</Badge>;
  const expired = cred.expires_at && new Date(cred.expires_at).getTime() < Date.now();
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
      <Badge tone={expired ? "warn" : "ok"} dot>
        {expired ? "Expired" : cred.kind === "oauth" ? "Connected with OAuth" : "Token saved"}
      </Badge>
      <span title={dateTime(cred.updated_at)}>updated {relTime(cred.updated_at)}</span>
      {cred.expires_at && !expired && <span title={dateTime(cred.expires_at)}>· expires {relTime(cred.expires_at)}</span>}
    </div>
  );
}

export function CredentialActions({ ns, connection, spec, cred, size }: {
  ns: string;
  connection: string;
  spec: PersonalSpec & { secrets?: Record<string, string> };
  cred: Credential | undefined;
  size?: "sm";
}) {
  const [open, setOpen] = useState(false);
  const oauth = useStartOAuth();
  const remove = useRemoveCredential();
  const confirm = useConfirm();
  const fields = userFields(spec);
  return (
    <div className="flex flex-wrap gap-2">
      {spec.oauth && (
        <Button variant="primary" size={size} icon={<Link2 className="size-3.5" />} loading={oauth.isPending} onClick={() => oauth.mutate({ ns, connection })}>
          {cred ? "Reconnect" : "Connect account"}
        </Button>
      )}
      {fields.length > 0 && (
        <Button variant={spec.oauth ? "secondary" : "primary"} size={size} icon={<KeyRound className="size-3.5" />} onClick={() => setOpen(true)}>
          {cred ? "Replace token" : spec.oauth ? "Paste a token" : "Add token"}
        </Button>
      )}
      {cred && (
        <Button
          variant="ghost"
          size={size}
          icon={<Trash2 className="size-3.5" />}
          loading={remove.isPending}
          onClick={async () => {
            const ok = await confirm({ title: `Remove your credential for ${connection}?`, description: "Runs that act for you will wait until you connect again.", confirmLabel: "Remove", danger: true });
            if (ok) remove.mutate({ ns, connection });
          }}
        >
          Remove
        </Button>
      )}
      <TokenDialog open={open} onOpenChange={setOpen} ns={ns} connection={connection} fields={fields} />
    </div>
  );
}

function TokenDialog({ open, onOpenChange, ns, connection, fields }: { open: boolean; onOpenChange: (o: boolean) => void; ns: string; connection: string; fields: string[] }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const put = useSetCredential();
  const filled = Object.fromEntries(Object.entries(values).filter(([, v]) => v));
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) setValues({});
        onOpenChange(o);
      }}
      title={`Your credential for ${connection}`}
      description={`Stored encrypted for you only, in ${ns}. Nobody can read it back, admins included.`}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={put.isPending}
            disabled={!Object.keys(filled).length}
            onClick={() => put.mutate({ ns, connection, values: filled }, { onSuccess: () => (setValues({}), onOpenChange(false)) })}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {fields.map((f, i) => (
          <Field key={f} label={humanize(f)}>
            <Input mono type="password" autoComplete="off" autoFocus={i === 0} value={values[f] ?? ""} onChange={(e) => setValues({ ...values, [f]: e.target.value })} />
          </Field>
        ))}
        {put.error && <p className="text-xs text-bad">{put.error instanceof ApiError ? put.error.detail : String(put.error)}</p>}
      </div>
    </Dialog>
  );
}

/** Link to a connection's own credential on the settings page. */
export function connectPath(ns: string, connection: string) {
  return `/settings/credentials?connection=${encodeURIComponent(`${ns}/${connection}`)}`;
}

export function ConnectLink({ ns, connection, children }: { ns: string; connection: string; children?: ReactNode }) {
  return (
    <Link to={connectPath(ns, connection)} className="inline-flex items-center gap-1 text-accent hover:underline">
      {children ?? "Connect your account"} <ExternalLink className="size-3" />
    </Link>
  );
}
