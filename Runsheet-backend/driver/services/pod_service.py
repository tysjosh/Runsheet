"""
``PODSubmissionService`` — the whole POD submission rule, in one place.

Everything here was inline in ``submit_pod``
(``driver/api/pod_endpoints.py:676-1145``): artifact validation, refusal
handling, OTP verification, gallons resolution (including the meter-ticket
OCR fall-back), the hash-chain append, BOL finalization, the job-timeline
event append, and the WebSocket broadcast. It now runs below a resolved
:class:`~driver.services.work_ref.WorkRef`, so the job-keyed handler and the
order-keyed sibling (R5.20) share one implementation and cannot diverge on a
validation rule or an error code (R5.23, R7.19).

Collaborators arrive through the constructor from the same module globals
``configure_pod_endpoints`` already sets, plus ``order_service`` and
``order_repository`` (R5.24). There is no dependency-injection container, no
service locator, and no FastAPI ``Depends`` for collaborators.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Service interfaces
and §POD Flow.

Validates: Requirements 4.5, 4.6, 4.7, 5.7, 5.8, 5.14, 5.16, 5.17, 5.23, 5.24,
15.14
- 5.7: a non-refusal submission requires a signature and at least one photo
- 5.8: ``geotag`` is required — asserted by ``PODRequest.geotag`` being a
  non-optional :class:`~driver.models.GeoPoint`
- 5.14: a refusal requires a ``refusal_reason_code`` and makes
  ``signature_ref`` and ``photo_refs`` optional. **This is the bug fix**: the
  handler required ``photo_refs`` unconditionally at
  ``pod_endpoints.py:772-777``, outside the ``if not is_refusal`` guard that
  protected ``signature_ref``; both checks live inside the non-refusal branch
  here
- 5.16: the POD is appended to the tenant's hash chain and handed to the BOL
  finalizer
- 5.17: every supplied ``file_ref`` is validated against the caller's tenant
  prefix and a foreign prefix is 403 ``FORBIDDEN`` with no persistence
- 5.23, 5.24: one implementation, collaborators through the constructor
- 15.14: authorization rejections name the work reference only. The job
  assignment check itself now lives in
  :meth:`~driver.services.work_ref.WorkRefResolver.resolve_job`, whose
  ``forbidden`` details carry only ``job_id`` — the handler's
  ``requesting_driver`` / ``assigned_driver`` details
  (``pod_endpoints.py:721-727``) are gone

Audit-actor and broadcast-identity values are the canonical
``WorkRef.driver_id`` (``tenant.driver_id or tenant.user_id``, resolved by
``require_driver_identity``). That matters for the broadcast: a
``DriverWSManager`` connection is keyed on ``driver_id``, so the handler's
``_broadcast_pod_event(driver_id=tenant.user_id)`` (``:1130``) could never
reach the driver it was addressed to.

The four POD corrections are applied here (task 10.1, R5.9, R5.11, R5.12,
R5.15, R5.22):
- a resolved ``order_id`` that is absent or blank is 422
  ``POD_ORDER_REFERENCE_REQUIRED``, checked before any OCR call and long
  before ``canonicalize_pod`` could see ``order_id: ""``
- a non-refusal that resolves no ``delivered_gallons`` is 409
  ``POD_GALLONS_CONFIRMATION_REQUIRED`` (carrying the OCR diagnostic) when a
  meter ticket was supplied, and 422 ``DELIVERED_GALLONS_REQUIRED`` when none
  was, so a null gallon count never reaches the hash chain
- the three OTP failures are ``AppException`` raises with real status codes —
  422 ``OTP_REQUIRED``, 409 ``OTP_NOT_PROVISIONED``, 403
  ``OTP_VERIFICATION_FAILED`` — because the offline queue's disposition matrix
  is status-code driven and the former HTTP 200 bodies would dequeue a failed
  POD as successful
- a refusal records the literal ``0`` gallons with
  ``delivered_gallons_source = "refused"``

The write order is the contract (R4.5, R4.6, R4.7, task 10.2), and it is fixed
by one constraint: *a POD must survive a failed order transition so the
submission can be reconciled without re-capturing artifacts.* Hence

1. :meth:`PodHashChainWriter.persist` under ``pod_chain_lock:{tenant_id}`` —
   the durable write of record,
2. ``pod_bol_finalizer.maybe_generate``, documented never to raise,
3. the ``pod_submitted`` / ``delivery_refused`` timeline event and the
   broadcast,
4. ``OrderService.apply_status_transition`` to ``delivered``, or to ``failed``
   when the submission records a refusal — **last**, and not compensated.

When step 4 fails the POD row is already committed with its hash-chain links
intact; it is patched with ``pod_status_transition = "pending"`` and
``pod_status_transition_error = "<error_code>"`` and the response is a 409
carrying the transition's own error code plus the ``pod_id``, so a dispatcher
or a reconciliation job can finish the transition later. The order status is
the reconcilable side because the hash chain is append-only and cannot be
rolled back at all.

A refusal also writes ``refusal_reason_code`` onto the order document, so the
order carries its own failure reason without a join back to the POD (R4.6).
"""

import asyncio
import hmac
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Optional

from driver.models import PODRequest
from driver.services.driver_es_mappings import PROOF_OF_DELIVERY_INDEX
from driver.services.geo_utils import haversine_distance_meters
from driver.services.pod_otp_service import assert_otp_window_open
from driver.services.work_ref import WorkRef
from errors.codes import ErrorCode
from errors.exceptions import (
    AppException,
    delivered_gallons_required,
    forbidden,
    invalid_request,
    otp_not_provisioned,
    otp_required as otp_required_error,
    otp_verification_failed,
    pod_gallons_confirmation_required,
    pod_order_reference_required,
)
from scheduling.services.scheduling_es_mappings import TENANT_JOB_POLICIES_INDEX
from services.pod_hash_chain_writer import (
    PodChainLockTimeout,
    PodChainPersistenceError,
)

logger = logging.getLogger(__name__)

#: Default geotag radius in meters when the tenant declares no policy.
DEFAULT_POD_RADIUS_METERS = 500

#: Outer OCR budget enforced by this service. :class:`MeterTicketOCRService`
#: already applies its own 15-second per-call timeout; this is the
#: belt-and-braces guard so a misbehaving stub cannot stall a POD submission
#: past the documented budget (Req 4.2.6).
POD_OCR_HARD_TIMEOUT_SECONDS: float = 15.0

#: The eight ``DeliveryRefusalReason`` values, echoed in the rejection details
#: when a refusal arrives without a reason code (R5.14).
_REFUSAL_REASON_CODES: tuple[str, ...] = (
    "customer_refused",
    "customer_unavailable",
    "access_denied",
    "unsafe_site",
    "wrong_product",
    "insufficient_capacity",
    "payment_hold",
    "other",
)

#: Diagnostic reported on a 409 ``POD_GALLONS_CONFIRMATION_REQUIRED`` when a
#: meter ticket was supplied but no OCR attempt could be made at all (no OCR
#: backend wired). The driver still has to confirm the count, and the app has
#: to be told *something* about why.
OCR_DIAGNOSTIC_UNAVAILABLE: str = "ocr_unavailable"

#: Job/order document keys that may carry a provisioned delivery OTP, in the
#: order ``_extract_expected_otp`` reads them.
_OTP_KEYS: tuple[str, ...] = ("pod_otp", "delivery_otp", "otp_code", "expected_otp")

#: The order status a successful POD submission drives the order to (R4.5).
POD_DELIVERED_STATUS: str = "delivered"

#: The order status a POD submission recording a refusal drives the order to
#: (R4.6).
POD_REFUSED_STATUS: str = "failed"

#: Value written to ``proof_of_delivery.pod_status_transition`` when the POD is
#: durable but its order transition did not land (R4.7). Nothing compensates
#: it: a reconciliation job or a dispatcher completes the transition later.
POD_TRANSITION_PENDING: str = "pending"
POD_TRANSITION_COMPLETED: str = "completed"

