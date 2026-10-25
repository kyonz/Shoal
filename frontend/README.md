# Shoal web UI

The browser app for Shoal: workflow builder, live run viewer, integrations, campaigns, catalog, admin console and in-app docs. It is a Vite + React + TypeScript single-page app that talks only to the public `/v1` API.

## Develop

```sh
npm install
npm run dev          # http://localhost:5173
```

The dev server proxies `/v1` and `/scim` to the engine at `SHOAL_API_URL` (default `http://localhost:8080`). Start the engine with `SHOAL_PUBLIC_URL=http://localhost:5173` so OIDC callbacks and the session cookie land on the dev origin.

Sign in with your identity provider, or choose **Use an API key instead** and paste a tenant API key (or the system admin token plus a tenant slug).

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Dev server with hot reload |
| `npm run build` | Typecheck and build to `dist/` |
| `npm run typecheck` | `tsc -b` only |
| `npm test` | Unit tests (Vitest) |
| `npm run gen:api` | Regenerate `src/api/schema.d.ts` from a running engine's `/v1/openapi.json` |

Regenerate the API types whenever the engine's API changes; type errors then point at every screen that needs updating.

## Layout

- `src/api` — typed client (openapi-fetch), SSE stream helper, TanStack Query hooks
- `src/components` — UI primitives, overlays, code editor, schema-driven forms, app shell
- `src/features/workflow` — workflow graph model, validation, expressions, canvas nodes
- `src/pages` — one folder per area: `workflows`, `runs`, `connect`, `catalog`, `admin`, `docs`
- `src/pages/docs` — renders `../docs/**/*.md` plus UI guides and reference pages generated from the live API

Builder canvas positions are stored on the workflow in the `ui.shoal.dev/layout` annotation, so YAML applied from the CLI keeps its layout.
