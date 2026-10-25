/** /settings/credentials: the connections that act as you, and your token or OAuth grant for each. */
import { useQueries } from "@tanstack/react-query";
import { CheckCircle2, KeyRound, ShieldAlert, XCircle } from "lucide-react";
import { useEffect, useMemo, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import { rawJson, type Resource } from "@/api/client";
import { useNamespaces } from "@/api/hooks";
import { cn } from "@/lib/cn";
import { nsPath } from "@/lib/ns";
import { Badge, Card, EmptyState, ErrorNotice, Page, PageHeader, Skeleton, Table, Td, Th, Tr } from "@/components/ui";
import { IntegrationMark } from "@/pages/connect/shared";
import {
  CredentialActions,
  CredentialStatus,
  MODE_LABEL,
  useIsPerson,
  useMyCredentials,
  type Credential,
  type CredentialMode,
  type PersonalSpec,
} from "@/pages/connect/PersonalCredentials";

type ConnSpec = PersonalSpec & { integration?: string; secrets?: Record<string, string> };

interface Row {
  ns: string;
  name: string;
  description?: string | null;
  spec?: ConnSpec;
  cred?: Credential;
}

/** Connections in every namespace I can see that take each person's own credential. */
function usePersonalConnections() {
  const namespaces = useNamespaces();
  const names = (namespaces.data ?? []).map((n) => n.name);
  const lists = useQueries({
    queries: names.map((ns) => ({
      queryKey: ["resources", "connections", ns],
      queryFn: () => rawJson<Resource[]>(`/v1/namespaces/${encodeURIComponent(ns)}/connections`),
      retry: false,
    })),
  });
  const loading = namespaces.isLoading || lists.some((q) => q.isLoading);
  const seen = new Map<string, Resource>();
  for (const q of lists)
    for (const r of q.data ?? []) {
      const spec = r.spec as ConnSpec;
      if (spec.credentials && spec.credentials !== "shared") seen.set(`${r.metadata.namespace}/${r.metadata.name}`, r);
    }
  return { connections: [...seen.values()], loading, error: namespaces.error };
}

export default function Credentials() {
  const person = useIsPerson();
  const [params, setParams] = useSearchParams();
  const mine = useMyCredentials();
  const { connections, loading, error } = usePersonalConnections();
  const focus = params.get("connection");
  const connected = params.get("connected");
  const failed = params.get("error");

  const rows = useMemo(() => {
    const byKey = new Map<string, Row>();
    for (const r of connections) {
      const ns = r.metadata.namespace ?? "";
      byKey.set(`${ns}/${r.metadata.name}`, { ns, name: r.metadata.name, description: r.metadata.description, spec: r.spec as ConnSpec });
    }
    // Credentials for connections I can no longer see still show, so they can be removed.
    for (const c of mine.data ?? []) {
      const k = `${c.namespace}/${c.connection}`;
      byKey.set(k, { ...(byKey.get(k) ?? { ns: c.namespace, name: c.connection }), cred: c });
    }
    return [...byKey.values()].sort((a, b) => Number(!!b.cred) - Number(!!a.cred) || `${a.ns}/${a.name}`.localeCompare(`${b.ns}/${b.name}`));
  }, [connections, mine.data]);

  useEffect(() => {
    const target = focus ?? connected;
    if (!target || loading) return;
    document.getElementById(`cred-${target}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focus, connected, loading]);

  if (!person)
    return (
      <Page>
        <PageHeader title="Connected accounts" />
        <EmptyState
          icon={<ShieldAlert className="size-5" />}
          title="Only people connect their own accounts"
          description="You're signed in as a service account. Service accounts always use a connection's shared credential."
        />
      </Page>
    );

  const dismiss = () => setParams({}, { replace: true });

  return (
    <Page>
      <PageHeader
        icon={<KeyRound className="size-4" />}
        title="Connected accounts"
        description="Some connections act as the person a run is for, using that person's own token or OAuth sign-in. Connect yours here. Values are encrypted, write-only and never shown again, even to admins."
      />
      {connected && (
        <Notice tone="ok" onDismiss={dismiss}>
          <CheckCircle2 className="size-4" /> Connected <span className="font-mono">{connected}</span>. Runs waiting for it carry on by themselves.
        </Notice>
      )}
      {failed && (
        <Notice tone="bad" onDismiss={dismiss}>
          <XCircle className="size-4" /> The provider didn't complete the sign-in (<span className="font-mono">{failed}</span>). Try again, or paste a token instead.
        </Notice>
      )}
      {error ? <ErrorNotice error={error} className="mb-4" /> : null}
      <Card>
        {loading || mine.isLoading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-12" />
            <Skeleton className="h-12" />
          </div>
        ) : !rows.length ? (
          <EmptyState
            icon={<KeyRound className="size-5" />}
            title="Nothing to connect"
            description="No connection you can see asks for each person's own credential. Admins turn this on in a connection's Whose credential settings."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Connection</Th>
                <Th>Mode</Th>
                <Th>Status</Th>
                <Th className="text-right">Actions</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const key = `${r.ns}/${r.name}`;
                const highlighted = key === focus || key === connected;
                return (
                  <Tr key={key} className={cn(highlighted && "bg-accent-soft/60")}>
                    <Td>
                      <div id={`cred-${key}`} className="flex items-center gap-2.5">
                        <IntegrationMark integrationKey={r.spec?.integration ?? ""} size="sm" />
                        <div className="min-w-0">
                          <Link to={nsPath(r.ns, "connections", r.name)} className="font-medium hover:text-accent">
                            {r.name}
                          </Link>
                          <div className="truncate text-xs text-faint">
                            {r.ns}
                            {r.description ? ` · ${r.description}` : ""}
                          </div>
                        </div>
                      </div>
                    </Td>
                    <Td>{r.spec ? <Badge>{MODE_LABEL[r.spec.credentials as CredentialMode]}</Badge> : <span className="text-xs text-faint">No longer visible</span>}</Td>
                    <Td>
                      <CredentialStatus cred={r.cred} />
                    </Td>
                    <Td className="text-right">
                      <div className="flex justify-end">
                        <CredentialActions ns={r.ns} connection={r.name} spec={r.spec ?? {}} cred={r.cred} size="sm" />
                      </div>
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
    </Page>
  );
}

function Notice({ tone, children, onDismiss }: { tone: "ok" | "bad"; children: ReactNode; onDismiss: () => void }) {
  return (
    <div
      role="status"
      className={cn(
        "mb-4 flex items-center gap-2 rounded-lg border px-3 py-2 text-[13px]",
        tone === "ok" ? "border-ok/25 bg-ok-soft text-ok" : "border-bad/25 bg-bad-soft text-bad",
      )}
    >
      {children}
      <button className="ml-auto text-xs underline-offset-2 hover:underline" onClick={onDismiss}>
        Dismiss
      </button>
    </div>
  );
}
