# Deploying Shoal

One image runs everything: `shoal serve` (API, optionally with an embedded worker) and
`shoal worker`. Workers and API servers are stateless; all state is in Postgres.

## Container image

```sh
docker build -f deploy/Dockerfile -t shoal .     # from the repository root
docker run -p 8080:8080 -e SHOAL_ADMIN_TOKEN=change-me shoal
```

The image builds the UI from `frontend/` when it exists and serves it from `/`. It runs as
UID 10001 with SQLite under `/var/lib/shoal` by default; set `SHOAL_DATABASE_URL` for Postgres.
No image is published yet: build it yourself as above, and push it to a registry when your cluster
needs to pull it. Once releases are tagged, the manual release workflow publishes
`ghcr.io/kyonz/shoal` (amd64 and arm64), which is the chart's default image.

## Sandbox image

The `docker` and `kubernetes` sandbox backends start `ghcr.io/kyonz/shoal-sandbox:latest` unless a
sandbox profile names another `image`. It has bash, Python, Node, git and curl, and runs as a
non-root user in `/workspace`. Until a release publishes it, build it where your workers can use it:

```sh
docker build -f deploy/sandbox.Dockerfile -t ghcr.io/kyonz/shoal-sandbox:latest deploy/
```

For Kubernetes, push it to your registry and set `image` in your sandbox profiles to that name.
Any image with `sh` and the tools your steps need works too.

## docker compose (Postgres, API, workers)

```sh
cp deploy/.env.example deploy/.env             # set real secrets
docker compose -f deploy/docker-compose.yaml --env-file deploy/.env up -d --build
shoal --server http://localhost:8080 --token "$SHOAL_ADMIN_TOKEN" tenants create acme
docker compose -f deploy/docker-compose.yaml up -d --scale worker=3
```

Compose refuses to start until `SHOAL_SECRET_KEY`, `SHOAL_ADMIN_TOKEN` and `POSTGRES_PASSWORD`
are set, so it never runs with well-known defaults.

To let the `docker` sandbox backend start containers, uncomment the Docker socket mount on the
worker (this gives the worker root-equivalent access to the host) and build the
[sandbox image](#sandbox-image) on the host.

To drive runs with a Temporal cluster you already run, set `SHOAL_ORCHESTRATOR=temporal` and
`SHOAL_TEMPORAL_ADDRESS` in `deploy/.env`. Adding `-f deploy/docker-compose.temporal.yaml` starts a
throwaway Temporal dev server to try it with. See [Running Shoal on Temporal](../docs/temporal.md).

## Kubernetes (Helm)

```sh
docker build -f deploy/Dockerfile -t registry.example.com/shoal:0.1.0 .   # until a release is published
docker push registry.example.com/shoal:0.1.0

helm install shoal deploy/helm/shoal -n shoal --create-namespace \
  --set image.repository=registry.example.com/shoal --set image.tag=0.1.0 \
  --set config.publicUrl=https://shoal.example.com \
  --set ingress.enabled=true --set ingress.hosts[0].host=shoal.example.com \
  --set-json 'ingress.hosts[0].paths=[{"path":"/","pathType":"Prefix"}]'
```

What the chart deploys:

- an API Deployment (2 replicas, PDB, optional HPA) behind a Service and optional Ingress;
- a worker Deployment (2 replicas, PDB, optional HPA) that waits for the API to create the schema;
- a Secret with `SHOAL_SECRET_KEY`, `SHOAL_ADMIN_TOKEN` and `SHOAL_DATABASE_URL`, generated once and kept
  across upgrades (or bring your own with `secrets.existingSecret`);
- a single-instance Postgres StatefulSet for evaluation (`postgresql.enabled`, on by default). For
  production set `postgresql.enabled=false` and `database.url` to a managed database;
- with `kubernetesSandboxes.enabled`, namespaces plus a Role and RoleBinding so workers can run
  sandboxes as pods (or a ClusterRole with `clusterWide=true` for per-tenant namespaces);
- an optional Prometheus `ServiceMonitor` for `/metrics`.

Runs are driven by the database unless you set `orchestrator.type=temporal` and
`orchestrator.temporal.address` to use a Temporal cluster you already run (with an API key Secret or
an mTLS Secret if it needs one). See [Running Shoal on Temporal](../docs/temporal.md).

Pods run as non-root with a read-only root filesystem; `/tmp` and the local sandbox directory
are `emptyDir` volumes. Model and sandbox credentials go in `extraEnv` / `extraEnvFrom`, and
`serviceAccount.annotations` takes workload identity bindings. After installing, `helm test shoal`
checks `/readyz`, and the release notes print how to fetch the admin token and create a tenant.
