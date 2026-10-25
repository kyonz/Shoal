import { lazy } from "react";
import type { RouteObject } from "react-router";
import { Lazy } from "@/App";

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

/** Namespaced: integrations, connections, secrets, triggers, campaigns, campaign-runs. */
export const connectRoutes: RouteObject[] = [
  { path: "integrations", element: <Lazy><Integrations /></Lazy> },
  { path: "integrations/:key", element: <Lazy><IntegrationDetail /></Lazy> },
  { path: "connections", element: <Lazy><Connections /></Lazy> },
  { path: "connections/new", element: <Lazy><ConnectionEditor key="new" /></Lazy> },
  { path: "connections/:name", element: <Lazy><ConnectionEditor /></Lazy> },
  { path: "secrets", element: <Lazy><Secrets /></Lazy> },
  { path: "triggers", element: <Lazy><Triggers /></Lazy> },
  { path: "triggers/new", element: <Lazy><TriggerEditor key="new" /></Lazy> },
  { path: "triggers/:name", element: <Lazy><TriggerEditor /></Lazy> },
  { path: "campaigns", element: <Lazy><Campaigns /></Lazy> },
  { path: "campaigns/new", element: <Lazy><CampaignEditor key="new" /></Lazy> },
  { path: "campaigns/:name", element: <Lazy><CampaignEditor /></Lazy> },
  { path: "campaign-runs/:id", element: <Lazy><CampaignRun /></Lazy> },
];
