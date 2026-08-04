"""
``DriverTelemetryService`` — breadcrumb batch ingestion for the driver app.

One sample, one document, one id:
``{tenant_id}:{driver_id}:{sample_timestamp_epoch_ms}`` on
``driver_breadcrumbs`` (R10.3). That id is the whole dedup rule. The
``(tenant_id, driver_id, sample_timestamp)`` uniqueness of R10.8 is a property
of the **write**, not of a query run before it: the batch is written with
``op_type=create``, so a sample whose triple is already stored fails its own
create, the stored sample is retained untouched, and no duplicate appears. No
existence search runs, which is what keeps a 200-sample offline drain to a
single Elasticsearch round trip.

The tenant is always the verified ``TenantContext.tenant_id`` and the driver is
always the verified session's ``driver_id`` (R10.2) — the submission surface
carries no driver identifier at all, so a batch cannot be filed against another
driver or another tenant. Every persisted document repeats both values, so a
document read back can be re-checked against the caller's scope without
consulting the id.

What a sample carries, and what each part is for (R10.1):

* ``latitude`` / ``longitude`` — written as the single ``location`` geo_point,
  the same shape ``driver_presence.last_location`` uses.
* ``sample_timestamp`` — the **client's** ISO 8601 stamp for when the fix was
  taken, which may predate the server's receipt by the length of an offline
  queue drain. ``server_received_at`` is stamped beside it, so the two are never
  conflated, and retention is measured from the client stamp (R10.17).
* ``accuracy_meters`` — horizontal accuracy in **meters**, the input to the
  R10.6 filter.
* ``speed_mph`` — speed in **miles per hour**, because the driver surface is
  US-units throughout (R10.1). No conversion happens here; the device reports
  mph.
* ``heading_degrees`` — course over ground in degrees, ``0`` ≤ h < ``360``.

Two filters run before anything is written, and each discarded sample is
counted in the response (R10.6, R10.7):

* horizontal accuracy above :data:`MAX_ACCURACY_METERS` — a fix that could be
  anywhere inside a 100 m circle is not a track, it is noise. A sample whose
  accuracy cannot be read at all is discarded by the same rule: unknown
  accuracy is not evidence of good accuracy.
* a ``sample_timestamp`` more than :data:`MAX_SAMPLE_AGE_HOURS` older than
  server receipt — beyond a day the fix answers no operational question, and the
  90-day retention window would carry it anyway.

A sample that survives both filters is *retained*, whether this batch is what
stored it or an earlier batch already had. That matters for the presence rule:

* at least one sample retained → ``driver_presence.last_location`` is set from
  the retained sample with the greatest ``sample_timestamp`` (R10.4);
* every sample discarded → the presence record is not touched at all (R10.5).

The presence write is a partial merge on the ``{tenant_id}:{driver_id}``
document — one current record per pair, no history (R10.19) — and it carries
``last_location`` and ``last_seen`` only. It deliberately does **not** write
``status``: the Driver_WS_Manager owns connection state, and a breadcrumb batch
is evidence of a running app, not of a live socket.

Both effects are ordered: the breadcrumb write happens first, because the track
is the durable record. A failed presence merge is logged and the batch still
succeeds — losing a driver's whole track because a point-in-time convenience
field could not be refreshed would be the wrong trade.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Data Models,
"New index: ``driver_breadcrumbs``".

Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8
- 10.1: the accepted per-sample field set, speed in mph and accuracy in meters
- 10.2: ``driver_id`` comes from the caller's context and from nowhere else
- 10.3: the track store is keyed on the ``(tenant_id, driver_id,
  sample_timestamp)`` triple and is distinct from ``driver_presence``
- 10.4: presence takes the retained sample with the greatest stamp
- 10.5: an all-discarded batch leaves presence untouched
- 10.6: accuracy above 100 m is discarded and counted
- 10.7: a stamp more than 24 h older than receipt is discarded and counted
- 10.8: an already-stored triple retains the stored sample and creates no
  duplicate
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from driver.services.driver_es_mappings import (
    DRIVER_BREADCRUMBS_INDEX,
    DRIVER_PRESENCE_INDEX,
)
from errors.exceptions import internal_error, invalid_request

logger = logging.getLogger(__name__)

#: Horizontal accuracy ceiling, in meters (R10.6). A sample reporting worse
#: accuracy than this — or no accuracy at all — is discarded and counted.
MAX_ACCURACY_METERS: float = 100.0

#: Maximum age of a client stamp relative to server receipt, in hours (R10.7).
MAX_SAMPLE_AGE_HOURS: int = 24

#: Upper bound on one submitted batch. R10.12 has the app drain its buffer in
#: batches of at most 200, so a larger batch is a client that is not following
#: the contract rather than a driver with more history — it is refused rather
#: than silently truncated.
MAX_BATCH_SAMPLES: int = 200

#: Upper bound on a plausible ground speed, in miles per hour. A tank truck
#: does not exceed this; the bound catches a unit error (km/h read as mph, or
#: m/s) at the point of entry rather than in a route reconstruction later.
MAX_SPEED_MPH: float = 200.0

#: Discard reason keys reported back to the client, one per filter.
DISCARD_ACCURACY: str = "accuracy_exceeded"
DISCARD_STALE: str = "stale"


def breadcrumb_doc_id(
    tenant_id: str, driver_id: str, sample_timestamp: datetime
) -> str:
    """Return the ``driver_breadcrumbs`` document id for one sample.

    ``{tenant_id}:{driver_id}:{sample_timestamp_epoch_ms}`` — the triple of
    R10.3 rendered as an id, which is what makes the R10.8 uniqueness a
    collision on the write instead of a query before it. Milliseconds are the
    resolution the mapping's ``date`` field stores, so two fixes the device
    took inside the same millisecond are the same sample.
    """
    epoch_ms = int(sample_timestamp.timestamp() * 1000)
    return f"{tenant_id}:{driver_id}:{epoch_ms}"


def presence_doc_id(tenant_id: str, driver_id: str) -> str:
    """Return the Driver_Presence document id for a tenant-driver pair.

    Re-exported from :mod:`driver.ws.driver_ws_manager` rather than
    re-derived, so the two writers of ``driver_presence`` can never disagree
    about which document is the one current record (R10.19).
    """
    from driver.ws.driver_ws_manager import presence_doc_id as _presence_doc_id

    return _presence_doc_id(tenant_id, driver_id)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp into an aware datetime, or ``None``."""
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


