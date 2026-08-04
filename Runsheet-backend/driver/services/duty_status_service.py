"""
``DutyStatusService`` — duty status as an append-only event log.

Before this module there was one availability value per driver and no record of
how it got there: ``drivers_current.status`` is a single ``keyword`` on a
``dynamic: strict`` index that carries no previous status, no actor, and no
source (``fuel/services/order_es_mappings.py:152-205``). This service inverts
that. ``duty_status_events`` is the authoritative history (R13.11, R13.14) and
``drivers_current.status`` becomes the current-value projection of the event
carrying the greatest ``server_received_at`` for that ``(tenant_id, driver_id)``
pair (R13.15).

Two consequences shape every method here:

**The event is the durable write and it goes first.** If the append fails the
transition is rejected and the projection is left untouched, so no projection
value can exist without an event behind it (R13.17). If the append succeeds and
the projection write then fails, the transition has *already happened* — the
history is correct and only the denormalized copy lags. That case answers 202
``DUTY_STATUS_PROJECTION_PENDING`` (R13.18), a 2xx carrying an error code, so the
app's offline queue dequeues the submission instead of retrying it and appending
a second event for the same intent. :meth:`current` repairs the projection from
the latest event on the next read.

**The log is append-only.** Every transition is a new document with a fresh id
``{tenant_id}:{driver_id}:{ulid}``; this module calls ``index_document`` with a
never-before-used id and calls neither ``update_document`` nor any delete
against ``duty_status_events``. The only writer permitted to touch an existing
event document is the 36-month retention job of R10.18, which does not live here
(R13.13).

There is deliberately **no** field recording a driver certification of an event,
an edit history, or an annotation (R13.22), this module writes no
Hours-of-Service record, and it sends no duty-status value to any external
system: R17.28 places the record of duty status with the carrier's ELD, and
Runsheet is not an ELD.

Presence is a separate axis. A dropped WebSocket does not change duty status
(R13.9) — nothing in this module reads or writes ``driver_presence``.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Data Models,
"New index: ``duty_status_events``".

Validates: Requirements 13.1, 13.2, 13.3, 13.6, 13.7, 13.11, 13.12, 13.13,
13.14, 13.15, 13.17, 13.18, 13.19, 13.22, 17.28
- 13.1: ``active``, ``on_break``, and ``off_duty`` are accepted from a driver
- 13.2: a driver-submitted ``inactive`` is 403 ``FORBIDDEN`` — ``inactive`` stays
  an administrator-set value
- 13.3: an accepted transition appends one event and then writes ``status`` on
  the ``drivers_current`` record whose ``driver_id`` matches the event's
- 13.6: ``off_duty`` submitted while the driver holds an assigned order in
  status ``in_transit`` is 409 ``ACTIVE_DELIVERY_IN_PROGRESS``
- 13.7, 13.12: every event carries the actor, the previous status, the new
  status, the client-asserted ``event_timestamp``, the ``server_received_at``,
  and a ``source`` in ``{driver, admin, system}``
- 13.13: append-only — no update and no delete against an existing event
- 13.15: the projection tracks the greatest ``server_received_at``
- 13.17: append failure rejects the transition, projection unchanged
- 13.18: projection failure after a durable append is 202
  ``DUTY_STATUS_PROJECTION_PENDING``, reconciled on the next read
- 13.19: an administrator-set ``inactive`` is recorded with the administrator in
  ``actor_id`` and ``source: "admin"``
- 13.22, 17.28: no certification, edit-history, or annotation field; no HOS
  record; no external emission
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Sequence

from driver.services.driver_es_mappings import DUTY_STATUS_EVENTS_INDEX
from errors.exceptions import (
    AppException,
    active_delivery_in_progress,
    duty_status_projection_pending,
    forbidden,
    invalid_request,
    resource_not_found,
)
from fuel.services.order_es_mappings import DRIVERS_CURRENT_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

#: The four ``DriverStatus`` values (``fuel/order_models.py:63``). There is no
#: ``on_duty``: the app's on-duty control maps to ``active`` (R13.4).
DUTY_STATUSES: tuple[str, ...] = ("active", "inactive", "on_break", "off_duty")

#: The statuses a driver may set for itself (R13.1). ``inactive`` is absent by
#: design (R13.2).
DRIVER_SETTABLE_STATUSES: tuple[str, ...] = ("active", "on_break", "off_duty")

#: The administrator-set value, recorded as an event like any other (R13.19).
ADMIN_ONLY_STATUS: str = "inactive"

#: ``source`` vocabulary (R13.12).
DUTY_STATUS_SOURCES: tuple[str, ...] = ("driver", "admin", "system")

#: The order status that blocks a driver-submitted ``off_duty`` (R13.6).
BLOCKING_ORDER_STATUS: str = "in_transit"

#: Upper bound on the events one history read returns. A duty-status day is a
#: handful of transitions, so this is a guard against an unbounded scan rather
#: than a paging scheme.
HISTORY_MAX_EVENTS: int = 1000

#: Crockford base32, the ULID alphabet: no ``I``, ``L``, ``O``, or ``U``.
_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _base32_encode(value: int, length: int) -> str:
    """Encode ``value`` as ``length`` Crockford base32 characters, big-endian."""
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD_BASE32[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(*, now_ms: Optional[int] = None) -> str:
    """Return a 26-character ULID: 48-bit millisecond timestamp + 80 random bits.

    Lexicographic order on the string follows creation order at millisecond
    resolution, which is what makes ``{tenant_id}:{driver_id}:{ulid}`` a document
    id that sorts by creation and never collides across tenants. The random tail
    comes from :mod:`secrets`, so two events minted in the same millisecond do
    not collide either.
    """
    timestamp_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return _base32_encode(timestamp_ms, 10) + _base32_encode(
        secrets.randbits(80), 16
    )


def duty_status_event_doc_id(tenant_id: str, driver_id: str, ulid: str) -> str:
    """Return the ``duty_status_events`` document id for one event."""
    return f"{tenant_id}:{driver_id}:{ulid}"


def _iso(value: Any) -> Optional[str]:
    """Normalize a timestamp to an ISO-8601 string, or ``None``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.isoformat()
    text = str(value).strip()
    return text or None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware datetime, or ``None``."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sources(response: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` payloads from an ES search response."""
    if not response:
        return []
    outer = response.get("hits") if hasattr(response, "get") else None
    if not outer:
        return []
    hits = outer.get("hits") if hasattr(outer, "get") else []
    out: List[Dict[str, Any]] = []
    for hit in hits or []:
        source = hit.get("_source") if hasattr(hit, "get") else None
        if source:
            out.append(dict(source))
    return out


