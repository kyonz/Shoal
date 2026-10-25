#!/usr/bin/env bash
# Build and test everything locally (the same checks as .github/workflows/ci.yml).
#
#   scripts/check.sh                 # all sections
#   scripts/check.sh engine helm     # only some: engine frontend terraform helm image
#
# Needs uv; go, helm, kubeconform, node, docker and the temporal CLI are used when
# installed and their sections are skipped (with a warning) when not.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
sections=("$@")
[ ${#sections[@]} -eq 0 ] && sections=(engine frontend terraform helm image)
failed=()
skipped=()

want() { [[ " ${sections[*]} " == *" $1 "* ]]; }
have() { command -v "$1" >/dev/null 2>&1; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
run() {  # run NAME CMD...: run a section with errexit (outside any `if`, where bash would ignore it), record failures and keep going
  local name="$1" rc; shift
  set +e
  (set -e; "$@")
  rc=$?
  set -e
  [ "$rc" -eq 0 ] || failed+=("$name")
}

engine() {
  cd "$root/engine"
  uv sync --locked --all-extras --quiet
  uv run ruff check .
  SHOAL_TEST_DATABASE_URL= uv run pytest -q  # SQLite
  if [ -n "${SHOAL_TEST_DATABASE_URL:-}" ]; then  # e.g. postgresql+asyncpg://postgres@127.0.0.1:5432/shoal_test
    echo "again against Postgres"
    uv run pytest -q
  fi
  local temporal_address="${SHOAL_TEST_TEMPORAL_ADDRESS:-}" temporal_pid=""
  if [ -z "$temporal_address" ] && have temporal; then  # a throwaway dev server
    local port="${SHOAL_CHECK_TEMPORAL_PORT:-17233}"
    temporal server start-dev --headless --port "$port" --log-level error >/dev/null 2>&1 &
    temporal_pid=$!
    temporal_address="localhost:$port"
    for _ in $(seq 1 30); do temporal operator cluster health --address "$temporal_address" >/dev/null 2>&1 && break; sleep 1; done
  fi
  if [ -n "$temporal_address" ]; then
    echo "again with runs driven by Temporal ($temporal_address)"
    SHOAL_TEST_TEMPORAL_ADDRESS="$temporal_address" uv run pytest -q || { [ -z "$temporal_pid" ] || kill "$temporal_pid"; return 1; }
    [ -z "$temporal_pid" ] || kill "$temporal_pid"
  else
    echo "temporal CLI not installed: skipping the Temporal run of the suite"
  fi
  uv run shoal --help >/dev/null
}

frontend() {
  cd "$root/frontend"
  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
  npm run lint --if-present
  npm run typecheck --if-present
  npm test --if-present
  npm run build
}

terraform_provider() {
  cd "$root/terraform-provider"
  local unformatted; unformatted="$(gofmt -l .)"
  [ -z "$unformatted" ] || { echo "gofmt needed:"; echo "$unformatted"; return 1; }
  go vet ./...
  go build ./...
  go test ./...
  # TestMain starts an engine from ../engine; terraform-plugin-testing downloads Terraform when it is not installed
  TF_ACC=1 go test ./... -count=1 -timeout 20m
}

helm_chart() {
  local chart="$root/deploy/helm/shoal"
  helm lint --strict "$chart"
  if ! have kubeconform; then echo "kubeconform not installed: skipping schema validation"; return 0; fi
  helm template shoal "$chart" | kubeconform -strict -summary -skip ServiceMonitor
  helm template shoal "$chart" \
    --set postgresql.enabled=false,database.url=postgresql+asyncpg://u:p@db/shoal \
    --set ingress.enabled=true,api.autoscaling.enabled=true,worker.autoscaling.enabled=true \
    --set kubernetesSandboxes.enabled=true,metrics.serviceMonitor.enabled=true,api.embeddedWorker=true \
    | kubeconform -strict -summary -skip ServiceMonitor
  helm template shoal "$chart" --set orchestrator.type=temporal,orchestrator.temporal.address=temporal:7233 \
    --set orchestrator.temporal.apiKeySecret.name=temporal-key,orchestrator.temporal.mtlsSecret=temporal-tls \
    --set orchestrator.temporal.versioning.buildId=v1 \
    | kubeconform -strict -summary -skip ServiceMonitor
  helm template shoal "$chart" --set secrets.existingSecret=shoal-creds,worker.enabled=false \
    --set kubernetesSandboxes.enabled=true,kubernetesSandboxes.clusterWide=true \
    | kubeconform -strict -summary
}

image() {
  cd "$root"
  docker build -f deploy/Dockerfile -t shoal:check .
  docker compose -f deploy/docker-compose.yaml --env-file deploy/.env.example config -q
  local name="shoal-check-$$" port="${SHOAL_CHECK_PORT:-18080}"
  docker run -d --name "$name" -p "$port:8080" -e SHOAL_ADMIN_TOKEN=check shoal:check >/dev/null
  trap 'docker rm -f '"$name"' >/dev/null 2>&1 || true' RETURN
  for _ in $(seq 1 30); do curl -sf "http://localhost:$port/readyz" >/dev/null && break; sleep 1; done
  docker run --rm --network host shoal:check --server "http://localhost:$port" --token check tenants create check
}

if want engine; then step "engine: ruff + pytest"; run engine engine; fi
if want frontend; then
  if [ -f "$root/frontend/package.json" ] && have npm; then step "frontend"; run frontend frontend
  else skipped+=("frontend (no frontend/ or npm)"); fi
fi
if want terraform; then
  if have go; then step "terraform provider"; run terraform terraform_provider
  else skipped+=("terraform (go not installed)"); fi
fi
if want helm; then
  if have helm; then step "helm chart"; run helm helm_chart
  else skipped+=("helm (helm not installed)"); fi
fi
if want image; then
  if have docker && docker info >/dev/null 2>&1; then step "container image"; run image image
  else skipped+=("image (docker not available)"); fi
fi

echo
for s in "${skipped[@]}"; do echo "skipped: $s"; done
if [ ${#failed[@]} -gt 0 ]; then echo "FAILED: ${failed[*]}"; exit 1; fi
echo "all checks passed"
