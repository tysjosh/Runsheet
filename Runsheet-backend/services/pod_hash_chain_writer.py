"""
POD Hash Chain Writer — atomic hash-chain persistence for Proof-of-Delivery
records (fuel-ops-hardening Capability 4, Requirement 4.5.2).

Every POD record is persisted with a ``pod_hash`` (SHA-256 of its canonical
payload) and a ``previous_pod_hash`` that equals the ``pod_hash`` of the
immediately prior POD in the tenant's chain. The first POD's
``previous_pod_hash`` is the zero-hash (64 ``"0"`` characters). Retroactive
mutation of any field covered by :func:`services.pod_hash_chain.canonicalize_pod`
therefore breaks the chain at the modified POD and is detectable by the
verification endpoint (Task 8.11 / Requirement 4.5.4).

Hashing and persistence must be **atomic** per tenant: two concurrent
submissions for the same tenant must not both read the same ``latest``
pod_hash and then write two sibling PODs that each reference it (only one
can legitimately chain from a given prior hash). We serialize per-tenant
writes with a short-lived Redis lock (``pod_chain_lock:{tenant_id}`` with a
5-second TTL) acquired via ``SET NX EX``. The TTL is long enough to cover
an ES ``index_document`` call (typically < 1s) but short enough that a
crashed writer cannot starve subsequent writes; the writer re-checks the
chain sequence after acquiring the lock so concurrency failures are safe.

The public entry point is :class:`PodHashChainWriter.persist`, which returns
a dict with the persisted POD plus the ``pod_hash``, ``previous_pod_hash``,
and ``chain_sequence`` fields that were written.

Validates: Requirement 4.5.2
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from driver.services.driver_es_mappings import PROOF_OF_DELIVERY_INDEX
from services.pod_hash_chain import ZERO_HASH, compute_pod_hash

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Redis key template for the per-tenant POD hash-chain lock.
POD_CHAIN_LOCK_KEY_PATTERN: str = "pod_chain_lock:{tenant_id}"

#: Lock TTL in seconds. Covers a single ES ``index_document`` round-trip with
#: comfortable margin (Requirement 4.5.2).
POD_CHAIN_LOCK_TTL_SECONDS: int = 5

#: Maximum wall-clock time the writer will spend trying to acquire the lock
#: before raising :class:`PodChainLockTimeout`. Kept just above the TTL so a
#: single stalled writer cannot block the request indefinitely while still
#: absorbing routine contention.
DEFAULT_LOCK_ACQUIRE_TIMEOUT_SECONDS: float = 6.0

#: Sleep between lock-acquisition retries (Redis NX polling).
_LOCK_ACQUIRE_POLL_INTERVAL_SECONDS: float = 0.05


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PodChainLockTimeout(RuntimeError):
    """Raised when the per-tenant POD hash-chain lock cannot be acquired."""


class PodChainPersistenceError(RuntimeError):
    """Raised when POD persistence itself fails under the chain lock."""


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class PodHashChainWriter:
    """Persist POD records with atomic hash-chain updates per tenant.

    Parameters
    ----------
    es_service:
        ``ElasticsearchService``-compatible instance exposing
        ``index_document(index, doc_id, doc)`` and ``search_documents``.
    redis_client:
        Optional async Redis client. When ``None``, persistence falls back to
        a process-local :class:`asyncio.Lock` per tenant — sufficient for
        single-process deployments and tests, but callers should pass a real
        Redis client in multi-replica production.
    lock_ttl_seconds:
        TTL applied to the Redis lock key. Defaults to :data:`POD_CHAIN_LOCK_TTL_SECONDS`.
    lock_acquire_timeout_seconds:
        Max wall-clock time the writer will spend blocking on
        :meth:`_acquire_lock` before raising :class:`PodChainLockTimeout`.
    index_name:
        ES index receiving POD writes; defaults to
        :data:`driver.services.driver_es_mappings.PROOF_OF_DELIVERY_INDEX`.

    Validates: Requirement 4.5.2
    """

    def __init__(
        self,
        es_service: Any,
        redis_client: Optional[Any] = None,
        *,
        lock_ttl_seconds: int = POD_CHAIN_LOCK_TTL_SECONDS,
        lock_acquire_timeout_seconds: float = DEFAULT_LOCK_ACQUIRE_TIMEOUT_SECONDS,
        index_name: str = PROOF_OF_DELIVERY_INDEX,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service is required")
        if lock_ttl_seconds <= 0:
            raise ValueError("lock_ttl_seconds must be positive")
        if lock_acquire_timeout_seconds <= 0:
            raise ValueError("lock_acquire_timeout_seconds must be positive")

        self._es = es_service
        self._redis = redis_client
        self._lock_ttl = int(lock_ttl_seconds)
        self._lock_timeout = float(lock_acquire_timeout_seconds)
        self._index = index_name
        # Per-tenant asyncio locks used when no Redis client is wired in.
        self._local_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def persist(
        self,
        tenant_id: str,
        pod_doc: Mapping[str, Any],
    ) -> dict:
        """Persist ``pod_doc`` with atomic hash-chain fields.

        The caller supplies the full POD document (``pod_id``, ``order_id``,
        ``delivered_gallons``, ``recipient_name``, ``signature_ref``,
        ``photo_refs``, ``geotag``, ``delivered_at`` / ``timestamp``, etc).
        The writer:

            1. Acquires the per-tenant Redis lock (``pod_chain_lock:{tenant_id}``).
            2. Reads the latest prior POD's ``pod_hash`` (or :data:`ZERO_HASH`
               when the tenant's chain is empty).
            3. Computes the new ``pod_hash`` using
               :func:`services.pod_hash_chain.compute_pod_hash` over the
               canonical payload (Requirement 4.5.1).
            4. Writes the POD to the ``proof_of_delivery`` index with
               ``pod_hash``, ``previous_pod_hash``, and ``chain_sequence`` set.
            5. Releases the lock.

        Returns the persisted document augmented with the hash-chain fields so
        callers can surface them in responses / WebSocket events.

        Raises
        ------
        ValueError
            If ``tenant_id`` is blank or ``pod_doc`` is missing ``pod_id`` /
            ``tenant_id`` mismatch.
        PodChainLockTimeout
            If the lock cannot be acquired within
            ``lock_acquire_timeout_seconds``.
        PodChainPersistenceError
            If ES persistence fails under the lock.
        """
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not isinstance(pod_doc, Mapping):
            raise ValueError("pod_doc must be a mapping")
        pod_id = pod_doc.get("pod_id")
        if not pod_id:
            raise ValueError("pod_doc.pod_id is required")
        doc_tenant = pod_doc.get("tenant_id", tenant_id)
        if doc_tenant != tenant_id:
            raise ValueError(
                f"pod_doc.tenant_id ({doc_tenant!r}) does not match tenant_id ({tenant_id!r})"
            )

        lock_key = POD_CHAIN_LOCK_KEY_PATTERN.format(tenant_id=tenant_id)

        async with _ChainLock(
            redis_client=self._redis,
            key=lock_key,
            ttl_seconds=self._lock_ttl,
            acquire_timeout_seconds=self._lock_timeout,
            local_locks=self._local_locks,
            tenant_id=tenant_id,
        ):
            previous_hash, chain_sequence = await self._fetch_latest_chain_state(
                tenant_id=tenant_id
            )

            # Build the hashing view. ``compute_pod_hash`` only reads the
            # canonical fields; any extras on ``pod_doc`` are ignored. We
            # always pass a ``delivered_gallons`` (defaulting to 0.0) and
            # supply ``delivered_at`` from either the explicit field or the
            # POD ``timestamp`` to match canonicalize_pod's contract.
            hashing_view = dict(pod_doc)
            hashing_view.setdefault("tenant_id", tenant_id)
            if hashing_view.get("delivered_gallons") is None:
                hashing_view["delivered_gallons"] = 0.0
            if not hashing_view.get("delivered_at"):
                hashing_view["delivered_at"] = (
                    hashing_view.get("timestamp")
                    or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                )
            hashing_view["previous_pod_hash"] = previous_hash

            try:
                pod_hash = compute_pod_hash(hashing_view)
            except (ValueError, TypeError) as exc:
                raise PodChainPersistenceError(
                    f"Failed to compute pod_hash for tenant {tenant_id}: {exc}"
                ) from exc

            persisted = dict(pod_doc)
            persisted.setdefault("tenant_id", tenant_id)
            persisted["pod_hash"] = pod_hash
            persisted["previous_pod_hash"] = previous_hash
            persisted["chain_sequence"] = chain_sequence + 1
            persisted["persisted_at"] = datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )

            try:
                await self._es.index_document(self._index, str(pod_id), persisted)
            except Exception as exc:
                raise PodChainPersistenceError(
                    f"Failed to persist POD {pod_id} for tenant {tenant_id}: {exc}"
                ) from exc

            logger.info(
                "POD persisted with hash chain: tenant=%s pod_id=%s sequence=%d "
                "previous_hash=%s… hash=%s…",
                tenant_id,
                pod_id,
                persisted["chain_sequence"],
                previous_hash[:12],
                pod_hash[:12],
            )
            return persisted

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_latest_chain_state(
        self, tenant_id: str
    ) -> tuple[str, int]:
        """Return ``(previous_pod_hash, chain_sequence)`` for ``tenant_id``.

        Queries the ``proof_of_delivery`` index for the most recently
        persisted POD for the tenant (sorted by ``persisted_at`` desc, then
        ``chain_sequence`` desc for ties / missing ``persisted_at``). Falls
        back to :data:`ZERO_HASH` + sequence ``0`` when the tenant's chain is
        empty so the first POD is correctly initialized (Requirement 4.5.2).
        """
        query = {
            "query": {"term": {"tenant_id": tenant_id}},
            "sort": [
                {"chain_sequence": {"order": "desc", "missing": "_last"}},
                {"persisted_at": {"order": "desc", "missing": "_last"}},
            ],
            "size": 1,
        }
        try:
            response = await self._es.search_documents(self._index, query, size=1)
        except Exception as exc:
            logger.warning(
                "Failed to fetch latest POD chain state for tenant=%s: %s — "
                "falling back to zero-hash",
                tenant_id,
                exc,
            )
            return ZERO_HASH, 0

        hits = (response or {}).get("hits", {}).get("hits", [])
        if not hits:
            return ZERO_HASH, 0

        source = hits[0].get("_source", {}) or {}
        previous_hash = source.get("pod_hash") or ZERO_HASH
        chain_sequence = source.get("chain_sequence")
        try:
            chain_sequence_int = int(chain_sequence) if chain_sequence is not None else 0
        except (TypeError, ValueError):
            chain_sequence_int = 0
        return str(previous_hash), chain_sequence_int


# ---------------------------------------------------------------------------
# Chain lock (async context manager)
# ---------------------------------------------------------------------------


class _ChainLock:
    """Async context manager acquiring the per-tenant POD hash-chain lock.

    Prefers a Redis ``SET NX EX`` lock (shared across processes) and falls
    back to an in-process :class:`asyncio.Lock` when no Redis client is wired
    in — the latter is only correct in single-process tests / dev.
    """

    def __init__(
        self,
        *,
        redis_client: Any,
        key: str,
        ttl_seconds: int,
        acquire_timeout_seconds: float,
        local_locks: dict,
        tenant_id: str,
    ) -> None:
        self._redis = redis_client
        self._key = key
        self._ttl = ttl_seconds
        self._timeout = acquire_timeout_seconds
        self._local_locks = local_locks
        self._tenant_id = tenant_id
        self._token = uuid.uuid4().hex
        self._using_redis = redis_client is not None
        self._local_lock: Optional[asyncio.Lock] = None

    async def __aenter__(self) -> "_ChainLock":
        if self._using_redis:
            await self._acquire_redis_lock()
        else:
            self._local_lock = self._local_locks.setdefault(
                self._tenant_id, asyncio.Lock()
            )
            try:
                await asyncio.wait_for(
                    self._local_lock.acquire(), timeout=self._timeout
                )
            except asyncio.TimeoutError as exc:
                raise PodChainLockTimeout(
                    f"Timed out acquiring local POD chain lock for tenant {self._tenant_id}"
                ) from exc
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._using_redis:
            await self._release_redis_lock()
        elif self._local_lock is not None and self._local_lock.locked():
            self._local_lock.release()

    async def _acquire_redis_lock(self) -> None:
        deadline = asyncio.get_event_loop().time() + self._timeout
        attempts = 0
        while True:
            attempts += 1
            try:
                acquired = await self._redis.set(
                    self._key,
                    self._token,
                    nx=True,
                    ex=self._ttl,
                )
            except Exception as exc:
                # Redis failure — fall back to local lock so a transient Redis
                # outage does not take down POD submission, but log loudly so
                # ops can investigate. Lock correctness degrades to
                # single-process scope until Redis recovers.
                logger.error(
                    "Redis unavailable acquiring POD chain lock for tenant=%s: %s "
                    "— falling back to local lock",
                    self._tenant_id,
                    exc,
                )
                self._using_redis = False
                self._local_lock = self._local_locks.setdefault(
                    self._tenant_id, asyncio.Lock()
                )
                try:
                    await asyncio.wait_for(
                        self._local_lock.acquire(),
                        timeout=max(deadline - asyncio.get_event_loop().time(), 0.01),
                    )
                except asyncio.TimeoutError as tex:
                    raise PodChainLockTimeout(
                        f"Timed out acquiring POD chain lock for tenant "
                        f"{self._tenant_id} (redis_fallback)"
                    ) from tex
                return
            if acquired:
                return
            if asyncio.get_event_loop().time() >= deadline:
                raise PodChainLockTimeout(
                    f"Timed out acquiring POD chain lock for tenant "
                    f"{self._tenant_id} after {attempts} attempts "
                    f"({self._timeout:.1f}s)"
                )
            await asyncio.sleep(_LOCK_ACQUIRE_POLL_INTERVAL_SECONDS)

    async def _release_redis_lock(self) -> None:
        # Best-effort token-matched release so we never unlock someone else's
        # lock if ours expired mid-flight. ``GET`` + ``DEL`` is racy, but the
        # TTL bounds the blast radius to ``POD_CHAIN_LOCK_TTL_SECONDS`` and
        # the fuel-ops persistence path is well below that.
        try:
            current = await self._redis.get(self._key)
        except Exception as exc:
            logger.warning(
                "Failed to read POD chain lock for release (tenant=%s): %s",
                self._tenant_id,
                exc,
            )
            return
        if current is None:
            return
        token = current.decode() if isinstance(current, bytes) else str(current)
        if token != self._token:
            # Our lock already expired and another writer owns it — nothing
            # to release.
            return
        try:
            await self._redis.delete(self._key)
        except Exception as exc:
            logger.warning(
                "Failed to release POD chain lock (tenant=%s): %s",
                self._tenant_id,
                exc,
            )


__all__ = [
    "POD_CHAIN_LOCK_KEY_PATTERN",
    "POD_CHAIN_LOCK_TTL_SECONDS",
    "DEFAULT_LOCK_ACQUIRE_TIMEOUT_SECONDS",
    "PodChainLockTimeout",
    "PodChainPersistenceError",
    "PodHashChainWriter",
]
