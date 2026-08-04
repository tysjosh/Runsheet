"""
``InspectionService`` — driver-submitted vehicle inspection intake.

One report, one document, one id: ``{tenant_id}:{inspection_id}`` on
``vehicle_inspections`` (R8.3). The tenant is always the verified
``TenantContext.tenant_id`` and the driver is always the verified session's
``driver_id``, so a report cannot be filed against another tenant or another
driver — the composite id makes that a property of the write rather than a check
inside it.

What the report carries, and what each part is for:

* ``asset_id`` and ``odometer_miles`` — the inspected vehicle and its reading in
  **miles**, which is what the mapping's ``odometer_miles`` double means; the
  driver surface is US-units throughout (R8.3).
* ``inspection_timestamp`` — the **client's** stamp for when the driver walked
  the vehicle, which may predate the server's receipt by the length of an
  offline queue drain. ``server_received_at`` is stamped here alongside it, so
  the two are never conflated. Retention is measured from the client stamp.
* ``inspection_local_date`` — the calendar day in the tenant's timezone,
  **precomputed by the client**. It is stored as a keyword so the Phase 2
  "first transition in a calendar day" gate is one term filter rather than a
  range query plus a timezone calculation. Absent, it is derived from the UTC
  date of ``inspection_timestamp``, which is the only day the server can name
  without knowing the tenant's zone.
* ``defects`` — zero or more entries, each carrying a ``component`` from
  :data:`INSPECTION_COMPONENTS`, a ``severity`` of ``minor`` or
  ``out_of_service``, a free-text ``note``, and zero or more photo ``file_ref``
  values (R8.4). Every ``file_ref`` is validated against the caller's tenant
  prefix through ``FileStorageService.validate_ref`` — the same validator the
  POD and exception surfaces use, not a second implementation (R15.8).

Ordering is deliberate: **every** validation, including the tenant-prefix check
on every ``file_ref``, completes before the single ``index_document`` call. A
report that names one foreign artifact leaves nothing behind.

``has_out_of_service_defect`` is denormalized onto the document because the
unconditional gate reads it as a term filter rather than as a nested query on
every transition (R8.5, R8.6).

Three effects follow an accepted report, and all three are **unconditional**:

* ``expires_at`` is stamped at ``inspection_timestamp`` + 15 months on *every*
  accepted report, in every tenant (R8.9). The client's stamp is what retention
  is measured from, not the server's receipt — a report drained from an offline
  queue a day late still expires 15 months after the walk-around.
* A report carrying at least one ``out_of_service`` defect sets the inspected
  asset's ``operational_state`` to ``out_of_service`` on the asset index (R8.5).
* The same report broadcasts an ``asset_out_of_service`` escalation to the
  dispatcher WebSocket channel, so the operator sees the stopped asset without
  polling (R8.5).

:meth:`InspectionService.is_asset_out_of_service` is the first read side, and it
is the one the unconditional transition gate calls: a term filter on
``vehicle_inspections.has_out_of_service_defect`` scoped to the tenant and the
asset, which is why the boolean is denormalized in the first place. There is no
return-to-service path in Phase 1 — no requirement defines one — so an asset
that has ever been reported out of service stays gated until a Phase 2
maintenance clearance surface exists. That is stated here rather than left to be
inferred from the query.

:meth:`InspectionService.has_pretrip_inspection` is the second read side, and it
is the one the **flag-gated** pre-trip gate calls (R8.7): term filters on
``driver_id``, ``asset_id``, ``inspection_type``, and the precomputed
``inspection_local_date`` keyword, so the "first transition in a calendar day"
rule is one term match rather than a timezone calculation on every transition.
Both reads re-validate ``tenant_id`` on each returned document behind
``inject_tenant_filter``, because a safety gate must not be able to reach a
verdict from another tenant's report.

**Exactly one flag read lives in this module, and it decides exactly one
question.** :meth:`InspectionService._post_trip_accepted` reads the overlay key
``driver.pretrip_inspection_required`` to decide whether an
``inspection_type: post_trip`` submission is accepted (R8.8). That is the second
and last of the two places R8.11 permits the flag to be consulted; the first is
the pre-trip transition gate in
``driver/services/order_transition_service.py``. The flag defaults to disabled
and fails closed to disabled when Redis is unavailable (R8.12).

Nothing else here reads it. A ``pre_trip`` report is accepted, the
out-of-service effect is applied, ``expires_at`` is stamped, and both read sides
answer the same way in every tenant, whatever the flag says (R8.11, R8.13) — so
a tenant that has not enabled the inspection workflow still records reports and
still stops an asset a driver has reported unsafe.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Data Models,
"New index: ``vehicle_inspections``".

Validates: Requirements 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9, 8.11, 8.12, 8.13,
15.8
- 8.3: a pre-trip report carrying the acting ``driver_id``, the asset, the
  odometer reading in miles, an inspection timestamp, and a defect list
- 8.4: each defect carries a component from the defined list, a severity of
  ``minor`` or ``out_of_service``, a note, and zero or more photo refs
- 8.5: an ``out_of_service`` defect sets the asset's operational state and
  broadcasts an escalation to the dispatcher channel, in every tenant
- 8.6: the read side the gate calls is one term filter on the denormalized
  boolean
- 8.7: the pre-trip existence read the flag-gated gate calls, keyed on the
  driver, the asset, and the precomputed ``inspection_local_date``
- 8.8: a ``post_trip`` report carries the same field set as a ``pre_trip`` one
  and is accepted where the tenant has enabled the workflow, the two
  distinguished by the ``inspection_type`` keyword
- 8.9: every accepted report carries ``expires_at`` at ``inspection_timestamp``
  + 15 months, in every tenant
- 8.11, 8.12, 8.13: intake, the out-of-service effect, and retention read no
  feature flag, so a tenant that has not enabled the inspection workflow still
  records reports and still stops the asset
- 15.8: every ``file_ref`` is tenant-prefix validated before anything persists
"""

