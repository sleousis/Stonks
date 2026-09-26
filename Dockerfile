# syntax=docker/dockerfile:1
#
# Stonks image: REST API + trader console + scheduler worker, one image.
#
#   console  (Node)   npm ci + ng build          -> /web/dist
#   build    (uv)     uv sync --locked --no-dev   -> /app/.venv
#   runtime  (slim)   venv + config + console, non-root, healthcheck
#
# Build:  docker build -t stonks .
# Run:    docker run -p 8000:8000 -v stonks-data:/data -e STONKS_API_TOKEN=... stonks

ARG PYTHON_VERSION=3.13

# ---- 1. Angular console ----------------------------------------------------
# The bundle is platform-neutral, so build it on the native builder platform
# (fast) even when the final image is multi-arch.
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS console
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- 2. Python package with uv ---------------------------------------------
FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm AS build
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first (cached layer), then the project itself.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- 3. Runtime --------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="stonks" \
      org.opencontainers.image.description="Stonks research and trading system: API, console and scheduler" \
      org.opencontainers.image.licenses="LicenseRef-All-Rights-Reserved"

RUN groupadd --system --gid 10001 stonks \
 && useradd --system --uid 10001 --gid stonks --home-dir /app --shell /usr/sbin/nologin stonks \
 && mkdir -p /data \
 && chown stonks:stonks /data

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# load_settings() reads config/default.toml relative to the working directory,
# and [api].ui_dist = "web/dist" resolves to /app/web/dist.
COPY config ./config
COPY --from=console /web/dist ./web/dist

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STONKS_DATA_DIR=/data

USER stonks
EXPOSE 8000

# /api/health is the public liveness probe (no token needed).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]

# Bind all interfaces inside the container; only Caddy reaches it (compose
# does not publish port 8000).
CMD ["stonks", "serve", "--host", "0.0.0.0", "--port", "8000"]
