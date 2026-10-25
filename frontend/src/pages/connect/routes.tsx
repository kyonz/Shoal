import { lazy, type ReactNode } from "react";
import { Navigate, useLocation, useParams, type RouteObject } from "react-router";
import { Lazy } from "@/App";
import { OrgScope, SHARED_NS } from "@/lib/ns";

const Integrations = lazy(() => import("./Integrations"));
const IntegrationDetail = lazy(() => import("./IntegrationDetail"));
const Connections = lazy(() => import("./Connections"));
const ConnectionEditor = lazy(() => import("./ConnectionEditor"));
const Secrets = lazy(() => import("./Secrets"));
const Triggers = lazy(() => import("./Triggers"));
const TriggerEditor = lazy(() => import("./TriggerEditor"));
const Campaigns = lazy(() => import("./Campaigns"));
const CampaignEditor = lazy(() => import("./CampaignEditor"));
const CampaignRun = lazy(() => import("./CampaignRun"));
const SlackBuilder = lazy(() => import("./SlackBuilder"));
const AppSetup = lazy(() => import("./AppSetup"));

/** The shared namespace's connections and secrets are organisation configuration: they live in the admin console. */
function NamespaceOnly({ children }: { children: ReactNode }) {
  const { ns } = useParams();
  const { pathname, search } = useLocation();
  if (ns === SHARED_NS) return <Navigate to={pathname.replace(/^\/ns\/shared\//, "/admin/") + search} replace />;
  return <Lazy>{children}</Lazy>;
}

/** Namespaced: integrations, connections, secrets, triggers, campaigns, campaign-runs. */
export const connectRoutes: RouteObject[] = [
  { path: "integrations", element: <Lazy><Integrations /></Lazy> },
  { path: "integrations/:key", element: <Lazy><IntegrationDetail /></Lazy> },
  { path: "connections", element: <NamespaceOnly><Connections /></NamespaceOnly> },
  { path: "connections/new", element: <NamespaceOnly><ConnectionEditor key="new" /></NamespaceOnly> },
  { path: "connections/:name", element: <NamespaceOnly><ConnectionEditor /></NamespaceOnly> },
  { path: "connections/:name/setup", element: <NamespaceOnly><AppSetup /></NamespaceOnly> },
  { path: "secrets", element: <NamespaceOnly><Secrets /></NamespaceOnly> },
  { path: "slack/builder", element: <Lazy><SlackBuilder /></Lazy> },
  { path: "triggers", element: <Lazy><Triggers /></Lazy> },
  { path: "triggers/new", element: <Lazy><TriggerEditor key="new" /></Lazy> },
  { path: "triggers/:name", element: <Lazy><TriggerEditor /></Lazy> },
  { path: "campaigns", element: <Lazy><Campaigns /></Lazy> },
  { path: "campaigns/new", element: <Lazy><CampaignEditor key="new" /></Lazy> },
  { path: "campaigns/:name", element: <Lazy><CampaignEditor /></Lazy> },
  { path: "campaign-runs/:id", element: <Lazy><CampaignRun /></Lazy> },
];

/** Admin console: organisation connections and the shared secrets they use (/admin/connections, /admin/secrets). */
export const orgConnectRoutes: RouteObject[] = [
  { path: "connections", element: <Lazy><OrgScope><Connections /></OrgScope></Lazy> },
  { path: "connections/new", element: <Lazy><OrgScope><ConnectionEditor key="new" /></OrgScope></Lazy> },
  { path: "connections/:name", element: <Lazy><OrgScope><ConnectionEditor /></OrgScope></Lazy> },
  { path: "secrets", element: <Lazy><OrgScope><Secrets /></OrgScope></Lazy> },
];
