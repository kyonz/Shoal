import { lazy } from "react";
import type { RouteObject } from "react-router";
import { Lazy } from "@/App";
import { InstanceLayout } from "./InstanceLayout";

const Tenants = lazy(() => import("./Tenants"));
const TenantDetail = lazy(() => import("./TenantDetail"));
const Accounts = lazy(() => import("./Accounts"));
const InstanceAudit = lazy(() => import("./InstanceAudit"));
const Orchestrator = lazy(() => import("./Orchestrator"));

/** Instance console: /instance/* */
export const instanceRoutes: RouteObject[] = [
  {
    path: "instance",
    element: <InstanceLayout />,
    children: [
      { index: true, element: <Lazy><Tenants /></Lazy> },
      { path: "tenants/:slug", element: <Lazy><TenantDetail /></Lazy> },
      { path: "accounts", element: <Lazy><Accounts /></Lazy> },
      { path: "orchestrator", element: <Lazy><Orchestrator /></Lazy> },
      { path: "audit", element: <Lazy><InstanceAudit /></Lazy> },
    ],
  },
];
