/** Cloud identities: in-page AWS role setup (trust policy, namespace binding) and a Test button. */
import { useQuery } from "@tanstack/react-query";
import { Cloud, FlaskConical, TriangleAlert } from "lucide-react";
import { Fragment, useEffect, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { rawJson } from "@/api/client";
import { Button, Card, CardHeader, CodeEditor, CopyButton, CopyField, Segmented } from "@/components/ui";
import type { EditorContext, EditorExtension } from "./ResourceEditorPage";

type Scope = "identity" | "namespace";

type Method = "console" | "cli" | "terraform";

interface Instructions {
  title: string;
  summary: string;
  console: string[];
  cli: string;
  terraform: string;
}

interface AccountSetup extends Instructions {
  values: { label: string; value: string }[];
}

interface RoleSetup extends Instructions {
  policy: Record<string, unknown>;
  explain: { field: string; value: string; meaning: string }[];
}

interface TrustPolicy {
  identity: string;
  subject: string;
  trust: string;
  scope: Scope;
  role_arn: string;
  role_name: string;
  account_setup: AccountSetup;
  role_setup: RoleSetup;
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

function HowTo({ setup, method }: { setup: Instructions; method: Method }) {
  if (method === "console")
    return (
      <ol className="list-decimal space-y-1 pl-5">
        {setup.console.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ol>
    );
  return <Command value={method === "cli" ? setup.cli : setup.terraform} />;
}

function Step({ n, title, summary, children }: { n: number; title: string; summary?: string; children: ReactNode }) {
  return (
    <section className="space-y-2 border-t border-border pt-4 first:border-t-0 first:pt-0">
      <div className="flex items-baseline gap-2">
        <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-accent/15 text-xs font-semibold text-accent">{n}</span>
        <h3 className="font-medium">{title}</h3>
      </div>
      {summary && <p className="text-muted">{summary}</p>}
      {children}
    </section>
  );
}

function SetupGuide({ ctx }: { ctx: EditorContext }) {
  const [scope, setScope] = useState<Scope>("identity");
  const [method, setMethod] = useState<Method>("console");
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
  const policy = data ? JSON.stringify(data.role_setup.policy, null, 2) : "";
  return (
    <Card>
      <CardHeader
        title="Set up AWS"
        description="Shoal never stores AWS keys. Your AWS account trusts Shoal, the role trusts that connection, and each step gets a short-lived session tagged with its namespace, workflow, run and the person who started it."
      />
      <div className="space-y-4 p-4 text-sm">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
          <div className="space-y-1">
            <div className="text-xs font-medium text-muted">Who may assume the role</div>
            <Segmented<Scope>
              value={scope}
              onChange={setScope}
              options={[
                { value: "identity", label: "Only this identity", title: "The trust policy names this identity exactly" },
                { value: "namespace", label: `Any identity in ${ctx.ns}`, title: "Bind the role to this Shoal namespace" },
              ]}
            />
          </div>
          <div className="space-y-1">
            <div className="text-xs font-medium text-muted">Show instructions for</div>
            <Segmented<Method>
              value={method}
              onChange={setMethod}
              options={[
                { value: "console", label: "AWS console" },
                { value: "cli", label: "AWS CLI" },
                { value: "terraform", label: "Terraform" },
              ]}
            />
          </div>
        </div>
        <p className="text-muted">
          {scope === "identity"
            ? "The safest choice: the role can only be used through this one cloud identity."
            : `The role can be used by any cloud identity in the ${ctx.ns} namespace, and only for runs in ${ctx.ns}. Namespace admins can then register more identities for it without changing AWS.`}
        </p>
        {data?.notes.map((note) => (
          <p key={note} className="flex items-start gap-2 text-warn">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" /> {note}
          </p>
        ))}
        {error ? (
          <p className="text-muted">Fill in the role ARN and region to see the setup steps. ({error instanceof Error ? error.message : String(error)})</p>
        ) : data ? (
          <div className="space-y-4">
            <Step n={1} title={data.account_setup.title} summary={data.account_setup.summary}>
              <div className="grid gap-2 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-center">
                {data.account_setup.values.map((v) => (
                  <Fragment key={v.label}>
                    <span className="text-xs text-muted">{v.label}</span>
                    <CopyField value={v.value} />
                  </Fragment>
                ))}
              </div>
              <HowTo setup={data.account_setup} method={method} />
            </Step>
            <Step n={2} title={data.role_setup.title} summary={data.role_setup.summary}>
              <div className="flex items-center justify-between">
                <span className="font-medium">Trust policy for {data.role_name}</span>
                <CopyButton value={policy} label="Copy" />
              </div>
              <CodeEditor value={policy} language="json" readOnly minHeight="180px" maxHeight="420px" onChange={() => undefined} />
              <details className="rounded-lg border border-border p-2">
                <summary className="cursor-pointer text-xs font-medium">What each part means</summary>
                <dl className="mt-2 space-y-2">
                  {data.role_setup.explain.map((e) => (
                    <div key={e.field}>
                      <dt className="font-mono text-xs">{e.field}</dt>
                      <dd className="text-muted">{e.meaning}</dd>
                    </div>
                  ))}
                </dl>
              </details>
              <HowTo setup={data.role_setup} method={method} />
            </Step>
            <Step
              n={3}
              title="Give the role only the permissions the workflows need"
              summary="Attach managed or inline permission policies to the role as usual. Session tags can narrow them further, e.g. a condition on aws:PrincipalTag/shoal:namespace or aws:PrincipalTag/shoal:workflow."
            >
              <Command value={`aws iam attach-role-policy --role-name ${data.role_name} --policy-arn <policy ARN>`} />
            </Step>
            <Step
              n={4}
              title="Grant workflows, save, then Test"
              summary="List the workflows (and optionally agents) that may use this identity under Allow, save, and choose Test at the top of the page. Test assumes the role once, as a run would, and throws the credentials away."
            >
              {null}
            </Step>
          </div>
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
  tabs: (ctx) => [
    {
      value: "aws-setup",
      label: (
        <>
          <Cloud className="size-3.5" /> AWS setup
        </>
      ),
      content: <SetupGuide ctx={ctx} />,
      showOnNew: true,
    },
  ],
  actions: (ctx) => <TestButton ctx={ctx} />,
};
