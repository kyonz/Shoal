/**
 * Links into the in-app documentation, for "Learn more" links on other
 * screens: `docsLink("guides/workflow-builder", "expressions")`.
 */
export function docsLink(slug: string, anchor?: string): string {
  const clean = slug.replace(/^\/+|\/+$/g, "").replace(/\.md$/, "");
  return `/docs/${clean}${anchor ? `#${anchor}` : ""}`;
}

/** Well-known pages, so callers don't hard-code slugs. */
export const DOCS = {
  gettingStarted: "getting-started",
  builder: "guides/workflow-builder",
  liveRuns: "guides/live-runs",
  integrations: "integrations/overview",
  resources: "reference/resources",
  steps: "reference/steps",
  api: "reference/api",
  architecture: "architecture",
} as const;
