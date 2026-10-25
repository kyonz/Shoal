import { lazy } from "react";
import type { RouteObject } from "react-router";
import { Lazy } from "@/App";

const Overview = lazy(() => import("@/pages/Overview"));

export const workflowRoutes: RouteObject[] = [
  { path: "overview", element: <Lazy><Overview /></Lazy> },
];
