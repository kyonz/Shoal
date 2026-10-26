/** The sign-in section of a claude-code gateway: an API key, each person's own Claude subscription, or one shared account. */
import { KeyRound, LogIn, TriangleAlert, UserRound } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { rawJson } from "@/api/client";
import { Link } from "react-router";
import { Badge, Button, Card, CardHeader, Field, Segmented, Switch } from "@/components/ui";
import { useIsPerson } from "@/pages/connect/PersonalCredentials";
import { LinkDialog, useClaudeGateways, useRefresh } from "@/pages/settings/ClaudeSubscriptions";
import type { EditorContext } from "./ResourceEditorPage";

export const CLAUDE_CODE_TERMS =
  "Subscription mode uses each person's own Claude plan for the runs they start. Anthropic's terms do not allow routing Free, Pro or Max credentials on behalf of other people, and plan usage limits assume ordinary, individual use. Use an API key for shared, scheduled or service-account runs.";

type Mode = "api_key" | "subscription" | "shared_account";

export const SHARED_ACCOUNT_NOTE =
  "Every call through this gateway, for everyone in the organisation (people, schedules, service accounts and the assistant), uses the one Claude account signed in here. Anthropic's terms do not allow one person's Free, Pro or Max plan to be used by more than one individual, so keep this to single-person instances and testing.";

export function ClaudeCodeGatewaySection({ ctx }: { ctx: EditorContext }) {
  const { spec, setSpec, canWrite } = ctx;
  const config = (spec.config as Record<string, unknown>) ?? {};
  const mode: Mode = (config.mode as Mode) ?? (spec.api_key_secret ? "api_key" : "subscription");
  const acknowledged = config.acknowledge_subscription_terms === true;
  const setConfig = (patch: Record<string, unknown>) => {
    const next = { ...config, ...patch };
    for (const [k, v] of Object.entries(next)) if (v === undefined) delete next[k];
    setSpec({ ...spec, config: next });
  };
  return (
    <Card>
      <CardHeader
        title="Claude Code sign-in"
        description="Agents with runtime claude-code run the Claude Code CLI (claude -p) in their sandbox through this gateway. It also works as a model gateway for the assistant and tool-loop agents."
      />
      <fieldset disabled={!canWrite} className="space-y-4 p-4">
        <Field label="Who pays">
          <Segmented<Mode>
            value={mode}
            onChange={(m) => setConfig({ mode: m, acknowledge_subscription_terms: m === "subscription" ? acknowledged || undefined : undefined })}
            options={[
              { value: "api_key", label: "API key", icon: <KeyRound className="size-3.5" />, title: "The gateway's API key, for every run" },
              { value: "subscription", label: "Each person's Claude plan", icon: <UserRound className="size-3.5" />, title: "Pro, Max, Team or Enterprise" },
              { value: "shared_account", label: "One Claude account", icon: <LogIn className="size-3.5" />, title: "An admin signs in once; everything uses it" },
            ]}
          />
        </Field>
        {mode === "api_key" ? (
          <p className="text-[13px] text-muted">
            Set <span className="font-mono">api_key_secret</span> above to a shared secret holding a Claude Console key. Base URL and headers reach the CLI as{" "}
            <span className="font-mono">ANTHROPIC_BASE_URL</span> and <span className="font-mono">ANTHROPIC_CUSTOM_HEADERS</span>, so LiteLLM or Portkey work too.
            Every run can use it, including schedules and service accounts.
          </p>
        ) : mode === "shared_account" ? (
          <div className="space-y-3">
            <div className="flex gap-2 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2 text-[13px] text-warn">
              <TriangleAlert className="mt-0.5 size-4 shrink-0" />
              <span>{SHARED_ACCOUNT_NOTE}</span>
            </div>
            {ctx.name && !ctx.isNew && (ctx.resource?.spec as { config?: { mode?: string } } | undefined)?.config?.mode === "shared_account" ? (
              <SharedAccount gateway={ctx.name} />
            ) : (
              <p className="text-[13px] text-muted">Save the gateway, then sign it in to Claude here.</p>
            )}
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex gap-2 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2 text-[13px] text-warn">
              <TriangleAlert className="mt-0.5 size-4 shrink-0" />
              <span>
                {CLAUDE_CODE_TERMS}{" "}
                <a className="underline" href="https://code.claude.com/docs/en/legal-and-compliance" target="_blank" rel="noreferrer">
                  Anthropic's terms
                </a>
              </span>
            </div>
            <Switch
              checked={acknowledged}
              onCheckedChange={(v) => setConfig({ acknowledge_subscription_terms: v || undefined })}
              label="I understand: each person links their own subscription, and it only runs what they start"
            />
            {ctx.name && !ctx.isNew && <MySubscription gateway={ctx.name} saved={!ctx.dirty} />}
            <p className="text-[13px] text-muted">
              People link their plan once under{" "}
              <Link to="/settings/credentials" className="text-accent hover:underline">
                Connected accounts
              </Link>
              , by signing in to Claude from the browser or pasting a token from <span className="font-mono">claude setup-token</span>. Runs by someone who
              hasn't linked wait for them; runs by service accounts fail.
            </p>
          </div>
        )}
      </fieldset>
    </Card>
  );
}