#: Document keys that may carry the customer a POD signature must bind to.
_CUSTOMER_ID_KEYS: tuple[str, ...] = (
    "customer_id",
    "destination_customer_id",
    "account_id",
)


# ---------------------------------------------------------------------------
# Pure helpers (moved verbatim from driver/api/pod_endpoints.py)
# ---------------------------------------------------------------------------


def extract_expected_otp(work_doc: Optional[dict]) -> Optional[str]:
    """Return the expected delivery OTP provisioned on the work document.

    The OTP is provisioned when a delivery is dispatched and shared with the
    recipient out-of-band. Returns ``None`` when no OTP has been provisioned
    so callers can fail closed.
    """
    if not work_doc:
        return None
    for key in _OTP_KEYS:
        value = work_doc.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def extract_customer_identity(work_doc: Optional[dict]) -> Optional[str]:
    """Return the customer/account id a POD signature must bind to, if known."""
    if not isinstance(work_doc, dict):
        return None
    for key in _CUSTOMER_ID_KEYS:
        value = work_doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    customer = work_doc.get("customer")
    if isinstance(customer, dict):
        value = customer.get("customer_id") or customer.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def validate_geotag(
    geotag_lat: float,
    geotag_lng: float,
    dest_lat: float,
    dest_lng: float,
    radius_meters: float,
) -> bool:
    """Return True if geotag is within radius of destination (no mismatch)."""
    distance = haversine_distance_meters(geotag_lat, geotag_lng, dest_lat, dest_lng)
    return distance <= radius_meters


def _destination_from_document(work_doc: Optional[dict]) -> Optional[dict]:
    """Return ``{"lat", "lng"}`` for a work document, or ``None``.

    ES ``geo_point`` values are stored as ``{"lat": ..., "lon": ...}``; both
    spellings of the longitude key are accepted.
    """
    if not isinstance(work_doc, dict):
        return None
    dest = work_doc.get("destination_location") or work_doc.get("delivery_location")
    if not isinstance(dest, dict):
        return None
    return {"lat": dest.get("lat"), "lng": dest.get("lon", dest.get("lng"))}


