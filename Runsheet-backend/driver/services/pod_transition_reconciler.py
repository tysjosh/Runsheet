"""Repair POD-to-order transitions that were interrupted after POD persistence.

The POD is the immutable evidence record and is written first.  Its mutable
``pod_status_transition`` field starts as ``pending`` so this worker can safely
finish the order projection after a process exit, transient repository error,
or invalid timing race without asking the driver to recapture evidence.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from driver.services.driver_es_mappings import PROOF_OF_DELIVERY_INDEX
from driver.services.pod_service import (
    POD_DELIVERED_STATUS,
    POD_REFUSED_STATUS,
    POD_TRANSITION_COMPLETED,
    POD_TRANSITION_PENDING,
    PODSubmissionService,
)

logger = logging.getLogger(__name__)

POD_TRANSITION_REPAIR_INTERVAL_SECONDS = 60
POD_TRANSITION_REPAIR_BATCH_SIZE = 100
_REPAIR_LOCK_TTL_SECONDS = 120


def _document(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    if hasattr(value, "dict"):
        return value.dict()
    raise TypeError("order repository returned an unsupported document")


class PODTransitionReconciler:
    """Idempotently complete pending POD order transitions."""

    def __init__(
        self,
        *,
        es_service: Any,
        order_repository: Any,
        order_service: Any,
        redis_client: Optional[Any] = None,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service is required")
        if order_repository is None:
            raise ValueError("order_repository is required")
        if order_service is None:
            raise ValueError("order_service is required")
        self._es = es_service
        self._orders = order_repository
        self._order_service = order_service
        self._redis = redis_client
        self._local_locks: dict[str, asyncio.Lock] = {}

    async def repair_pending(
        self, *, limit: int = POD_TRANSITION_REPAIR_BATCH_SIZE
    ) -> dict:
        """Repair one bounded, oldest-first batch and return cycle counts."""
        response = await self._es.search_documents(
            PROOF_OF_DELIVERY_INDEX,
            {
                "query": {
                    "term": {"pod_status_transition": POD_TRANSITION_PENDING}
                },
                "sort": [
                    {"persisted_at": {"order": "asc", "unmapped_type": "date"}},
                    {"pod_id": {"order": "asc"}},
                ],
                "size": limit,
            },
            limit,
        )
        hits = (response.get("hits") or {}).get("hits") or []
        counts = {
            "examined": len(hits),
            "repaired": 0,
            "failed": 0,
            "skipped_locked": 0,
        }
        for hit in hits:
            pod = dict(hit.get("_source") or {})
            pod.setdefault("pod_id", hit.get("_id"))
            async with self._repair_lock(pod) as acquired:
                if not acquired:
                    counts["skipped_locked"] += 1
                    continue
                try:
                    await self._repair_one(pod)
                    counts["repaired"] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    await self._record_failure(pod, exc)
                    logger.exception(
                        "POD transition repair failed: tenant=%s pod=%s "
                        "order=%s error=%s",
                        pod.get("tenant_id"),
                        pod.get("pod_id"),
                        pod.get("order_id"),
                        exc,
                    )
        return counts

    async def _repair_one(self, pod: dict) -> None:
        tenant_id = str(pod.get("tenant_id") or "").strip()
        order_id = str(pod.get("order_id") or "").strip()
        pod_id = str(pod.get("pod_id") or "").strip()
        if not tenant_id or not order_id or not pod_id:
            raise ValueError("pending POD is missing tenant_id, order_id, or pod_id")

        stored = await self._orders.get(tenant_id, order_id)
        if stored is None:
            raise LookupError(f"order {order_id} was not found")
        order = _document(stored)
        if order.get("tenant_id") != tenant_id:
            raise PermissionError("order tenant does not match POD tenant")

        refused = bool(pod.get("refused_delivery"))
        target_status = POD_REFUSED_STATUS if refused else POD_DELIVERED_STATUS
        bol = await self._find_bol(tenant_id=tenant_id, pod_id=pod_id)

        if refused:
            reason = pod.get("refusal_reason_code")
            if reason:
                order["refusal_reason_code"] = reason
        else:
            delivery_result = PODSubmissionService._build_delivery_result(
                order=order,
                pod_doc=pod,
                bol_doc=bol,
            )
            order["delivery_result"] = delivery_result

        if order.get("status") == target_status:
            if not refused and not _document(stored).get("delivery_result"):
                order = await self._order_service.reconcile_delivery_result(
                    order=_document(stored),
                    delivery_result=order["delivery_result"],
                    actor_user_id=pod.get("driver_id"),
                    client_event_timestamp=pod.get("delivered_at")
                    or pod.get("timestamp"),
                )
        else:
            order = await self._order_service.apply_status_transition(
                order=order,
                new_status=target_status,
                reason=pod.get("refusal_reason_code"),
                actor_user_id=pod.get("driver_id") or "pod_transition_reconciler",
                client_event_timestamp=pod.get("delivered_at")
                or pod.get("timestamp"),
            )

        await self._es.update_document(
            PROOF_OF_DELIVERY_INDEX,
            pod_id,
            {
                "pod_status_transition": POD_TRANSITION_COMPLETED,
                "pod_status_transition_error": None,
            },
        )
        logger.info(
            "POD transition repaired: tenant=%s pod=%s order=%s status=%s",
            tenant_id,
            pod_id,
            order_id,
            target_status,
        )

    async def _find_bol(self, *, tenant_id: str, pod_id: str) -> Optional[dict]:
        response = await self._es.search_documents(
            "bill_of_lading",
            {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"tenant_id": tenant_id}},
                            {"term": {"pod_id": pod_id}},
                        ]
                    }
                },
                "size": 1,
            },
            1,
        )
        hits = (response.get("hits") or {}).get("hits") or []
        return dict(hits[0].get("_source") or {}) if hits else None

    async def _record_failure(self, pod: dict, exc: Exception) -> None:
        pod_id = pod.get("pod_id")
        if not pod_id:
            return
        error = f"{type(exc).__name__}:{exc}"[:240]
        try:
            await self._es.update_document(
                PROOF_OF_DELIVERY_INDEX,
                pod_id,
                {
                    "pod_status_transition": POD_TRANSITION_PENDING,
                    "pod_status_transition_error": error,
                },
            )
        except Exception:
            logger.exception(
                "Could not persist POD transition repair failure for pod=%s",
                pod_id,
            )

    @asynccontextmanager
    async def _repair_lock(self, pod: dict) -> AsyncIterator[bool]:
        tenant_id = pod.get("tenant_id") or "unknown"
        pod_id = pod.get("pod_id") or "unknown"
        key = f"pod_transition_repair:{tenant_id}:{pod_id}"
        if self._redis is not None:
            acquired = bool(
                await self._redis.set(
                    key,
                    "1",
                    ex=_REPAIR_LOCK_TTL_SECONDS,
                    nx=True,
                )
            )
            try:
                yield acquired
            finally:
                if acquired:
                    try:
                        await self._redis.delete(key)
                    except Exception:
                        logger.warning("Failed to release POD repair lock %s", key)
            return

        lock = self._local_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            yield False
            return
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()
