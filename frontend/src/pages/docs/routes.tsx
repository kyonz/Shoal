import { lazy } from "react";
import type { RouteObject } from "react-router";
import { Lazy } from "@/App";

const DocsLayout = lazy(() => import("./DocsLayout"));
const DocsHome = lazy(() => import("./DocsHome"));
const DocPage = lazy(() => import("./DocPage"));

/** In-app documentation: /docs/* */
export const docsRoutes: RouteObject[] = [
  {
    path: "docs",
    element: (
      <Lazy>
        <DocsLayout />
      </Lazy>
    ),
    children: [
      { index: true, element: <Lazy><DocsHome /></Lazy> },
      { path: "*", element: <Lazy><DocPage /></Lazy> },
    ],
  },
];