from __future__ import annotations

import calendar
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from driver.services.driver_es_mappings import VEHICLE_INSPECTIONS_INDEX
from errors.exceptions import forbidden, internal_error, invalid_request
from ops.middleware.tenant_guard import inject_tenant_filter

logger = logging.getLogger(__name__)

#: The defined component list of R8.4. FMCSA §396.11 walk-around items plus the
#: cargo-tank items a fuel hauler inspects. A component outside this list is
#: 400 ``INVALID_REQUEST`` — ``other`` exists so a driver is never blocked from
#: filing, and the note carries what the vocabulary cannot.
INSPECTION_COMPONENTS: tuple[str, ...] = (
    "service_brakes",
    "parking_brake",
    "steering_mechanism",
    "lighting_devices",
    "tires",
    "wheels_and_rims",
    "horn",
    "windshield_wipers",
    "rear_vision_mirrors",
    "coupling_devices",
    "suspension",
    "frame_and_body",
    "exhaust_system",
    "fuel_system",
    "emergency_equipment",
    "fire_extinguisher",
    "cargo_tank_shell",
    "cargo_tank_valves",
    "hoses_and_fittings",
    "pump",
    "meter_and_register",
    "vapor_recovery",
    "bottom_loading_equipment",
    "grounding_equipment",
    "placards_and_markings",
    "other",
)

#: Defect severities (R8.4). ``out_of_service`` is the value that stops the
#: asset, unconditionally and in every tenant.
DEFECT_SEVERITIES: tuple[str, ...] = ("minor", "out_of_service")

#: The severity that stops an asset.
OUT_OF_SERVICE_SEVERITY: str = "out_of_service"

#: The ``inspection_type`` vocabulary (R8.8). ``pre_trip`` is accepted in every
#: tenant; ``post_trip`` is accepted only where the overlay flag is enabled.
INSPECTION_TYPES: tuple[str, ...] = ("pre_trip", "post_trip")

#: The type accepted unconditionally, in every tenant (R8.3).
PRE_TRIP: str = "pre_trip"

#: The type accepted only where ``PRETRIP_FLAG_KEY`` is enabled (R8.8).
POST_TRIP: str = "post_trip"

#: Overlay feature-flag key for the inspection workflow (R8.12). Read by
#: :meth:`InspectionService._post_trip_accepted` and by **nothing else in this
#: module** — this is the second and last of the exactly two places R8.11 permits
#: the flag to be consulted; the first is ``_pretrip_required`` in
#: ``driver/services/order_transition_service.py``. The literal is repeated there
#: rather than imported, because a service must not depend on the gate that calls
#: it; the two are asserted to agree by test.
PRETRIP_FLAG_KEY: str = "driver.pretrip_inspection_required"

#: Overlay states that mean "enabled". ``shadow`` observes without enforcing and
#: ``disabled`` is the default, so both leave the post-trip path closed.
_ENFORCING_OVERLAY_STATES: frozenset[str] = frozenset(
    {"active_gated", "active_auto"}
)

#: Guard on one report's defect list. A walk-around finds defects, not
#: thousands of them; this bounds a single strict-mapping nested write.
MAX_DEFECTS: int = 50

#: Guard on one defect's photo list.
MAX_PHOTOS_PER_DEFECT: int = 10

#: Upper bound on a plausible odometer reading, in miles. A tractor retires
#: well below this; the bound exists to catch a unit or transcription error at
#: the point of entry rather than in a report six months later.
MAX_ODOMETER_MILES: float = 5_000_000.0

#: Retention period for an inspection report (R8.9), in months from the
#: **client's** ``inspection_timestamp``. Months rather than days because the
#: requirement is written in months and 15 months is not a fixed number of days.
#: The sweep that acts on ``expires_at`` is the driver retention job's work; this
#: module only stamps the field.
INSPECTION_RETENTION_MONTHS: int = 15

#: The asset index. ``assets`` is an alias onto ``trucks`` (see
#: ``services/ref_loaders.py``); the concrete index name is used here because
#: this is a write path, and the two existing asset write paths in
#: ``data_endpoints.py`` write ``trucks`` directly.
ASSET_INDEX: str = "trucks"

