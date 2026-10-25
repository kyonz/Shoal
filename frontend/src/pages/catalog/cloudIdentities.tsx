/** Cloud identities: in-page AWS role setup (trust policy, namespace binding) and a Test button. */
import { useQuery } from "@tanstack/react-query";
import { FlaskConical, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { rawJson } from "@/api/client";
import { Button, Card, CardHeader, CodeEditor, CopyButton, Segmented } from "@/components/ui";
import type { EditorContext, EditorExtension } from "./ResourceEditorPage";

type Scope = "identity" | "namespace";

interface SetupStep {
  title: string;
  detail: string;
  command: string;
}

interface TrustPolicy {
  identity: string;
  subject: string;
  trust: string;
  scope: Scope;
  role_arn: string;
  issuer?: string;
  policy: Record<string, unknown>;
  steps: SetupStep[];
  notes: string[];
}

interface TestResult {
  ok: boolean;
  identity: string;
  assumed_role_arn?: string;
  expires_at?: string;
  error?: string;
  hint?: string;
}

function useDebounced<T>(value: T, ms: number): T {
  const [current, setCurrent] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setCurrent(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return current;
}

function Command({ value }: { value: string }) {
  return (
    <div className="flex items-start gap-1 rounded-lg border border-border bg-subtle p-2">
      <pre className="min-w-0 flex-1 overflow-x-auto font-mono text-xs whitespace-pre">{value}</pre>
      <CopyButton value={value} />
    </div>
  );
}

function SetupGuide({ ctx }: { ctx: EditorContext }) {
  const [scope, setScope] = useState<Scope>("identity");
  const name = ctx.draft.name || "new-identity";
  const spec = useDebounced(JSON.stringify(ctx.spec), 400);
  const { data, error } = useQuery({
    queryKey: ["cloud-identities", "trust-policy", ctx.ns, name, scope, spec],
    queryFn: () =>
      rawJson<TrustPolicy>(`/v1/namespaces/${ctx.ns}/cloud-identities/${encodeURIComponent(name)}/trust-policy?scope=${scope}`, {
        method: "POST",
        body: JSON.stringify({ metadata: {}, spec: JSON.parse(spec) }),
      }),
    retry: false,
  });
  const policy = data ? JSON.stringify(data.policy, null, 2) : "";
  return (
    <Card>
      <CardHeader
        title="Set up the role in AWS"
        description="Shoal never stores AWS keys. The role trusts Shoal, and each step gets a short-lived session tagged with its namespace, workflow, run and the person who started it."
      />
      <div className="space-y-4 p-4 text-sm">
        <div className="space-y-2">
          <div className="font-medium">Who may assume the role</div>
          <Segmented<Scope>
            value={scope}
            onChange={setScope}
            options={[
              { value: "identity", label: "Only this identity", title: "The trust policy names this identity exactly" },
              { value: "namespace", label: `Any identity in ${ctx.ns}`, title: "Bind the role to this Shoal namespace" },
            ]}
          />
          <p className="text-muted">
            {scope === "identity"
              ? "The safest choice: the role can only be used through this one cloud identity."
              : `The role can be used by any cloud identity in the ${ctx.ns} namespace, and only for runs in ${ctx.ns}. Namespace admins can then register more identities for it without changing AWS.`}
          </p>
        </div>
        {error ? (
          <p className="text-muted">Fill in the role ARN and region to see the setup steps. ({error instanceof Error ? error.message : String(error)})</p>
        ) : data ? (
          <>
            <ol className="space-y-3">
              {data.steps.map((step, i) => (
                <li key={step.title} className="space-y-1.5">
                  <div className="font-medium">
                    {i + 1}. {step.title}
                  </div>
                  <p className="text-muted">{step.detail}</p>
                  {step.command && <Command value={step.command} />}
                  {step.detail.includes("trust.json") && (
                    <div className="space-y-1.5 pt-1">
                      <div className="flex items-center justify-between">
                        <span className="font-medium">trust.json</span>
                        <CopyButton value={policy} label="Copy" />
                      </div>
                      <CodeEditor value={policy} language="json" readOnly minHeight="180px" maxHeight="420px" onChange={() => undefined} />
                      <p className="text-muted">
                        {data.trust === "web_identity" ? "Subject" : "ExternalId"}: <code className="font-mono text-xs">{data.subject}</code>
                      </p>
                    </div>
                  )}
                </li>
              ))}
            </ol>
            {data.notes.map((note) => (
              <p key={note} className="flex items-start gap-2 text-warn">
                <TriangleAlert className="mt-0.5 size-4 shrink-0" /> {note}
              </p>
            ))}
          </>
        ) : null}
      </div>
    </Card>
  );
}

function TestButton({ ctx }: { ctx: EditorContext }) {
  const [busy, setBusy] = useState(false);
  if (ctx.isNew || !ctx.canWrite || !ctx.name) return null;
  const test = async () => {
    if (ctx.dirty) {
      toast.error("Save your changes first: Test uses the saved identity.");
      return;
    }
    setBusy(true);
    try {
      const r = await rawJson<TestResult>(`/v1/namespaces/${ctx.ns}/cloud-identities/${ctx.name}/test`, { method: "POST" });
      if (r.ok) toast.success(`Assumed ${r.assumed_role_arn ?? "the role"}`, { description: r.expires_at ? `Session valid until ${r.expires_at}` : undefined });
      else toast.error(r.error ?? "Could not assume the role", { description: r.hint, duration: 15000 });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Test failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button icon={<FlaskConical className="size-4" />} loading={busy} onClick={() => void test()} title="Assume the role once to check the setup">
      Test
    </Button>
  );
}

export const cloudIdentityExtension: EditorExtension = {
  sections: (ctx) => <SetupGuide ctx={ctx} />,
  actions: (ctx) => <TestButton ctx={ctx} />,
};
