# syntax=docker/dockerfile:1.7
# The Shoal image: API server, worker and CLI, plus the built UI when frontend/ exists.
#
#   docker build -f deploy/Dockerfile -t shoal .          (from the repository root)
#   docker run -p 8080:8080 -e SHOAL_ADMIN_TOKEN=... shoal   (serve, with an embedded worker)

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
ARG SHOAL_EXTRAS="--extra postgres --extra kubernetes --extra docker --extra temporal --extra jev"
# Dependencies first: this layer is reused until uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=engine/uv.lock,target=uv.lock \
    --mount=type=bind,source=engine/pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project ${SHOAL_EXTRAS}
COPY engine/ /app/
# The wheel bundles the integration template from ../templates (see pyproject.toml).
COPY templates/ /templates/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable ${SHOAL_EXTRAS}

# --------------------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim
LABEL org.opencontainers.image.title="shoal" \
      org.opencontainers.image.description="Shoal: multi-tenant engine for multi-agent AI workflows" \
      org.opencontainers.image.source="https://github.com/kyonz/ai-workflow-manager" \
      org.opencontainers.image.licenses="Apache-2.0"
# git and ssh for agents and scripts that work on repositories in local sandboxes.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git openssh-client tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --home-dir /var/lib/shoal shoal \
    && install -d -o shoal -g shoal /var/lib/shoal/sandboxes
# Optional: the Claude Code CLI, for signing in to Claude subscriptions from the UI and for
# claude-code agents in local sandboxes (docs/claude-code.md). Build with --build-arg CLAUDE_CODE=1.
ARG CLAUDE_CODE=0
RUN if [ "$CLAUDE_CODE" = 1 ]; then \
      apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* && \
      curl -fsSL https://claude.ai/install.sh | bash && cp -L /root/.local/bin/claude /usr/local/bin/claude && rm -rf /root/.local; \
    fi
COPY --from=build /app/.venv /app/.venv
COPY --from=ui /out /app/ui
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SHOAL_HOST=0.0.0.0 \
    SHOAL_PORT=8080 \
    SHOAL_STATIC_DIR=/app/ui \
    SHOAL_LOCAL_SANDBOX_ROOT=/var/lib/shoal/sandboxes \
    SHOAL_DATABASE_URL=sqlite+aiosqlite:////var/lib/shoal/shoal.db
USER 10001
WORKDIR /var/lib/shoal
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
  CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
ENTRYPOINT ["tini", "--", "shoal"]
CMD ["serve"]
