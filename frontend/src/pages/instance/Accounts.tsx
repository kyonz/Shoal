/** /instance/accounts: everyone who has signed in, their tenants, and instance roles. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck, UsersRound } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { toast } from "sonner";
import { ApiError, client, unwrap } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { dateTime, relTime } from "@/lib/format";
import { Badge, Card, EmptyState, Page, PageHeader, Segmented, Sheet, Skeleton, Switch, Table, Td, Th, Tr } from "@/components/ui";
import { Avatar, KV, SearchInput } from "@/pages/admin/shared";
import { INSTANCE_ROLE_LABEL, instanceKeys, useAccount, useAccounts, type Account } from "./api";

function useDebounced<T>(value: T, ms = 250) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

type RoleFilter = "all" | "instance-admin" | "instance-auditor";

export function InstanceRoleBadge({ role }: { role?: string | null }) {
  if (!role) return null;
  return (
    <Badge tone={role === "instance-admin" ? "accent" : "info"}>
      <ShieldCheck className="size-3" /> {INSTANCE_ROLE_LABEL[role] ?? role}
    </Badge>
  );
}

export default function Accounts() {
  const { me } = useAuth();
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState("");
  const [role, setRole] = useState<RoleFilter>("all");
  const accounts = useAccounts(useDebounced(q));
  const selected = params.get("account");
  const rows = (accounts.data ?? []).filter((a) => role === "all" || a.instance_role === role);

  return (
    <Page wide>
      <PageHeader
        icon={<UsersRound className="size-4" />}
        title="Accounts"
        description="One account per person on this deployment, created when they first sign in. Each account links to a user in every tenant they belong to."
      />
      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
          <SearchInput value={q} onChange={setQ} placeholder="Search by name or email" className="w-72" />
          <Segmented<RoleFilter>
            value={role}
            onChange={setRole}
            options={[
              { value: "all", label: "Everyone" },
              { value: "instance-admin", label: "Instance admins" },
              { value: "instance-auditor", label: "Auditors" },
            ]}
          />
          <div className="flex-1" />
          {accounts.data && <span className="text-xs text-faint">{rows.length} accounts</span>}
        </div>
        {accounts.isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>
        ) : !rows.length ? (
          <EmptyState
            icon={<UsersRound className="size-5" />}
            title={q || role !== "all" ? "No accounts match" : "Nobody has signed in yet"}
            description={q || role !== "all" ? undefined : "Accounts appear when people sign in through single sign-on."}
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Person</Th>
                <Th>Instance role</Th>
                <Th>Status</Th>
                <Th>Last sign-in</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <Tr key={a.id} onClick={() => setParams({ account: a.id })}>
                  <Td>
                    <div className="flex items-center gap-2.5">
                      <Avatar name={a.display_name || a.email || a.id} />
                      <div className="min-w-0">
                        <div className="truncate font-medium">
                          {a.display_name}
                          {a.id === me?.account_id && <span className="ml-1.5 text-xs font-normal text-faint">(you)</span>}
                        </div>
                        <div className="truncate text-xs text-faint">{a.email ?? a.id}</div>
                      </div>
                    </div>
                  </Td>
                  <Td>{a.instance_role ? <InstanceRoleBadge role={a.instance_role} /> : <span className="text-xs text-faint">None</span>}</Td>
                  <Td>{a.active ? <Badge tone="ok" dot>Active</Badge> : <Badge tone="bad" dot>Deactivated</Badge>}</Td>
                  <Td className="text-muted" title={dateTime(a.last_login_at)}>
                    {a.last_login_at ? relTime(a.last_login_at) : "Never"}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <AccountSheet id={selected} onClose={() => setParams({}, { replace: true })} />
    </Page>
  );
}

const NO_ROLE = "none";

function AccountSheet({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { me, instance } = useAuth();
  const qc = useQueryClient();
  const account = useAccount(id);
  const a = account.data;
  const self = !!a && a.id === me?.account_id;
  const update = useMutation({
    mutationFn: (body: { instance_role?: Account["instance_role"]; active?: boolean }) =>
      unwrap(client.PATCH("/v1/instance/accounts/{account_id}", { params: { path: { account_id: id! } }, body: body as never })),
    onSuccess: (r) => {
      qc.setQueryData(instanceKeys.account(r.id), r);
      void qc.invalidateQueries({ queryKey: ["instance", "accounts"] });
      toast.success(`${r.display_name} updated`);
    },
    // The server refuses changes to yourself and removing the last instance admin; say why.
    onError: (e) => toast.error(e instanceof ApiError ? e.detail : String(e)),
  });
  const locked = !instance.admin || self || update.isPending;

  return (
    <Sheet open={!!id} onOpenChange={(o) => !o && onClose()} title={a?.display_name ?? "Account"} description={a?.email ?? undefined}>
      {account.isLoading || !a ? (
        <div className="space-y-2">
          <Skeleton className="h-8" />
          <Skeleton className="h-24" />
        </div>
      ) : (
        <div className="space-y-6">
          <div className="flex items-center gap-3">
            <Avatar name={a.display_name || a.email || a.id} className="size-10 text-sm" />
            <div className="min-w-0">
              <div className="flex items-center gap-2 font-medium">
                {a.display_name}
                <InstanceRoleBadge role={a.instance_role} />
              </div>
              <div className="truncate text-xs text-faint">{a.email}</div>
            </div>
          </div>

          <section>
            <h3 className="mb-2 text-[13px] font-semibold">Instance role</h3>
            <Segmented
              value={(a.instance_role ?? NO_ROLE) as string}
              onChange={(v) => update.mutate({ instance_role: v === NO_ROLE ? null : v })}
              options={[
                { value: NO_ROLE, label: "None" },
                { value: "instance-auditor", label: "Auditor" },
                { value: "instance-admin", label: "Admin" },
              ]}
              disabled={locked}
            />
            <p className="mt-2 text-xs text-muted">
              {self
                ? "This is you. Another instance admin has to change your role or deactivate you."
                : "Instance admins manage every tenant and act as tenant admin anywhere. Auditors can read everything but change nothing."}
            </p>
          </section>

          <section className="flex items-start justify-between gap-4">
            <div>
              <h3 className="text-[13px] font-semibold">Can sign in</h3>
              <p className="mt-0.5 text-xs text-muted">Deactivating locks them out of every tenant at once. Their tenant users and history are kept.</p>
            </div>
            <Switch checked={a.active} disabled={locked} onCheckedChange={(active) => update.mutate({ active })} />
          </section>

          <section>
            <h3 className="mb-2 text-[13px] font-semibold">Tenants</h3>
            {a.tenants.length ? (
              <div className="divide-y divide-border rounded-lg border border-border">
                {a.tenants.map((t) => (
                  <Link key={t.tenant} to={`/instance/tenants/${encodeURIComponent(t.tenant)}`} className="flex items-center gap-3 px-3 py-2 hover:bg-subtle/60">
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[13px] font-medium">{t.tenant_name}</div>
                      <div className="truncate font-mono text-[11px] text-faint">
                        {t.tenant} · {t.username}
                      </div>
                    </div>
                    <div className="flex flex-wrap justify-end gap-1">
                      {t.roles.length ? (
                        t.roles.map((r) => (
                          <Badge key={r} tone={r === "tenant-admin" ? "accent" : "neutral"} className="font-mono">
                            {r}
                          </Badge>
                        ))
                      ) : (
                        <span className="text-xs text-faint">Namespace roles</span>
                      )}
                    </div>
                  </Link>
                ))}
              </div>
            ) : (
              <p className="text-xs text-muted">Not a member of any tenant yet.</p>
            )}
          </section>

          <section className="divide-y divide-border">
            <KV label="Signed in through">
              <span className="font-mono text-xs">{a.oidc_issuer}</span>
            </KV>
            <KV label="First sign-in">{dateTime(a.created_at)}</KV>
            <KV label="Last sign-in">{a.last_login_at ? dateTime(a.last_login_at) : "Never"}</KV>
            <KV label="Account id">
              <span className="font-mono text-xs">{a.id}</span>
            </KV>
          </section>
        </div>
      )}
    </Sheet>
  );
}
