import { lazy } from "react";
import type { RouteObject } from "react-router";
import { Lazy } from "@/App";
import { adminCatalogRoutes } from "@/pages/catalog/routes";
import { orgConnectRoutes } from "@/pages/connect/routes";
import { AdminLayout } from "./AdminLayout";

const Overview = lazy(() => import("./Overview"));
const Users = lazy(() => import("./Users"));
const Groups = lazy(() => import("./Groups"));
const ServiceAccounts = lazy(() => import("./ServiceAccounts"));
const Namespaces = lazy(() => import("./Namespaces"));
const NamespaceDetail = lazy(() => import("./NamespaceDetail"));
const Roles = lazy(() => import("./Roles"));
const AuthSettings = lazy(() => import("./AuthSettings"));
const ApiKeys = lazy(() => import("./ApiKeys"));
const Identities = lazy(() => import("./Identities"));
const Audit = lazy(() => import("./Audit"));
const Federation = lazy(() => import("./Federation"));
const AssistantSettings = lazy(() => import("./AssistantSettings"));

/** Admin console: /admin/* */
export const adminRoutes: RouteObject[] = [
  {
    path: "admin",
    element: <AdminLayout />,
    children: [
      {
        index: true,
        element: (
          <Lazy>
            <Overview />
          </Lazy>
        ),
      },
      {
        path: "assistant",
        element: (
          <Lazy>
            <AssistantSettings />
          </Lazy>
        ),
      },
      {
        path: "users",
        element: (
          <Lazy>
            <Users />
          </Lazy>
        ),
      },
      {
        path: "groups",
        element: (
          <Lazy>
            <Groups />
          </Lazy>
        ),
      },
      {
        path: "service-accounts",
        element: (
          <Lazy>
            <ServiceAccounts />
          </Lazy>
        ),
      },
      {
        path: "namespaces",
        element: (
          <Lazy>
            <Namespaces />
          </Lazy>
        ),
      },
      {
        path: "namespaces/:name",
        element: (
          <Lazy>
            <NamespaceDetail />
          </Lazy>
        ),
      },
      {
        path: "roles",
        element: (
          <Lazy>
            <Roles />
          </Lazy>
        ),
      },
      {
        path: "auth",
        element: (
          <Lazy>
            <AuthSettings />
          </Lazy>
        ),
      },
      {
        path: "api-keys",
        element: (
          <Lazy>
            <ApiKeys />
          </Lazy>
        ),
      },
      {
        path: "identities",
        element: (
          <Lazy>
            <Identities />
          </Lazy>
        ),
      },
      {
        path: "audit",
        element: (
          <Lazy>
            <Audit />
          </Lazy>
        ),
      },
      {
        path: "federation",
        element: (
          <Lazy>
            <Federation />
          </Lazy>
        ),
      },
      ...adminCatalogRoutes,
      ...orgConnectRoutes,
    ],
  },
];
