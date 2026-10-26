/** Link my own Claude subscription to each claude-code gateway that runs on people's plans. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, LogIn, Sparkles, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { rawJson } from "@/api/client";
import { cn } from "@/lib/cn";
import { dateTime } from "@/lib/format";
import { Badge, Button, Card, CardHeader, Dialog, Field, Input, Segmented, Table, Td, Th, Tr, useConfirm } from "@/components/ui";
import { credentialKeys } from "@/pages/connect/PersonalCredentials";

export interface ClaudeGateway {
  gateway: string;
  description?: string | null;
  mode: "api_key" | "subscription";
  problem?: string | null;
  linked: boolean;
  linked_at?: string | null;
  expires_at?: string | null;
  terms?: string | null;
}

const KEY = ["me", "claude-code"] as const;
const base = (gateway: string) => `/v1/me/claude-code/${encodeURIComponent(gateway)}`;

export function useClaudeGateways(enabled: boolean) {
  return useQuery({ queryKey: KEY, queryFn: () => rawJson<ClaudeGateway[]>("/v1/me/claude-code"), enabled, retry: false });
}

export function useRefresh() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: KEY });
    void qc.invalidateQueries({ queryKey: credentialKeys.mine });
  };
}

export function ClaudeSubscriptions({ focus }: { focus?: string | null }) {
  const { data } = useClaudeGateways(true);
  const [linking, setLinking] = useState<string | null>(null);
  const refresh = useRefresh();
  const confirm = useConfirm();
  const unlink = useMutation({
    mutationFn: (gateway: string) => rawJson(base(gateway), { method: "DELETE" }),
    onSuccess: (_r, gateway) => {
      toast.success(`Unlinked your Claude subscription from ${gateway}`);
      refresh();
    },
  });
  const rows = (data ?? []).filter((g) => g.mode === "subscription");
  if (!rows.length) return null;
  return (
    <Card className="mb-4">
      <CardHeader
        title="Claude subscription"
        description={rows[0].terms ?? "Claude Code runs you start use your own Claude plan."}
      />
      <Table>
        <thead>
          <tr>
            <Th>Gateway</Th>
            <Th>Status</Th>
            <Th className="text-right">Actions</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((g) => (
            <Tr key={g.gateway} className={cn(focus === `shared/${g.gateway}` && "bg-accent-soft/60")}>
              <Td>
                <div id={`cred-shared/${g.gateway}`} className="flex items-center gap-2.5">
                  <Sparkles className="size-4 text-accent" />
                  <div className="min-w-0">
                    <div className="font-medium">{g.gateway}</div>
                    <div className="truncate text-xs text-faint">{g.description ?? "Claude Code"}</div>
                  </div>
                </div>
              </Td>
              <Td>
                {g.problem ? (
                  <Badge tone="warn" title={g.problem}>Not set up by an admin yet</Badge>
                ) : g.linked ? (
                  <Badge tone="ok" dot title={g.expires_at ? `Token expires ${dateTime(g.expires_at)}` : undefined}>
                    Linked {g.linked_at ? dateTime(g.linked_at) : ""}
                  </Badge>
                ) : (
                  <Badge>Not linked</Badge>
                )}
              </Td>
              <Td className="text-right">
                <div className="flex justify-end gap-2">
                  <Button size="sm" variant={g.linked ? "secondary" : "primary"} icon={<LogIn className="size-3.5" />} disabled={!!g.problem} onClick={() => setLinking(g.gateway)}>
                    {g.linked ? "Sign in again" : "Link my subscription"}
                  </Button>
                  {g.linked && (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={<Trash2 className="size-3.5" />}
                      onClick={async () => {
                        if (await confirm({ title: `Unlink ${g.gateway}?`, description: "Runs you start on this gateway will wait until you link a subscription again." }))
                          unlink.mutate(g.gateway);
                      }}
                    >
                      Unlink
                    </Button>
                  )}
                </div>
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
      {linking && <LinkDialog gateway={linking} onClose={() => setLinking(null)} onLinked={refresh} />}
    </Card>
  );
}

export function LinkDialog({ gateway, onClose, onLinked, shared = false }: { gateway: string; onClose: () => void; onLinked: () => void; shared?: boolean }) {
  const base = (g: string) => (shared ? `/v1/claude-code/gateways/${encodeURIComponent(g)}/account` : `/v1/me/claude-code/${encodeURIComponent(g)}`);
  const [how, setHow] = useState<"browser" | "token">("browser");
  const [login, setLogin] = useState<{ login_id: string; url: string } | null>(null);
  const [code, setCode] = useState("");
  const [token, setToken] = useState("");
  const done = () => {
    toast.success(shared ? `Signed ${gateway} in: every call through it now uses this Claude account.` : `Linked your Claude subscription to ${gateway}. Runs waiting for it carry on by themselves.`);
    onLinked();
    onClose();
  };
  const start = useMutation({
    mutationFn: () => rawJson<{ login_id: string; url: string }>(`${base(gateway)}/login`, { method: "POST" }),
    onSuccess: setLogin,
  });
  const finish = useMutation({
    mutationFn: () => rawJson(`${base(gateway)}/login/${login!.login_id}`, { method: "POST", body: JSON.stringify({ code }) }),
    onSuccess: done,
    onError: () => setLogin(null),
  });
  const paste = useMutation({
    mutationFn: () => rawJson(`${base(gateway)}/token`, { method: "PUT", body: JSON.stringify({ token }) }),
    onSuccess: done,
  });
  const close = () => {
    if (login) void rawJson(`${base(gateway)}/login/${login.login_id}`, { method: "DELETE" }).catch(() => undefined);
    onClose();
  };
  const error = (start.error ?? finish.error ?? paste.error) as Error | null;
  return (
    <Dialog
      open
      onOpenChange={(o) => !o && close()}
      title={shared ? "Sign the gateway in to Claude" : "Link your Claude subscription"}
      description={shared ? `Every call through ${gateway}, for everyone, will use the Claude account you sign in with. Sign-in happens on claude.ai; Shoal never sees your password.` : `Claude Code runs you start through ${gateway} will use your own plan. Sign-in happens on claude.ai; Shoal never sees your password.`}
      footer={
        how === "token" ? (
          <Button onClick={() => paste.mutate()} disabled={token.trim().length < 20} loading={paste.isPending}>
            Save token
          </Button>
        ) : login ? (
          <Button onClick={() => finish.mutate()} disabled={!code.trim()} loading={finish.isPending}>
            Finish sign-in
          </Button>
        ) : (
          <Button icon={<LogIn className="size-3.5" />} onClick={() => start.mutate()} loading={start.isPending}>
            Start sign-in
          </Button>
        )
      }
    >
      <div className="space-y-4">
        <Segmented
          value={how}
          onChange={setHow}
          options={[
            { value: "browser", label: "Sign in with Claude" },
            { value: "token", label: "Paste a token" },
          ]}
        />
        {how === "browser" ? (
          login ? (
            <ol className="list-decimal space-y-3 pl-5 text-[13px]">
              <li>
                <a href={login.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">
                  Open the Claude sign-in page <ExternalLink className="size-3.5" />
                </a>{" "}
                and approve access.
              </li>
              <li>
                <Field label="Paste the code claude.ai shows you">
                  <Input mono autoFocus value={code} onChange={(e) => setCode(e.target.value)} placeholder="code#state" />
                </Field>
              </li>
            </ol>
          ) : (
            <p className="text-[13px] text-muted">
              Shoal starts Claude Code's own sign-in (<span className="font-mono">claude setup-token</span>) and gives you Anthropic's sign-in link. You
              approve on claude.ai and paste back the code it shows. The resulting one-year token is stored encrypted, only for your runs.
            </p>
          )
        ) : (
          <Field label="Token" description="Run claude setup-token on your own machine and paste what it prints (it starts with sk-ant-oat).">
            <Input mono type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="sk-ant-oat01-…" />
          </Field>
        )}
        {error && <p className="text-[13px] text-bad">{error.message}</p>}
      </div>
    </Dialog>
  );
}