#: The asset's operational state once a driver reports an out-of-service defect
#: (R8.5). Written to the declared ``operational_state`` keyword, which is
#: deliberately **not** the asset's ``status`` field: ``status`` carries movement
#: state (``idle``, ``in_transit``, ``maintenance``) and is overwritten by
#: tracking updates, so folding an operational verdict into it would lose the
#: verdict on the next position report.
ASSET_OUT_OF_SERVICE_STATE: str = "out_of_service"

#: Dispatcher-channel event type for the escalation (R8.5). The dispatcher
#: channel is the scheduling WebSocket manager, the same one
#: ``ExceptionReportService`` broadcasts ``exception_escalation`` on.
ASSET_OUT_OF_SERVICE_EVENT: str = "asset_out_of_service"


def _plus_months(moment: datetime, months: int) -> datetime:
    """Return ``moment`` advanced by ``months`` calendar months.

    The day of month is clamped to the target month's length, so a report filed
    on 31 March expires on 30 June rather than rolling into July. Written here
    rather than pulled from ``dateutil`` because it is six lines and the
    repository carries no month-arithmetic helper to reuse.
    """
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def retention_expires_at(inspection_timestamp: datetime) -> datetime:
    """Return the report's expiry: ``inspection_timestamp`` + 15 months (R8.9).

    Unconditional — no tenant, no flag, and no policy value is an input.
    """
    return _plus_months(inspection_timestamp, INSPECTION_RETENTION_MONTHS)


def inspection_doc_id(tenant_id: str, inspection_id: str) -> str:
    """Return the ``vehicle_inspections`` document id for one report.

    ``{tenant_id}:{inspection_id}`` — the tenant prefix is what keeps two
    tenants that mint the same ``inspection_id`` from colliding on one
    document.
    """
    return f"{tenant_id}:{inspection_id}"


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


