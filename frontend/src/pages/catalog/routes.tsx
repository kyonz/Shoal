import { lazy } from "react";
import { Navigate, useParams, type RouteObject } from "react-router";
import { Lazy } from "@/App";
import type { CatalogPlural } from "./pages";

const List = lazy(() => import("./pages").then((m) => ({ default: m.List })));
const Editor = lazy(() => import("./pages").then((m) => ({ default: m.Editor })));
const EvalRunPage = lazy(() => import("./evals/EvalRunPage"));
const BlueprintsPage = lazy(() => import("./blueprints/BlueprintsPage"));
const BlueprintDetailPage = lazy(() => import("./blueprints/BlueprintDetailPage"));
const SkillMarketplacePage = lazy(() => import("./skillSources/SkillMarketplacePage"));

/** Remount the editor when the resource name changes. */
export function KeyedEditor({ plural }: { plural: CatalogPlural }) {
  const { name, ns } = useParams();
  return <Editor key={`${ns}/${plural}/${name ?? "_new"}`} plural={plural} />;
}

/** Old namespaced links to kinds that moved to the admin console. */
function ToAdmin({ plural }: { plural: string }) {
  const { name } = useParams();
  return <Navigate to={`/admin/${plural}${name ? `/${encodeURIComponent(name)}` : ""}`} replace />;
}

const generic: CatalogPlural[] = ["agents", "skills", "sandbox-profiles", "cloud-identities", "evals"];
const admin: CatalogPlural[] = ["gateways", "model-policies", "skill-sources", "custom-integrations"];

/** Tenant-wide kinds edited in the admin console: /admin/gateways, /admin/model-policies, /admin/skill-sources, /admin/custom-integrations. */
export const adminCatalogRoutes: RouteObject[] = admin.flatMap((plural): RouteObject[] => [
  { path: plural, element: <Lazy><List plural={plural} /></Lazy> },
  { path: `${plural}/new`, element: <Lazy><KeyedEditor plural={plural} /></Lazy> },
  { path: `${plural}/:name`, element: <Lazy><KeyedEditor plural={plural} /></Lazy> },
]);

/** Namespaced: agents, skills, sandbox-profiles, cloud-identities, evals, blueprints, skill-marketplace. */
export const catalogRoutes: RouteObject[] = [
  ...admin.flatMap((plural): RouteObject[] => [
    { path: plural, element: <ToAdmin plural={plural} /> },
    { path: `${plural}/:name`, element: <ToAdmin plural={plural} /> },
  ]),
  ...generic.flatMap((plural): RouteObject[] => [
    { path: plural, element: <Lazy><List plural={plural} /></Lazy> },
    { path: `${plural}/new`, element: <Lazy><KeyedEditor plural={plural} /></Lazy> },
    { path: `${plural}/:name`, element: <Lazy><KeyedEditor plural={plural} /></Lazy> },
  ]),
  { path: "skill-marketplace", element: <Lazy><SkillMarketplacePage /></Lazy> },
  { path: "eval-runs/:id", element: <Lazy><EvalRunPage /></Lazy> },
  { path: "blueprints", element: <Lazy><BlueprintsPage /></Lazy> },
  { path: "blueprints/new", element: <Lazy><KeyedEditor plural="blueprints" /></Lazy> },
  { path: "blueprints/:name", element: <Lazy><BlueprintDetailPage /></Lazy> },
  { path: "blueprints/:name/edit", element: <Lazy><KeyedEditor plural="blueprints" /></Lazy> },
];