def _as_order_document(order) -> dict:
    """Normalize a repository result into a plain mutable dict.

    ``FuelOrderRepository.get`` returns a ``FuelOrder``; fakes and the cutover
    read paths hand back a raw document. Both are accepted, exactly as
    :class:`~driver.services.work_ref.WorkRefResolver` accepts both, so the
    transition step depends on the shape of the data rather than on the class.
    """
    if isinstance(order, dict):
        return dict(order)
    model_dump = getattr(order, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    return dict(order)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class PODSubmissionService:
    """The whole POD business rule (R5.23).

    Collaborators are the same objects ``configure_pod_endpoints`` already
    stores in module globals, plus ``order_service`` and ``order_repository``
    (R5.24). Every one except ``es_service`` is optional, and an absent
    collaborator degrades exactly as the handler degrades today: no
    ``job_service`` means no timeline event and no job lookup, no
    ``pod_bol_finalizer`` means no BOL, no ``ocr_service`` means the driver's
    typed gallon count is the only source, and no ``pod_hash_chain_writer``
    means a loud error log plus an un-chained write so the POD is not lost.

    Args:
        es_service: Elasticsearch service. Required.
        job_service: Scheduling job service, for ``_append_event`` and
            ``_get_job_doc``.
        order_service: ``fuel`` ``OrderService``, the single writer of
            fuel-order status. Drives the final ``delivered`` / ``failed``
            transition (R4.5, R4.6). Absent, the POD still persists and the
            order is left for reconciliation with a loud log.
        order_repository: ``FuelOrderRepository``, read to obtain the order
            document the transition mutates on the job-keyed path (the
            order-keyed path already carries it on the ``WorkRef``).
        file_storage_service: Required once any ``file_ref`` is submitted —
            it is what enforces the tenant prefix (R5.17).
        pod_hash_chain_writer: :class:`PodHashChainWriter` (R5.16).
        pod_bol_finalizer: POD→BOL finalizer, documented never to raise.
        ocr_service: :class:`MeterTicketOCRService`.
        driver_ws_manager: ``DriverWSManager``, keyed on ``driver_id``.
        scheduling_ws_manager: Dispatcher-facing broadcast manager.
        redis_client: Held for parity with the module globals; the hash-chain
            writer receives its own client from ``configure_pod_endpoints``.
    """

    def __init__(
        self,
        *,
        es_service,
        job_service=None,
        order_service=None,
        order_repository=None,
        file_storage_service=None,
        pod_hash_chain_writer=None,
        pod_bol_finalizer=None,
        ocr_service=None,
        reconciliation_service=None,
        driver_ws_manager=None,
        scheduling_ws_manager=None,
        redis_client=None,
    ) -> None:
        self._es_service = es_service
        self._job_service = job_service
        self._order_service = order_service
        self._order_repository = order_repository
        self._file_storage_service = file_storage_service
        self._pod_hash_chain_writer = pod_hash_chain_writer
        self._pod_bol_finalizer = pod_bol_finalizer
        self._ocr_service = ocr_service
        self._reconciliation_service = reconciliation_service
        self._driver_ws_manager = driver_ws_manager
        self._scheduling_ws_manager = scheduling_ws_manager
        self._redis_client = redis_client

    # -- public API -----------------------------------------------------

    async def submit(
        self,
        ref: WorkRef,
        body: PODRequest,
        *,
        request_id: str,
    ) -> dict:
        """Submit proof of delivery for a resolved unit of work.

        Validate artifacts → resolve refusal → verify OTP → resolve gallons →
        append hash chain → finalize BOL → append event → broadcast → transition
        the order. The four writes run in that order and the transition is last,
        so a failed transition leaves a durable POD behind (R4.7).

        Args:
            ref: The resolved work reference. Assignment authorization has
                already happened in the resolver, so nothing here re-checks it.
            body: The validated request body.
            request_id: The ``RequestIDMiddleware`` request id, echoed on the
                response.

        Returns:
            ``{"data": <persisted pod document>, "request_id": ...}``, the
            job-keyed response shape unchanged (R5.20).

        Raises:
            AppException: on every rejection — ``invalid_request`` (400) for a
                missing refusal reason, a missing signature or photo, a missing
                signature ``customer_id``, or a malformed ``file_ref``;
                ``forbidden`` (403) for a customer-identity mismatch or a
                cross-tenant ``file_ref``; 422
                ``POD_ORDER_REFERENCE_REQUIRED`` for a blank ``order_id``; 422
                ``OTP_REQUIRED`` / 409 ``OTP_NOT_PROVISIONED`` / 403
                ``OTP_VERIFICATION_FAILED`` for the OTP failures; 409
                ``POD_GALLONS_CONFIRMATION_REQUIRED`` or 422
                ``DELIVERED_GALLONS_REQUIRED`` for an unresolved gallon count;
                503 / 500 when the hash chain cannot be written; 409 carrying
                the transition's own error code and the ``pod_id`` when the
                order transition fails after the POD is durable (R4.7).

        Validates: Requirements 4.5, 4.6, 4.7, 5.7, 5.8, 5.9, 5.11, 5.12, 5.14,
        5.15, 5.16, 5.17, 5.22, 5.23, 5.29
        """
        es = self._require_es()
        now = datetime.now(timezone.utc).isoformat()
        pod_id = str(uuid.uuid4())

        # The document the rule reads its context from: the job document on
        # the job-keyed path, the fuel order on the order-keyed path.
        work_doc = ref.job_doc if ref.kind == "job" else ref.order_doc
        # Audit actor and broadcast identity: the canonical driver id, which
        # ``require_driver_identity`` has already proven is present.
        actor = ref.driver_id

        artifacts = self._resolve_artifacts(body)
        refusal = self._resolve_refusal(body)
        self._validate_artifact_presence(artifacts, refusal)

        customer = self._validate_customer_identity(
            body=body,
            work_doc=work_doc,
            signature_ref=artifacts["signature_ref"],
        )

        self._validate_file_refs(
            tenant_id=ref.tenant_id,
            actor=actor,
            artifacts=artifacts,
        )

        # Before any OCR call and long before the hash chain: a POD that names
        # no order is un-attributable (R5.22).
        order_id = await self._resolve_order_id(ref)

        policies = await self._get_tenant_policies(ref.tenant_id)
        radius_meters = policies.get("pod_radius_meters", DEFAULT_POD_RADIUS_METERS)
        otp_required = policies.get("otp_required", False)

        otp_verified = self._verify_otp(
            body=body,
            ref=ref,
            work_doc=work_doc,
            otp_required=otp_required,
        )

        location_mismatch = await self._resolve_location_mismatch(
            ref=ref,
            body=body,
            work_doc=work_doc,
            radius_meters=radius_meters,
        )

        if refusal["is_refusal"]:
            # A refusal delivered nothing: the literal 0 is what the chain
            # entry records, and no meter ticket is read (R5.15).
            ocr_resolution = self._refused_gallons_result()
        else:
            ocr_resolution = await self._resolve_delivered_gallons(
                ref=ref,
                body=body,
                meter_ticket_ref=artifacts["meter_ticket_ref"],
                pod_id=pod_id,
                actor=actor,
            )
            self._require_delivered_gallons(
                ocr_resolution=ocr_resolution,
                meter_ticket_ref=artifacts["meter_ticket_ref"],
            )

        pod_doc = self._build_pod_document(
            ref=ref,
            body=body,
            pod_id=pod_id,
            actor=actor,
            artifacts=artifacts,
            refusal=refusal,
            customer=customer,
            ocr_resolution=ocr_resolution,
            otp_verified=otp_verified,
            location_mismatch=location_mismatch,
            order_id=order_id,
        )

        # Step 1 — the durable write. Everything after this point must leave
        # the POD in place (R4.7).
        pod_doc = await self._persist(es=es, tenant_id=ref.tenant_id, pod_doc=pod_doc)
        # Step 2 — the BOL, documented never to raise.
        bol_doc = await self._finalize_bol(
            tenant_id=ref.tenant_id,
            pod_doc=pod_doc,
            actor=actor,
        )
        # Step 3 — the timeline event and the broadcast.
        await self._append_event(
            ref=ref,
            body=body,
            pod_id=pod_id,
            actor=actor,
            refusal=refusal,
            otp_verified=otp_verified,
            location_mismatch=location_mismatch,
            now=now,
        )
        await self._broadcast(
            ref=ref,
            body=body,
            pod_id=pod_id,
            actor=actor,
            refusal=refusal,
            otp_verified=otp_verified,
            location_mismatch=location_mismatch,
            now=now,
        )
        # Step 4 — the order transition, last and uncompensated (R4.5-R4.7).
        transitioned_order = await self._apply_order_transition(
            ref=ref,
            body=body,
            order_id=order_id,
            refusal=refusal,
            pod_doc=pod_doc,
            bol_doc=bol_doc,
        )
        if not refusal["is_refusal"] and transitioned_order is not None:
            await self._compute_reconciliation(
                pod_doc=pod_doc,
                order=transitioned_order,
            )

        return {"data": pod_doc, "request_id": request_id}

    # -- collaborators --------------------------------------------------

    def _require_es(self):
        """Return the Elasticsearch service or fail loudly."""
        if self._es_service is None:
            raise RuntimeError(
                "PODSubmissionService has no es_service. Pass one from "
                "configure_pod_endpoints() during startup."
            )
        return self._es_service

    def _require_file_storage(self):
        """Return the file storage service or fail loudly.

        Only reached when the submission carries at least one ``file_ref``,
        which is the only case in which the tenant-prefix check (R5.17) has
        anything to validate.
        """
        if self._file_storage_service is None:
            raise RuntimeError(
                "PODSubmissionService has no file_storage_service. Pass one "
                "from configure_pod_endpoints() during startup."
            )
        return self._file_storage_service

    # -- artifacts and refusal -----------------------------------------

    @staticmethod
    def _resolve_artifacts(body: PODRequest) -> dict:
        """Normalize the artifact references on the request.

        ``file_ref`` values are preferred; the raw URLs stay accepted for
        backward compatibility but are deprecated (Req 4.1.4).
        """
        return {
            "signature_ref": (body.signature_ref or "").strip() or None,
            "photo_refs": [p for p in (body.photo_refs or []) if p],
            "meter_ticket_ref": (body.meter_ticket_ref or "").strip() or None,
            "legacy_signature_url": (body.signature_url or "").strip() or None,
            "legacy_photo_urls": [p for p in (body.photo_urls or []) if p],
        }

    @staticmethod
    def _resolve_refusal(body: PODRequest) -> dict:
        """Resolve the refusal branch and require a reason code (R5.14).

        Raises:
            AppException: ``invalid_request`` when a refusal carries no
                ``refusal_reason_code``.
        """
        is_refusal = bool(body.refused_delivery)
        reason_code = (
            body.refusal_reason_code.value
            if body.refusal_reason_code is not None
            else None
        )

        if is_refusal and not reason_code:
            raise invalid_request(
                message="refusal_reason_code is required for refused deliveries",
                details={
                    "missing": ["refusal_reason_code"],
                    "allowed_reason_codes": list(_REFUSAL_REASON_CODES),
                },
            )

        return {
            "is_refusal": is_refusal,
            "reason_code": reason_code,
            "note": (body.refusal_note or "").strip() or None,
            "status": "refused" if is_refusal else "submitted",
            "event_type": "delivery_refused" if is_refusal else "pod_submitted",
        }

    @staticmethod
    def _validate_artifact_presence(artifacts: dict, refusal: dict) -> None:
        """Require a signature and at least one photo on a non-refusal (R5.7).

        Both checks sit inside the non-refusal branch. The handler required
        ``photo_refs`` unconditionally (``pod_endpoints.py:772-777``), outside
        the guard that protected ``signature_ref``, so a refusal with no
        photograph — a customer who turned the truck away at the gate — was
        rejected. R5.14 makes both artifacts optional for a refusal.
        """
        if refusal["is_refusal"]:
            return

        if not artifacts["signature_ref"] and not artifacts["legacy_signature_url"]:
            raise invalid_request(
                message="signature is required",
                details={"missing": ["signature_ref"]},
            )
        if not artifacts["photo_refs"] and not artifacts["legacy_photo_urls"]:
            raise invalid_request(
                message="at least one photo is required",
                details={"missing": ["photo_refs"]},
            )

    @staticmethod
    def _validate_customer_identity(
        *,
        body: PODRequest,
        work_doc: Optional[dict],
        signature_ref: Optional[str],
    ) -> dict:
        """Bind a submitted signature to the work's customer, when known.

        Returns:
            ``{"submitted_customer_id", "expected_customer_id",
            "signature_customer_validated"}``.

        Raises:
            AppException: ``invalid_request`` when a signature is submitted
                without a ``customer_id``; ``forbidden`` when the submitted
                customer is not the work's customer.
        """
        expected_customer_id = extract_customer_identity(work_doc)
        submitted_customer_id = (body.customer_id or "").strip() or None
        signature_customer_validated = False

        if signature_ref and expected_customer_id:
            if not submitted_customer_id:
                raise invalid_request(
                    message="customer_id is required when submitting a signature_ref",
                    details={
                        "missing": ["customer_id"],
                        "reason": "signature_customer_identity_required",
                    },
                )
            if submitted_customer_id != expected_customer_id:
                raise forbidden(
                    message="Signature customer identity mismatch",
                    details={
                        "reason": "signature_customer_identity_mismatch",
                        "expected_customer_id": expected_customer_id,
                        "submitted_customer_id": submitted_customer_id,
                    },
                )
            signature_customer_validated = True

        return {
            "submitted_customer_id": submitted_customer_id,
            "expected_customer_id": expected_customer_id,
            "signature_customer_validated": signature_customer_validated,
        }

    def _validate_file_refs(
        self,
        *,
        tenant_id: str,
        actor: str,
        artifacts: dict,
    ) -> Optional[dict]:
        """Reject any ``file_ref`` that is not the caller's tenant's (R5.17).

        ``FileStorageService.validate_ref`` raises ``PermissionError`` on a
        foreign tenant prefix, which becomes 403 ``FORBIDDEN`` here — before
        anything is persisted, so a cross-tenant reference leaves no partial
        POD behind.
        """
        refs_to_validate: list[tuple[str, str]] = []
        if artifacts["signature_ref"]:
            refs_to_validate.append(("signature_ref", artifacts["signature_ref"]))
        for idx, ref_value in enumerate(artifacts["photo_refs"]):
            refs_to_validate.append((f"photo_refs[{idx}]", ref_value))
        if artifacts["meter_ticket_ref"]:
            refs_to_validate.append(("meter_ticket_ref", artifacts["meter_ticket_ref"]))

        if not refs_to_validate:
            return None

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
                    "Cross-tenant POD file_ref denied: tenant=%s field=%s ref=%s err=%s",
                    tenant_id,
                    field_name,
                    ref_value,
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

    # -- policies and OTP ----------------------------------------------

    async def _get_tenant_policies(self, tenant_id: str) -> dict:
        """Fetch tenant job policies from ES, returning defaults if not found.

        Returns a dict with keys ``pod_required``, ``pod_radius_meters``, and
        ``otp_required``. ``otp_required`` defaults to false (R5.31).
        """
        es = self._require_es()
        defaults = {
            "pod_required": False,
            "pod_radius_meters": DEFAULT_POD_RADIUS_METERS,
            "otp_required": False,
        }
        try:
            query = {
                "query": {"term": {"tenant_id": tenant_id}},
                "size": 1,
            }
            response = await es.search_documents(
                TENANT_JOB_POLICIES_INDEX, query, size=1
            )
            hits = response.get("hits", {}).get("hits", [])
            if hits:
                source = hits[0]["_source"]
                return {
                    "pod_required": source.get(
                        "pod_required", defaults["pod_required"]
                    ),
                    "pod_radius_meters": source.get(
                        "pod_radius_meters", defaults["pod_radius_meters"]
                    ),
                    "otp_required": source.get(
                        "otp_required", defaults["otp_required"]
                    ),
                }
        except Exception as exc:
            logger.warning(
                "Failed to fetch tenant policies for %s, using defaults: %s",
                tenant_id,
                exc,
            )
        return defaults

    @staticmethod
    def _otp_details(ref: WorkRef) -> dict:
        """Rejection details for an OTP failure: the work reference only.

        Never the submitted code, never the provisioned code, never the
        driver's identity (R15.14).
        """
        return {"work_kind": ref.kind, "work_id": ref.work_id}

    def _verify_otp(
        self,
        *,
        body: PODRequest,
        ref: WorkRef,
        work_doc: Optional[dict],
        otp_required: bool,
    ) -> bool:
        """Verify the submitted delivery OTP against the provisioned code.

        The comparison is constant-time so the code cannot be recovered
        through timing, and neither operand is logged. The posture is
        **fail closed**: when the tenant requires an OTP and the work carries
        none, the submission is rejected rather than rubber-stamped, because
        accepting any code there would defeat the control.

        Returns:
            ``True`` when a provisioned code was verified, ``False`` when the
            tenant does not require one.

        Raises:
            AppException: 422 ``OTP_REQUIRED`` when the tenant requires a code
                and none was submitted; 409 ``OTP_NOT_PROVISIONED`` when none
                was provisioned to verify against; 403
                ``OTP_VERIFICATION_FAILED`` on a mismatch. These were HTTP 200
                bodies carrying an ``error_code`` — a 200 is not the structured
                error envelope R15.10 requires, and the offline queue's
                status-code-driven disposition matrix would dequeue such a
                submission as successful and lose the delivery record (R11.14).
                Also 409 ``OTP_WINDOW_EXPIRED`` from
                :func:`~driver.services.pod_otp_service.assert_otp_window_open`
                when the right code arrives after its validity window closed
                (R5.29) — checked *after* the comparison, so a wrong code is
                never told that it was merely late.

        Validates: Requirements 5.9, 5.29, 15.10
        """
        if not otp_required:
            return False

        submitted_otp = (body.otp or "").strip()
        if not submitted_otp:
            raise otp_required_error(details=self._otp_details(ref))

        expected_otp = extract_expected_otp(work_doc)
        if not expected_otp:
            logger.warning(
                "POD OTP required but no expected OTP provisioned: "
                "tenant=%s work=%s:%s",
                ref.tenant_id,
                ref.kind,
                ref.work_id,
            )
            raise otp_not_provisioned(details=self._otp_details(ref))

        if not hmac.compare_digest(submitted_otp, str(expected_otp).strip()):
            logger.warning(
                "POD OTP mismatch: tenant=%s work=%s:%s driver=%s",
                ref.tenant_id,
                ref.kind,
                ref.work_id,
                ref.driver_id,
            )
            raise otp_verification_failed(details=self._otp_details(ref))

        # The code is the right code — but a code has a lifetime. The window
        # rule lives in ``pod_otp_service`` beside the generator so the two can
        # never disagree about what "valid" means (R5.29).
        assert_otp_window_open(work_doc)

        return True

    # -- geotag ---------------------------------------------------------

    async def _resolve_location_mismatch(
        self,
        *,
        ref: WorkRef,
        body: PODRequest,
        work_doc: Optional[dict],
        radius_meters: float,
    ) -> bool:
        """Return True when the POD geotag is outside the work's radius.

        A work document with no destination coordinates cannot produce a
        mismatch, which keeps the check non-blocking exactly as it is today.
        """
        destination = _destination_from_document(work_doc)
        if destination is None and ref.kind == "job" and self._job_service is not None:
            destination = await self._fetch_job_destination(ref)

        if (
            not destination
            or destination.get("lat") is None
            or destination.get("lng") is None
        ):
            return False

        return not validate_geotag(
            body.geotag.lat,
            body.geotag.lng,
            destination["lat"],
            destination["lng"],
            radius_meters,
        )

    async def _fetch_job_destination(self, ref: WorkRef) -> Optional[dict]:
        """Read the job's destination coordinates, or ``None`` on any failure."""
        try:
            job_doc = await self._job_service._get_job_doc(ref.work_id, ref.tenant_id)
        except Exception as exc:
            logger.warning(
                "Failed to fetch job destination for %s: %s", ref.work_id, exc
            )
            return None
        return _destination_from_document(job_doc)

    # -- gallons resolution --------------------------------------------

    async def _resolve_delivered_gallons(
        self,
        *,
        ref: WorkRef,
        body: PODRequest,
        meter_ticket_ref: Optional[str],
        pod_id: str,
        actor: str,
    ) -> dict:
        """Resolve ``delivered_gallons`` from the driver or the meter ticket.

        A cross-tenant ``meter_ticket_ref`` reaching the OCR read is 403
        ``FORBIDDEN`` — the same rejection :meth:`_validate_file_refs` already
        produced, kept so the two layers stay in lock-step (R5.17).
        """
        try:
            return await self._resolve_delivered_gallons_via_ocr(
                tenant_id=ref.tenant_id,
                meter_ticket_ref=meter_ticket_ref,
                driver_gallons=body.delivered_gallons,
                pod_id=pod_id,
                actor=actor,
            )
        except PermissionError as exc:
            logger.warning(
                "Cross-tenant meter_ticket_ref denied during OCR: "
                "tenant=%s ref=%s err=%s",
                ref.tenant_id,
                meter_ticket_ref,
                exc,
            )
            raise forbidden(
                message="Cross-tenant file_ref denied",
                details={
                    "reason": "cross_tenant_file_ref",
                    "field": "meter_ticket_ref",
                },
            )

    async def _resolve_delivered_gallons_via_ocr(
        self,
        *,
        tenant_id: str,
        meter_ticket_ref: Optional[str],
        driver_gallons: Optional[float],
        pod_id: str,
        actor: Optional[str],
    ) -> dict:
        """Drive the meter-ticket OCR pipeline during a POD submission.

        * A driver-entered ``delivered_gallons`` is authoritative and skips
          OCR entirely (source ``manual``, no ``ocr_error``).
        * With a ``meter_ticket_ref`` and no driver value,
          :meth:`MeterTicketOCRService.extract` runs inside a 15-second budget;
          a high-confidence extraction wins with source ``ocr`` (Req 4.2.4).
        * A timeout, a provider error, or ``requires_manual_review`` falls
          through to manual entry with a short ``ocr_error`` diagnostic
          (Req 4.2.5, 4.2.6).
        * No ``meter_ticket_ref`` and no wired OCR service are both pure
          manual defaults with no ``ocr_error``, because no attempt was made.

        ``PermissionError`` propagates so the caller can translate it to 403.
        """
        if driver_gallons is not None:
            return self._gallons_result(
                delivered_gallons=float(driver_gallons), source="manual"
            )

        if not meter_ticket_ref or self._ocr_service is None:
            return self._gallons_result(delivered_gallons=None, source="manual")

        try:
            result = await asyncio.wait_for(
                self._ocr_service.extract(
                    tenant_id=tenant_id,
                    file_ref=meter_ticket_ref,
                    pod_id=pod_id,
                    actor=actor,
                ),
                timeout=POD_OCR_HARD_TIMEOUT_SECONDS,
            )
        except PermissionError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "OCR outer timeout after %.1fs for pod_id=%s tenant=%s",
                POD_OCR_HARD_TIMEOUT_SECONDS,
                pod_id,
                tenant_id,
            )
            return self._gallons_result(
                delivered_gallons=None,
                source="manual",
                ocr_error="textract_timeout",
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "OCR call raised unexpectedly for pod_id=%s tenant=%s: %s",
                pod_id,
                tenant_id,
                exc,
            )
            return self._gallons_result(
                delivered_gallons=None,
                source="manual",
                ocr_error=f"ocr_error:{type(exc).__name__}",
            )

        ocr_result_id = getattr(result, "ocr_result_id", None)
        confidence = getattr(result, "confidence", None)
        extracted = getattr(result, "extracted_gallons", None)
        requires_review = getattr(result, "requires_manual_review", True)
        service_error = getattr(result, "error_details", None)

        if service_error:
            return self._gallons_result(
                delivered_gallons=None,
                source="manual",
                ocr_result_id=ocr_result_id,
                ocr_confidence=confidence,
                ocr_requires_manual_review=True,
                ocr_error=service_error,
            )
        if extracted is None or requires_review:
            return self._gallons_result(
                delivered_gallons=None,
                source="manual",
                ocr_result_id=ocr_result_id,
                ocr_confidence=confidence,
                ocr_requires_manual_review=True,
                ocr_error="requires_manual_review",
            )

        return self._gallons_result(
            delivered_gallons=float(extracted),
            source="ocr",
            ocr_result_id=ocr_result_id,
            ocr_confidence=confidence,
            ocr_requires_manual_review=False,
        )

    @classmethod
    def _refused_gallons_result(cls) -> dict:
        """The gallons record for a refusal: the literal ``0`` (R5.15).

        A refused delivery transferred nothing, so ``0`` is both the truthful
        value and the one ``canonicalize_pod`` needs — it raises on ``None``.
        No meter ticket is read: there is no ticket for a delivery that never
        happened, and reading one would let OCR contradict the refusal.
        """
        return cls._gallons_result(delivered_gallons=0, source="refused")

    @staticmethod
    def _require_delivered_gallons(
        *,
        ocr_resolution: dict,
        meter_ticket_ref: Optional[str],
    ) -> None:
        """Reject a non-refusal that resolved no gallon count (R5.11, R5.12).

        ``canonicalize_pod`` raises on a null ``delivered_gallons``, and the
        hash-chain writer's ``0.0`` default would silently record a delivery of
        nothing. Both are worse than a rejection the driver can answer:

        * a meter ticket was supplied but produced nothing usable — 409
          ``POD_GALLONS_CONFIRMATION_REQUIRED`` carrying the OCR diagnostic, so
          the app can prompt for the count and resubmit under the same
          idempotency key
        * no meter ticket was supplied at all — 422
          ``DELIVERED_GALLONS_REQUIRED``, because there was never anything to
          read

        Raises:
            AppException: 409 or 422 per the above.
        """
        delivered_gallons = ocr_resolution["delivered_gallons"]
        if delivered_gallons is not None:
            if math.isfinite(float(delivered_gallons)) and float(delivered_gallons) > 0:
                return
            raise invalid_request(
                message=(
                    "delivered_gallons must be greater than zero for a "
                    "completed delivery"
                ),
                details={
                    "field": "delivered_gallons",
                    "value": delivered_gallons,
                },
            )

        if meter_ticket_ref:
            raise pod_gallons_confirmation_required(
                details={
                    "missing": ["delivered_gallons"],
                    "ocr_error": (
                        ocr_resolution["ocr_error"] or OCR_DIAGNOSTIC_UNAVAILABLE
                    ),
                    "ocr_result_id": ocr_resolution["ocr_result_id"],
                    "ocr_confidence": ocr_resolution["ocr_confidence"],
                }
            )

        raise delivered_gallons_required(
            details={"missing": ["delivered_gallons"]},
        )

    @staticmethod
    def _gallons_result(
        *,
        delivered_gallons: Optional[float],
        source: str,
        ocr_result_id: Optional[str] = None,
        ocr_confidence: Optional[float] = None,
        ocr_requires_manual_review: Optional[bool] = None,
        ocr_error: Optional[str] = None,
    ) -> dict:
        """Build the gallons-resolution record persisted on the POD."""
        return {
            "delivered_gallons": delivered_gallons,
            "source": source,
            "ocr_result_id": ocr_result_id,
            "ocr_confidence": ocr_confidence,
            "ocr_requires_manual_review": ocr_requires_manual_review,
            "ocr_error": ocr_error,
        }

    # -- the POD document ----------------------------------------------

    async def _resolve_order_id(self, ref: WorkRef) -> str:
        """Resolve the ``order_id`` the POD is attributed to.

        On the order-keyed path this is the path parameter, already confirmed
        to exist in the caller's tenant with ``assigned_driver_id`` equal to
        the caller (R5.21) — no job document is consulted. On the job-keyed
        path it is the job's ``order_id``, re-read when the resolver could not
        carry the job document.

        Raises:
            AppException: 422 ``POD_ORDER_REFERENCE_REQUIRED`` when the
                resolved value is absent or blank (R5.22). ``canonicalize_pod``
                stringifies whatever it is handed, so a blank value used to
                produce a chain entry with ``order_id: ""`` — a POD attributable
                to nothing. That does tighten the job-keyed path: a job with no
                ``order_id`` now 422s where it previously wrote a bad chain
                entry. An un-attributable POD was never a valid response.

        Validates: Requirements 5.21, 5.22
        """
        resolved = ref.order_id
        if not resolved and (
            ref.kind == "job" and ref.job_doc is None and self._job_service is not None
        ):
            try:
                job_doc = await self._job_service._get_job_doc(
                    ref.work_id, ref.tenant_id
                )
                resolved = (job_doc or {}).get("order_id")
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Failed to re-read order_id for job %s: %s", ref.work_id, exc
                )
                resolved = None

        order_id = str(resolved or "").strip()
        if not order_id:
            raise pod_order_reference_required(
                details={
                    "missing": ["order_id"],
                    "work_kind": ref.kind,
                    "work_id": ref.work_id,
                }
            )
        return order_id

    @staticmethod
    def _build_pod_document(
        *,
        ref: WorkRef,
        body: PODRequest,
        pod_id: str,
        actor: str,
        artifacts: dict,
        refusal: dict,
        customer: dict,
        ocr_resolution: dict,
        otp_verified: bool,
        location_mismatch: bool,
        order_id: str,
    ) -> dict:
        """Build the ``proof_of_delivery`` document.

        The deprecated URL fields are echoed only when no ``file_ref`` was
        supplied for that artifact, so a file_ref submission never persists a
        stale URL (Req 4.1.4). ``driver_id`` records the canonical acting
        driver.
        """
        signature_ref = artifacts["signature_ref"]
        photo_refs = artifacts["photo_refs"]
        effective_signature_url = (
            artifacts["legacy_signature_url"] if not signature_ref else ""
        )
        effective_photo_urls = artifacts["legacy_photo_urls"] if not photo_refs else []

        return {
            "pod_id": pod_id,
            "job_id": ref.job_id,
            "order_id": order_id,
            "driver_id": actor,
            "recipient_name": body.recipient_name,
            "customer_id": customer["submitted_customer_id"],
            "expected_customer_id": customer["expected_customer_id"],
            "signature_customer_validated": customer["signature_customer_validated"],
            "signature_ref": signature_ref,
            "photo_refs": photo_refs,
            "meter_ticket_ref": artifacts["meter_ticket_ref"],
            "signature_url": effective_signature_url,
            "photo_urls": effective_photo_urls,
            "delivered_gallons": ocr_resolution["delivered_gallons"],
            "delivered_gallons_source": ocr_resolution["source"],
            "ocr_result_id": ocr_resolution["ocr_result_id"],
            "ocr_confidence": ocr_resolution["ocr_confidence"],
            "ocr_requires_manual_review": ocr_resolution[
                "ocr_requires_manual_review"
            ],
            "ocr_error": ocr_resolution["ocr_error"],
            "delivered_at": body.timestamp,
            "geotag": {"lat": body.geotag.lat, "lon": body.geotag.lng},
            "timestamp": body.timestamp,
            "otp_verified": otp_verified,
            "location_mismatch": location_mismatch,
            "status": refusal["status"],
            "refused_delivery": refusal["is_refusal"],
            "refusal_reason_code": refusal["reason_code"],
            "refusal_note": refusal["note"],
            "tenant_id": ref.tenant_id,
            # Persist as pending before any downstream work. If the process
            # exits after the immutable POD write, the repair worker can still
            # discover and complete the order transition.
            "pod_status_transition": POD_TRANSITION_PENDING,
        }

    # -- persistence, BOL, event, broadcast -----------------------------

    async def _persist(self, *, es, tenant_id: str, pod_doc: dict) -> dict:
        """Persist the POD with its hash-chain links (R5.16).

        The writer serializes concurrent submissions per tenant under
        ``pod_chain_lock:{tenant_id}``, reads the tenant's latest ``pod_hash``
        (or the zero-hash for the first POD), computes the new ``pod_hash``
        from the canonical payload, and writes under the lock. The persisted
        record replaces the pre-hash document for every downstream step, so
        the event and the response carry the hash fields.

        Raises:
            AppException: 503 when the chain lock cannot be acquired, 500 when
                the write fails. Neither leaves a partial POD.
        """
        pod_id = pod_doc["pod_id"]

        if self._pod_hash_chain_writer is None:
            # Fallback for a misconfigured deployment: write without a chain so
            # the POD is not lost. The loud log surfaces the misconfiguration;
            # hash verification will flag the POD as un-chained on first audit.
            logger.error(
                "POD hash-chain writer not configured — POD %s persisted "
                "without pod_hash / previous_pod_hash (tenant=%s). Pass "
                "pod_hash_chain_writer or es_service to "
                "configure_pod_endpoints() to enable hashing.",
                pod_id,
                tenant_id,
            )
            await es.index_document(PROOF_OF_DELIVERY_INDEX, pod_id, pod_doc)
            return pod_doc

        try:
            return await self._pod_hash_chain_writer.persist(
                tenant_id=tenant_id,
                pod_doc=pod_doc,
            )
        except PodChainLockTimeout as exc:
            logger.error(
                "POD hash-chain lock timeout for tenant=%s pod_id=%s: %s",
                tenant_id,
                pod_id,
                exc,
            )
            raise AppException(
                error_code=ErrorCode.SESSION_STORE_UNAVAILABLE,
                message="POD persistence is temporarily busy — please retry",
                status_code=503,
                details={
                    "reason": "pod_chain_lock_timeout",
                    "tenant_id": tenant_id,
                    "pod_id": pod_id,
                },
            ) from exc
        except PodChainPersistenceError as exc:
            logger.error(
                "POD hash-chain persistence failed for tenant=%s pod_id=%s: %s",
                tenant_id,
                pod_id,
                exc,
            )
            raise AppException(
                error_code=ErrorCode.INTERNAL_ERROR,
                message="Failed to persist POD record",
                status_code=500,
                details={
                    "reason": "pod_persistence_failed",
                    "tenant_id": tenant_id,
                    "pod_id": pod_id,
                },
            ) from exc

    async def _finalize_bol(
        self, *, tenant_id: str, pod_doc: dict, actor: str
    ):
        """Generate the Bill of Lading through the existing finalizer (R5.16).

        Gated per tenant by the ``overlay.bol_generation`` feature flag inside
        the finalizer, which is documented never to raise and to persist a
        ``pending_regeneration`` stub on failure. The extra guard here is a
        double safety net: a BOL failure must never block a persisted POD.
        """
        if self._pod_bol_finalizer is None:
            return None
        try:
            return await self._pod_bol_finalizer.maybe_generate(
                tenant_id=tenant_id,
                pod=pod_doc,
                actor=actor,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "POD BOL finalizer raised unexpectedly for pod_id=%s: %s",
                pod_doc.get("pod_id"),
                exc,
            )
            return None

    async def _append_event(
        self,
        *,
        ref: WorkRef,
        body: PODRequest,
        pod_id: str,
        actor: str,
        refusal: dict,
        otp_verified: bool,
        location_mismatch: bool,
        now: str,
    ) -> None:
        """Append ``pod_submitted`` / ``delivery_refused`` to the job timeline.

        ``actor_id`` is the canonical acting driver. A failure is logged and
        swallowed: the POD is already committed and must not be lost because a
        timeline write failed. Skipped on the order-keyed path, which has no
        job timeline.
        """
        if self._job_service is None or not ref.job_id:
            return None
        try:
            await self._job_service._append_event(
                job_id=ref.job_id,
                event_type=refusal["event_type"],
                tenant_id=ref.tenant_id,
                actor_id=actor,
                payload={
                    "pod_id": pod_id,
                    "recipient_name": body.recipient_name,
                    "location_mismatch": location_mismatch,
                    "otp_verified": otp_verified,
                    "status": refusal["status"],
                    "refused_delivery": refusal["is_refusal"],
                    "refusal_reason_code": refusal["reason_code"],
                    "timestamp": now,
                },
            )
        except Exception as exc:
            logger.warning(
                "Failed to append %s event for job %s: %s",
                refusal["event_type"],
                ref.job_id,
                exc,
            )

    async def _broadcast(
        self,
        *,
        ref: WorkRef,
        body: PODRequest,
        pod_id: str,
        actor: str,
        refusal: dict,
        otp_verified: bool,
        location_mismatch: bool,
        now: str,
    ) -> None:
        """Broadcast the POD event to dispatchers and to the acting driver."""
        event_data = {
            "job_id": ref.job_id,
            "order_id": ref.order_id,
            "pod_id": pod_id,
            "recipient_name": body.recipient_name,
            "location_mismatch": location_mismatch,
            "otp_verified": otp_verified,
            "status": refusal["status"],
            "refused_delivery": refusal["is_refusal"],
            "refusal_reason_code": refusal["reason_code"],
            "timestamp": now,
            "tenant_id": ref.tenant_id,
        }
        await self._broadcast_pod_event(
            refusal["event_type"],
            event_data,
            driver_id=actor,
            tenant_id=ref.tenant_id,
        )

    async def _broadcast_pod_event(
        self,
        event_type: str,
        event_data: dict,
        driver_id: Optional[str] = None,
        tenant_id: str = "",
    ) -> None:
        """Broadcast a POD event through both WS managers.

        ``driver_id`` is the canonical ``drivers_current.driver_id``, which is
        what a ``DriverWSManager`` connection is keyed on. The handler passed
        ``tenant.user_id`` here (``pod_endpoints.py:1130``), which could never
        match a driver-keyed connection, so the acting driver's own device
        never received its POD acknowledgement.

        A broadcast failure on either channel is logged and swallowed: the POD
        is already persisted.
        """
        if self._scheduling_ws_manager is not None:
            try:
                await self._scheduling_ws_manager.broadcast(
                    event_type,
                    event_data,
                    tenant_id=tenant_id or event_data.get("tenant_id", ""),
                )
            except Exception as exc:
                logger.warning(
                    "Scheduling WS broadcast failed for %s on work %s: %s",
                    event_type,
                    event_data.get("job_id") or event_data.get("order_id"),
                    exc,
                )

        if self._driver_ws_manager is not None:
            try:
                if driver_id and hasattr(self._driver_ws_manager, "send_to_driver"):
                    await self._driver_ws_manager.send_to_driver(
                        driver_id,
                        {"type": event_type, "data": event_data},
                    )
                elif hasattr(self._driver_ws_manager, "broadcast"):
                    await self._driver_ws_manager.broadcast(event_type, event_data)
            except Exception as exc:
                logger.warning(
                    "Driver WS broadcast failed for %s on work %s: %s",
                    event_type,
                    event_data.get("job_id") or event_data.get("order_id"),
                    exc,
                )

    # -- step 4: the order transition, last and uncompensated ------------

    async def _apply_order_transition(
        self,
        *,
        ref: WorkRef,
        body: PODRequest,
        order_id: str,
        refusal: dict,
        pod_doc: dict,
        bol_doc=None,
    ) -> Optional[dict]:
        """Drive the order to ``delivered`` / ``failed`` (R4.5, R4.6, R4.7).

        Runs after the POD is durable, the BOL has been attempted, and the
        event and broadcast have gone out, because it is the only step that may
        legitimately fail without taking the POD with it. ``OrderService`` is
        the single writer of fuel-order status, so the state-machine guard, the
        delivery-window guard, the order event, the driver counters, the
        broadcast, and the subscribers all run exactly as they do for a
        dispatcher-initiated transition (R4.1).

        A refusal carries its ``refusal_reason_code`` onto the order document
        as well, so the order states its own failure reason without a join back
        to the POD (R4.6).

        Degradations, all of which leave the POD intact and log loudly:
        no ``order_service`` wired, no readable order document. Neither is
        turned into a client-visible error — the POD is the record the driver
        cannot re-capture, and the order status is reconcilable.

        Raises:
            AppException: 409 carrying the transition's own error code, the
                ``pod_id``, and the target status, after patching the POD with
                ``pod_status_transition = "pending"`` (R4.7).

        Validates: Requirements 4.5, 4.6, 4.7
        """
        target_status = (
            POD_REFUSED_STATUS if refusal["is_refusal"] else POD_DELIVERED_STATUS
        )
        pod_id = pod_doc.get("pod_id")

        if self._order_service is None:
            logger.error(
                "No order_service wired — POD %s is persisted but order %s "
                "was not transitioned to %s (tenant=%s). Pass order_service "
                "to configure_pod_endpoints() during startup.",
                pod_id,
                order_id,
                target_status,
                ref.tenant_id,
            )
            await self._record_transition_failure(
                tenant_id=ref.tenant_id,
                pod_doc=pod_doc,
                error_code="ORDER_SERVICE_UNAVAILABLE",
            )
            return None

        order = await self._load_order_for_transition(ref=ref, order_id=order_id)
        if order is None:
            logger.error(
                "Order %s could not be read for the POD transition — POD %s "
                "is persisted and the order is left at its current status "
                "(tenant=%s)",
                order_id,
                pod_id,
                ref.tenant_id,
            )
            await self._record_transition_failure(
                tenant_id=ref.tenant_id,
                pod_doc=pod_doc,
                error_code="ORDER_NOT_FOUND",
            )
            return None

        if (order.get("status") or "") == target_status:
            # The order is already where this submission would take it (a POD
            # replayed without an idempotency key, or a dispatcher who got
            # there first). ``apply_status_transition`` answers 409 for
            # ``X → X`` because no status maps to itself in
            # ``VALID_STATUS_TRANSITIONS``, and a 409 here would report a
            # failure that did not happen — the same reasoning R4.10 applies
            # to the driver transition endpoint. Nothing is appended.
            if (
                target_status == POD_DELIVERED_STATUS
                and not order.get("delivery_result")
            ):
                order = await self._order_service.reconcile_delivery_result(
                    order=order,
                    delivery_result=self._build_delivery_result(
                        order=order,
                        pod_doc=pod_doc,
                        bol_doc=bol_doc,
                    ),
                    actor_user_id=ref.driver_id,
                    client_event_timestamp=body.timestamp,
                )
            await self._record_transition_completed(
                tenant_id=ref.tenant_id,
                pod_doc=pod_doc,
            )
            return order

        if refusal["is_refusal"] and refusal["reason_code"]:
            order["refusal_reason_code"] = refusal["reason_code"]
        elif not refusal["is_refusal"]:
            order["delivery_result"] = self._build_delivery_result(
                order=order,
                pod_doc=pod_doc,
                bol_doc=bol_doc,
            )

        try:
            transitioned = await self._order_service.apply_status_transition(
                order=order,
                new_status=target_status,
                reason=refusal["reason_code"],
                actor_user_id=ref.driver_id,
                client_event_timestamp=body.timestamp,
            )
            await self._record_transition_completed(
                tenant_id=ref.tenant_id,
                pod_doc=pod_doc,
            )
            return transitioned
        except Exception as exc:
            error_code = (
                exc.error_code
                if isinstance(exc, AppException)
                else ErrorCode.INTERNAL_ERROR
            )
            logger.error(
                "POD %s is persisted but order %s failed to transition to %s "
                "(tenant=%s): %s",
                pod_id,
                order_id,
                target_status,
                ref.tenant_id,
                exc,
            )
            await self._record_transition_failure(
                tenant_id=ref.tenant_id,
                pod_doc=pod_doc,
                error_code=error_code.value,
            )
            details = {
                "reason": "pod_order_transition_failed",
                "pod_id": pod_id,
                "order_id": order_id,
                "target_status": target_status,
                "transition_error_code": error_code.value,
                "pod_status_transition": POD_TRANSITION_PENDING,
            }
            if isinstance(exc, AppException) and exc.details:
                details["transition_details"] = exc.details
            raise AppException(
                error_code=error_code,
                message=(
                    "Proof of delivery was recorded but the order status "
                    "transition failed"
                ),
                status_code=409,
                details=details,
            ) from exc

    async def _compute_reconciliation(self, *, pod_doc: dict, order: dict) -> None:
        """Persist ordered/loaded/delivered variance when a real load is resolvable.

        A missing load plan is not replaced with ordered or delivered gallons:
        doing so would manufacture a zero variance. The POD and invoice remain
        valid, while reconciliation logs the absent operational evidence.
        """
        if self._reconciliation_service is None:
            return

        try:
            loading_plan = await self._resolve_loading_plan(order)
            if loading_plan is None:
                logger.warning(
                    "Reconciliation deferred: no matching load assignment for "
                    "order=%s tenant=%s",
                    order.get("order_id"),
                    order.get("tenant_id"),
                )
                return

            reconciliation_order = dict(order)
            reconciliation_order["ordered_gallons"] = order.get("gallons_requested")
            invoice = await self._find_invoice_for_order(order)
            if invoice is not None:
                reconciliation_order["invoice_id"] = invoice.get("invoice_id")
                reconciliation_order["invoiced_gallons"] = sum(
                    float(item.get("quantity_gallons") or 0)
                    for item in (invoice.get("line_items") or [])
                )

            await self._reconciliation_service.compute(
                pod=pod_doc,
                order=reconciliation_order,
                loading_plan=loading_plan,
            )
        except Exception as exc:
            # Delivery and invoice writes have already committed. Reconciliation
            # is a repairable projection and must not make the driver resubmit.
            logger.exception(
                "Automatic reconciliation failed for pod=%s order=%s: %s",
                pod_doc.get("pod_id"),
                order.get("order_id"),
                exc,
            )

    async def _resolve_loading_plan(self, order: dict) -> Optional[dict]:
        """Resolve and normalize the load-plan assignment for one order."""
        plan_ref = order.get("assigned_run_id")
        tenant_id = order.get("tenant_id")
        if not plan_ref or not tenant_id:
            return None

        query = {
            "query": {
                "bool": {
                    "must": [{"term": {"tenant_id": tenant_id}}],
                    "should": [
                        {"term": {"plan_id": plan_ref}},
                        {"term": {"run_id": plan_ref}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            "size": 5,
        }
        response = await self._es_service.search_documents(
            "mvp_load_plans", query, 5
        )
        hits = (response.get("hits") or {}).get("hits") or []
        station_keys = {
            str(value)
            for value in (order.get("customer_tank_id"), order.get("order_id"))
            if value
        }
        for hit in hits:
            source = hit.get("_source") or {}
            assignments = source.get("assignments") or []
            matching = [
                assignment
                for assignment in assignments
                if str(assignment.get("station_id")) in station_keys
            ]
            if not matching:
                continue
            loaded_liters = sum(
                float(assignment.get("quantity_liters") or 0)
                for assignment in matching
            )
            if loaded_liters <= 0:
                continue
            from Agents.support.volume_units import LITERS_PER_US_GALLON

            return {
                "plan_id": source.get("plan_id") or plan_ref,
                "tenant_id": tenant_id,
                "loaded_gallons": loaded_liters / LITERS_PER_US_GALLON,
            }
        return None

    async def _find_invoice_for_order(self, order: dict) -> Optional[dict]:
        """Return the invoice synchronously created by order.delivered, if any."""
        response = await self._es_service.search_documents(
            "invoices_current",
            {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"tenant_id": order.get("tenant_id")}},
                            {"term": {"order_id": order.get("order_id")}},
                        ]
                    }
                },
                "size": 1,
            },
            1,
        )
        hits = (response.get("hits") or {}).get("hits") or []
        return (hits[0].get("_source") or {}) if hits else None

    @staticmethod
    def _build_delivery_result(*, order: dict, pod_doc: dict, bol_doc=None) -> dict:
        """Build the canonical POD snapshot consumed by invoicing and ERP sync."""
        from fuel.order_models import DeliveryResult

        intake_metadata = order.get("intake_metadata") or {}
        if hasattr(intake_metadata, "model_dump"):
            intake_metadata = intake_metadata.model_dump(mode="python")

        def _bol_value(name: str):
            if bol_doc is None:
                return None
            if isinstance(bol_doc, dict):
                return bol_doc.get(name)
            return getattr(bol_doc, name, None)

        result = DeliveryResult(
            pod_id=pod_doc["pod_id"],
            actual_gallons=pod_doc["delivered_gallons"],
            actual_gallons_source=pod_doc["delivered_gallons_source"],
            delivered_at=pod_doc["delivered_at"],
            recipient_name=pod_doc["recipient_name"],
            driver_id=pod_doc.get("driver_id"),
            signature_ref=pod_doc.get("signature_ref"),
            photo_refs=pod_doc.get("photo_refs") or [],
            meter_ticket_ref=pod_doc.get("meter_ticket_ref"),
            bol_id=_bol_value("bol_id"),
            bol_ref=_bol_value("file_ref"),
            pod_hash=pod_doc.get("pod_hash"),
            geotag=pod_doc["geotag"],
            otp_verified=bool(pod_doc.get("otp_verified")),
            location_mismatch=bool(pod_doc.get("location_mismatch")),
            source_system=intake_metadata.get("source_system"),
            source_record_id=intake_metadata.get("source_record_id"),
        )
        return result.model_dump(mode="json")

    async def _load_order_for_transition(
        self, *, ref: WorkRef, order_id: str
    ) -> Optional[dict]:
        """Return the order document the transition mutates, or ``None``.

        The order-keyed path already carries the document the resolver fetched
        and authorized, so no second read is issued for it. The job-keyed path
        reads it through ``order_repository.get``, which is tenant-scoped; the
        returned document's ``tenant_id`` is re-validated here before it is
        used for anything (R15.11).
        """
        if ref.kind == "order" and ref.order_doc is not None:
            return _as_order_document(ref.order_doc)

        if self._order_repository is None:
            return None

        try:
            order = await self._order_repository.get(ref.tenant_id, order_id)
        except Exception as exc:
            logger.warning(
                "Failed to read order %s for the POD transition (tenant=%s): %s",
                order_id,
                ref.tenant_id,
                exc,
            )
            return None

        if order is None:
            return None

        doc = _as_order_document(order)
        if doc.get("tenant_id") != ref.tenant_id:
            logger.warning(
                "Order %s resolved to another tenant during the POD "
                "transition — treated as unreadable (tenant=%s)",
                order_id,
                ref.tenant_id,
            )
            return None
        return doc

    async def _record_transition_failure(
        self, *, tenant_id: str, pod_doc: dict, error_code: str
    ) -> None:
        """Mark the persisted POD as awaiting its order transition (R4.7).

        The patch is bookkeeping for reconciliation, not a compensation: the
        POD stays exactly as it was chained, and nothing rolls back. A failure
        to write the patch is logged and swallowed — the 409 the caller is
        about to receive is what matters, and the POD is durable either way.
        """
        fields = {
            "pod_status_transition": POD_TRANSITION_PENDING,
            "pod_status_transition_error": error_code,
        }
        pod_doc.update(fields)

        if self._es_service is None:  # pragma: no cover - defensive
            return
        try:
            await self._es_service.update_document(
                PROOF_OF_DELIVERY_INDEX, pod_doc["pod_id"], fields
            )
        except Exception as exc:
            logger.error(
                "Failed to record the pending order transition on POD %s "
                "(tenant=%s): %s",
                pod_doc.get("pod_id"),
                tenant_id,
                exc,
            )

    async def _record_transition_completed(
        self, *, tenant_id: str, pod_doc: dict
    ) -> None:
        """Mark the mutable transition projection complete.

        These fields are deliberately outside the POD hash canonicalization,
        so operational repair bookkeeping cannot alter the evidence chain.
        """
        fields = {
            "pod_status_transition": POD_TRANSITION_COMPLETED,
            "pod_status_transition_error": None,
        }
        pod_doc.update(fields)
        try:
            await self._es_service.update_document(
                PROOF_OF_DELIVERY_INDEX, pod_doc["pod_id"], fields
            )
        except Exception as exc:
            # A persisted ``pending`` marker is safe: the repair worker will
            # observe an already-transitioned order and close it idempotently.
            logger.error(
                "Failed to mark the order transition complete on POD %s "
                "(tenant=%s): %s",
                pod_doc.get("pod_id"),
                tenant_id,
                exc,
            )
