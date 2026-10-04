# CRIP: one image for Azure App Service (Web App for Containers).
#
#   docker build -t crip .
#
# Stage 1 builds the React UI; stage 2 is the FastAPI app that serves the UI,
# its runtime /config.js and the /api, all from one origin on $PORT (8000).
# Packages come from the public npm / PyPI registries.

ARG NODE_IMAGE=node:22-alpine
ARG PYTHON_IMAGE=python:3.12-slim

# --------------------------------------------------------------------------- UI build
FROM ${NODE_IMAGE} AS ui
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
# Retries + a tunable socket limit: some networks (Docker Desktop NAT, proxies)
# reset connections when npm opens many at once. The cache mount speeds rebuilds.
ARG NPM_MAXSOCKETS=15
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund --maxsockets=${NPM_MAXSOCKETS} \
      --fetch-retries=5 --fetch-retry-mintimeout=20000 --fetch-retry-maxtimeout=120000
COPY frontend/ ./
RUN npm run build

# --------------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}

# CRIP_SQLITE_PATH: App Service persists /home across restarts (WEBSITES_ENABLE_APP_SERVICE_STORAGE=true).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    PORT=8000 \
    CRIP_FOUNDRY_DEFINITIONS_DIR=/app/foundry/definitions \
    CRIP_STATIC_DIR=/app/static \
    CRIP_SECRETS_DIR=/app/.secrets \
    CRIP_SQLITE_PATH=/home/data/crip.db

WORKDIR /app
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/crip_backend backend/crip_backend
RUN pip install ./backend && rm -rf backend
COPY foundry foundry
COPY --from=ui /src/dist static

# Non-root. /home/data is pre-created for local runs; App Service mounts its
# persistent /home over it (if that mount is not writable the app falls back to
# /tmp and logs a warning).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin crip \
    && mkdir -p /home/data && chown -R 10001:10001 /home/data
USER 10001
EXPOSE 8000

# Shell form so $PORT is expanded (App Service may set it; WEBSITES_PORT=8000 tells it where we listen).
CMD ["sh", "-c", "exec uvicorn --factory crip_backend.main:create_app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