class InspectionService:
    """Validates and persists one driver-submitted inspection report.

    Args:
        es_service: Anything exposing ``index_document(index, doc_id, document)``.
            Required — it is the store, and without it there is no report.
        file_storage_service: Supplies ``validate_ref``, which is what enforces
            the tenant prefix on a submitted photo ``file_ref`` (R15.8). It is
            required only once a report carries at least one ref: a report with
            no photos needs nothing validated, and a report with photos is
            refused rather than persisted with unvalidated references.
        feature_flag_service: Anything exposing
            ``get_overlay_state(key, tenant_id)``. Read in exactly one place —
            :meth:`_post_trip_accepted`, which decides whether a ``post_trip``
            submission is accepted (R8.8). Absent, the flag reads as disabled, so
            post-trip intake is refused while pre-trip intake, the
            out-of-service effect, and retention carry on unchanged in every
            tenant (R8.11, R8.12, R8.13).
        scheduling_ws_manager: The dispatcher-facing socket manager, exposing
            ``broadcast(event_type, event_data)`` — the same collaborator
            ``ExceptionReportService`` escalates over. Absent, the escalation is
            logged and the report, the asset state, and the gate are unaffected:
            a realtime frame nobody is listening for must not fail a driver's
            submission (R8.5).
    """

    def __init__(
        self,
        *,
        es_service,
        file_storage_service=None,
        feature_flag_service=None,
        scheduling_ws_manager=None,
    ) -> None:
        if es_service is None:
            raise ValueError("InspectionService requires an es_service")
        self._es_service = es_service
        self._file_storage_service = file_storage_service
        self._feature_flag_service = feature_flag_service
        self._scheduling_ws_manager = scheduling_ws_manager

    # -- the rule -------------------------------------------------------

    async def submit(
        self,
        tenant_id: str,
        driver_id: str,
        *,
        asset_id: str,
        odometer_miles: Any,
        inspection_timestamp: Any = None,
        inspection_local_date: Any = None,
        defects: Optional[Sequence[Any]] = None,
        inspection_type: str = PRE_TRIP,
    ) -> Dict[str, Any]:
        """Record one inspection report and return the persisted document.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The acting driver, from the verified session claim.
            asset_id: The inspected vehicle.
            odometer_miles: The odometer reading, in miles.
            inspection_timestamp: The client's ISO-8601 stamp for the
                inspection. Blank falls back to the server clock, which is the
                closest honest value available when the client sends none.
            inspection_local_date: The precomputed ``YYYY-MM-DD`` calendar day
                in the tenant's timezone. Absent, the UTC date of
                ``inspection_timestamp`` is used.
            defects: Zero or more defect entries, each a mapping (or a Pydantic
                model) carrying ``component``, ``severity``, ``note``, and
                ``photo_refs``.
            inspection_type: ``pre_trip`` or ``post_trip``. ``pre_trip`` is
                accepted in every tenant; ``post_trip`` carries the same field
                set and is accepted only where the tenant has enabled
                ``driver.pretrip_inspection_required`` (R8.8).

        Returns:
            The persisted ``vehicle_inspections`` document, carrying
            ``has_out_of_service_defect`` and the ``expires_at`` retention stamp.

        Raises:
            AppException: 400 ``INVALID_REQUEST`` for a blank asset, an
                unusable odometer reading, a malformed timestamp or local date,
                an unknown ``component`` or ``severity``, or a ``post_trip``
                submission in a tenant that has not enabled the workflow; 403
                ``FORBIDDEN`` for a photo ``file_ref`` outside the caller's
                tenant prefix; 500 ``INTERNAL_ERROR`` when refs are submitted
                with no file storage wired to validate them.

        Validates: Requirements 8.3, 8.4, 8.5, 8.8, 8.9, 8.11, 8.12, 8.13, 15.8
        """
        tenant_id = self._require_text(tenant_id, "tenant_id")
        driver_id = self._require_text(driver_id, "driver_id")
        asset_id = self._require_text(asset_id, "asset_id")
        report_type = await self._validated_inspection_type(
            inspection_type, tenant_id
        )
        odometer = self._validated_odometer(odometer_miles)

        server_received_at = datetime.now(timezone.utc)
        client_timestamp = self._validated_timestamp(
            inspection_timestamp, fallback=server_received_at
        )
        local_date = self._validated_local_date(
            inspection_local_date, fallback=client_timestamp
        )

        normalized_defects = self._validated_defects(defects)

        # R15.8 — every ref is checked before anything is written, so a report
        # naming one foreign artifact persists nothing at all.
        self._validate_photo_refs(
            tenant_id=tenant_id, actor=driver_id, defects=normalized_defects
        )

        inspection_id = str(uuid.uuid4())
        document = {
            "inspection_id": inspection_id,
            "tenant_id": tenant_id,
            "driver_id": driver_id,
            "asset_id": asset_id,
            "inspection_type": report_type,
            "odometer_miles": odometer,
            "inspection_timestamp": client_timestamp.isoformat(),
            "server_received_at": server_received_at.isoformat(),
            "inspection_local_date": local_date,
            "defects": normalized_defects,
            # Denormalized for the unconditional gate's term filter (R8.5,
            # R8.6).
            "has_out_of_service_defect": any(
                defect["severity"] == OUT_OF_SERVICE_SEVERITY
                for defect in normalized_defects
            ),
            # R8.9 — 15 months from the client's stamp, on every accepted
            # report, in every tenant. No flag and no policy value is an input.
            "expires_at": retention_expires_at(client_timestamp).isoformat(),
        }

        await self._es_service.index_document(
            VEHICLE_INSPECTIONS_INDEX,
            inspection_doc_id(tenant_id, inspection_id),
            document,
        )

        # R8.5 — the report is persisted first, because the denormalized
        # boolean on it is what the gate reads. The asset-state write and the
        # dispatcher escalation follow, and neither consults a flag.
        if document["has_out_of_service_defect"]:
            await self._apply_out_of_service_effect(
                tenant_id=tenant_id,
                driver_id=driver_id,
                asset_id=asset_id,
                document=document,
            )

        logger.info(
            "Inspection recorded tenant=%s driver=%s asset=%s type=%s "
            "defects=%d out_of_service=%s",
            tenant_id,
            driver_id,
            asset_id,
            report_type,
            len(normalized_defects),
            document["has_out_of_service_defect"],
        )
        return document

    # -- the unconditional out-of-service effect (R8.5) -----------------

    async def _apply_out_of_service_effect(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        asset_id: str,
        document: Dict[str, Any],
    ) -> None:
        """Stop the asset and tell the dispatcher, in every tenant (R8.5).

        Two effects, each isolated from the other and both isolated from the
        submission's outcome. The report is already persisted and its
        denormalized boolean is what the gate reads, so neither a failed asset
        write nor an unreachable socket may turn an accepted report into a
        rejected one — that would discard the driver's report *and* leave the
        asset moving. Each failure is logged loudly instead.

        Nothing here reads ``_feature_flag_service`` or any tenant policy value.

        Validates: Requirements 8.5, 8.11, 8.13
        """
        await self._set_asset_out_of_service(
            tenant_id=tenant_id, asset_id=asset_id
        )
        await self._broadcast_out_of_service(
            tenant_id=tenant_id,
            driver_id=driver_id,
            asset_id=asset_id,
            document=document,
        )

    async def _set_asset_out_of_service(
        self, *, tenant_id: str, asset_id: str
    ) -> bool:
        """Set the asset's ``operational_state`` to ``out_of_service``.

        Tenant ownership is confirmed by a tenant-filtered lookup before the
        partial update, which is the pattern the existing asset write path uses
        (``data_endpoints.py`` verify-then-``update_document``): the document id
        is the asset id, so an unfiltered update would let one tenant's report
        move another tenant's asset.

        Returns ``True`` when the state was written. An asset with no record in
        the asset index is a warning, not a failure — the driver may have typed
        an identifier the fleet does not carry, and the report still stands and
        still gates that identifier.

        Validates: Requirements 8.5
        """
        try:
            query = inject_tenant_filter(
                {"query": {"bool": {"filter": [{"term": {"_id": asset_id}}]}}},
                tenant_id,
            )
            query["size"] = 1
            response = await self._es_service.search_documents(
                ASSET_INDEX, query, 1
            )
            hits = (response or {}).get("hits", {}).get("hits", [])
            if not hits:
                logger.warning(
                    "Out-of-service defect reported against an asset with no "
                    "record: tenant=%s asset=%s — the report still gates that "
                    "asset",
                    tenant_id,
                    asset_id,
                )
                return False

            await self._es_service.update_document(
                ASSET_INDEX,
                hits[0].get("_id") or asset_id,
                {
                    "operational_state": ASSET_OUT_OF_SERVICE_STATE,
                    "last_update": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            logger.error(
                "Failed to set asset operational_state=out_of_service for "
                "tenant=%s asset=%s: %s — the report is persisted and the "
                "transition gate still blocks this asset",
                tenant_id,
                asset_id,
                exc,
            )
            return False

        logger.info(
            "Asset taken out of service by driver inspection: tenant=%s "
            "asset=%s",
            tenant_id,
            asset_id,
        )
        return True

    async def _broadcast_out_of_service(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        asset_id: str,
        document: Dict[str, Any],
    ) -> None:
        """Broadcast the escalation on the dispatcher channel (R8.5).

        The payload names the asset, the reporting driver, the report, and the
        components that stopped the asset — enough for a dispatcher to act
        without a follow-up read. Photo refs are omitted: the frame is a
        notification, and artifact access goes through the presign surface.
        """
        if self._scheduling_ws_manager is None:
            logger.warning(
                "No dispatcher channel wired — out-of-service escalation for "
                "tenant=%s asset=%s was not broadcast",
                tenant_id,
                asset_id,
            )
            return

        event_data = {
            "tenant_id": tenant_id,
            "asset_id": asset_id,
            "driver_id": driver_id,
            "inspection_id": document["inspection_id"],
            "operational_state": ASSET_OUT_OF_SERVICE_STATE,
            "inspection_timestamp": document["inspection_timestamp"],
            "defects": [
                {
                    "component": defect["component"],
                    "severity": defect["severity"],
                    "note": defect["note"],
                }
                for defect in document["defects"]
                if defect["severity"] == OUT_OF_SERVICE_SEVERITY
            ],
        }
        try:
            await self._scheduling_ws_manager.broadcast(
                ASSET_OUT_OF_SERVICE_EVENT, event_data
            )
        except Exception as exc:
            logger.warning(
                "Dispatcher broadcast failed for %s on tenant=%s asset=%s: %s",
                ASSET_OUT_OF_SERVICE_EVENT,
                tenant_id,
                asset_id,
                exc,
            )

    # -- the read side the transition gate calls (R8.6) -----------------

    async def is_asset_out_of_service(
        self, tenant_id: str, asset_id: str
    ) -> bool:
        """Whether any accepted report has taken this asset out of service.

        One tenant-filtered term query on the denormalized boolean — the reason
        it is denormalized (R8.6). No nested query, no flag read, no tenant
        policy read, and the same answer in every tenant.

        There is no return-to-service path in Phase 1, so this is an existence
        question rather than a "latest report wins" question: a clean walk-around
        by a driver is not a repair, and no requirement defines who may clear the
        state. Phase 2's maintenance surface is where that belongs.

        Args:
            tenant_id: The verified tenant scope.
            asset_id: The asset the transition's order is assigned to.

        Returns:
            ``True`` when at least one report in this tenant records an
            ``out_of_service`` defect against this asset.

        Raises:
            AppException: 500 ``INTERNAL_ERROR`` when the state cannot be read.
                Failing closed is deliberate: answering ``False`` on an
                unreadable index would let a vehicle a driver has reported unsafe
                move, and answering ``True`` would attribute a 409 to an asset
                that may be fine. Neither is honest, so neither is returned.

        Validates: Requirements 8.6, 8.11, 8.13
        """
        if not tenant_id or not asset_id:
            return False

        query = inject_tenant_filter(
            {
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"asset_id": asset_id}},
                            {"term": {"has_out_of_service_defect": True}},
                        ]
                    }
                }
            },
            tenant_id,
        )
        query["size"] = 1

        try:
            response = await self._es_service.search_documents(
                VEHICLE_INSPECTIONS_INDEX, query, 1
            )
        except Exception as exc:
            logger.error(
                "Asset out-of-service state unreadable for tenant=%s asset=%s: "
                "%s",
                tenant_id,
                asset_id,
                exc,
            )
            raise internal_error(
                message="The asset's out-of-service state cannot be read",
                details={
                    "reason": "asset_out_of_service_state_unreadable",
                    "asset_id": asset_id,
                },
            )

        hits = (response or {}).get("hits", {}).get("hits", [])
        return bool(hits)

    # -- the read side the flag-gated pre-trip gate calls (R8.7) ---------

    async def has_pretrip_inspection(
        self,
        tenant_id: str,
        driver_id: str,
        asset_id: str,
        local_date: str,
    ) -> bool:
        """Whether this driver filed a pre-trip report on this asset that day.

        The read behind the flag-gated pre-trip gate (R8.7). Four term filters
        on top of the tenant filter — ``driver_id``, ``asset_id``,
        ``inspection_type: pre_trip``, and ``inspection_local_date`` — which is
        exactly why ``inspection_local_date`` is stored as a precomputed keyword
        rather than derived from a range query over ``inspection_timestamp``:
        the tenant's calendar day is the client's to name, and the gate is one
        term match rather than a timezone calculation on every transition.

        The "first transition in a calendar day" wording of R8.7 falls out of
        this shape rather than needing a transition counter. A driver with no
        report for the day is blocked on the day's first ``in_transit``, files
        the report, and every later transition that day finds it. The gate never
        has to know which transition was the first one.

        A ``post_trip`` report does not satisfy the pre-trip requirement, so
        ``inspection_type`` is filtered rather than assumed — Phase 2 accepts
        both types onto this index (R8.8).

        **No feature flag is read here.** The flag lives in the gate that calls
        this method, in exactly one place (R8.11); this is a plain existence
        question with the same answer in every tenant.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The acting driver, from the verified session claim.
            asset_id: The asset the transition's order is assigned to.
            local_date: The driver's calendar day as ``YYYY-MM-DD``.

        Returns:
            ``True`` when at least one ``pre_trip`` report in this tenant names
            this driver, this asset, and this calendar day.

        Raises:
            AppException: 400 ``INVALID_REQUEST`` when ``local_date`` is not a
                ``YYYY-MM-DD`` calendar day — an unparseable day would silently
                match nothing and read as "no inspection filed"; 500
                ``INTERNAL_ERROR`` when the index cannot be read. Failing closed
                mirrors :meth:`is_asset_out_of_service`: answering ``True`` on
                an unreadable index would let an uninspected vehicle roll in a
                tenant that has asked for the check, and answering ``False``
                would blame a driver who may well have filed the report.

        Validates: Requirements 8.7, 8.11, 8.12
        """
        if not tenant_id or not driver_id or not asset_id:
            return False

        day = self._validated_local_date_filter(local_date)

        query = inject_tenant_filter(
            {
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"driver_id": driver_id}},
                            {"term": {"asset_id": asset_id}},
                            {"term": {"inspection_type": PRE_TRIP}},
                            {"term": {"inspection_local_date": day}},
                        ]
                    }
                }
            },
            tenant_id,
        )
        query["size"] = 1

        try:
            response = await self._es_service.search_documents(
                VEHICLE_INSPECTIONS_INDEX, query, 1
            )
        except Exception as exc:
            logger.error(
                "Pre-trip inspection state unreadable for tenant=%s driver=%s "
                "asset=%s date=%s: %s",
                tenant_id,
                driver_id,
                asset_id,
                day,
                exc,
            )
            raise internal_error(
                message="The driver's pre-trip inspection state cannot be read",
                details={
                    "reason": "pretrip_inspection_state_unreadable",
                    "asset_id": asset_id,
                    "inspection_local_date": day,
                },
            )

        hits = (response or {}).get("hits", {}).get("hits", [])
        return bool(self._own_tenant_sources(hits, tenant_id))

    @staticmethod
    def _own_tenant_sources(
        hits: Sequence[Any], tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Return only the hits whose stored ``tenant_id`` is the caller's.

        ``inject_tenant_filter`` is the first line of defence and this is the
        second: a hit whose document does not carry the caller's ``tenant_id``
        is dropped and logged rather than counted, so a filter that was ever
        bypassed cannot turn into a cross-tenant verdict on a safety gate.
        """
        own: List[Dict[str, Any]] = []
        for hit in hits or []:
            source = (hit or {}).get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "InspectionService: dropping inspection %s outside tenant "
                    "%s",
                    source.get("inspection_id"),
                    tenant_id,
                )
                continue
            own.append(source)
        return own

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

    async def _validated_inspection_type(self, value: Any, tenant_id: str) -> str:
        """Return the report type, opening ``post_trip`` behind the flag (R8.8).

        ``pre_trip`` is accepted in every tenant and reads no flag — that path
        must stay identical whatever the overlay says (R8.11, R8.13).
        ``post_trip`` is the one value whose acceptance is conditional: where a
        tenant has enabled ``driver.pretrip_inspection_required`` it is recorded
        with the same field set as a pre-trip report, and where the tenant has
        not it is refused with a reason that names why rather than silently
        stored. The vocabulary itself is unchanged — the mapping has always
        declared both values — so a tenant enabling the workflow needs no
        migration.

        Validates: Requirements 8.3, 8.8, 8.11, 8.12
        """
        text = str(value or PRE_TRIP).strip().lower()
        if text == PRE_TRIP:
            return PRE_TRIP
        if text == POST_TRIP:
            if await self._post_trip_accepted(tenant_id):
                return POST_TRIP
            raise invalid_request(
                message=(
                    "Post-trip inspection reports are not enabled for this "
                    "tenant"
                ),
                details={
                    "field": "inspection_type",
                    "reason": "post_trip_intake_not_enabled",
                    "allowed": [PRE_TRIP],
                },
            )
        raise invalid_request(
            message="Unknown inspection_type",
            details={
                "field": "inspection_type",
                "allowed": list(INSPECTION_TYPES),
            },
        )

    async def _post_trip_accepted(self, tenant_id: str) -> bool:
        """Whether this tenant has enabled the post-trip accept path (R8.12).

        The **only** flag read in this module, and it decides exactly one
        question: whether ``inspection_type: post_trip`` is accepted. It is not
        consulted by the pre-trip accept path, by the out-of-service effect, by
        the retention stamp, or by either read side (R8.11, R8.13).

        Defaults to disabled and fails closed to disabled: an absent
        ``feature_flag_service``, a service without ``get_overlay_state``, an
        unreachable Redis, or any read failure all mean "not enabled". Failing
        closed here is the safe direction — a refused post-trip report is a 400
        the driver can act on, whereas accepting one in a tenant that has not
        asked for the workflow would write a record nobody expects onto a
        strict-mapping index.
        """
        get_state = getattr(self._feature_flag_service, "get_overlay_state", None)
        if not callable(get_state):
            return False
        try:
            state = await get_state(PRETRIP_FLAG_KEY, tenant_id)
        except Exception as exc:
            logger.warning(
                "Inspection workflow flag unreadable for tenant=%s (%s) — "
                "treating as disabled, so post-trip intake stays closed",
                tenant_id,
                exc,
            )
            return False
        return state in _ENFORCING_OVERLAY_STATES

    @staticmethod
    def _validated_odometer(value: Any) -> float:
        """Return the odometer reading in miles as a non-negative float."""
        try:
            miles = float(value)
        except (TypeError, ValueError):
            raise invalid_request(
                message="odometer_miles must be a number of miles",
                details={"field": "odometer_miles"},
            )
        if miles != miles or miles in (float("inf"), float("-inf")):
            raise invalid_request(
                message="odometer_miles must be a finite number of miles",
                details={"field": "odometer_miles"},
            )
        if miles < 0 or miles > MAX_ODOMETER_MILES:
            raise invalid_request(
                message="odometer_miles is outside the plausible range",
                details={
                    "field": "odometer_miles",
                    "min": 0,
                    "max": MAX_ODOMETER_MILES,
                },
            )
        return miles

    @staticmethod
    def _validated_timestamp(value: Any, *, fallback: datetime) -> datetime:
        """Return the client's inspection timestamp, or the server clock."""
        if value is None or not str(value).strip():
            return fallback
        parsed = _parse_timestamp(value)
        if parsed is None:
            raise invalid_request(
                message="inspection_timestamp must be an ISO-8601 timestamp",
                details={"field": "inspection_timestamp"},
            )
        return parsed

    @staticmethod
    def _validated_local_date(value: Any, *, fallback: datetime) -> str:
        """Return the ``YYYY-MM-DD`` calendar day, deriving it when absent.

        The client precomputes this in the tenant's timezone. When it sends
        none, the UTC date of the inspection timestamp is used — the only day
        the server can name without knowing the tenant's zone.
        """
        text = "" if value is None else str(value).strip()
        if not text:
            return fallback.astimezone(timezone.utc).date().isoformat()
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            raise invalid_request(
                message="inspection_local_date must be a YYYY-MM-DD calendar day",
                details={"field": "inspection_local_date"},
            )
        return parsed.isoformat()

    @staticmethod
    def _validated_local_date_filter(value: Any) -> str:
        """Return the ``YYYY-MM-DD`` day the pre-trip gate asks about (R8.7).

        Unlike :meth:`_validated_local_date` there is no fallback: the gate
        always names a day, and an unparseable one must not be turned into a
        term filter that quietly matches nothing and reads as "no inspection
        filed".
        """
        text = "" if value is None else str(value).strip()
        try:
            return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
        except ValueError:
            raise invalid_request(
                message="local_date must be a YYYY-MM-DD calendar day",
                details={"field": "local_date", "value": text},
            )

    def _validated_defects(
        self, defects: Optional[Sequence[Any]]
    ) -> List[Dict[str, Any]]:
        """Normalize and validate the defect list (R8.4).

        Each entry becomes exactly the four keys the nested mapping declares,
        so the ``dynamic: strict`` write cannot be surprised by a fifth.

        Validates: Requirements 8.4
        """
        entries = list(defects or [])
        if len(entries) > MAX_DEFECTS:
            raise invalid_request(
                message="Too many defects on one inspection report",
                details={"field": "defects", "max": MAX_DEFECTS},
            )

        normalized: List[Dict[str, Any]] = []
        for index, entry in enumerate(entries):
            data = self._as_mapping(entry, f"defects[{index}]")
            component = str(data.get("component") or "").strip().lower()
            if component not in INSPECTION_COMPONENTS:
                raise invalid_request(
                    message="Unknown defect component",
                    details={
                        "field": f"defects[{index}].component",
                        "allowed": list(INSPECTION_COMPONENTS),
                    },
                )
            severity = str(data.get("severity") or "").strip().lower()
            if severity not in DEFECT_SEVERITIES:
                raise invalid_request(
                    message="Unknown defect severity",
                    details={
                        "field": f"defects[{index}].severity",
                        "allowed": list(DEFECT_SEVERITIES),
                    },
                )
            photo_refs = self._validated_photo_ref_list(
                data.get("photo_refs"), index
            )
            note = data.get("note")
            normalized.append(
                {
                    "component": component,
                    "severity": severity,
                    "note": (str(note).strip() if note is not None else "") or "",
                    "photo_refs": photo_refs,
                }
            )
        return normalized

    @staticmethod
    def _as_mapping(entry: Any, field: str) -> Dict[str, Any]:
        """Normalize a defect entry (mapping or Pydantic model) into a dict."""
        if isinstance(entry, dict):
            return dict(entry)
        model_dump = getattr(entry, "model_dump", None)
        if callable(model_dump):
            return dict(model_dump(mode="python"))
        raise invalid_request(
            message="Each defect must be an object",
            details={"field": field},
        )

    @staticmethod
    def _validated_photo_ref_list(value: Any, defect_index: int) -> List[str]:
        """Return the defect's photo refs as a list of non-empty strings."""
        if value is None:
            return []
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise invalid_request(
                message="photo_refs must be a list of file_ref values",
                details={"field": f"defects[{defect_index}].photo_refs"},
            )
        refs = [str(ref).strip() for ref in value if str(ref or "").strip()]
        if len(refs) > MAX_PHOTOS_PER_DEFECT:
            raise invalid_request(
                message="Too many photos on one defect",
                details={
                    "field": f"defects[{defect_index}].photo_refs",
                    "max": MAX_PHOTOS_PER_DEFECT,
                },
            )
        return refs

    def _validate_photo_refs(
        self,
        *,
        tenant_id: str,
        actor: str,
        defects: Sequence[Dict[str, Any]],
    ) -> None:
        """Reject any photo ``file_ref`` that is not the caller's tenant's.

        ``FileStorageService.validate_ref`` — the same validator the POD and
        exception surfaces use — raises ``PermissionError`` on a foreign tenant
        prefix, which becomes 403 ``FORBIDDEN`` here. The check runs before the
        single write, so a cross-tenant reference leaves no partial report
        behind, and the rejection names the field rather than the ref's owner.

        Validates: Requirements 15.8
        """
        refs_to_validate: List[tuple[str, str]] = []
        for defect_index, defect in enumerate(defects):
            for ref_index, ref_value in enumerate(defect["photo_refs"]):
                refs_to_validate.append(
                    (
                        f"defects[{defect_index}].photo_refs[{ref_index}]",
                        ref_value,
                    )
                )

        if not refs_to_validate:
            return

        file_storage = self._require_file_storage()
        for field_name, ref_value in refs_to_validate:
            try:
                file_storage.validate_ref(
                    tenant_id=tenant_id,
                    file_ref=ref_value,
                    actor=actor,
                )
            except PermissionError as exc:
                logger.warning(
                    "Cross-tenant inspection file_ref denied: tenant=%s "
                    "field=%s err=%s",
                    tenant_id,
                    field_name,
                    exc,
                )
                raise forbidden(
                    message="Cross-tenant file_ref denied",
                    details={
                        "reason": "cross_tenant_file_ref",
                        "field": field_name,
                    },
                )
            except ValueError as exc:
                raise invalid_request(
                    message="Invalid file_ref",
                    details={"field": field_name, "reason": str(exc)},
                )

    def _require_file_storage(self):
        """Return the file storage service, failing closed when it is absent.

        A report carrying photo refs cannot be persisted without the validator
        that enforces the tenant prefix — accepting it would store references
        nobody checked.
        """
        if self._file_storage_service is None:
            logger.error(
                "InspectionService has no file_storage_service; a report "
                "carrying photo refs cannot be validated"
            )
            raise internal_error(
                message="Inspection photo references cannot be validated",
                details={"reason": "file_storage_service_not_configured"},
            )
        return self._file_storage_service


__all__ = [
    "InspectionService",
    "INSPECTION_COMPONENTS",
    "DEFECT_SEVERITIES",
    "INSPECTION_TYPES",
    "OUT_OF_SERVICE_SEVERITY",
    "PRE_TRIP",
    "POST_TRIP",
    "PRETRIP_FLAG_KEY",
    "MAX_DEFECTS",
    "MAX_PHOTOS_PER_DEFECT",
    "INSPECTION_RETENTION_MONTHS",
    "ASSET_INDEX",
    "ASSET_OUT_OF_SERVICE_STATE",
    "ASSET_OUT_OF_SERVICE_EVENT",
    "inspection_doc_id",
    "retention_expires_at",
]
