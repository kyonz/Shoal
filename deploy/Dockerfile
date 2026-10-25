# syntax=docker/dockerfile:1.7
# The AWM image: API server, worker and CLI, plus the built UI when frontend/ exists.
#
#   docker build -f deploy/Dockerfile -t awm .          (from the repository root)
#   docker run -p 8080:8080 -e AWM_ADMIN_TOKEN=... awm   (serve, with an embedded worker)

ARG PYTHON_VERSION=3.13
ARG NODE_VERSION=22

# --------------------------------------------------------------------------- UI
FROM node:${NODE_VERSION}-slim AS ui
WORKDIR /src
# Builds the UI only when the repository has one, so the image builds either way.
RUN --mount=type=bind,source=.,target=/ctx \
    mkdir -p /out && \
    if [ -f /ctx/frontend/package.json ]; then \
      cp -r /ctx/frontend/. /src/ && \
      (npm ci --no-audit --no-fund || npm install --no-audit --no-fund) && \
      npm run build && cp -r dist/. /out/; \
    else \
      echo "no frontend/ in the build context; the image serves the API only"; \
    fi

# --------------------------------------------------------------------------- Python
FROM python:${PYTHON_VERSION}-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
ARG AWM_EXTRAS="--extra postgres --extra kubernetes --extra docker"
# Dependencies first: this layer is reused until uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=engine/uv.lock,target=uv.lock \
    --mount=type=bind,source=engine/pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project ${AWM_EXTRAS}
COPY engine/ /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable ${AWM_EXTRAS}

# --------------------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim
LABEL org.opencontainers.image.title="awm" \
      org.opencontainers.image.description="AI Workflow Manager: multi-tenant engine for multi-agent AI workflows" \
      org.opencontainers.image.source="https://github.com/kyonz/ai-workflow-manager" \
      org.opencontainers.image.licenses="Apache-2.0"
# git and ssh for agents and scripts that work on repositories in local sandboxes.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git openssh-client tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --home-dir /var/lib/awm awm \
    && install -d -o awm -g awm /var/lib/awm/sandboxes
COPY --from=build /app/.venv /app/.venv
COPY --from=ui /out /app/ui
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    AWM_HOST=0.0.0.0 \
    AWM_PORT=8080 \
    AWM_STATIC_DIR=/app/ui \
    AWM_LOCAL_SANDBOX_ROOT=/var/lib/awm/sandboxes \
    AWM_DATABASE_URL=sqlite+aiosqlite:////var/lib/awm/awm.db
USER 10001
WORKDIR /var/lib/awm
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
  CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
ENTRYPOINT ["tini", "--", "awm"]
CMD ["serve"]
