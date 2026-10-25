/**
 * The documentation set: Markdown from the repo's docs/ folder (bundled at
 * build time, so it always matches the running version), the UI guides in
 * ./content, and pages generated from the live API schemas.
 */

export type DocKind = "markdown" | "generated";

export interface DocPage {
  slug: string;
  title: string;
  section: string;
  kind: DocKind;
  /** Markdown source (empty for generated pages). */
  body: string;
  /** Short description for cards and search results. */
  summary: string;
  /** Sort weight within the section. */
  order: number;
}

export interface DocSection {
  key: string;
  title: string;
  description: string;
  pages: DocPage[];
}

const SECTIONS: { key: string; title: string; description: string }[] = [
  { key: "getting-started", title: "Getting started", description: "Sign in, pick a namespace and run your first workflow." },
  { key: "concepts", title: "Concepts", description: "Tenants, namespaces, agents, workflows, runs and more." },
  { key: "guides", title: "Guides", description: "The visual builder, live runs, approvals and the journeys." },
  { key: "administration", title: "Administration", description: "OIDC, SCIM, roles, namespaces, API keys and audit." },
  { key: "integrations", title: "Building integrations", description: "The SDK, the test kit and the remote protocol." },
  { key: "reference", title: "Reference", description: "Resource schemas, step types and the HTTP API." },
  { key: "internals", title: "Internals", description: "The architecture: executor, leases, waits, sandboxes, plugins." },
];