/** Sign in with your own Claude plan right here (the same link as under Connected accounts). */
function MySubscription({ gateway, saved }: { gateway: string; saved: boolean }) {
  const person = useIsPerson();
  const { data } = useClaudeGateways(person);
  const refresh = useRefresh();
  const [open, setOpen] = useState(false);
  if (!person) return null;
  const mine = data?.find((g) => g.gateway === gateway);
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border px-3 py-2 text-[13px]">
      <span className="min-w-0 flex-1">
        Your Claude subscription:{" "}
        {mine?.linked ? <Badge tone="ok" dot>Linked</Badge> : <Badge>Not linked</Badge>}
        <span className="ml-2 text-muted">Runs and chats you start on this gateway use your own plan.</span>
      </span>
      <Button size="sm" variant={mine?.linked ? "secondary" : "primary"} icon={<LogIn className="size-3.5" />}
        disabled={!saved || !mine || !!mine.problem} title={!saved ? "Save the gateway first" : mine?.problem ?? undefined} onClick={() => setOpen(true)}>
        {mine?.linked ? "Sign in again" : "Sign in with Claude"}
      </Button>
      {open && <LinkDialog gateway={gateway} onClose={() => setOpen(false)} onLinked={refresh} />}
    </div>
  );
}

/** The gateway's one shared Claude account: sign it in with the Claude Code login link, or a pasted token. */
function SharedAccount({ gateway }: { gateway: string }) {
  const qc = useQueryClient();
  const key = ["claude-code", "account", gateway];
  const { data } = useQuery({
    queryKey: key,
    queryFn: () => rawJson<{ signed_in: boolean; secret: string }>(`/v1/claude-code/gateways/${encodeURIComponent(gateway)}/account`),
    retry: false,
  });
  const [open, setOpen] = useState(false);
  const signOut = useMutation({
    mutationFn: () => rawJson(`/v1/claude-code/gateways/${encodeURIComponent(gateway)}/account`, { method: "DELETE" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: key }),
  });
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border px-3 py-2 text-[13px]">
      <span className="min-w-0 flex-1">
        Claude account: {data?.signed_in ? <Badge tone="ok" dot>Signed in</Badge> : <Badge>Not signed in</Badge>}
        {data && <span className="ml-2 text-muted">Token kept in the shared secret <span className="font-mono">{data.secret}</span>.</span>}
      </span>
      <Button size="sm" variant={data?.signed_in ? "secondary" : "primary"} icon={<LogIn className="size-3.5" />} onClick={() => setOpen(true)}>
        {data?.signed_in ? "Sign in again" : "Sign in with Claude"}
      </Button>
      {data?.signed_in && (
        <Button size="sm" variant="ghost" loading={signOut.isPending} onClick={() => signOut.mutate()}>
          Sign out
        </Button>
      )}
      {open && <LinkDialog shared gateway={gateway} onClose={() => setOpen(false)} onLinked={() => void qc.invalidateQueries({ queryKey: key })} />}
    </div>
  );
}
