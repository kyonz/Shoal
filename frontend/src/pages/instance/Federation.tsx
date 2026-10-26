/**
 * /instance/federation: which tenants on this instance are served from member deployments
 * (hub links). Instance admins create, change and revoke links; auditors can look.
 * What a member lets the hub do is set in that member's own admin console.
 */
import { useQuery } from "@tanstack/react-query";
import { CircleDot, KeyRound, MoreHorizontal, Network, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { client, unwrap, type Schemas } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { dateTime, relTime } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  CopyField,
  Dialog,
  EmptyState,
  ErrorNotice,
  Field,
  Input,
  Menu,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
  NativeSelect,
  Page,
  PageHeader,
  Segmented,
  Skeleton,
  Table,
  Td,
  Th,
  Tooltip,
  Tr,
  useConfirm,
} from "@/components/ui";
import { MODE_LABEL, MODE_OPTIONS, type FederationMode } from "@/lib/federation";
import { adminKeys, useAction, type FederationLink } from "@/pages/admin/shared";

type Mode = FederationMode;
type LinkCreated = Schemas["LinkCreated"];

/** Instance console › Federation. */
export default function InstanceFederation() {
  const { instance } = useAuth();
  return (
    <Page>
      <PageHeader
        title="Federation"
        description="Serve tenants from their own deployments, keep their data there, and still reach them from this instance. Each member decides what this hub may see and do."
      />
      <HubLinks readOnly={!instance.admin} />
    </Page>
  );
}