const repoDocs = import.meta.glob("../../../../docs/**/*.md", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const uiDocs = import.meta.glob("./content/*.md", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

/** UI guides: file name -> placement. */
const UI_PLACEMENT: Record<string, { slug: string; section: string; order: number }> = {
  "getting-started": { slug: "getting-started", section: "getting-started", order: 0 },
  "workflow-builder": { slug: "guides/workflow-builder", section: "guides", order: 0 },
  "live-runs": { slug: "guides/live-runs", section: "guides", order: 1 },
  "building-integrations": { slug: "integrations/overview", section: "integrations", order: 0 },
};

export function titleOf(md: string, fallback: string): string {
  const m = md.match(/^#\s+(.+)$/m);
  return m ? m[1].trim() : fallback;
}

/** First paragraph of prose after the title, without Markdown syntax. */
export function summaryOf(md: string): string {
  const lines = md.replace(/^#\s+.+$/m, "").split(/\r?\n/);
  const para: string[] = [];
  for (const line of lines) {
    const t = line.trim();
    if (!t) {
      if (para.length) break;
      continue;
    }
    if (/^(#|```|\||>|-{3,}|[-*] |\d+\. )/.test(t) || /^[A-Z][a-z]+:\s\*\*/.test(t)) {
      if (para.length) break;
      continue;
    }
    para.push(t);
  }
  return plain(para.join(" ")).slice(0, 220);
}

export function plain(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/<[^>]+>/g, "")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[`*_>#|]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function humanize(s: string) {
  const t = s.replace(/[-_]/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}

function buildPages(): DocPage[] {
  const pages: DocPage[] = [];
  for (const [path, body] of Object.entries(repoDocs)) {
    const rel = path.replace(/^.*?\/docs\//, "").replace(/\.md$/, "");
    const parts = rel.split("/");
    const folder = parts.length > 1 ? parts[0] : "";
    // Known folders map to their section; new folders get a section of their own; root files are internals.
    const section = folder || "internals";
    if (!SECTIONS.some((s) => s.key === section)) {
      SECTIONS.splice(SECTIONS.length - 1, 0, { key: section, title: humanize(section), description: "" });
    }
    const base = parts[parts.length - 1];
    pages.push({
      slug: rel,
      title: titleOf(body, humanize(base)),
      section,
      kind: "markdown",
      body,
      summary: summaryOf(body),
      order: base === "index" || base === "README" ? -1 : 10,
    });
  }
  for (const [path, body] of Object.entries(uiDocs)) {
    const name = path.replace(/^.*\//, "").replace(/\.md$/, "");
    const place = UI_PLACEMENT[name] ?? { slug: `guides/${name}`, section: "guides", order: 50 };
    if (pages.some((p) => p.slug === place.slug)) continue; // repo docs win
    pages.push({ ...place, title: titleOf(body, humanize(name)), kind: "markdown", body, summary: summaryOf(body) });
  }
  pages.push(
    {
      slug: "reference/resources",
      title: "Resource schemas",
      section: "reference",
      kind: "generated",
      body: "",
      summary: "Every resource kind and its spec fields, generated from the schemas the API validates against.",
      order: 0,
    },
    {
      slug: "reference/steps",
      title: "Step types",
      section: "reference",
      kind: "generated",
      body: "",
      summary: "Fields of every workflow step type, generated from the server's step schema.",
      order: 1,
    },
    {
      slug: "reference/api",
      title: "HTTP API",
      section: "reference",
      kind: "generated",
      body: "",
      summary: "Every /v1 operation grouped by tag, rendered from the live OpenAPI document.",
      order: 2,
    },
  );
  return pages;
}

export const PAGES: DocPage[] = buildPages();

export const DOC_SECTIONS: DocSection[] = SECTIONS.map((s) => ({
  ...s,
  pages: PAGES.filter((p) => p.section === s.key).sort((a, b) => a.order - b.order || a.title.localeCompare(b.title)),
})).filter((s) => s.pages.length > 0);

/** Pages in reading order (for previous/next). */
export const ORDERED: DocPage[] = DOC_SECTIONS.flatMap((s) => s.pages);

export function findPage(slug: string): DocPage | undefined {
  const clean = slug.replace(/^\/+|\/+$/g, "").replace(/\.md$/, "");
  return PAGES.find((p) => p.slug === clean);
}

/**
 * Resolve a Markdown link found on page `from` to an app route. Relative
 * `.md` links (`../guides/x.md#y`) become `/docs/guides/x#y`; external and
 * absolute links are returned unchanged.
 */
export function resolveDocHref(href: string, from: string): string {
  if (/^[a-z]+:/i.test(href) || href.startsWith("#") || href.startsWith("/")) return href;
  const [pathPart, hash] = href.split("#");
  const dir = from.split("/").slice(0, -1);
  for (const seg of pathPart.split("/")) {
    if (seg === "..") dir.pop();
    else if (seg && seg !== ".") dir.push(seg);
  }
  const slug = dir.join("/").replace(/\.md$/, "");
  return `/docs/${slug}${hash ? `#${hash}` : ""}`;
}

// --------------------------------------------------------------------------- search

export interface Heading {
  depth: number;
  text: string;
  id: string;
}

/** github-slugger compatible ids (what rehype-slug produces). */
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .trim()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s/g, "-");
}

export function headingsOf(md: string): Heading[] {
  const out: Heading[] = [];
  const seen = new Map<string, number>();
  let fence = false;
  for (const line of md.split(/\r?\n/)) {
    if (/^\s*```/.test(line)) fence = !fence;
    if (fence) continue;
    const m = line.match(/^(#{1,6})\s+(.+?)\s*#*$/);
    if (!m) continue;
    const text = plain(m[2]);
    let id = slugify(text);
    const n = seen.get(id) ?? 0;
    seen.set(id, n + 1);
    if (n) id = `${id}-${n}`;
    out.push({ depth: m[1].length, text, id });
  }
  return out;
}

interface IndexEntry {
  page: DocPage;
  title: string;
  headings: Heading[];
  text: string;
}

const INDEX: IndexEntry[] = PAGES.map((page) => ({
  page,
  title: page.title.toLowerCase(),
  headings: headingsOf(page.body),
  text: plain(page.body || page.summary),
}));

export interface SearchHit {
  page: DocPage;
  score: number;
  /** Heading closest to the match, for deep links. */
  heading?: Heading;
  snippet: string;
}

export function search(query: string, limit = 12): SearchHit[] {
  const terms = query.toLowerCase().split(/\s+/).filter((t) => t.length > 1);
  if (!terms.length) return [];
  const hits: SearchHit[] = [];
  for (const e of INDEX) {
    const lower = e.text.toLowerCase();
    let score = 0;
    let ok = true;
    for (const t of terms) {
      const inTitle = e.title.includes(t);
      const inHead = e.headings.some((h) => h.text.toLowerCase().includes(t));
      const count = lower.split(t).length - 1;
      if (!inTitle && !inHead && !count) {
        ok = false;
        break;
      }
      score += (inTitle ? 20 : 0) + (inHead ? 6 : 0) + Math.min(count, 10);
    }
    if (!ok) continue;
    const heading = e.headings.find((h) => h.depth > 1 && terms.some((t) => h.text.toLowerCase().includes(t)));
    const at = Math.max(0, lower.indexOf(terms[0]));
    const start = Math.max(0, at - 60);
    const snippet = (start > 0 ? "…" : "") + e.text.slice(start, start + 180).trim() + (start + 180 < e.text.length ? "…" : "");
    hits.push({ page: e.page, score, heading, snippet });
  }
  return hits.sort((a, b) => b.score - a.score).slice(0, limit);
}
