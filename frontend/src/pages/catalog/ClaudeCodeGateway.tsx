/** The sign-in section of a claude-code gateway: an API key, or each person's own Claude subscription. */
import { KeyRound, TriangleAlert, UserRound } from "lucide-react";
import { Link } from "react-router";
import { Card, CardHeader, Field, Segmented, Switch } from "@/components/ui";
import type { EditorContext } from "./ResourceEditorPage";

export const CLAUDE_CODE_TERMS =
  "Subscription mode uses each person's own Claude plan for the runs they start. Anthropic's terms do not allow routing Free, Pro or Max credentials on behalf of other people, and plan usage limits assume ordinary, individual use. Use an API key for shared, scheduled or service-account runs.";

type Mode = "api_key" | "subscription";

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
        description="Agents with runtime claude-code run the Claude Code CLI (claude -p) in their sandbox through this gateway."
      />
      <fieldset disabled={!canWrite} className="space-y-4 p-4">
        <Field label="Who pays">
          <Segmented<Mode>
            value={mode}
            onChange={(m) => setConfig({ mode: m, acknowledge_subscription_terms: m === "subscription" ? acknowledged || undefined : undefined })}
            options={[
              { value: "api_key", label: "API key", icon: <KeyRound className="size-3.5" />, title: "The gateway's API key, for every run" },
              { value: "subscription", label: "Each person's Claude plan", icon: <UserRound className="size-3.5" />, title: "Pro, Max, Team or Enterprise" },
            ]}
          />
        </Field>
        {mode === "api_key" ? (
          <p className="text-[13px] text-muted">
            Set <span className="font-mono">api_key_secret</span> above to a shared secret holding a Claude Console key. Base URL and headers reach the CLI as{" "}
            <span className="font-mono">ANTHROPIC_BASE_URL</span> and <span className="font-mono">ANTHROPIC_CUSTOM_HEADERS</span>, so LiteLLM or Portkey work too.
            Every run can use it, including schedules and service accounts.
          </p>
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
