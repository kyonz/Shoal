import { Children, isValidElement, memo, useMemo, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import { Link } from "react-router";
import rehypeSlug from "rehype-slug";
import remarkGfm from "remark-gfm";
import { CopyButton } from "@/components/ui";
import { resolveDocHref } from "./registry";

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const child = Children.toArray(children)[0];
  const className = isValidElement<{ className?: string }>(child) ? child.props.className ?? "" : "";
  const lang = className.match(/language-(\S+)/)?.[1];
  const code = textOf(children).replace(/\n$/, "");
  return (
    <div className="group relative">
      {lang && (
        <span className="pointer-events-none absolute top-2 right-11 font-mono text-[10px] tracking-wide text-faint uppercase opacity-0 transition-opacity group-hover:opacity-100">
          {lang}
        </span>
      )}
      <div className="absolute top-1.5 right-1.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
        <CopyButton value={code} />
      </div>
      <pre>{children}</pre>
    </div>
  );
}

// Raw HTML is not rendered, so `<kbd>X</kbd>` is rewritten to inline code with a marker.
const KBD = "\u2063kbd:";

function withKbd(md: string): string {
  return md.replace(/<kbd>([^<`]+)<\/kbd>/g, (_m, k: string) => `\`${KBD}${k}\``);
}

/** Renders Markdown with the docs typography; `slug` resolves relative links. */
export const Markdown = memo(function Markdown({ source, slug, className = "prose-shoal" }: { source: string; slug: string; className?: string }) {
  // Stable component identities: new ones on every render would remount the whole article.
  const components = useMemo<Components>(() => ({
    pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
    a: ({ href = "", children }) => {
      const to = resolveDocHref(href, slug);
      if (to.startsWith("/") && !to.startsWith("//") && !to.startsWith("/v1/")) return <Link to={to}>{children}</Link>;
      const external = /^https?:/.test(to);
      return (
        <a href={to} target={external ? "_blank" : undefined} rel={external ? "noreferrer" : undefined}>
          {children}
        </a>
      );
    },
    code: ({ children, className, node: _node, ...rest }) => {
      const text = textOf(children);
      if (!className && text.startsWith(KBD)) {
        return (
          <kbd className="rounded border border-border bg-subtle px-1.5 py-px font-sans text-[11px] font-medium whitespace-nowrap text-muted shadow-card">
            {text.slice(KBD.length)}
          </kbd>
        );
      }
      return (
        <code className={className} {...rest}>
          {children}
        </code>
      );
    },
  }), [slug]);
  return (
    <div className={`${className} [&_hr+h2]:mt-0 [&_hr+h2]:border-t-0 [&_hr+h2]:pt-0`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSlug]} components={components}>
        {withKbd(source)}
      </ReactMarkdown>
    </div>
  );
});
