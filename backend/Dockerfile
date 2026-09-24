# Build context: repository root (docker build -f backend/Dockerfile .)
# The image contains the backend package AND foundry/ (definitions + the
# registration script, which the Helm post-install hook Job runs from this image).
# Base image is a build arg so a self-hosted agent without Docker Hub access can
# pull it from an ACR mirror (pipeline variable baseImagePython).
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CRIP_FOUNDRY_DEFINITIONS_DIR=/app/foundry/definitions

WORKDIR /app
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/crip_backend backend/crip_backend
# Python packages come from the internal Nexus repository. The pipeline passes
# the pip config as BuildKit secret "pipconf" (and the corporate CA as "pipca"):
# they are mounted only for this RUN and never stored in a layer or in the image
# history. Without the secret (e.g. a plain local build) pip uses its default index.
RUN --mount=type=secret,id=pipconf \
    --mount=type=secret,id=pipca \
    if [ -f /run/secrets/pipconf ]; then export PIP_CONFIG_FILE=/run/secrets/pipconf; fi; \
    pip install ./backend && rm -rf backend
COPY foundry foundry

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin crip
USER 10001
EXPOSE 8000

CMD ["uvicorn", "--factory", "crip_backend.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
