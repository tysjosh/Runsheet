"""
Redis-backed voice submission ledger for idempotency conflict detection
and order-id recall on replay (Surface A).

The existing :class:`~ops.ingestion.idempotency.IdempotencyService` only
tracks *key existence* — it returns ``status="duplicate"`` with
``order_id=None`` and keeps no body comparison. The Dinee voice contract
requires more:

- **Req 5.4** — the same ``X-Idempotency-Key`` reused with a *different*
  request body for the same tenant must be a **409 conflict**.
- **Req 9.2** — a replay (same key + same body) must return the *original*
  minted order id, without invoking the pipeline again.

Neither can be satisfied by key existence alone, so this ledger records
``(tenant_id, idempotency_key) -> {body_sha256, order_id, disposition}``.
It does **not** replace the pipeline's idempotency — the bridge consults it
before/after the pipeline call, and the pipeline's ``IdempotencyService``
still runs inside ``ingest_webhook`` as belt-and-suspenders dedup.

Keys are tenant-scoped (``voice_idem:{tenant_id}:{idempotency_key}``) so the
same idempotency key under two tenants is independent (Req 5.2). It is
backed by the same Redis client the ``IdempotencyService`` uses and uses the
same TTL convention (default 72h), so ledger entries expire in lock-step
with the pipeline's idempotency markers.

Requirements: 5.1, 5.2, 5.4, 9.2
"""

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LedgerEntry:
    """A recorded voice submission outcome for a ``(tenant, key)`` pair.

    Attributes:
        body_sha256: Hex SHA-256 of the exact raw request body that produced
            this outcome. The bridge compares the presented body's hash
            against this to distinguish a replay (equal → recall the order)
            from a conflict (different → 409).
        order_id: The pipeline-minted order id, replayed verbatim on a
            same-body retry (Req 9.2).
        disposition: The acceptance disposition recorded at first sight
            (e.g. ``accepted`` / ``review_hold`` / ``duplicate``).
    """

    body_sha256: str
    order_id: Optional[str]
    disposition: str


class VoiceSubmissionLedger:
    """Redis-backed conflict + order-id recall store for voice submissions.

    Mirrors :class:`IdempotencyService`'s connection and TTL conventions.
    The Redis client may be **injected** (to reuse the exact client the
    ``IdempotencyService`` already connected) or self-managed via
    :meth:`connect` / :meth:`disconnect` when constructed with a
    ``redis_url``.
    """

    KEY_TEMPLATE = "voice_idem:{tenant_id}:{idempotency_key}"

    def __init__(
        self,
        redis_url: Optional[str] = None,
        ttl_hours: int = 72,
        *,
        client=None,
    ):
        self.redis_url = redis_url
        self.ttl = timedelta(hours=ttl_hours)
        # When a client is injected we reuse the exact connection the
        # IdempotencyService already established; otherwise connect() lazily
        # creates one from redis_url.
        self.client = client

    async def connect(self) -> None:
        """Establish a Redis connection from ``redis_url``.

        Not required when a pre-connected ``client`` was injected at
        construction time.
        """
        if self.client is not None:
            return
        if not self.redis_url:
            raise RuntimeError(
                "VoiceSubmissionLedger needs a redis_url or an injected client "
                "to connect."
            )
        import redis.asyncio as redis
        self.client = redis.from_url(self.redis_url, decode_responses=True)

    async def disconnect(self) -> None:
        """Close a self-managed Redis connection.

        Only closes connections this ledger opened via :meth:`connect`;
        injected clients are owned by their provider and left untouched.
        """
        if self.client is not None and self.redis_url:
            await self.client.close()
            self.client = None

    def _get_key(self, tenant_id: str, idempotency_key: str) -> str:
        """Compose the tenant-scoped Redis key (Req 5.2)."""
        return self.KEY_TEMPLATE.format(
            tenant_id=tenant_id, idempotency_key=idempotency_key
        )

    async def lookup(self, tenant_id: str, key: str) -> Optional[LedgerEntry]:
        """Return the recorded outcome for ``(tenant_id, key)``, or ``None``.

        ``None`` means this is a new submission for the tenant; the bridge
        proceeds to the pipeline. A returned entry lets the bridge decide
        replay vs. conflict by comparing ``body_sha256`` (Req 5.1, 5.4, 9.2).
        """
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        raw = await self.client.get(self._get_key(tenant_id, key))
        if raw is None:
            return None
        data = json.loads(raw)
        return LedgerEntry(
            body_sha256=data["body_sha256"],
            order_id=data.get("order_id"),
            disposition=data["disposition"],
        )

    async def record(
        self,
        tenant_id: str,
        key: str,
        body_sha256: str,
        order_id: Optional[str],
        disposition: str,
    ) -> None:
        """Persist the outcome of a first-seen submission with the idempotency TTL.

        Called after a ``processed`` pipeline result so a later retry with
        the same key can be recalled (same body → Req 9.2) or rejected
        (different body → Req 5.4).
        """
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        redis_key = self._get_key(tenant_id, key)
        payload = json.dumps(
            {
                "body_sha256": body_sha256,
                "order_id": order_id,
                "disposition": disposition,
            }
        )
        ttl_seconds = int(self.ttl.total_seconds())
        await self.client.setex(redis_key, ttl_seconds, payload)
        logger.debug(
            "Recorded voice submission ledger entry for tenant=%s key=%s "
            "order_id=%s disposition=%s (TTL: %s hours)",
            tenant_id,
            key,
            order_id,
            disposition,
            self.ttl.total_seconds() / 3600,
        )
