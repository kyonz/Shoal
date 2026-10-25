import { BookOpen, ChevronRight, FileCode2, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router";
import { cn } from "@/lib/cn";
import { Kbd } from "@/components/ui";
import { DOC_SECTIONS, search, type SearchHit } from "./registry";

export function Highlight({ text, query }: { text: string; query: string }) {
  const terms = query.toLowerCase().split(/\s+/).filter((t) => t.length > 1);
  if (!terms.length) return <>{text}</>;
  const re = new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return (
    <>
      {text.split(re).map((part, i) =>
        i % 2 ? (
          <mark key={i} className="rounded-sm bg-accent-soft px-0.5 text-accent">
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}

export function hitHref(hit: SearchHit) {
  return `/docs/${hit.page.slug}${hit.heading ? `#${hit.heading.id}` : ""}`;
}

/** Search box with keyboard navigation; "/" or ⌘/ focuses it. */
export function DocsSearch({ autoFocus, large, onNavigate }: { autoFocus?: boolean; large?: boolean; onNavigate?: () => void }) {
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();
  const hits = useMemo(() => search(q), [q]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      const typing = target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName);
      if ((e.key === "/" && (e.metaKey || e.ctrlKey)) || (e.key === "/" && !typing)) {
        e.preventDefault();
        input.current?.focus();
        input.current?.select();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = (hit: SearchHit) => {
    navigate(hitHref(hit));
    setQ("");
    input.current?.blur();
    onNavigate?.();
  };

  return (
    <div className="relative">
      <div
        className={cn(
          "flex items-center gap-2 rounded-lg border border-border bg-panel shadow-card focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/25",
          large ? "h-11 px-3.5" : "h-8 px-2.5",
        )}
      >
        <Search className={cn("shrink-0 text-faint", large ? "size-4" : "size-3.5")} />
        <input
          ref={input}
          autoFocus={autoFocus}
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setActive(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActive((a) => Math.min(a + 1, hits.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((a) => Math.max(a - 1, 0));
            } else if (e.key === "Enter" && hits[active]) {
              go(hits[active]);
            } else if (e.key === "Escape") {
              setQ("");
              input.current?.blur();
            }
          }}
          placeholder="Search the docs…"
          aria-label="Search the docs"
          style={{ outline: "none" }}
          className={cn("min-w-0 flex-1 bg-transparent outline-none focus-visible:outline-none placeholder:text-faint", large ? "text-[15px]" : "text-[13px]")}
        />
        {q ? (
          <button onClick={() => setQ("")} className="text-faint hover:text-fg" aria-label="Clear search">
            <X className="size-3.5" />
          </button>
        ) : (
          <Kbd>/</Kbd>
        )}
      </div>
      {q.trim().length > 1 && (
        <div
          className={cn(
            "absolute top-full right-0 left-0 z-30 mt-1.5 max-h-[60vh] animate-pop-in overflow-y-auto rounded-xl border border-border bg-raised p-1 shadow-pop",
            !large && "w-[420px] max-w-[calc(100vw-2rem)]",
          )}
        >
          {hits.length === 0 && <div className="px-3 py-6 text-center text-[13px] text-muted">No results for “{q}”.</div>}
          {hits.map((hit, i) => (
            <button
              key={hit.page.slug + (hit.heading?.id ?? "")}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => go(hit)}
              className={cn("block w-full rounded-lg px-3 py-2 text-left", i === active && "bg-subtle")}
            >
              <div className="flex items-center gap-1.5 text-[13px] font-medium">
                <Highlight text={hit.page.title} query={q} />
                {hit.heading && (
                  <>
                    <ChevronRight className="size-3 text-faint" />
                    <span className="truncate text-muted">
                      <Highlight text={hit.heading.text} query={q} />
                    </span>
                  </>
                )}
              </div>
              <div className="mt-0.5 line-clamp-2 text-xs leading-snug text-faint">
                <Highlight text={hit.snippet} query={q} />
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function DocsLayout() {
  return (
    <div className="flex min-h-full">
      <aside className="sticky top-0 hidden h-[calc(100vh-3rem)] w-64 shrink-0 flex-col border-r border-border bg-panel/50 md:flex">
        <div className="border-b border-border p-3">
          <Link to="/docs" className="mb-3 flex items-center gap-2 px-1 text-[13px] font-semibold">
            <BookOpen className="size-4 text-accent" /> Documentation
          </Link>
          <DocsSearch />
        </div>
        <nav className="flex-1 overflow-y-auto px-2 py-3">
          {DOC_SECTIONS.map((section) => (
            <div key={section.key} className="mb-4">
              <div className="px-2 pb-1 text-[10.5px] font-semibold tracking-wider text-faint uppercase">{section.title}</div>
              {section.pages.map((page) => (
                <NavLink
                  key={page.slug}
                  to={`/docs/${page.slug}`}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] transition-colors",
                      isActive ? "bg-accent-soft font-medium text-accent" : "text-muted hover:bg-subtle hover:text-fg",
                    )
                  }
                >
                  <span className="min-w-0 flex-1 truncate">{page.title}</span>
                  {page.kind === "generated" && <FileCode2 className="size-3 shrink-0 opacity-60" aria-label="Generated" />}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
      </aside>
      <div className="min-w-0 flex-1">
        <Outlet />
      </div>
    </div>
  );
}