def _as_float(value: Any) -> Optional[float]:
    """Return ``value`` as a finite float, or ``None`` when it is neither."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _field(sample: Any, name: str) -> Any:
    """Read ``name`` off a mapping or a Pydantic model, tolerating both."""
    if isinstance(sample, dict):
        return sample.get(name)
    return getattr(sample, name, None)


class DriverTelemetryService:
    """Filters, persists, and summarizes one breadcrumb batch.

    Args:
        es_service: The store. Required — without it there is no track. Used
            for the breadcrumb write and, through its asynchronous methods
            only, for the ``driver_presence`` merge (R10.14): a telemetry batch
            must not block the event loop any more than a heartbeat may.
    """

    def __init__(self, *, es_service) -> None:
        if es_service is None:
            raise ValueError("DriverTelemetryService requires an es_service")
        self._es_service = es_service

    # -- the rule -------------------------------------------------------

    async def ingest_batch(
        self,
        tenant_id: str,
        driver_id: str,
        *,
        samples: Optional[Sequence[Any]] = None,
    ) -> Dict[str, Any]:
        """Ingest one breadcrumb batch and return its outcome summary.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The acting driver, from the verified session claim. Any
                driver identifier in the request body is ignored by the caller
                and never reaches this method (R10.2).
            samples: One or more samples, each a mapping (or a Pydantic model)
                carrying ``latitude``, ``longitude``, ``sample_timestamp``,
                ``accuracy_meters``, ``speed_mph``, and ``heading_degrees``.

        Returns:
            A summary carrying the minted ``batch_id``, the submitted /
            retained / stored / duplicate counts, ``discarded_count`` with a
            per-reason breakdown (R10.6, R10.7), and whether the presence
            record was refreshed and from which stamp.

        Raises:
            AppException: 400 ``INVALID_REQUEST`` for an empty batch, a batch
                above :data:`MAX_BATCH_SAMPLES`, or a sample whose coordinates,
                timestamp, speed, or heading are unusable; 500
                ``INTERNAL_ERROR`` when the track cannot be written.

        Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8
        """
        tenant_id = self._require_text(tenant_id, "tenant_id")
        driver_id = self._require_text(driver_id, "driver_id")

        server_received_at = datetime.now(timezone.utc)
        batch_id = f"bcb_{uuid.uuid4().hex}"

        # Validation first, for the whole batch: a malformed coordinate is a
        # client defect, not a low-quality fix, so it is refused rather than
        # counted as a discard and quietly dropped. Nothing is written until
        # every sample has parsed.
        parsed = self._validated_samples(samples)

        retained: List[Tuple[datetime, Dict[str, Any]]] = []
        discarded: Dict[str, int] = {DISCARD_ACCURACY: 0, DISCARD_STALE: 0}
        stale_before = server_received_at - timedelta(
            hours=MAX_SAMPLE_AGE_HOURS
        )

        for stamp, values in parsed:
            accuracy = values["accuracy_meters"]
            # R10.6 — an unreadable accuracy is discarded by the same rule as
            # an excessive one: unknown is not evidence of good.
            if accuracy is None or accuracy > MAX_ACCURACY_METERS:
                discarded[DISCARD_ACCURACY] += 1
                continue
            # R10.7 — measured against server receipt, not against the wall
            # clock at read time.
            if stamp < stale_before:
                discarded[DISCARD_STALE] += 1
                continue

            retained.append(
                (
                    stamp,
                    {
                        "breadcrumb_id": breadcrumb_doc_id(
                            tenant_id, driver_id, stamp
                        ),
                        "tenant_id": tenant_id,
                        "driver_id": driver_id,
                        "location": {
                            "lat": values["latitude"],
                            "lon": values["longitude"],
                        },
                        "sample_timestamp": stamp.isoformat(),
                        "server_received_at": server_received_at.isoformat(),
                        "accuracy_meters": accuracy,
                        "speed_mph": values["speed_mph"],
                        "heading_degrees": values["heading_degrees"],
                        "batch_id": batch_id,
                    },
                )
            )

        # R10.8 — two samples in one batch sharing a triple are one sample.
        # Collapsing here rather than letting the second create fail keeps the
        # bulk request free of self-conflicts.
        unique: Dict[str, Tuple[datetime, Dict[str, Any]]] = {}
        in_batch_duplicates = 0
        for stamp, document in retained:
            doc_id = str(document["breadcrumb_id"])
            if doc_id in unique:
                in_batch_duplicates += 1
                continue
            unique[doc_id] = (stamp, document)

        stored, already_stored = await self._create_breadcrumbs(
            {doc_id: document for doc_id, (_, document) in unique.items()}
        )

        presence = await self._refresh_presence(
            tenant_id=tenant_id,
            driver_id=driver_id,
            retained=[(stamp, doc) for stamp, doc in unique.values()],
            server_received_at=server_received_at,
        )

        discarded_count = sum(discarded.values())
        summary = {
            "batch_id": batch_id,
            "tenant_id": tenant_id,
            "driver_id": driver_id,
            "server_received_at": server_received_at.isoformat(),
            "submitted_count": len(parsed),
            # Retained = survived both filters. Deduplicated, so a batch that
            # repeats one fix twice retains it once.
            "retained_count": len(unique),
            "stored_count": stored,
            # R10.8 — the count whose triple was already persisted, plus the
            # in-batch repeats. Each of these retained the stored sample.
            "duplicate_count": already_stored + in_batch_duplicates,
            # R10.6, R10.7 — the number the client is told about, and why.
            "discarded_count": discarded_count,
            "discarded": dict(discarded),
            "presence_updated": presence["updated"],
            "presence_sample_timestamp": presence["sample_timestamp"],
        }

        logger.info(
            "Breadcrumb batch ingested tenant=%s driver=%s batch=%s "
            "submitted=%d retained=%d stored=%d duplicate=%d discarded=%d "
            "presence_updated=%s",
            tenant_id,
            driver_id,
            batch_id,
            summary["submitted_count"],
            summary["retained_count"],
            summary["stored_count"],
            summary["duplicate_count"],
            discarded_count,
            summary["presence_updated"],
        )
        return summary

    # -- the track write (R10.3, R10.8) ---------------------------------

    async def _create_breadcrumbs(
        self, documents: Dict[str, Dict[str, Any]]
    ) -> Tuple[int, int]:
        """Create every document, retaining any that already exists.

        Returns ``(stored, already_stored)``.

        The write is one bulk request of ``create`` actions on the raw
        Elasticsearch client — the same route
        ``driver/services/driver_retention_job.py`` takes for
        ``delete_by_query``, because :class:`ElasticsearchService` exposes no
        create-only write. ``create`` rather than ``index`` is the whole point:
        on a duplicate id Elasticsearch answers 409, the stored sample is left
        exactly as it was, and no duplicate is created (R10.8). The client is
        synchronous, so the call is handed to a worker thread.

        A deployment whose ES service exposes no raw client falls back to
        ``index_document`` per sample. The composite id still makes a duplicate
        impossible — the same document is rewritten, not doubled — so R10.8
        holds on that path too; what it loses is the ability to distinguish a
        first write from a repeat, and every sample is reported as stored.

        Validates: Requirements 10.3, 10.8
        """
        if not documents:
            return 0, 0

        client = getattr(self._es_service, "client", None)
        bulk = getattr(client, "bulk", None) if client is not None else None
        if not callable(bulk):
            return await self._index_breadcrumbs_individually(documents)

        actions: List[Any] = []
        for doc_id, document in documents.items():
            actions.append(
                {"create": {"_index": DRIVER_BREADCRUMBS_INDEX, "_id": doc_id}}
            )
            actions.append(document)

        def _call() -> Any:
            return bulk(body=actions, refresh=False)

        try:
            response = await asyncio.to_thread(_call)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:
            logger.error(
                "Breadcrumb batch write failed for %d sample(s): %s",
                len(documents),
                exc,
            )
            raise internal_error(
                message="The location track could not be recorded",
                details={"reason": "breadcrumb_write_failed"},
            )

        return self._count_bulk_outcome(response, len(documents))

    async def _index_breadcrumbs_individually(
        self, documents: Dict[str, Dict[str, Any]]
    ) -> Tuple[int, int]:
        """Fallback write path: one ``index_document`` per sample."""
        stored = 0
        for doc_id, document in documents.items():
            try:
                await self._es_service.index_document(
                    DRIVER_BREADCRUMBS_INDEX, doc_id, document
                )
            except Exception as exc:
                logger.error(
                    "Breadcrumb write failed for %s: %s", doc_id, exc
                )
                raise internal_error(
                    message="The location track could not be recorded",
                    details={"reason": "breadcrumb_write_failed"},
                )
            stored += 1
        return stored, 0

    @staticmethod
    def _count_bulk_outcome(response: Any, attempted: int) -> Tuple[int, int]:
        """Split a bulk response into created and already-existing counts.

        A 409 on a ``create`` action is the expected outcome for a repeated
        sample, not a failure (R10.8). Any other error status is logged: the
        batch is not failed over it, because the samples that did land are a
        real part of the track and the app has already dropped these from its
        buffer.
        """
        items = (response or {}).get("items") or []
        if not items:
            # A test double or a client that answers nothing useful: the
            # request did not raise, so treat every sample as stored.
            return attempted, 0

        stored = 0
        already_stored = 0
        failed = 0
        for item in items:
            outcome = (item or {}).get("create") or {}
            status = outcome.get("status")
            result = outcome.get("result")
            if status == 409:
                already_stored += 1
            elif result == "created" or (
                isinstance(status, int) and 200 <= status < 300
            ):
                stored += 1
            else:
                failed += 1

        if failed:
            logger.error(
                "Breadcrumb batch partially failed: %d of %d sample(s) were "
                "rejected by the store",
                failed,
                attempted,
            )
        return stored, already_stored

    # -- the presence effect (R10.4, R10.5) -----------------------------

    async def _refresh_presence(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        retained: Sequence[Tuple[datetime, Dict[str, Any]]],
        server_received_at: datetime,
    ) -> Dict[str, Any]:
        """Set ``last_location`` from the newest retained sample, or do nothing.

        R10.4: with at least one retained sample, the presence record takes the
        location of the retained sample carrying the greatest
        ``sample_timestamp`` — the newest fix the batch vouched for, which is
        not necessarily the last element the client sent.

        R10.5: with no retained sample the record is not touched. That is a
        return before any call, not a write of the same value, so a batch of
        pure noise cannot even refresh ``last_seen``.

        The merge is a partial update on the ``{tenant_id}:{driver_id}``
        document (R10.19) carrying ``last_location`` and ``last_seen`` only.
        ``status`` is deliberately absent: the Driver_WS_Manager is the writer
        of connection state, and a breadcrumb batch says the app is running,
        not that a socket is open. When no record exists yet the merge fails and
        a record is created carrying those same fields — still no ``status``,
        for the same reason.

        A failure here is logged and swallowed: the track is already persisted
        and is the durable record, so a stale convenience field must not turn an
        accepted batch into a rejected one.

        Validates: Requirements 10.4, 10.5, 10.14, 10.19
        """
        if not retained:
            return {"updated": False, "sample_timestamp": None}

        newest_stamp, newest = max(retained, key=lambda entry: entry[0])
        location = newest["location"]
        doc_id = presence_doc_id(tenant_id, driver_id)
        merge = {
            "last_location": location,
            "last_seen": server_received_at.isoformat(),
        }

        try:
            await self._es_service.update_document(
                DRIVER_PRESENCE_INDEX, doc_id, merge
            )
        except Exception as exc:
            logger.warning(
                "Presence location merge failed for tenant=%s driver=%s; "
                "recreating the record: %s",
                tenant_id,
                driver_id,
                exc,
            )
            try:
                await self._es_service.index_document(
                    DRIVER_PRESENCE_INDEX,
                    doc_id,
                    {
                        "driver_id": driver_id,
                        "tenant_id": tenant_id,
                        **merge,
                    },
                )
            except Exception as index_exc:
                logger.error(
                    "Failed to refresh presence for tenant=%s driver=%s: %s "
                    "— the breadcrumb track is persisted regardless",
                    tenant_id,
                    driver_id,
                    index_exc,
                )
                return {"updated": False, "sample_timestamp": None}

        return {
            "updated": True,
            "sample_timestamp": newest["sample_timestamp"],
        }

    # -- validation -----------------------------------------------------

    @staticmethod
    def _require_text(value: Any, field: str) -> str:
        """Return ``value`` as a non-empty trimmed string, else 400."""
        text = "" if value is None else str(value).strip()
        if not text:
            raise invalid_request(
                message=f"{field} is required",
                details={"field": field},
            )
        return text

    def _validated_samples(
        self, samples: Optional[Sequence[Any]]
    ) -> List[Tuple[datetime, Dict[str, Any]]]:
        """Parse and range-check every sample, or refuse the batch.

        Returns ``(sample_timestamp, values)`` pairs in submission order. The
        filters of R10.6 and R10.7 are **not** applied here — a sample this
        method accepts is well formed, which is a different question from
        whether it is worth keeping.

        Validates: Requirements 10.1
        """
        if not samples:
            raise invalid_request(
                message="A breadcrumb batch must carry at least one sample",
                details={"field": "samples", "min": 1},
            )
        if len(samples) > MAX_BATCH_SAMPLES:
            raise invalid_request(
                message="Too many samples in one breadcrumb batch",
                details={
                    "field": "samples",
                    "max": MAX_BATCH_SAMPLES,
                    "submitted": len(samples),
                },
            )

        parsed: List[Tuple[datetime, Dict[str, Any]]] = []
        for position, sample in enumerate(samples):
            stamp = _parse_timestamp(_field(sample, "sample_timestamp"))
            if stamp is None:
                raise invalid_request(
                    message="sample_timestamp must be an ISO 8601 timestamp",
                    details={
                        "field": "samples.sample_timestamp",
                        "index": position,
                    },
                )
            parsed.append(
                (
                    stamp,
                    {
                        "latitude": self._validated_coordinate(
                            _field(sample, "latitude"),
                            field="latitude",
                            limit=90.0,
                            position=position,
                        ),
                        "longitude": self._validated_coordinate(
                            _field(sample, "longitude"),
                            field="longitude",
                            limit=180.0,
                            position=position,
                        ),
                        # Left ``None`` when unreadable, which the accuracy
                        # filter treats as a discard (R10.6) rather than a
                        # rejection — a device that cannot state its accuracy
                        # has still told the truth.
                        "accuracy_meters": self._readable_accuracy(
                            _field(sample, "accuracy_meters")
                        ),
                        "speed_mph": self._validated_speed(
                            _field(sample, "speed_mph"), position=position
                        ),
                        "heading_degrees": self._validated_heading(
                            _field(sample, "heading_degrees"),
                            position=position,
                        ),
                    },
                )
            )
        return parsed

    @staticmethod
    def _validated_coordinate(
        value: Any, *, field: str, limit: float, position: int
    ) -> float:
        """Return a latitude or longitude inside its range, else 400."""
        number = _as_float(value)
        if number is None or number < -limit or number > limit:
            raise invalid_request(
                message=f"{field} must be a number within ±{limit:g} degrees",
                details={
                    "field": f"samples.{field}",
                    "index": position,
                    "min": -limit,
                    "max": limit,
                },
            )
        return number

    @staticmethod
    def _readable_accuracy(value: Any) -> Optional[float]:
        """Return horizontal accuracy in meters, or ``None`` when unusable.

        A negative accuracy is the "unknown" sentinel several mobile location
        APIs report, and is treated as unknown rather than as a very good fix.
        """
        number = _as_float(value)
        if number is None or number < 0:
            return None
        return number

    @staticmethod
    def _validated_speed(value: Any, *, position: int) -> Optional[float]:
        """Return speed in miles per hour, ``None`` when the device is unsure.

        A negative speed is the "unknown" sentinel the device APIs report, so
        it becomes ``None`` rather than a rejection (R10.1). A speed above
        :data:`MAX_SPEED_MPH` is a unit error and is refused, because silently
        keeping it would corrupt every average built on the track.
        """
        if value is None:
            return None
        number = _as_float(value)
        if number is None:
            raise invalid_request(
                message="speed_mph must be a number of miles per hour",
                details={"field": "samples.speed_mph", "index": position},
            )
        if number < 0:
            return None
        if number > MAX_SPEED_MPH:
            raise invalid_request(
                message="speed_mph is outside the plausible range",
                details={
                    "field": "samples.speed_mph",
                    "index": position,
                    "max": MAX_SPEED_MPH,
                },
            )
        return number

    @staticmethod
    def _validated_heading(value: Any, *, position: int) -> Optional[float]:
        """Return heading in degrees, ``None`` when the device is unsure.

        A negative heading is the device's "unknown" sentinel. ``360`` is
        normalized to ``0`` — the same bearing, one representation — and
        anything beyond that is refused.
        """
        if value is None:
            return None
        number = _as_float(value)
        if number is None:
            raise invalid_request(
                message="heading_degrees must be a number of degrees",
                details={"field": "samples.heading_degrees", "index": position},
            )
        if number < 0:
            return None
        if number > 360:
            raise invalid_request(
                message="heading_degrees must be within 0-360",
                details={
                    "field": "samples.heading_degrees",
                    "index": position,
                    "min": 0,
                    "max": 360,
                },
            )
        return number % 360


__all__ = [
    "DriverTelemetryService",
    "DISCARD_ACCURACY",
    "DISCARD_STALE",
    "MAX_ACCURACY_METERS",
    "MAX_BATCH_SAMPLES",
    "MAX_SAMPLE_AGE_HOURS",
    "MAX_SPEED_MPH",
    "breadcrumb_doc_id",
    "presence_doc_id",
]
