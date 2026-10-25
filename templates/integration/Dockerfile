FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
# git: shoal is installed from its repository until it is published on PyPI
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
# uv honours [tool.uv.sources] in pyproject.toml; plain pip would look for `shoal` on PyPI instead
RUN uv pip install --system --no-cache .
ENV SHOAL_INTEGRATION_TOKEN=""
EXPOSE 8080
CMD ["python", "-m", "shoal.sdk.server", "shoal_starter:StarterIntegration", "--host", "0.0.0.0", "--port", "8080"]
