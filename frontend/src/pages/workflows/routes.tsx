import { lazy } from "react";
import type { RouteObject } from "react-router";
import { Lazy } from "@/App";

const Overview = lazy(() => import("@/pages/Overview"));
const WorkflowList = lazy(() => import("./WorkflowList"));
const Builder = lazy(() => import("./Builder"));
const RunsList = lazy(() => import("@/pages/runs/RunsList"));
const RunViewer = lazy(() => import("@/pages/runs/RunViewer"));

export const workflowRoutes: RouteObject[] = [
  { path: "overview", element: <Lazy><Overview /></Lazy> },
  { path: "workflows", element: <Lazy><WorkflowList /></Lazy> },
  { path: "workflows/new", element: <Lazy><Builder key="new" /></Lazy> },
  { path: "workflows/:name", element: <Lazy><Builder /></Lazy> },
  { path: "runs", element: <Lazy><RunsList /></Lazy> },
  { path: "runs/:runId", element: <Lazy><RunViewer /></Lazy> },
];
