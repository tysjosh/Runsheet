# syntax=docker/dockerfile:1
#
# Runsheet backend runtime image.
#
# Two stages so build tooling never reaches the runtime layer: wheels are
# resolved and installed into a virtualenv in `builder`, then the venv is copied
# forward. The runtime stage carries no compiler and no pip cache.
#
# Python is pinned to 3.11 to match CI (`PYTHON_VERSION: "3.11"`) and the local
# venv (3.11.8). The suite has never run on another minor, and pydantic-core /
# psycopg / scikit-learn all ship version-specific wheels, so drifting the
# interpreter here would mean shipping an untested combination.

# ---------------------------------------------------------------------------
# Stage 1 — build the virtualenv
# ---------------------------------------------------------------------------
# Base images come from the AWS ECR Public mirror of the Docker Official Images
# (same image, same tag). CodeBuild pulls Docker Hub anonymously from shared IPs
# and gets 429 Too Many Requests; public.ecr.aws has no such limit from AWS.
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential is needed only for any sdist that has no wheel for this
# platform. It stays in the builder stage and is never shipped.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied on its own so a source-only change does not invalidate the dependency
# layer — the install is the slow step by an order of magnitude.
COPY requirements.txt ./
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt

# US state boundaries for StateBoundaryDetector (regional analytics, IFTA).
# The Census file is public domain and not committed; the script pins its
# sha256 and exits non-zero on a mismatch or a failed verification, so a bad
# download fails the build. Its own layer, before the source COPY, so it
# caches across source-only changes.
COPY scripts/download_shapefile.py /build/scripts/download_shapefile.py
RUN python /build/scripts/download_shapefile.py --output-dir /opt/geodata

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS runtime

# PYTHONUNBUFFERED so log lines reach the collector as they are written rather
# than sitting in a pipe buffer — without it a crash loses the reason for it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080

# curl is here for the container healthcheck below, not for the application.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin --uid 10001 runsheet

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=runsheet:runsheet . /app
# After the source COPY so data/ in the build context (README/.gitkeep) does
# not hide it. StateBoundaryDetector reads data/us_states.shp from /app.
COPY --from=builder --chown=runsheet:runsheet /opt/geodata/ /app/data/

# Non-root: the process needs no write access to the image, and a container
# escape from an unprivileged uid is worth strictly less than from root.
USER runsheet

EXPOSE 8080

# /health/ready is the readiness probe — it checks Elasticsearch reachability
# and returns 503 when a dependency is down. /health/live would report healthy
# with every datastore unreachable, which is the correct liveness semantics but
# useless as a rollout gate.
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD curl --fail --silent --show-error "http://127.0.0.1:${PORT}/health/ready" || exit 1

# Migrations are NOT run here. They are a separate, ordered step in the deploy
# runbook (docs/deploy-runbook.md): running `alembic upgrade head` from an
# app container start means N replicas racing the same migration, and this
# chain contains a destructive revision (0007_drop_shipments_current).
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
