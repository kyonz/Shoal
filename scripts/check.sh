#!/usr/bin/env bash
# Build and test everything locally (the same checks as .github/workflows/ci.yml).
#
#   scripts/check.sh                 # all sections
#   scripts/check.sh engine helm     # only some: engine frontend terraform helm image
#
# Needs uv; go, terraform, helm, kubeconform, node and docker are used when
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
run() {  # run NAME CMD...: record failures and keep going
  local name="$1"; shift
  if ! "$@"; then failed+=("$name"); fi
}

engine() {
  cd "$root/engine"
  uv sync --locked --all-extras --quiet
  uv run ruff check .
  uv run pytest -q
  uv run awm --help >/dev/null
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
  if have terraform; then
    TF_ACC=1 go test ./... -count=1 -timeout 20m  # TestMain starts an engine from ../engine
  else
    echo "terraform not installed: skipping acceptance tests"
  fi
}

helm_chart() {
  local chart="$root/deploy/helm/awm"
  helm lint --strict "$chart"
  if ! have kubeconform; then echo "kubeconform not installed: skipping schema validation"; return 0; fi
  helm template awm "$chart" | kubeconform -strict -summary -skip ServiceMonitor
  helm template awm "$chart" \
    --set postgresql.enabled=false,database.url=postgresql+asyncpg://u:p@db/awm \
    --set ingress.enabled=true,api.autoscaling.enabled=true,worker.autoscaling.enabled=true \
    --set kubernetesSandboxes.enabled=true,metrics.serviceMonitor.enabled=true,api.embeddedWorker=true \
    | kubeconform -strict -summary -skip ServiceMonitor
  helm template awm "$chart" --set secrets.existingSecret=awm-creds,worker.enabled=false \
    --set kubernetesSandboxes.enabled=true,kubernetesSandboxes.clusterWide=true \
    | kubeconform -strict -summary
}

image() {
  cd "$root"
  docker build -f deploy/Dockerfile -t awm:check .
  docker compose -f deploy/docker-compose.yaml config -q
  local name="awm-check-$$" port="${AWM_CHECK_PORT:-18080}"
  docker run -d --name "$name" -p "$port:8080" -e AWM_ADMIN_TOKEN=check awm:check >/dev/null
  trap 'docker rm -f '"$name"' >/dev/null 2>&1 || true' RETURN
  for _ in $(seq 1 30); do curl -sf "http://localhost:$port/readyz" >/dev/null && break; sleep 1; done
  docker run --rm --network host awm:check --server "http://localhost:$port" --token check tenants create check
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
