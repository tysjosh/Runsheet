"""
ES-backed idempotency middleware for driver endpoints.

Implements request deduplication using the ``idempotency_keys``
Elasticsearch index. When a request includes an ``X-Idempotency-Key``
header, the middleware checks for a cached response and returns it
immediately on duplicate. First-time requests are processed normally
and the response is stored with a configurable TTL (default 24 hours).

Applied as a FastAPI dependency on driver endpoints that accept the
``X-Idempotency-Key`` header.

Validates: Requirements 14.1, 14.2, 14.3, 14.4
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from driver.services.driver_es_mappings import IDEMPOTENCY_KEYS_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class IdempotencyMiddleware:
    """ES-backed idempotency for driver endpoints.

    Stores request responses keyed by ``(idempotency_key, tenant_id)``
    in the ``idempotency_keys`` Elasticsearch index. Cached responses
    are returned with an ``X-Idempotent-Replayed: true`` header.

    Validates: Requirements 14.1, 14.2, 14.3, 14.4
    """

    DEFAULT_TTL_HOURS = 24

    def __init__(
        self,
        es_service: ElasticsearchService,
        ttl_hours: int = DEFAULT_TTL_HOURS,
    ) -> None:
        self._es = es_service
        self._ttl_hours = ttl_hours

    def _make_doc_id(self, idempotency_key: str, tenant_id: str) -> str:
        """Build a deterministic ES document ID from key + tenant."""
        return f"{tenant_id}:{idempotency_key}"

    async def check_and_cache(
        self, idempotency_key: str, tenant_id: str
    ) -> Optional[dict]:
        """Return cached response if *idempotency_key* exists, else ``None``.

        Validates: Requirement 14.1
        """
        doc_id = self._make_doc_id(idempotency_key, tenant_id)
        try:
            doc = await self._es.get_document(IDEMPOTENCY_KEYS_INDEX, doc_id)
            if doc:
                # Check if the key has expired
                expires_at = doc.get("expires_at")
                if expires_at:
                    expiry = datetime.fromisoformat(
                        expires_at.replace("Z", "+00:00")
                    )
                    if datetime.now(timezone.utc) > expiry:
                        # Expired — treat as cache miss
                        return None
                return doc.get("response")
        except Exception as exc:
            # get_document raises AppException on not-found; treat as miss
            logger.debug(
                "Idempotency cache miss for key=%s tenant=%s: %s",
                idempotency_key,
                tenant_id,
                exc,
            )
        return None

    async def store_response(
        self,
        idempotency_key: str,
        tenant_id: str,
        response_body: dict,
        status_code: int = 200,
    ) -> None:
        """Store *response_body* with a TTL for later replay.

        Validates: Requirement 14.2
        """
        doc_id = self._make_doc_id(idempotency_key, tenant_id)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=self._ttl_hours)

        doc = {
            "idempotency_key": idempotency_key,
            "tenant_id": tenant_id,
            "response": {
                "body": response_body,
                "status_code": status_code,
            },
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

        try:
            await self._es.index_document(IDEMPOTENCY_KEYS_INDEX, doc_id, doc)
        except Exception as exc:
            # Non-fatal — the request was already processed successfully
            logger.warning(
                "Failed to store idempotency key=%s tenant=%s: %s",
                idempotency_key,
                tenant_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Module-level singleton, wired at bootstrap
# ---------------------------------------------------------------------------

_idempotency_middleware: Optional[IdempotencyMiddleware] = None


def configure_idempotency_middleware(
    *,
    es_service: ElasticsearchService,
    ttl_hours: int = IdempotencyMiddleware.DEFAULT_TTL_HOURS,
) -> IdempotencyMiddleware:
    """Create and register the global IdempotencyMiddleware instance.

    Called once during application startup from ``bootstrap/scheduling.py``.
    """
    global _idempotency_middleware
    _idempotency_middleware = IdempotencyMiddleware(
        es_service=es_service, ttl_hours=ttl_hours
    )
    return _idempotency_middleware


def get_idempotency_middleware() -> Optional[IdempotencyMiddleware]:
    """Return the configured middleware instance (or ``None``)."""
    return _idempotency_middleware


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


class IdempotencyResult:
    """Carries the outcome of the idempotency check into the endpoint.

    Attributes:
        key: The idempotency key from the header, or ``None`` if absent.
        cached_response: The previously stored response dict, or ``None``.
        is_replay: ``True`` when a cached response was found.
    """

    __slots__ = ("key", "cached_response", "is_replay")

    def __init__(
        self,
        key: Optional[str] = None,
        cached_response: Optional[dict] = None,
    ) -> None:
        self.key = key
        self.cached_response = cached_response
        self.is_replay = cached_response is not None

    def replay_response(self):
        """Build a JSONResponse with the ``X-Idempotent-Replayed`` header.

        Validates: Requirement 14.4
        """
        body = self.cached_response.get("body", {})
        status = self.cached_response.get("status_code", 200)
        return JSONResponse(
            content=body,
            status_code=status,
            headers={"X-Idempotent-Replayed": "true"},
        )


async def check_idempotency(request: Request) -> IdempotencyResult:
    """FastAPI dependency that performs the idempotency check.

    Usage in an endpoint::

        @router.post("/jobs/{job_id}/ack")
        async def ack_job(
            ...,
            idempotency: IdempotencyResult = Depends(check_idempotency),
        ):
            if idempotency.is_replay:
                return idempotency.replay_response()
            ...

    When the ``X-Idempotency-Key`` header is absent the dependency
    returns an ``IdempotencyResult`` with ``key=None`` and
    ``is_replay=False``, so the endpoint processes normally.

    Validates: Requirements 14.1, 14.3
    """
    key = request.headers.get("x-idempotency-key")
    if not key:
        # No idempotency header — process normally (Req 14.3)
        return IdempotencyResult()

    middleware = get_idempotency_middleware()
    if middleware is None:
        # Middleware not configured — process normally
        return IdempotencyResult(key=key)

    # Extract tenant_id from the request state or auth header
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        # Try to get from Authorization header via TenantContext
        # Fall back to a default for safety
        tenant_id = "unknown"

    cached = await middleware.check_and_cache(key, tenant_id)
    if cached is not None:
        return IdempotencyResult(key=key, cached_response=cached)

    return IdempotencyResult(key=key)


async def store_idempotency_response(
    key: str,
    tenant_id: str,
    response_body: dict,
    status_code: int = 200,
) -> None:
    """Helper to store a response after successful processing.

    Called by endpoints after they have produced a response, so that
    subsequent requests with the same key return the cached version.

    Validates: Requirement 14.2
    """
    middleware = get_idempotency_middleware()
    if middleware is None:
        return
    await middleware.store_response(key, tenant_id, response_body, status_code)
