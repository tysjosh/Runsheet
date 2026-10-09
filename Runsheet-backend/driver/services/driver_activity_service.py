"""
``DriverActivityService`` — the dispatcher read of driver messages and exceptions.

Drivers post messages (``job_messages``) and field exceptions
(``driver_exceptions``) through the ``/api/driver/*`` surface, but nothing let
an admin or dispatcher read them back (G1). This service reads both existing
stores, normalizes each row to one shape, and returns a single timeline,
newest first. It adds no store and writes nothing (D13).

Scoping:

* per job: messages and exceptions with ``job_id == job_id``;
* per driver: messages with ``driver_id == id`` (order-keyed threads) or
  ``sender_id == id`` (the job-keyed path stamps the canonical ``driver_id``
  of the verified driver session as ``sender_id``, see
  ``driver/api/message_endpoints.py::_derive_sender``); exceptions with
  ``driver_id == id``.

Every query goes through ``inject_tenant_filter`` and every returned row is
re-checked against the caller's tenant, so a backend that ignored the filter
still could not leak another tenant's row.

Paging merges two stores, so each store is asked for the first
``page * size`` rows, the two lists are merged by timestamp, and the page is
sliced from the merge. The window is capped at :data:`MAX_WINDOW` rows per
store; a page past it is rejected with 422 rather than answered from a
truncated merge.

Validates: G1 (fleet-scheduling.md), D13
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from driver.services.driver_es_mappings import (
    DRIVER_EXCEPTIONS_INDEX,
    JOB_MESSAGES_INDEX,
)
from errors.codes import ErrorCode
from errors.exceptions import AppException
from ops.middleware.tenant_guard import inject_tenant_filter

logger = logging.getLogger(__name__)

#: Activity types, and the store each one reads.
ACTIVITY_TYPES = ("message", "exception")

#: Largest ``page * size`` the merge answers (rows fetched per store).
MAX_WINDOW = 1000


def _iso_utc(value: Optional[datetime]) -> Optional[str]:
    """Render a bound as an ISO-8601 UTC string; a naive datetime is read as UTC.

    Stored timestamps are ``datetime.now(timezone.utc).isoformat()`` strings, and
    the Postgres store compares string bounds lexically, so the bound must use
    the same ``+00:00`` form.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _normalize_message(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": doc.get("message_id"),
        "type": "message",
        "timestamp": doc.get("timestamp"),
        "driver_id": doc.get("driver_id") or doc.get("sender_id"),
        "sender_role": doc.get("sender_role"),
        "job_id": doc.get("job_id"),
        "order_id": doc.get("order_id"),
        "text": doc.get("body"),
    }


def _normalize_exception(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": doc.get("exception_id"),
        "type": "exception",
        "timestamp": doc.get("timestamp"),
        "driver_id": doc.get("driver_id"),
        "job_id": doc.get("job_id"),
        "order_id": doc.get("order_id"),
        "text": doc.get("note"),
        "exception_type": doc.get("exception_type"),
        "severity": doc.get("severity"),
    }


class DriverActivityService:
    """Read-only, tenant-scoped timeline over ``job_messages`` and ``driver_exceptions``.

    Args:
        es_service: The search facade (the Postgres document store in
            production) exposing ``search_documents``.
    """

    def __init__(self, es_service: Any) -> None:
        self._es = es_service

    async def list_for_job(
        self,
        tenant_id: str,
        job_id: str,
        *,
        types: Optional[Sequence[str]] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """Return the job's driver activity, newest first.

        Returns:
            ``{"items": [...], "total": int}`` for the requested page.
        """
        clause = {"term": {"job_id": job_id}}
        return await self._list(
            tenant_id,
            message_clause=clause,
            exception_clause=clause,
            types=types,
            start_date=start_date,
            end_date=end_date,
            page=page,
            size=size,
        )

    async def list_for_driver(
        self,
        tenant_id: str,
        driver_id: str,
        *,
        types: Optional[Sequence[str]] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """Return everything the driver posted or was named on, newest first.

        Returns:
            ``{"items": [...], "total": int}`` for the requested page.
        """
        return await self._list(
            tenant_id,
            message_clause={
                "bool": {
                    "should": [
                        {"term": {"driver_id": driver_id}},
                        {"term": {"sender_id": driver_id}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            exception_clause={"term": {"driver_id": driver_id}},
            types=types,
            start_date=start_date,
            end_date=end_date,
            page=page,
            size=size,
        )

    async def _list(
        self,
        tenant_id: str,
        *,
        message_clause: Dict[str, Any],
        exception_clause: Dict[str, Any],
        types: Optional[Sequence[str]],
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        page: int,
        size: int,
    ) -> Dict[str, Any]:
        window = page * size
        if window > MAX_WINDOW:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"page * size must not exceed {MAX_WINDOW}",
                status_code=422,
                details={"page": page, "size": size, "max_window": MAX_WINDOW},
            )

        wanted = set(types) if types else set(ACTIVITY_TYPES)
        time_range: Dict[str, str] = {}
        if start_date is not None:
            time_range["gte"] = _iso_utc(start_date)
        if end_date is not None:
            time_range["lte"] = _iso_utc(end_date)

        sources = []
        if "message" in wanted:
            sources.append((JOB_MESSAGES_INDEX, message_clause, _normalize_message))
        if "exception" in wanted:
            sources.append((DRIVER_EXCEPTIONS_INDEX, exception_clause, _normalize_exception))

        rows: List[Dict[str, Any]] = []
        total = 0
        for index, clause, normalize in sources:
            must: List[Dict[str, Any]] = [clause]
            if time_range:
                must.append({"range": {"timestamp": dict(time_range)}})
            query = inject_tenant_filter(
                {
                    "query": {"bool": {"must": must}},
                    "sort": [{"timestamp": {"order": "desc"}}],
                    "size": window,
                },
                tenant_id,
            )
            response = await self._es.search_documents(index, query, size=window)
            hits = response.get("hits", {})
            total += _total_of(hits)
            for hit in hits.get("hits", []):
                doc = hit.get("_source") or {}
                if doc.get("tenant_id") != tenant_id:
                    # Defense in depth: never return a row from another tenant.
                    logger.warning(
                        "Dropped a %s row outside tenant %s from driver activity",
                        index,
                        tenant_id,
                    )
                    continue
                rows.append(normalize(doc))

        rows.sort(key=lambda r: (r.get("timestamp") or "", r.get("id") or ""), reverse=True)
        start = (page - 1) * size
        return {"items": rows[start:start + size], "total": total}


def _total_of(hits: Dict[str, Any]) -> int:
    """Read ``hits.total`` in either the ES 7+ object form or the bare int form."""
    total = hits.get("total", 0)
    if isinstance(total, dict):
        return int(total.get("value", 0))
    return int(total or 0)
