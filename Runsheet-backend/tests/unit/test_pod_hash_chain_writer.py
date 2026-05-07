"""
Unit tests for ``services.pod_hash_chain_writer.PodHashChainWriter``.

Validates Requirement 4.5.2:

    * The first POD's ``previous_pod_hash`` is the zero-hash.
    * Subsequent PODs carry the prior POD's ``pod_hash`` as their
      ``previous_pod_hash``.
    * Per-tenant concurrent writes are serialized via the
      ``pod_chain_lock:{tenant_id}`` Redis lock with a 5-second TTL.
    * Redis lock acquisition failures raise :class:`PodChainLockTimeout`.
    * ES persistence failures raise :class:`PodChainPersistenceError`.
    * Tenants are isolated: POD A's chain is independent of POD B's.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.pod_hash_chain import ZERO_HASH, compute_pod_hash
from services.pod_hash_chain_writer import (
    POD_CHAIN_LOCK_KEY_PATTERN,
    POD_CHAIN_LOCK_TTL_SECONDS,
    PodChainLockTimeout,
    PodChainPersistenceError,
    PodHashChainWriter,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeES:
    """Minimal ES double: remembers written docs keyed by (index, doc_id)."""

    def __init__(self, *, fail_index: bool = False, fail_search: bool = False):
        self.docs: dict[tuple[str, str], dict] = {}
        self.fail_index = fail_index
        self.fail_search = fail_search
        self.index_calls: list[tuple[str, str, dict]] = []
        self.search_calls: list[tuple[str, dict, int]] = []

    async def index_document(self, index: str, doc_id: str, doc: dict) -> dict:
        self.index_calls.append((index, doc_id, doc))
        if self.fail_index:
            raise RuntimeError("simulated ES index failure")
        self.docs[(index, doc_id)] = dict(doc)
        return {"result": "created"}

    async def search_documents(
        self, index: str, query: dict, size: int = 100
    ) -> dict:
        self.search_calls.append((index, query, size))
        if self.fail_search:
            raise RuntimeError("simulated ES search failure")
        # Extract tenant_id from query term filter (our writer uses
        # ``{"term": {"tenant_id": ...}}``) and pick the most recent doc by
        # chain_sequence for that tenant/index.
        term = (query.get("query", {}) or {}).get("term", {}) or {}
        tenant_id = term.get("tenant_id")
        matches = [
            d
            for (idx, _doc_id), d in self.docs.items()
            if idx == index and (tenant_id is None or d.get("tenant_id") == tenant_id)
        ]
        matches.sort(
            key=lambda d: d.get("chain_sequence", 0),
            reverse=True,
        )
        top = matches[:1]
        return {
            "hits": {
                "hits": [{"_source": d} for d in top],
                "total": {"value": len(top)},
            }
        }


class _FakeRedis:
    """Minimal async Redis double supporting ``set(nx, ex)``, ``get``, ``delete``."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, float]] = {}
        # Instrumentation
        self.set_calls: list[tuple[str, str, bool, Optional[int]]] = []
        self.delete_calls: list[str] = []

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: Optional[int] = None,
    ) -> bool:
        self.set_calls.append((key, value, nx, ex))
        now = asyncio.get_event_loop().time()
        existing = self.store.get(key)
        if existing is not None and existing[1] > now:
            if nx:
                return False
        expires_at = now + (ex if ex is not None else 1_000_000)
        self.store[key] = (value, expires_at)
        return True

    async def get(self, key: str):
        now = asyncio.get_event_loop().time()
        entry = self.store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= now:
            self.store.pop(key, None)
            return None
        return value.encode()

    async def delete(self, key: str) -> int:
        self.delete_calls.append(key)
        return 1 if self.store.pop(key, None) is not None else 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pod_doc(
    *,
    tenant_id: str = "t1",
    pod_id: str = "pod-001",
    order_id: str = "ord-42",
    delivered_gallons: float = 120.5,
    recipient_name: str = "Jane Doe",
    signature_ref: str = "tenants/t1/signature/2024/01/02/sig.jpg",
    photo_refs: Optional[list] = None,
    geotag: Optional[dict] = None,
    delivered_at: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> dict:
    if photo_refs is None:
        photo_refs = [f"tenants/{tenant_id}/photo/2024/01/02/a.jpg"]
    if geotag is None:
        geotag = {"lat": 40.7128, "lon": -74.0060}
    ts = timestamp or "2024-01-02T03:04:05Z"
    return {
        "tenant_id": tenant_id,
        "pod_id": pod_id,
        "job_id": f"job-{pod_id}",
        "order_id": order_id,
        "recipient_name": recipient_name,
        "signature_ref": signature_ref,
        "photo_refs": list(photo_refs),
        "geotag": geotag,
        "delivered_gallons": delivered_gallons,
        "delivered_at": delivered_at or ts,
        "timestamp": ts,
    }


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


class TestWriterConstruction:
    def test_requires_es_service(self):
        with pytest.raises(ValueError, match="es_service"):
            PodHashChainWriter(es_service=None)

    def test_rejects_non_positive_lock_ttl(self):
        with pytest.raises(ValueError, match="lock_ttl_seconds"):
            PodHashChainWriter(es_service=_FakeES(), lock_ttl_seconds=0)

    def test_rejects_non_positive_acquire_timeout(self):
        with pytest.raises(ValueError, match="lock_acquire_timeout_seconds"):
            PodHashChainWriter(
                es_service=_FakeES(), lock_acquire_timeout_seconds=0
            )

    def test_defaults_ttl_to_five_seconds(self):
        # Requirement 4.5.2: Redis lock TTL must default to 5 seconds.
        assert POD_CHAIN_LOCK_TTL_SECONDS == 5

    def test_default_lock_key_pattern_matches_spec(self):
        # Requirement 4.5.2 — ``pod_chain_lock:{tenant_id}``.
        assert POD_CHAIN_LOCK_KEY_PATTERN == "pod_chain_lock:{tenant_id}"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestPersistInputValidation:
    @pytest.mark.asyncio
    async def test_rejects_blank_tenant_id(self):
        writer = PodHashChainWriter(es_service=_FakeES())
        with pytest.raises(ValueError, match="tenant_id"):
            await writer.persist(tenant_id="   ", pod_doc=_pod_doc())

    @pytest.mark.asyncio
    async def test_rejects_non_mapping_pod_doc(self):
        writer = PodHashChainWriter(es_service=_FakeES())
        with pytest.raises(ValueError, match="pod_doc"):
            await writer.persist(tenant_id="t1", pod_doc="not-a-dict")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_rejects_missing_pod_id(self):
        writer = PodHashChainWriter(es_service=_FakeES())
        pod = _pod_doc()
        pod.pop("pod_id")
        with pytest.raises(ValueError, match="pod_id"):
            await writer.persist(tenant_id=pod["tenant_id"], pod_doc=pod)

    @pytest.mark.asyncio
    async def test_rejects_mismatched_tenant_id(self):
        writer = PodHashChainWriter(es_service=_FakeES())
        with pytest.raises(ValueError, match="does not match"):
            await writer.persist(
                tenant_id="caller-tenant",
                pod_doc=_pod_doc(tenant_id="other-tenant"),
            )


# ---------------------------------------------------------------------------
# First-POD zero-hash initialization (Requirement 4.5.2)
# ---------------------------------------------------------------------------


class TestFirstPodInitialization:
    @pytest.mark.asyncio
    async def test_first_pod_previous_hash_is_zero_hash(self):
        es = _FakeES()
        writer = PodHashChainWriter(es_service=es)

        pod = _pod_doc()
        result = await writer.persist(tenant_id=pod["tenant_id"], pod_doc=pod)

        assert result["previous_pod_hash"] == ZERO_HASH
        assert result["previous_pod_hash"] == "0" * 64

    @pytest.mark.asyncio
    async def test_first_pod_sets_chain_sequence_one(self):
        es = _FakeES()
        writer = PodHashChainWriter(es_service=es)
        result = await writer.persist(
            tenant_id="t1", pod_doc=_pod_doc()
        )
        assert result["chain_sequence"] == 1

    @pytest.mark.asyncio
    async def test_first_pod_hash_matches_compute_pod_hash(self):
        es = _FakeES()
        writer = PodHashChainWriter(es_service=es)
        pod = _pod_doc()

        result = await writer.persist(tenant_id=pod["tenant_id"], pod_doc=pod)

        # Rebuild the exact canonical view the writer uses.
        hashing_view = dict(pod)
        hashing_view["previous_pod_hash"] = ZERO_HASH
        assert result["pod_hash"] == compute_pod_hash(hashing_view)


# ---------------------------------------------------------------------------
# Chained persistence (prev == prior hash)
# ---------------------------------------------------------------------------


class TestChainedPersistence:
    @pytest.mark.asyncio
    async def test_second_pod_links_to_first(self):
        es = _FakeES()
        writer = PodHashChainWriter(es_service=es)

        first = await writer.persist(
            tenant_id="t1",
            pod_doc=_pod_doc(tenant_id="t1", pod_id="pod-1"),
        )
        second = await writer.persist(
            tenant_id="t1",
            pod_doc=_pod_doc(tenant_id="t1", pod_id="pod-2", delivered_gallons=200.0),
        )

        assert second["previous_pod_hash"] == first["pod_hash"]
        assert second["chain_sequence"] == 2

    @pytest.mark.asyncio
    async def test_chain_of_three_preserves_order(self):
        es = _FakeES()
        writer = PodHashChainWriter(es_service=es)

        first = await writer.persist(
            tenant_id="t1",
            pod_doc=_pod_doc(tenant_id="t1", pod_id="pod-1"),
        )
        second = await writer.persist(
            tenant_id="t1",
            pod_doc=_pod_doc(tenant_id="t1", pod_id="pod-2"),
        )
        third = await writer.persist(
            tenant_id="t1",
            pod_doc=_pod_doc(tenant_id="t1", pod_id="pod-3"),
        )

        assert second["previous_pod_hash"] == first["pod_hash"]
        assert third["previous_pod_hash"] == second["pod_hash"]
        assert [p["chain_sequence"] for p in (first, second, third)] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_tenants_are_isolated(self):
        es = _FakeES()
        writer = PodHashChainWriter(es_service=es)

        t1_first = await writer.persist(
            tenant_id="t1", pod_doc=_pod_doc(tenant_id="t1", pod_id="t1-pod-1")
        )
        t2_first = await writer.persist(
            tenant_id="t2", pod_doc=_pod_doc(tenant_id="t2", pod_id="t2-pod-1")
        )
        t1_second = await writer.persist(
            tenant_id="t1", pod_doc=_pod_doc(tenant_id="t1", pod_id="t1-pod-2")
        )

        # Both tenants start from the zero-hash independently.
        assert t1_first["previous_pod_hash"] == ZERO_HASH
        assert t2_first["previous_pod_hash"] == ZERO_HASH
        # t1's second POD chains from t1's first — NOT from t2's POD.
        assert t1_second["previous_pod_hash"] == t1_first["pod_hash"]
        assert t1_second["previous_pod_hash"] != t2_first["pod_hash"]


# ---------------------------------------------------------------------------
# Redis lock behavior (Requirement 4.5.2)
# ---------------------------------------------------------------------------


class TestRedisLock:
    @pytest.mark.asyncio
    async def test_lock_uses_spec_key_pattern(self):
        es = _FakeES()
        redis = _FakeRedis()
        writer = PodHashChainWriter(es_service=es, redis_client=redis)

        await writer.persist(tenant_id="tenant-xyz", pod_doc=_pod_doc(tenant_id="tenant-xyz"))

        # Exactly one NX acquire should have been attempted for this tenant.
        acquire_calls = [c for c in redis.set_calls if c[0] == "pod_chain_lock:tenant-xyz"]
        assert len(acquire_calls) == 1
        key, _token, nx, ex = acquire_calls[0]
        assert key == "pod_chain_lock:tenant-xyz"
        assert nx is True
        assert ex == POD_CHAIN_LOCK_TTL_SECONDS == 5

    @pytest.mark.asyncio
    async def test_lock_is_released_after_successful_write(self):
        es = _FakeES()
        redis = _FakeRedis()
        writer = PodHashChainWriter(es_service=es, redis_client=redis)

        await writer.persist(tenant_id="t1", pod_doc=_pod_doc())

        assert "pod_chain_lock:t1" in redis.delete_calls

    @pytest.mark.asyncio
    async def test_concurrent_writes_are_serialized(self):
        es = _FakeES()
        redis = _FakeRedis()
        writer = PodHashChainWriter(es_service=es, redis_client=redis)

        # Kick off three concurrent writes for the same tenant. The writer
        # must serialize them via the Redis lock so each sees the prior POD
        # when computing its own ``previous_pod_hash`` — no two PODs may
        # reference the same previous_pod_hash.
        pods = [_pod_doc(pod_id=f"pod-{i}") for i in range(3)]
        results = await asyncio.gather(
            *[writer.persist(tenant_id=p["tenant_id"], pod_doc=p) for p in pods]
        )

        previous_hashes = [r["previous_pod_hash"] for r in results]
        pod_hashes = [r["pod_hash"] for r in results]
        sequences = sorted(r["chain_sequence"] for r in results)

        # All previous_pod_hashes and pod_hashes must be distinct (chain).
        assert len(set(previous_hashes)) == 3
        assert len(set(pod_hashes)) == 3
        assert sequences == [1, 2, 3]
        # Exactly one result has the zero-hash as previous (the first POD).
        assert previous_hashes.count(ZERO_HASH) == 1
        # Every non-first POD's previous_hash appears as a pod_hash of
        # another persisted POD.
        for prev in previous_hashes:
            if prev == ZERO_HASH:
                continue
            assert prev in pod_hashes

    @pytest.mark.asyncio
    async def test_lock_timeout_raises_when_redis_always_busy(self):
        es = _FakeES()

        class _BusyRedis(_FakeRedis):
            async def set(self, key, value, *, nx=False, ex=None):
                # Never grant the lock.
                self.set_calls.append((key, value, nx, ex))
                return False

        redis = _BusyRedis()
        writer = PodHashChainWriter(
            es_service=es,
            redis_client=redis,
            lock_acquire_timeout_seconds=0.1,
        )

        with pytest.raises(PodChainLockTimeout):
            await writer.persist(tenant_id="t1", pod_doc=_pod_doc())
        # ES must not have been written to when the lock was never acquired.
        assert es.index_calls == []


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_es_index_failure_raises_persistence_error(self):
        es = _FakeES(fail_index=True)
        writer = PodHashChainWriter(es_service=es)

        with pytest.raises(PodChainPersistenceError):
            await writer.persist(tenant_id="t1", pod_doc=_pod_doc())

    @pytest.mark.asyncio
    async def test_es_search_failure_falls_back_to_zero_hash(self):
        """Search failure should not break the chain — it falls back to zero-hash.

        This is a conservative safety choice: if ES is transiently unreachable
        for the lookup, we still persist the POD and surface the potential
        chain gap via the downstream verification endpoint (Task 8.11).
        """
        es = _FakeES(fail_search=True)
        writer = PodHashChainWriter(es_service=es)

        result = await writer.persist(tenant_id="t1", pod_doc=_pod_doc())

        assert result["previous_pod_hash"] == ZERO_HASH
        assert result["chain_sequence"] == 1


# ---------------------------------------------------------------------------
# Local-lock fallback (no Redis)
# ---------------------------------------------------------------------------


class TestLocalLockFallback:
    @pytest.mark.asyncio
    async def test_no_redis_client_still_chains_correctly(self):
        es = _FakeES()
        writer = PodHashChainWriter(es_service=es, redis_client=None)

        first = await writer.persist(tenant_id="t1", pod_doc=_pod_doc(pod_id="pod-1"))
        second = await writer.persist(tenant_id="t1", pod_doc=_pod_doc(pod_id="pod-2"))

        assert first["previous_pod_hash"] == ZERO_HASH
        assert second["previous_pod_hash"] == first["pod_hash"]

    @pytest.mark.asyncio
    async def test_local_lock_serializes_concurrent_same_tenant(self):
        es = _FakeES()
        writer = PodHashChainWriter(es_service=es, redis_client=None)

        pods = [_pod_doc(pod_id=f"pod-{i}") for i in range(5)]
        results = await asyncio.gather(
            *[writer.persist(tenant_id=p["tenant_id"], pod_doc=p) for p in pods]
        )
        sequences = sorted(r["chain_sequence"] for r in results)
        pod_hashes = [r["pod_hash"] for r in results]
        previous_hashes = [r["previous_pod_hash"] for r in results]

        assert sequences == [1, 2, 3, 4, 5]
        assert len(set(pod_hashes)) == 5
        assert previous_hashes.count(ZERO_HASH) == 1
