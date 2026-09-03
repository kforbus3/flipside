# syntax=docker/dockerfile:1

# ---- build the SPA ----
FROM node:20-slim AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ---- backend runtime ----
FROM python:3.14-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app STATIC_DIR=/app/static

# Docker CLI + buildx + compose plugins so the UI can orchestrate sibling
# containers (buildx + BuildKit enables cross-platform builds, e.g. amd64 images
# from an arm64 host).
ENV DOCKER_BUILDKIT=1
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

# iproute2: the UI enumerates the host's NICs by running this same image as a
# throwaway container in the host network namespace (see list_interfaces), so
# the operator picks a provisioning interface instead of typing one.
RUN apt-get update && apt-get install -y --no-install-recommends curl iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=frontend /fe/dist ./static

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8080/api/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
