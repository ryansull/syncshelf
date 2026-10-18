# SyncShelf image (SPEC §3.1, §2.4). CI builds and runs it on every PR; release.yml publishes
# it for amd64 and arm64 on a version tag.

# Build stage: install the locked runtime dependencies and the package into /app/.venv.
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
# Use the image's own Python, so the venv is valid in the runtime stage too.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
# Dependencies first, so a source-only change reuses this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
# The build backend needs the readme and licence that pyproject.toml names.
COPY README.md LICENSE ./
COPY src ./src
# Non-editable, so the runtime stage needs only the venv and not the source tree.
RUN uv sync --locked --no-dev --no-editable

# Runtime stage: no uv, no source, a fixed non-root user (§2.4: no PUID/PGID). /data is created
# and owned before VOLUME, so a fresh named volume starts out writable by 10001. A host bind
# mount keeps the host's ownership, which is why the host runs chown -R 10001:10001 (§2.4).
FROM python:3.14-slim
RUN groupadd --system --gid 10001 syncshelf \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin syncshelf \
    && mkdir /data \
    && chown 10001:10001 /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SYNCSHELF_CONFIG=/config/profiles.yaml
WORKDIR /app
VOLUME /data
USER 10001:10001
# server.listen defaults to 0.0.0.0:8000 (#51), so Traefik and a published port reach the
# service, and so does this health check, which runs inside the container on loopback.
# urlopen raises on a refused connection and on any non-2xx (the 503), so the check exits 1.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["python", "-m", "syncshelf"]
