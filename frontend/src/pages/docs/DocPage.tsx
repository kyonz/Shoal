import { ArrowLeft, ArrowRight, ChevronRight, FileCode2 } from "lucide-react";
import { lazy, Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router";
import { cn } from "@/lib/cn";
import { EmptyState, Spinner } from "@/components/ui";
import { Markdown } from "./Markdown";
import { DOC_SECTIONS, findPage, ORDERED, type DocPage as Doc } from "./registry";

const ResourceSchemas = lazy(() => import("./reference/ResourceSchemas"));
const StepTypes = lazy(() => import("./reference/StepTypes"));
const ApiReference = lazy(() => import("./reference/ApiReference"));

const GENERATED: Record<string, () => ReactNode> = {
  "reference/resources": () => <ResourceSchemas />,
  "reference/steps": () => <StepTypes />,
  "reference/api": () => <ApiReference />,
};

interface TocItem {
  id: string;
  text: string;
  depth: number;
}

/** Reads h2/h3 from the rendered article (so ids always match rehype-slug) and tracks the visible one. */
function useToc(article: React.RefObject<HTMLElement | null>, key: string) {
  const [items, setItems] = useState<TocItem[]>([]);
  const [active, setActive] = useState<string | null>(null);

  useEffect(() => {
    const el = article.current;
    if (!el) return;
    const collect = () => {
      const hs = Array.from(el.querySelectorAll<HTMLElement>("h2[id], h3[id]"));
      const next = hs.map((h) => ({ id: h.id, text: h.dataset.tocLabel ?? h.textContent ?? "", depth: h.tagName === "H2" ? 2 : 3 }));
      setItems((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
      return hs;
    };
    let headings = collect();
    setActive(headings[0]?.id ?? null);
    // Generated pages render after data loads; re-collect when the article changes.
    const mo = new MutationObserver(() => {
      headings = collect();
      observe();
    });
    mo.observe(el, { childList: true, subtree: true });
    let io: IntersectionObserver | null = null;
    const visible = new Set<string>();
    const observe = () => {
      io?.disconnect();
      visible.clear();
      io = new IntersectionObserver(
        (entries) => {
          for (const e of entries) {
            if (e.isIntersecting) visible.add(e.target.id);
            else visible.delete(e.target.id);
          }
          const first = headings.find((h) => visible.has(h.id));
          if (first) setActive(first.id);
          else if (headings[0] && headings[0].getBoundingClientRect().top > window.innerHeight * 0.35) setActive(headings[0].id);
        },
        { rootMargin: "-56px 0px -65% 0px" },
      );
      headings.forEach((h) => io!.observe(h));
    };
    observe();
    return () => {
      mo.disconnect();
      io?.disconnect();
    };
  }, [article, key]);

  return { items, active };
}

function Toc({ items, active }: { items: TocItem[]; active: string | null }) {
  if (items.length < 2) return null;
  return (
    <nav className="sticky top-6 max-h-[calc(100vh-7rem)] overflow-y-auto" aria-label="On this page">
      <div className="mb-2 text-[11px] font-semibold tracking-wider text-faint uppercase">On this page</div>
      <ul className="space-y-0.5 border-l border-border">
        {items.map((it) => (
          <li key={it.id}>
            <a
              href={`#${it.id}`}
              onClick={(e) => {
                e.preventDefault();
                document.getElementById(it.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
                history.replaceState(null, "", `#${it.id}`);
              }}
              className={cn(
                "-ml-px block border-l py-1 text-[12.5px] leading-snug transition-colors",
                it.depth === 3 ? "pl-6" : "pl-3",
                active === it.id ? "border-accent font-medium text-accent" : "border-transparent text-muted hover:text-fg",
              )}
            >
              {it.text}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function PrevNext({ page }: { page: Doc }) {
  const i = ORDERED.findIndex((p) => p.slug === page.slug);
  const prev = i > 0 ? ORDERED[i - 1] : undefined;
  const next = i >= 0 && i < ORDERED.length - 1 ? ORDERED[i + 1] : undefined;
  const card = "group flex flex-1 flex-col rounded-xl border border-border bg-panel px-4 py-3 shadow-card transition-colors hover:border-accent/50";
  return (
    <div className="mt-12 flex gap-3 border-t border-border pt-6">
      {prev ? (
        <Link to={`/docs/${prev.slug}`} className={card}>
          <span className="flex items-center gap-1 text-xs text-faint">
            <ArrowLeft className="size-3" /> Previous
          </span>
          <span className="mt-0.5 text-[13px] font-medium group-hover:text-accent">{prev.title}</span>
        </Link>
      ) : (
        <div className="flex-1" />
      )}
      {next ? (
        <Link to={`/docs/${next.slug}`} className={cn(card, "items-end text-right")}>
          <span className="flex items-center gap-1 text-xs text-faint">
            Next <ArrowRight className="size-3" />
          </span>
          <span className="mt-0.5 text-[13px] font-medium group-hover:text-accent">{next.title}</span>
        </Link>
      ) : (
        <div className="flex-1" />
      )}
    </div>
  );
}

export default function DocPage() {
  const slug = useParams()["*"] ?? "";
  const page = findPage(slug);
  const location = useLocation();
  const article = useRef<HTMLElement>(null);
  const { items, active } = useToc(article, slug);

  // Scroll to the hash (deep links from search and "Learn more"), else to the top.
  useEffect(() => {
    const id = decodeURIComponent(location.hash.slice(1));
    const t = setTimeout(() => {
      const target = id ? document.getElementById(id) : null;
      if (target) target.scrollIntoView({ block: "start" });
      else article.current?.closest("main")?.scrollTo({ top: 0 });
    }, 30);
    return () => clearTimeout(t);
  }, [slug, location.hash]);

  useEffect(() => {
    if (page) document.title = `${page.title} · Shoal docs`;
    return () => {
      document.title = "Shoal";
    };
  }, [page]);

  if (!page) {
    return (
      <EmptyState
        className="h-full"
        title="Page not found"
        description={`There is no documentation page at “${slug}”.`}
        action={<Link to="/docs" className="text-[13px] text-accent">Back to the docs</Link>}
      />
    );
  }

  const section = DOC_SECTIONS.find((s) => s.key === page.section);
  const generated = GENERATED[page.slug];

  return (
    <div className="mx-auto flex max-w-[1180px] gap-10 px-8 py-8">
      <article ref={article} className="min-w-0 flex-1">
        <div className="mb-4 flex items-center gap-1 text-xs text-muted">
          <Link to="/docs" className="hover:text-fg">
            Docs
          </Link>
          <ChevronRight className="size-3" />
          <span>{section?.title}</span>
        </div>
        {generated ? (
          <>
          <div className="prose-shoal max-w-none">
            <h1 className="flex items-center gap-2.5">
              {page.title}
              <span className="inline-flex items-center gap-1 rounded-md border border-accent/20 bg-accent-soft px-1.5 py-0.5 text-[11px] font-medium tracking-normal text-accent">
                <FileCode2 className="size-3" /> Generated from the live API
              </span>
            </h1>
            <p className="text-muted">{page.summary}</p>
          </div>
          <Suspense fallback={<Spinner />}>{generated()}</Suspense>
          </>
        ) : (
          <Markdown source={page.body} slug={page.slug} />
        )}
        <PrevNext page={page} />
      </article>
      <aside className="hidden w-56 shrink-0 xl:block">
        <Toc items={items} active={active} />
      </aside>
    </div>
  );
}
