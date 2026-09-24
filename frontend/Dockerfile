# Build context: repository root (docker build -f frontend/Dockerfile .)
# Base images are build args so a self-hosted agent without Docker Hub access can
# pull them from an ACR mirror (pipeline variables baseImageNode / baseImageNginx).
ARG NODE_IMAGE=node:22-alpine
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine

FROM ${NODE_IMAGE} AS build
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Unprivileged nginx: listens on 8080 and runs as uid 101, so the pod can run
# with runAsNonRoot and a read-only root filesystem (/tmp is an emptyDir).
FROM ${NGINX_IMAGE}
COPY --from=build /src/dist /usr/share/nginx/html
# Default config for `docker run`; in AKS the Helm chart mounts its own
# default.conf (backend service name) and config.js (tenant/client IDs).
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