function HubLinks({ readOnly }: { readOnly: boolean }) {
  const confirm = useConfirm();
  const links = useQuery({ queryKey: adminKeys.links, queryFn: () => unwrap(client.GET("/v1/federation/links")), refetchInterval: 10_000 });
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<LinkCreated | null>(null);

  const setMode = useAction(
    ({ tenant, mode }: { tenant: string; mode: Mode }) => unwrap(client.PATCH("/v1/federation/links/{tenant}", { params: { path: { tenant } }, body: { mode } })),
    { success: (_, a) => `${a.tenant} now asks for ${MODE_LABEL[a.mode].toLowerCase()} access`, invalidate: [adminKeys.links] },
  );
  const reissue = useAction(
    (tenant: string) => unwrap(client.POST("/v1/federation/links/{tenant}/enrollment", { params: { path: { tenant } } })),
    { invalidate: [adminKeys.links], onSuccess: (r) => setIssued(r) },
  );
  const revoke = useAction(
    (tenant: string) => unwrap(client.DELETE("/v1/federation/links/{tenant}", { params: { path: { tenant } } })),
    { success: (_, t) => `Revoked the link to ${t}`, invalidate: [adminKeys.links] },
  );

  const rows = links.data ?? [];
  return (
    <>
      <Card>
        <CardHeader
          title="Federated organisations"
          description="Calls for these tenants are forwarded to their member deployments over an outbound connection the member opens."
          actions={
            !readOnly && (
              <Button variant="primary" size="sm" icon={<Plus className="size-3.5" />} onClick={() => setCreating(true)}>
                Federate a tenant
              </Button>
            )
          }
        />
        {links.error ? (
          <ErrorNotice error={links.error} className="m-4" />
        ) : links.isLoading ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : !rows.length ? (
          <EmptyState
            icon={<Network className="size-5" />}
            title="No federated tenants"
            description="Federate an empty tenant, then give its enrollment token to the admin of the deployment that will hold its data."
            action={
              !readOnly && (
                <Button variant="primary" icon={<Plus className="size-3.5" />} onClick={() => setCreating(true)}>
                  Federate a tenant
                </Button>
              )
            }
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Organisation</Th>
                <Th>Connection</Th>
                <Th>Access</Th>
                <Th>Exposes</Th>
                <Th>Member</Th>
                <Th className="w-10" />
              </tr>
            </thead>
            <tbody>
              {rows.map((l) => (
                <Tr key={l.id}>
                  <Td>
                    <div className="font-mono text-[12.5px] font-medium">{l.tenant}</div>
                    <div className="text-[11px] text-faint">linked {relTime(l.created_at)}</div>
                  </Td>
                  <Td>
                    <ConnectionCell link={l} />
                  </Td>
                  <Td>
                    <Tooltip
                      content={`The hub asks for ${MODE_LABEL[l.requested_mode].toLowerCase()}${l.member_mode ? `; the member allows up to ${MODE_LABEL[l.member_mode].toLowerCase()}` : ""}.`}
                    >
                      <span>
                        <Badge tone={l.effective_mode === "read_write" ? "accent" : "info"}>{MODE_LABEL[l.effective_mode]}</Badge>
                      </span>
                    </Tooltip>
                  </Td>
                  <Td className="text-xs text-muted">
                    <div>{l.namespaces.length ? l.namespaces.join(", ") : "All namespaces"}</div>
                    <div className="text-faint">{l.visibility === "metadata" ? "Metadata only" : "Full run data"}</div>
                  </Td>
                  <Td className="text-xs text-muted">{memberVersion(l) ?? <span className="text-faint">—</span>}</Td>
                  <Td>
                    {!readOnly && (
                      <Menu>
                        <MenuTrigger asChild>
                          <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${l.tenant}`} icon={<MoreHorizontal className="size-4" />} />
                        </MenuTrigger>
                        <MenuContent align="end">
                          {(["read", "read_write"] as Mode[])
                            .filter((m) => m !== l.requested_mode)
                            .map((m) => (
                              <MenuItem key={m} icon={<CircleDot />} disabled={ended(l)} onSelect={() => setMode.mutate({ tenant: l.tenant, mode: m })}>
                                Ask for {MODE_LABEL[m].toLowerCase()}
                              </MenuItem>
                            ))}
                          <MenuItem icon={<KeyRound />} disabled={ended(l)} onSelect={() => reissue.mutate(l.tenant)}>
                            New enrollment token
                          </MenuItem>
                          <MenuSeparator />
                          <MenuItem
                            danger
                            icon={<Trash2 />}
                            disabled={ended(l)}
                            onSelect={async () => {
                              const ok = await confirm({
                                title: `Revoke the link to ${l.tenant}?`,
                                description: "The hub stops forwarding calls for this organisation. Its deployment keeps running on its own with all of its data.",
                                confirmLabel: "Revoke link",
                                danger: true,
                                typeToConfirm: l.tenant,
                              });
                              if (ok) revoke.mutate(l.tenant);
                            }}
                          >
                            Revoke link
                          </MenuItem>
                        </MenuContent>
                      </Menu>
                    )}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <CreateLinkDialog open={creating} onOpenChange={setCreating} onCreated={setIssued} />
      <EnrollmentDialog created={issued} onClose={() => setIssued(null)} />
    </>
  );
}

function memberVersion(l: FederationLink): string | null {
  const v = (l.member as { version?: string }).version;
  return v ? `v${v}` : null;
}

/** Revoked by the hub or left by the member: nothing is forwarded until the tenant is federated again. */
function ended(l: FederationLink): boolean {
  return l.status === "revoked" || l.status === "left";
}

function ConnectionCell({ link }: { link: FederationLink }) {
  if (link.status === "revoked") return <Badge>Revoked</Badge>;
  if (link.status === "left")
    return (
      <Tooltip content="The member deployment left the hub. Federate the tenant again to reconnect it.">
        <span>
          <Badge>Member left</Badge>
        </span>
      </Tooltip>
    );
  if (link.status === "pending")
    return (
      <div>
        <Badge tone="warn" dot>
          Waiting to enroll
        </Badge>
      </div>
    );
  return (
    <div>
      <Badge tone={link.connected ? "ok" : "bad"} dot>
        {link.connected ? "Connected" : "Offline"}
      </Badge>
      {link.last_seen_at && (
        <div className="mt-0.5 text-[11px] text-faint" title={dateTime(link.last_seen_at)}>
          seen {relTime(link.last_seen_at)}
        </div>
      )}
    </div>
  );
}

function CreateLinkDialog({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (o: boolean) => void; onCreated: (r: LinkCreated) => void }) {
  const [tenant, setTenant] = useState("");
  const [mode, setMode] = useState<Mode>("read_write");
  const tenants = useQuery({ queryKey: ["admin", "tenants"], queryFn: () => unwrap(client.GET("/v1/tenants")), enabled: open });
  const links = useQuery({ queryKey: adminKeys.links, queryFn: () => unwrap(client.GET("/v1/federation/links")), enabled: open });
  const linked = new Set((links.data ?? []).filter((l) => !ended(l)).map((l) => l.tenant));
  const create = useAction(() => unwrap(client.POST("/v1/federation/links", { body: { tenant, mode } })), {
    invalidate: [adminKeys.links],
    onSuccess: (r) => {
      onOpenChange(false);
      setTenant("");
      onCreated(r);
    },
  });
  const list = ((tenants.data ?? []) as { slug: string; name: string }[]).filter((t) => !linked.has(t.slug));
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Federate a tenant"
      description="Its data will live in a member deployment. Pick an empty tenant: anything already in it on the hub is no longer reachable once it's federated."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={create.isPending} disabled={!tenant} onClick={() => create.mutate(undefined)}>
            Federate
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Tenant" required>
          {list.length ? (
            <NativeSelect value={tenant} onChange={(e) => setTenant(e.target.value)}>
              <option value="">Choose a tenant…</option>
              {list.map((t) => (
                <option key={t.slug} value={t.slug}>
                  {t.name} ({t.slug})
                </option>
              ))}
            </NativeSelect>
          ) : (
            <Input value={tenant} onChange={(e) => setTenant(e.target.value.trim())} placeholder="tenant-slug" className="font-mono" />
          )}
        </Field>
        <Field label="Access the hub asks for" description="The member sets a ceiling when it joins and can lower this at any time.">
          <Segmented value={mode} onChange={setMode} options={MODE_OPTIONS} />
        </Field>
      </div>
    </Dialog>
  );
}

function EnrollmentDialog({ created, onClose }: { created: LinkCreated | null; onClose: () => void }) {
  const hub = window.location.origin;
  return (
    <Dialog
      open={!!created}
      onOpenChange={(o) => !o && onClose()}
      size="lg"
      title={created ? `Enroll ${created.link.tenant}` : ""}
      description="Give this single-use token to the admin of the member deployment. It can't be shown again."
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      {created && (
        <div className="space-y-4">
          <Field label="Enrollment token" description={`Expires ${relTime(created.enrollment_expires_at)} (${dateTime(created.enrollment_expires_at)}).`}>
            <CopyField value={created.enrollment_token} />
          </Field>
          <Field label="Hub URL">
            <CopyField value={hub} />
          </Field>
          <div className="rounded-lg border border-border bg-subtle/50 p-3 text-[13px] leading-relaxed text-muted">
            On the member, open <b className="text-fg">Admin console › Federation</b> and choose <b className="text-fg">Join a hub</b>, or run:
            <CopyField className="mt-2" value={`shoal federation join ${hub} --token ${created.enrollment_token}`} />
          </div>
        </div>
      )}
    </Dialog>
  );
}

