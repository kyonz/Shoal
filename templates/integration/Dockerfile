FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
ENV AWM_INTEGRATION_TOKEN=""
EXPOSE 8080
CMD ["python", "-m", "awm.sdk.server", "awm_starter:StarterIntegration", "--host", "0.0.0.0", "--port", "8080"]
