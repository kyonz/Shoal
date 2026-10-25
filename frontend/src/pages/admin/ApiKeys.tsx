import { useQuery } from "@tanstack/react-query";
import { KeyRound, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";
import { client, unwrap } from "@/api/client";
import { useNamespaces } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { dateTime, relTime } from "@/lib/format";
import { TagInput } from "@/components/forms/SchemaForm";
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Page,
  PageHeader,
  Segmented,
  Skeleton,
  Switch,
  Table,
  Td,
  Th,
  Tr,
  useConfirm,
} from "@/components/ui";
import { adminKeys, useAction, usePermissionCatalog, useServiceAccounts, useSubjectNames, type ApiKey } from "./shared";
import { TokenDialog } from "./TokenDialog";

function keyState(k: ApiKey): { label: string; tone: "ok" | "bad" | "neutral" | "warn" } {
  if (k.revoked_at) return { label: "Revoked", tone: "neutral" };
  if (k.expires_at && new Date(k.expires_at).getTime() < Date.now()) return { label: "Expired", tone: "bad" };
  if (k.expires_at && new Date(k.expires_at).getTime() - Date.now() < 7 * 86400_000) return { label: "Expires soon", tone: "warn" };
  return { label: "Active", tone: "ok" };
}

export default function ApiKeys() {
  const { can, me } = useAuth();
  const isAdmin = can("api-keys:manage");
  const [all, setAll] = useState(isAdmin);
  const [showRevoked, setShowRevoked] = useState(false);
  const keys = useQuery({
    queryKey: adminKeys.apiKeys(all && isAdmin),
    queryFn: () => unwrap(client.GET("/v1/api-keys", { params: { query: { all: all && isAdmin } } })),
  });
  const names = useSubjectNames();
  const confirm = useConfirm();
  const [params, setParams] = useSearchParams();
  const [open, setOpen] = useState(params.get("new") === "1");
  const [token, setToken] = useState<string | null>(null);

  useEffect(() => {
    if (params.get("new") === "1") setOpen(true);
  }, [params]);

  const revoke = useAction((id: string) => unwrap(client.DELETE("/v1/api-keys/{key_id}", { params: { path: { key_id: id } } })), {
    success: "Key revoked",
  });
  const rows = (keys.data ?? []).filter((k) => showRevoked || !k.revoked_at);

  return (
    <Page wide>
      <PageHeader
        title="API keys"
        description="Keys authenticate the CLI, Terraform, CI and integrators. A key can be limited to namespaces and permissions, never more than its owner holds."
        actions={
          <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setOpen(true)}>
            Create key
          </Button>
        }
      />
      <Card>
        <div className="flex flex-wrap items-center gap-3 border-b border-border p-3">
          {isAdmin && (
            <Segmented
              value={all ? "all" : "mine"}
              onChange={(v) => setAll(v === "all")}
              options={[
                { value: "mine", label: "My keys" },
                { value: "all", label: "All keys in tenant" },
              ]}
            />
          )}
          <Switch checked={showRevoked} onCheckedChange={setShowRevoked} label={<span className="text-xs text-muted">Show revoked</span>} />
        </div>
        {keys.isLoading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-9" />
            <Skeleton className="h-9" />
          </div>
        ) : !rows.length ? (
          <EmptyState
            icon={<KeyRound className="size-5" />}
            title="No API keys"
            description="Create a key for the CLI, Terraform or your CI pipeline."
            action={<Button onClick={() => setOpen(true)}>Create key</Button>}
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Key</Th>
                {all && <Th>Owner</Th>}
                <Th>Scopes</Th>
                <Th>Namespaces</Th>
                <Th>Last used</Th>
                <Th>Expires</Th>
                <Th>Status</Th>
                <Th className="w-20" />
              </tr>
            </thead>
            <tbody>
              {rows.map((k) => {
                const st = keyState(k);
                return (
                  <Tr key={k.id} className={cn(k.revoked_at && "opacity-60")}>
                    <Td>
                      <div className="font-medium">{k.name}</div>
                      <div className="font-mono text-[11px] text-faint">awm_{k.prefix}_…</div>
                    </Td>
                    {all && (
                      <Td className="text-muted">
                        {(k.owner_id && names.get(k.owner_id)?.name) ?? (k.owner_id === me?.user_id ? "You" : (k.created_by ?? "—"))}
                      </Td>
                    )}
                    <Td>
                      <div className="flex max-w-56 flex-wrap gap-1">
                        {k.scopes.slice(0, 3).map((s) => (
                          <Badge key={s} tone={s === "*" ? "warn" : "neutral"} className="font-mono">
                            {s === "*" ? "full access" : s}
                          </Badge>
                        ))}
                        {k.scopes.length > 3 && <Badge>+{k.scopes.length - 3}</Badge>}
                      </div>
                    </Td>
                    <Td className="text-muted">{k.namespaces.length ? k.namespaces.join(", ") : "All"}</Td>
                    <Td className="text-muted" title={dateTime(k.last_used_at)}>
                      {k.last_used_at ? relTime(k.last_used_at) : "Never"}
                    </Td>
                    <Td className="text-muted" title={dateTime(k.expires_at)}>
                      {k.expires_at ? relTime(k.expires_at) : "Never"}
                    </Td>
                    <Td>
                      <Badge tone={st.tone} dot>
                        {st.label}
                      </Badge>
                    </Td>
                    <Td className="text-right">
                      {!k.revoked_at && (
                        <Button
                          variant="ghost"
                          size="xs"
                          className="text-bad"
                          onClick={async () => {
                            if (
                              await confirm({
                                title: `Revoke ${k.name}?`,
                                description: "Anything using this key stops working immediately.",
                                danger: true,
                                confirmLabel: "Revoke key",
                              })
                            )
                              revoke.mutate(k.id);
                          }}
                        >
                          Revoke
                        </Button>
                      )}
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
      <CreateKeyDialog
        open={open}
        presetSa={params.get("sa") ?? ""}
        onOpenChange={(o) => {
          setOpen(o);
          if (!o && params.has("new")) {
            params.delete("new");
            params.delete("sa");
            setParams(params, { replace: true });
          }
        }}
        onCreated={setToken}
      />
      <TokenDialog
        token={token}
        onClose={() => setToken(null)}
        title="Your new API key"
        description="Use it as `Authorization: Bearer <key>`. It is shown only once."
      />
    </Page>
  );
}

type Access = "full" | "read" | "custom";
const EXPIRY = [
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
  { value: "never", label: "No expiry" },
  { value: "custom", label: "Custom date" },
];

function CreateKeyDialog({
  open,
  onOpenChange,
  onCreated,
  presetSa,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onCreated: (t: string) => void;
  presetSa: string;
}) {
  const { can } = useAuth();
  const sas = useServiceAccounts(open && can("users:manage"));
  const namespaces = useNamespaces();
  const catalog = usePermissionCatalog();
  const [name, setName] = useState("");
  const [sa, setSa] = useState("");
  const [access, setAccess] = useState<Access>("full");
  const [scopes, setScopes] = useState<string[]>([]);
  const [nsList, setNsList] = useState<string[]>([]);
  const [expiry, setExpiry] = useState("90");
  const [customDate, setCustomDate] = useState("");

  useEffect(() => {
    if (open) {
      setName("");
      setSa(presetSa);
      setAccess("full");
      setScopes([]);
      setNsList([]);
      setExpiry("90");
    }
  }, [open, presetSa]);

  const expiresAt = (() => {
    if (expiry === "never") return null;
    if (expiry === "custom") return customDate ? new Date(customDate).toISOString() : null;
    return new Date(Date.now() + Number(expiry) * 86400_000).toISOString();
  })();
  const finalScopes = access === "full" ? ["*"] : access === "read" ? ["*:read"] : scopes;

  const create = useAction(
    () =>
      unwrap(
        client.POST("/v1/api-keys", {
          body: { name: name.trim(), service_account: sa || null, scopes: finalScopes, namespaces: nsList, expires_at: expiresAt },
        }),
      ),
    {
      success: "API key created",
      onSuccess: (r) => {
        onOpenChange(false);
        onCreated(r.token);
      },
    },
  );

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Create API key"
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!name.trim() || !finalScopes.length || (expiry === "custom" && !customDate)}
            onClick={() => create.mutate(undefined)}
          >
            Create key
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name" required>
            <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="terraform-ci" />
          </Field>
          <Field label="Owner" description={sa ? "The key acts as this service account." : "The key acts as you."}>
            <NativeSelect value={sa} onChange={(e) => setSa(e.target.value)}>
              <option value="">Me</option>
              {(sas.data ?? []).map((s) => (
                <option key={s.id} value={s.name}>
                  sa:{s.name}
                </option>
              ))}
              {presetSa && !sas.data?.some((s) => s.name === presetSa) && <option value={presetSa}>sa:{presetSa}</option>}
            </NativeSelect>
          </Field>
        </div>
        <Field label="Permissions" description="Limits what the key may do, on top of its owner's roles.">
          <div className="grid grid-cols-3 gap-2">
            {(
              [
                ["full", "Full access", "Everything the owner can do."],
                ["read", "Read only", "View resources and runs."],
                ["custom", "Custom", "Pick permissions."],
              ] as const
            ).map(([v, t, d]) => (
              <button
                key={v}
                type="button"
                onClick={() => setAccess(v)}
                className={cn(
                  "rounded-lg border p-2.5 text-left transition-colors",
                  access === v ? "border-accent bg-accent-soft" : "border-border hover:border-border-strong",
                )}
              >
                <div className="text-[13px] font-medium">{t}</div>
                <div className="text-[11px] text-muted">{d}</div>
              </button>
            ))}
          </div>
          {access === "custom" && (
            <div className="mt-2 space-y-2">
              <TagInput value={scopes} onChange={setScopes} placeholder="e.g. runs:create, workflows:*" />
              <div className="flex max-h-28 flex-wrap gap-1 overflow-y-auto">
                {(catalog.data ?? [])
                  .filter((p) => !scopes.includes(p.name))
                  .map((p) => (
                    <button
                      key={p.name}
                      type="button"
                      title={p.description}
                      onClick={() => setScopes([...scopes, p.name])}
                      className="h-5 rounded border border-border px-1.5 font-mono text-[10.5px] text-muted hover:border-accent hover:text-fg"
                    >
                      + {p.name}
                    </button>
                  ))}
              </div>
            </div>
          )}
        </Field>
        <Field label="Namespaces" description={nsList.length ? `Only ${nsList.join(", ")}.` : "All namespaces the owner can access."}>
          <TagInput value={nsList} onChange={setNsList} options={(namespaces.data ?? []).map((n) => n.name)} />
        </Field>
        <Field label="Expiry">
          <div className="flex gap-2">
            <div className="w-44 shrink-0">
              <NativeSelect value={expiry} onChange={(e) => setExpiry(e.target.value)}>
                {EXPIRY.map((e) => (
                  <option key={e.value} value={e.value}>
                    {e.label}
                  </option>
                ))}
              </NativeSelect>
            </div>
            {expiry === "custom" && (
              <div className="w-44 shrink-0">
                <Input type="date" value={customDate} min={new Date().toISOString().slice(0, 10)} onChange={(e) => setCustomDate(e.target.value)} />
              </div>
            )}
            {expiresAt && <span className="self-center text-xs text-faint">Expires {dateTime(expiresAt)}</span>}
          </div>
        </Field>
      </div>
    </Dialog>
  );
}