def _as_document(record: Any) -> Optional[Dict[str, Any]]:
    """Normalize a repository result (model or raw dict) into a plain dict."""
    if record is None:
        return None
    if isinstance(record, dict):
        return dict(record)
    model_dump = getattr(record, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    return None


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class DutyStatusService:
    """Appends duty-status events, projects them, and serves the history.

    Args:
        es_service: The shared ``ElasticsearchService``. Required — it is what
            writes and reads ``duty_status_events``.
        driver_repository: ``DriverRepository``. Preferred writer of the
            projection, because it validates tenant ownership on the way in and
            round-trips the document through the ``Driver`` model. The write goes
            through ``project_duty_status``, the repository's one sanctioned
            entry point for ``status`` — ``update`` refuses the field, so this
            service is structurally the only writer of it (R13.16). Absent a
            repository, the projection is written with a partial
            ``update_document`` against ``drivers_current`` instead.
        order_repository: ``FuelOrderRepository``. Read to answer "does this
            driver hold an assigned order in ``in_transit``?" for the R13.6
            gate. Absent, the gate cannot be evaluated and a driver-submitted
            ``off_duty`` is rejected rather than let through, because letting it
            through would strand a delivery in progress.
    """

    def __init__(
        self,
        *,
        es_service,
        driver_repository=None,
        order_repository=None,
    ) -> None:
        self._es_service = es_service
        self._driver_repository = driver_repository
        self._order_repository = order_repository

    # ------------------------------------------------------------------
    # Transition (R13.1-R13.3, R13.6, R13.7, R13.11-R13.14, R13.17-R13.19)
    # ------------------------------------------------------------------

    async def transition(
        self,
        tenant_id: str,
        driver_id: str,
        new_status: str,
        *,
        actor_id: str,
        source: Literal["driver", "admin", "system"],
        event_timestamp: str,
        reason: Optional[str] = None,
    ) -> dict:
        """Append one duty-status event and project it onto ``drivers_current``.

        Order of operations is the contract: gate, read the previous status,
        append the event, then project. The append is the durable write and
        nothing after it is compensated.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The driver whose duty status changes.
            new_status: One of ``active``, ``inactive``, ``on_break``,
                ``off_duty``. ``inactive`` is administrator-only (R13.2).
            actor_id: The identity that caused the transition — the driver's own
                id, an administrator's user id, or ``"system"``.
            source: ``driver``, ``admin``, or ``system`` (R13.12).
            event_timestamp: The client-asserted ISO-8601 transition time. Blank
                falls back to the server clock, which is the only sane default
                for a system-initiated transition.
            reason: Optional free-text reason, used by administrator-set
                transitions.

        Returns:
            ``{"driver_id", "tenant_id", "previous_status", "new_status",
            "event_id", "event_timestamp", "server_received_at", "actor_id",
            "source", "reason", "projection_applied"}``. The caller wraps this in
            its own response envelope.

        Raises:
            AppException: 400 ``INVALID_REQUEST`` for an unknown status or
                source; 403 ``FORBIDDEN`` for a driver-submitted ``inactive``
                (R13.2); 409 ``ACTIVE_DELIVERY_IN_PROGRESS`` for a
                driver-submitted ``off_duty`` while an assigned order is
                ``in_transit`` (R13.6); the append failure itself when the event
                cannot be persisted (R13.17); 202
                ``DUTY_STATUS_PROJECTION_PENDING`` when the event is durable but
                the projection write is not (R13.18).

        Validates: Requirements 13.1, 13.2, 13.3, 13.6, 13.7, 13.11, 13.12,
        13.13, 13.17, 13.18, 13.19
        """
        es = self._require_es()
        tenant_id = self._require_text(tenant_id, "tenant_id")
        driver_id = self._require_text(driver_id, "driver_id")
        actor_id = self._require_text(actor_id, "actor_id")
        status = self._validated_status(new_status)
        source_value = self._validated_source(source)

        # Gate 1 — ``inactive`` is not a driver's to set (R13.2).
        if source_value == "driver" and status == ADMIN_ONLY_STATUS:
            raise forbidden(
                message="inactive is set by an administrator, not by a driver",
                details={
                    "requested_status": status,
                    "allowed_statuses": list(DRIVER_SETTABLE_STATUSES),
                },
            )

        # Gate 2 — a driver cannot walk away from a delivery in progress
        # (R13.6). Scoped to the driver path, exactly as R13.6 scopes it: an
        # administrator handling a breakdown still has a way to change the
        # value.
        if source_value == "driver" and status == "off_duty":
            await self._assert_no_delivery_in_progress(tenant_id, driver_id)

        server_received_at = utcnow()
        client_timestamp = self._validated_event_timestamp(
            event_timestamp, fallback=server_received_at
        )

        previous_status = await self._read_projected_status(tenant_id, driver_id)

        ulid = new_ulid(now_ms=int(server_received_at.timestamp() * 1000))
        event_id = duty_status_event_doc_id(tenant_id, driver_id, ulid)
        event_doc: Dict[str, Any] = {
            "event_id": event_id,
            "tenant_id": tenant_id,
            "driver_id": driver_id,
            # Nullable: the first event for a driver has no previous status.
            "previous_status": previous_status,
            "new_status": status,
            "event_timestamp": client_timestamp.isoformat(),
            "server_received_at": server_received_at.isoformat(),
            "actor_id": actor_id,
            "source": source_value,
            "reason": (reason or "").strip() or None,
        }

        # Step 1 — the durable write, under a fresh id. A failure here rejects
        # the transition and leaves the projection exactly as it was (R13.17):
        # nothing has been written to ``drivers_current`` at this point.
        await es.index_document(DUTY_STATUS_EVENTS_INDEX, event_id, event_doc)

        # Step 2 — the projection. The transition has already happened; this is
        # a denormalized copy of it (R13.3, R13.15).
        projected = await self._write_projection(
            tenant_id=tenant_id,
            driver_id=driver_id,
            new_status=status,
            event_id=event_id,
            server_received_at=server_received_at,
        )

        result = {
            "driver_id": driver_id,
            "tenant_id": tenant_id,
            "previous_status": previous_status,
            "new_status": status,
            "event_id": event_id,
            "event_timestamp": event_doc["event_timestamp"],
            "server_received_at": event_doc["server_received_at"],
            "actor_id": actor_id,
            "source": source_value,
            "reason": event_doc["reason"],
            "projection_applied": projected,
        }

        if not projected:
            # 2xx carrying an error code: the event is durable, so a retry would
            # append a duplicate. The offline queue must dequeue (R13.18), and
            # ``current`` repairs the projection on the next read.
            raise duty_status_projection_pending(
                details={
                    "driver_id": driver_id,
                    "event_id": event_id,
                    "duty_status": status,
                    "reason": "projection_write_failed",
                }
            )

        return result

    # ------------------------------------------------------------------
    # Current (R13.15, R13.16, R13.18)
    # ------------------------------------------------------------------

    async def current(self, tenant_id: str, driver_id: str) -> str:
        """Return the driver's current duty status, reconciling if it drifted.

        The event log is authoritative (R13.14), so the latest event wins. When
        the projection disagrees with it — the R13.18 case, where a durable
        append outran a failed projection write — the projection is repaired
        here, on the read, and the event's value is returned either way. A
        failed repair is logged and swallowed: a read must not fail because a
        write did.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The driver to read.

        Returns:
            One of the four ``DriverStatus`` values.

        Raises:
            AppException: 404 ``RESOURCE_NOT_FOUND`` when the tenant holds
                neither an event nor a ``drivers_current`` record for this
                driver.

        Validates: Requirements 13.15, 13.18
        """
        tenant_id = self._require_text(tenant_id, "tenant_id")
        driver_id = self._require_text(driver_id, "driver_id")

        latest = await self._read_latest_event(tenant_id, driver_id)
        projected = await self._read_projected_status(tenant_id, driver_id)

        if latest is None:
            # No history yet: a driver created before the event log existed, or
            # one that has never changed duty status.
            if projected is None:
                raise resource_not_found(
                    message="Driver record not found",
                    details={"driver_id": driver_id},
                )
            return projected

        authoritative = latest.get("new_status")
        if authoritative not in DUTY_STATUSES:
            logger.warning(
                "duty_status_events carries an unknown new_status %r for "
                "tenant=%s driver=%s; falling back to the projection",
                authoritative,
                tenant_id,
                driver_id,
            )
            if projected is None:
                raise resource_not_found(
                    message="Driver record not found",
                    details={"driver_id": driver_id},
                )
            return projected

        if projected != authoritative:
            logger.info(
                "Reconciling drivers_current.status from the event log: "
                "tenant=%s driver=%s projected=%r event=%r event_id=%s",
                tenant_id,
                driver_id,
                projected,
                authoritative,
                latest.get("event_id"),
            )
            await self._write_projection(
                tenant_id=tenant_id,
                driver_id=driver_id,
                new_status=authoritative,
                event_id=latest.get("event_id"),
                server_received_at=_parse_timestamp(
                    latest.get("server_received_at")
                ),
            )

        return authoritative

    # ------------------------------------------------------------------
    # History (R13.20, R13.21)
    # ------------------------------------------------------------------

    async def history(
        self,
        tenant_id: str,
        driver_id: str,
        *,
        range_start: str,
        range_end: str,
    ) -> list[dict]:
        """Return the driver's events inside a time range, oldest first.

        The range is inclusive on both ends and closes over ``event_timestamp``,
        the client-asserted time — the value a dispatcher reading a timeline
        means — not ``server_received_at``, which exists to order the projection.

        Authorization by role belongs to the caller: this method is scoped to one
        ``(tenant_id, driver_id)`` pair and has no parameter that could widen
        that scope, which is what makes the R13.21 check at the endpoint a simple
        comparison of the requested ``driver_id`` against the session's.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The driver whose history to read.
            range_start: Inclusive ISO-8601 lower bound on ``event_timestamp``.
            range_end: Inclusive ISO-8601 upper bound on ``event_timestamp``.

        Returns:
            The matching event documents, sorted by ``event_timestamp``
            ascending, capped at :data:`HISTORY_MAX_EVENTS`.

        Raises:
            AppException: 400 ``INVALID_REQUEST`` when either bound is not an
                ISO-8601 timestamp or the range is inverted.

        Validates: Requirements 13.20, 13.21
        """
        es = self._require_es()
        tenant_id = self._require_text(tenant_id, "tenant_id")
        driver_id = self._require_text(driver_id, "driver_id")

        start = _parse_timestamp(range_start)
        end = _parse_timestamp(range_end)
        invalid = [
            name
            for name, parsed in (("range_start", start), ("range_end", end))
            if parsed is None
        ]
        if invalid:
            raise invalid_request(
                message="range_start and range_end must be ISO-8601 timestamps",
                details={"invalid": invalid},
            )
        if start > end:
            raise invalid_request(
                message="range_start must not be after range_end",
                details={
                    "range_start": start.isoformat(),
                    "range_end": end.isoformat(),
                },
            )

        query = inject_tenant_filter(
            {
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"driver_id": driver_id}},
                            {
                                "range": {
                                    "event_timestamp": {
                                        "gte": start.isoformat(),
                                        "lte": end.isoformat(),
                                    }
                                }
                            },
                        ]
                    }
                }
            },
            tenant_id,
        )
        query["size"] = HISTORY_MAX_EVENTS
        query["sort"] = [{"event_timestamp": {"order": "asc"}}]

        response = await es.search_documents(
            DUTY_STATUS_EVENTS_INDEX, query, HISTORY_MAX_EVENTS
        )

        # Per-document tenant and driver re-validation: a filter regression
        # drops the document instead of leaking another tenant's or another
        # driver's history.
        return [
            event
            for event in _sources(response)
            if event.get("tenant_id") == tenant_id
            and event.get("driver_id") == driver_id
        ]

    # ------------------------------------------------------------------
    # Internals — reads
    # ------------------------------------------------------------------

    async def _read_latest_event(
        self, tenant_id: str, driver_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the event with the greatest ``server_received_at`` (R13.15).

        ``event_id`` descending breaks a tie inside the same millisecond: the id
        carries a ULID, so it sorts by creation for equal timestamps.
        """
        es = self._require_es()
        query = inject_tenant_filter(
            {"query": {"bool": {"filter": [{"term": {"driver_id": driver_id}}]}}},
            tenant_id,
        )
        query["size"] = 1
        query["sort"] = [
            {"server_received_at": {"order": "desc"}},
            {"event_id": {"order": "desc"}},
        ]

        try:
            response = await es.search_documents(
                DUTY_STATUS_EVENTS_INDEX, query, 1
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "DutyStatusService: latest-event read failed for tenant=%s "
                "driver=%s: %s",
                tenant_id,
                driver_id,
                exc,
            )
            return None

        for event in _sources(response):
            if event.get("tenant_id") == tenant_id and (
                event.get("driver_id") == driver_id
            ):
                return event
        return None

    async def _read_projected_status(
        self, tenant_id: str, driver_id: str
    ) -> Optional[str]:
        """Return ``drivers_current.status``, or ``None`` when there is no record.

        This is the value an event records as ``previous_status``. ``None`` is a
        legitimate answer for the first event of a driver that has no
        ``drivers_current`` row yet, and the mapping declares
        ``previous_status`` nullable for exactly that case.
        """
        record = await self._read_driver_document(tenant_id, driver_id)
        if record is None:
            return None
        status = record.get("status")
        return status if isinstance(status, str) and status.strip() else None

    async def _read_driver_document(
        self, tenant_id: str, driver_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the ``drivers_current`` document for the driver, or ``None``."""
        if self._driver_repository is not None:
            try:
                return _as_document(
                    await self._driver_repository.get(tenant_id, driver_id)
                )
            except Exception as exc:
                logger.warning(
                    "DutyStatusService: drivers_current read failed for "
                    "tenant=%s driver=%s: %s",
                    tenant_id,
                    driver_id,
                    exc,
                )
                return None

        es = self._require_es()
        query = inject_tenant_filter(
            {"query": {"bool": {"filter": [{"term": {"driver_id": driver_id}}]}}},
            tenant_id,
        )
        query["size"] = 1
        try:
            response = await es.search_documents(DRIVERS_CURRENT_INDEX, query, 1)
        except Exception as exc:
            logger.warning(
                "DutyStatusService: drivers_current read failed for tenant=%s "
                "driver=%s: %s",
                tenant_id,
                driver_id,
                exc,
            )
            return None
        return next(
            (s for s in _sources(response) if s.get("tenant_id") == tenant_id),
            None,
        )

    async def _assert_no_delivery_in_progress(
        self, tenant_id: str, driver_id: str
    ) -> None:
        """Reject ``off_duty`` while an assigned order is ``in_transit`` (R13.6).

        An absent ``order_repository`` is a rejection, not a pass: the gate
        exists to stop a driver abandoning a delivery, and a gate that cannot be
        evaluated must fail closed.
        """
        if self._order_repository is None:
            logger.error(
                "DutyStatusService has no order_repository; rejecting off_duty "
                "for tenant=%s driver=%s because the R13.6 gate cannot be "
                "evaluated",
                tenant_id,
                driver_id,
            )
            raise active_delivery_in_progress(
                details={
                    "driver_id": driver_id,
                    "reason": "delivery_check_unavailable",
                }
            )

        result = await self._order_repository.search_for_driver(
            tenant_id,
            driver_id,
            statuses=(BLOCKING_ORDER_STATUS,),
            page=1,
            size=1,
        )
        orders: Sequence[Any] = (result or {}).get("orders") or []
        in_transit = [
            doc
            for doc in (_as_document(order) for order in orders)
            if doc is not None and doc.get("status") == BLOCKING_ORDER_STATUS
        ]
        if not in_transit:
            return

        raise active_delivery_in_progress(
            details={
                "driver_id": driver_id,
                "order_status": BLOCKING_ORDER_STATUS,
                "order_id": in_transit[0].get("order_id"),
            }
        )

    # ------------------------------------------------------------------
    # Internals — the projection write
    # ------------------------------------------------------------------

    async def _write_projection(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        new_status: str,
        event_id: Optional[str],
        server_received_at: Optional[datetime],
    ) -> bool:
        """Write ``status`` and its bookkeeping onto ``drivers_current``.

        Returns ``True`` when the projection landed and ``False`` when it did
        not, rather than raising: the caller has to distinguish "the transition
        failed" from "the transition happened and the copy lags" (R13.18), and an
        exception here would erase that distinction.
        """
        updates: Dict[str, Any] = {
            "status": new_status,
            "duty_status_event_id": event_id,
            "duty_status_updated_at": _iso(server_received_at),
        }

        try:
            if self._driver_repository is not None:
                updated = await self._write_through_repository(
                    tenant_id=tenant_id,
                    driver_id=driver_id,
                    new_status=new_status,
                    event_id=event_id,
                    server_received_at=server_received_at,
                    updates=updates,
                )
                if updated is None:
                    logger.error(
                        "DutyStatusService: no drivers_current record to "
                        "project onto for tenant=%s driver=%s event=%s",
                        tenant_id,
                        driver_id,
                        event_id,
                    )
                    return False
                return True

            es = self._require_es()
            await es.update_document(DRIVERS_CURRENT_INDEX, driver_id, updates)
            return True
        except AppException as exc:
            logger.error(
                "DutyStatusService: projection write failed for tenant=%s "
                "driver=%s event=%s: %s",
                tenant_id,
                driver_id,
                event_id,
                exc.error_code,
            )
            return False
        except Exception as exc:
            logger.error(
                "DutyStatusService: projection write failed for tenant=%s "
                "driver=%s event=%s: %s",
                tenant_id,
                driver_id,
                event_id,
                exc,
            )
            return False

    async def _write_through_repository(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        new_status: str,
        event_id: Optional[str],
        server_received_at: Optional[datetime],
        updates: Dict[str, Any],
    ) -> Any:
        """Write the projection through the repository's sanctioned entry point.

        ``DriverRepository.project_duty_status`` is the only method permitted to
        write ``drivers_current.status``; ``DriverRepository.update`` refuses the
        field outright so no second write path exists (R13.16). The fallback to
        ``update`` covers a repository that predates that split — including the
        test doubles that expose only ``update`` — and is why this service does
        not simply assume the newer surface.
        """
        project = getattr(self._driver_repository, "project_duty_status", None)
        if callable(project):
            return await project(
                tenant_id,
                driver_id,
                status=new_status,
                event_id=event_id,
                updated_at=_iso(server_received_at),
            )
        return await self._driver_repository.update(
            tenant_id, driver_id, updates
        )

    # ------------------------------------------------------------------
    # Internals — validation
    # ------------------------------------------------------------------

    def _require_es(self):
        """Return the Elasticsearch service or fail loudly."""
        if self._es_service is None:
            raise RuntimeError(
                "DutyStatusService has no es_service. Pass one from "
                "configure_duty_status_endpoints() during startup."
            )
        return self._es_service

    @staticmethod
    def _require_text(value: Any, field: str) -> str:
        """Return a stripped non-empty string or raise ``ValueError``."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _validated_status(new_status: Any) -> str:
        """Return one of the four ``DriverStatus`` values (R13.1)."""
        status = new_status.strip() if isinstance(new_status, str) else new_status
        if status not in DUTY_STATUSES:
            raise invalid_request(
                message="new_status must be a duty status value",
                details={
                    "new_status": new_status,
                    "allowed_statuses": list(DUTY_STATUSES),
                },
            )
        return status

    @staticmethod
    def _validated_source(source: Any) -> str:
        """Return one of ``driver``, ``admin``, ``system`` (R13.12)."""
        value = source.strip() if isinstance(source, str) else source
        if value not in DUTY_STATUS_SOURCES:
            raise invalid_request(
                message="source must be driver, admin, or system",
                details={
                    "source": source,
                    "allowed_sources": list(DUTY_STATUS_SOURCES),
                },
            )
        return value

    @staticmethod
    def _validated_event_timestamp(
        event_timestamp: Any, *, fallback: datetime
    ) -> datetime:
        """Parse the client-asserted timestamp, defaulting to the server clock."""
        if event_timestamp is None or (
            isinstance(event_timestamp, str) and not event_timestamp.strip()
        ):
            return fallback
        parsed = _parse_timestamp(event_timestamp)
        if parsed is None:
            raise invalid_request(
                message="event_timestamp must be an ISO-8601 timestamp",
                details={"event_timestamp": event_timestamp},
            )
        return parsed


__all__ = [
    "ADMIN_ONLY_STATUS",
    "BLOCKING_ORDER_STATUS",
    "DRIVER_SETTABLE_STATUSES",
    "DUTY_STATUSES",
    "DUTY_STATUS_SOURCES",
    "HISTORY_MAX_EVENTS",
    "DutyStatusService",
    "duty_status_event_doc_id",
    "new_ulid",
]
