import { ArrowRight, BookOpen, Boxes, Code2, Compass, Cpu, Plug, Rocket, ShieldCheck } from "lucide-react";
import { useEffect } from "react";
import { Link } from "react-router";
import type { ReactNode } from "react";
import { DocsSearch } from "./DocsLayout";
import { DOC_SECTIONS, findPage } from "./registry";

const ICONS: Record<string, ReactNode> = {
  "getting-started": <Rocket className="size-4" />,
  concepts: <Compass className="size-4" />,
  guides: <BookOpen className="size-4" />,
  administration: <ShieldCheck className="size-4" />,
  integrations: <Plug className="size-4" />,
  reference: <Code2 className="size-4" />,
  internals: <Cpu className="size-4" />,
};

export default function DocsHome() {
  useEffect(() => {
    document.title = "Docs · Shoal";
    return () => {
      document.title = "Shoal";
    };
  }, []);
  const start = findPage("getting-started");
  return (
    <div className="mx-auto max-w-5xl px-8 py-10">
      <div className="relative overflow-hidden rounded-2xl border border-border bg-panel px-8 py-10 shadow-card">
        <div aria-hidden className="pointer-events-none absolute -top-24 -right-20 h-72 w-72 rounded-full bg-accent/10 blur-3xl" />
        <div className="relative max-w-2xl">
          <div className="mb-3 inline-flex items-center gap-1.5 rounded-full border border-border bg-subtle px-2.5 py-0.5 text-[11px] font-medium text-muted">
            <Boxes className="size-3" /> Matches the version you are running
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">Shoal documentation</h1>
          <p className="mt-2 text-[14px] leading-relaxed text-muted">
            Guides for building and running agent workflows, administering your organisation, building integrations, and the full
            reference for resources, step types and the HTTP API.
          </p>
          <div className="mt-6 max-w-xl">
            <DocsSearch large autoFocus />
          </div>
          {start && (
            <Link to={`/docs/${start.slug}`} className="mt-4 inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              New here? Start with {start.title} <ArrowRight className="size-3.5" />
            </Link>
          )}
        </div>
      </div>

      <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {DOC_SECTIONS.map((s) => (
          <div key={s.key} className="flex flex-col rounded-xl border border-border bg-panel p-5 shadow-card">
            <div className="flex items-center gap-2.5">
              <span className="flex size-8 items-center justify-center rounded-lg bg-accent-soft text-accent">{ICONS[s.key]}</span>
              <h2 className="text-[14px] font-semibold">{s.title}</h2>
            </div>
            {s.description && <p className="mt-2 text-[13px] leading-snug text-muted">{s.description}</p>}
            <ul className="mt-3 space-y-1 border-t border-border pt-3">
              {s.pages.map((p) => (
                <li key={p.slug}>
                  <Link to={`/docs/${p.slug}`} className="group flex items-center gap-1.5 text-[13px] text-fg hover:text-accent">
                    <ArrowRight className="size-3 text-faint transition-transform group-hover:translate-x-0.5 group-hover:text-accent" />
                    {p.title}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}
