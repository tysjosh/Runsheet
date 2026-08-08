# syntax=docker/dockerfile:1
#
# Runtime image for the Next.js dispatcher UI.
#
# ---------------------------------------------------------------------------
# Why this exists at all, and why not S3 + CloudFront
# ---------------------------------------------------------------------------
# Static hosting is the obvious cheap answer and it does not work for this app.
# Verified rather than assumed: setting ``output: "export"`` fails the build.
#
#   Error: export const dynamic = "force-static"/export const revalidate not
#   configured on route "/robots.txt" with "output: export"
#   Error: ... on route "/apple-icon"
#   ⚠ Specified "headers" will not automatically work with "output: export"
#
# There are eleven server-rendered routes (every [id] / [orderId] page) and no
# ``generateStaticParams`` anywhere, so each one would need one written for it;
# ``robots.ts``, ``sitemap.ts``, ``apple-icon.tsx`` and ``opengraph-image.tsx`` are
# all dynamic route handlers; the security headers in ``next.config.ts`` are
# silently dropped by a static export; and image optimization needs a server.
# Static hosting is therefore a rewrite, not a deployment choice.
#
# So the UI runs as a server, from Next's ``standalone`` output, which
# ``next.config.ts`` already supports via ``STANDALONE=true``.
#
# ---------------------------------------------------------------------------
# NEXT_PUBLIC_* values are BAKED IN AT BUILD TIME
# ---------------------------------------------------------------------------
# This is the one thing about this image that surprises people. Next inlines every
# ``NEXT_PUBLIC_*`` variable into the JavaScript bundle during ``next build``, so
# they are build arguments here, not runtime environment. Two consequences:
#
#   * the image is ENVIRONMENT-SPECIFIC. A staging image cannot be promoted to
#     production by changing an ECS environment variable; it has to be rebuilt.
#   * none of these values can be secret, because they are served to every
#     browser that loads the app. The Google Maps key is restricted by HTTP
#     referrer in the Google Cloud console, which is the actual control.
#
# Passing them as ARG (not ENV) also keeps them out of the final image's config
# beyond the bundle they are already compiled into.

# ---------------------------------------------------------------------------
# Stage 1 — dependencies
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS deps

WORKDIR /app

# Copied alone so a source-only change does not invalidate the dependency layer.
# npm ci (not install) so the lockfile is authoritative and the build is
# reproducible; it fails loudly if package.json and the lockfile disagree.
COPY package.json package-lock.json ./
RUN npm ci

# ---------------------------------------------------------------------------
# Stage 2 — build
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS builder

WORKDIR /app

ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_WS_URL
ARG NEXT_PUBLIC_ST_API_DOMAIN
ARG NEXT_PUBLIC_ST_WEBSITE_DOMAIN
ARG NEXT_PUBLIC_ST_API_BASE_PATH=/auth
ARG NEXT_PUBLIC_TENANT_ID
ARG NEXT_PUBLIC_GOOGLE_MAPS_API_KEY
ARG NEXT_PUBLIC_SITE_URL

ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL \
    NEXT_PUBLIC_ST_API_DOMAIN=$NEXT_PUBLIC_ST_API_DOMAIN \
    NEXT_PUBLIC_ST_WEBSITE_DOMAIN=$NEXT_PUBLIC_ST_WEBSITE_DOMAIN \
    NEXT_PUBLIC_ST_API_BASE_PATH=$NEXT_PUBLIC_ST_API_BASE_PATH \
    NEXT_PUBLIC_TENANT_ID=$NEXT_PUBLIC_TENANT_ID \
    NEXT_PUBLIC_GOOGLE_MAPS_API_KEY=$NEXT_PUBLIC_GOOGLE_MAPS_API_KEY \
    NEXT_PUBLIC_SITE_URL=$NEXT_PUBLIC_SITE_URL \
    STANDALONE=true \
    NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Refuse to build an image whose API origin is missing or plaintext. Without this
# the build succeeds and every request from the browser is blocked as mixed
# content at runtime, which presents as a UI that loads and then does nothing.
RUN case "${NEXT_PUBLIC_API_URL}" in \
      https://*) : ;; \
      "") echo "NEXT_PUBLIC_API_URL is required (see staging_aws.sh frontend-env)" >&2; exit 1 ;; \
      *) echo "NEXT_PUBLIC_API_URL must be https, got: ${NEXT_PUBLIC_API_URL}" >&2; exit 1 ;; \
    esac \
    && npm run build

# ---------------------------------------------------------------------------
# Stage 3 — runtime
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# curl for the container health check only. useradd rather than node's built-in
# "node" user so the uid matches the backend image's convention.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin --uid 10001 runsheet

WORKDIR /app

# Only the standalone bundle, its static assets and public/. Notably this leaves
# node_modules, the source tree and every .env* file out of the runtime image —
# the standalone output already contains the subset of node_modules it needs.
COPY --from=builder --chown=runsheet:runsheet /app/.next/standalone ./
COPY --from=builder --chown=runsheet:runsheet /app/.next/static ./.next/static
COPY --from=builder --chown=runsheet:runsheet /app/public ./public

USER runsheet

EXPOSE 3000

# / rather than a dedicated endpoint: the app has no health route of its own, and
# the root page is statically prerendered, so this answers without touching the
# backend. A health check that called the API would report the UI unhealthy
# whenever the API was down and cause a pointless replacement.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl --fail --silent --show-error "http://127.0.0.1:${PORT}/" || exit 1

# server.js is emitted by the standalone build. No npm in the path: it would add a
# process for nothing and swallow signals, which delays task shutdown.
CMD ["node", "server.js"]
