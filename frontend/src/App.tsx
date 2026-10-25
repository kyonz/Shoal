import { lazy, Suspense, type ReactNode } from "react";
import { createBrowserRouter, Navigate, Outlet, RouterProvider, useLocation, useRouteError, type RouteObject } from "react-router";
import { useAuth } from "@/lib/auth";
import { lastNamespace } from "@/lib/ns";
import { AppShell } from "@/components/layout/AppShell";
import { Button, EmptyState, Spinner } from "@/components/ui";
import Login from "@/pages/Login";
import { adminRoutes } from "@/pages/admin/routes";
import { catalogRoutes } from "@/pages/catalog/routes";
import { connectRoutes } from "@/pages/connect/routes";
import { docsRoutes } from "@/pages/docs/routes";
import { instanceRoutes } from "@/pages/instance/routes";
import { workflowRoutes } from "@/pages/workflows/routes";

const CredentialsPage = lazy(() => import("@/pages/settings/Credentials"));

export function Splash() {
  return (
    <div className="flex h-full items-center justify-center">
      <Spinner className="size-5" />
    </div>
  );
}

/** Wrap lazily-loaded pages so each area is its own chunk. */
export function Lazy({ children }: { children: ReactNode }) {
  return <Suspense fallback={<Splash />}>{children}</Suspense>;
}

function RequireAuth() {
  const { signedIn, loading } = useAuth();
  const location = useLocation();
  if (loading) return <Splash />;
  if (!signedIn) return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  return <Outlet />;
}

/** `/`: the last namespace used, if this tenant has it; the instance console when there's no tenant. */
function Home() {
  const { me, instance } = useAuth();
  if (!me?.tenant && instance.reader) return <Navigate to="/instance" replace />;
  const visible = me?.namespaces ?? [];
  const last = lastNamespace();
  const ns = visible.length && !visible.includes(last) && !me?.is_system_admin ? (visible.includes("default") ? "default" : visible[0]) : last;
  return <Navigate to={`/ns/${encodeURIComponent(ns)}/overview`} replace />;
}

function RouteError() {
  const error = useRouteError() as Error | undefined;
  return (
    <EmptyState
      className="h-full"
      title="Something went wrong"
      description={error?.message ?? "This page failed to load."}
      action={<Button onClick={() => window.location.reload()}>Reload</Button>}
    />
  );
}

function NotFound() {
  return <EmptyState className="h-full" title="Page not found" description="The page you were looking for does not exist." />;
}

const routes: RouteObject[] = [
  { path: "/login", element: <Login /> },
  {
    element: <RequireAuth />,
    errorElement: <RouteError />,
    children: [
      {
        element: <AppShell />,
        errorElement: <RouteError />,
        children: [
          { index: true, element: <Home /> },
          {
            path: "ns/:ns",
            children: [
              { index: true, element: <Navigate to="overview" replace /> },
              ...workflowRoutes,
              ...connectRoutes,
              ...catalogRoutes,
            ],
          },
          ...adminRoutes,
          ...instanceRoutes,
          ...docsRoutes,
          { path: "settings/credentials", element: <Lazy><CredentialsPage /></Lazy> },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
  },
];

const router = createBrowserRouter(routes);

export default function App() {
  return <RouterProvider router={router} />;
}
