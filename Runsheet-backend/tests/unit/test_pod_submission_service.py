"""
Unit tests for :class:`driver.services.pod_service.PODSubmissionService`.

Covers the rule the service extracted from ``submit_pod`` and the three
behavioural corrections the extraction carries:

* the refusal branch makes both ``signature_ref`` **and** ``photo_refs``
  optional (the handler required ``photo_refs`` unconditionally) — R5.14
* the audit actor and the broadcast identity are the canonical
  ``driver_id``, so a driver-keyed WS connection is actually reachable
* an authorization rejection names the work reference only — R15.14

and the four POD corrections:

* a blank resolved ``order_id`` is 422 ``POD_ORDER_REFERENCE_REQUIRED`` — R5.22
* a non-refusal with no resolvable gallon count is 409
  ``POD_GALLONS_CONFIRMATION_REQUIRED`` or 422 ``DELIVERED_GALLONS_REQUIRED``
  — R5.11, R5.12
* the OTP failures carry real status codes, not HTTP 200 bodies — R5.9, R15.10
* a refusal records the literal ``0`` gallons — R5.15

and the write ordering:

* the order transition is last, so a failed transition leaves a durable POD and
  a 409 carrying the transition error code and the ``pod_id`` — R4.5, R4.6, R4.7
* a verified code presented after its validity window is 409
  ``OTP_WINDOW_EXPIRED`` — R5.29

Validates: Requirements 4.5, 4.6, 4.7, 5.7, 5.9, 5.10, 5.11, 5.12, 5.14, 5.15,
5.16, 5.17, 5.21, 5.22, 5.23, 5.29, 15.10
"""

import sys
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

# Patch the ElasticsearchService singleton before scheduling imports, as the
# rest of the driver unit suite does.
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from driver.models import PODRequest  # noqa: E402
from driver.services.pod_service import PODSubmissionService  # noqa: E402
from driver.services.work_ref import WorkRef  # noqa: E402
from errors.exceptions import AppException  # noqa: E402
from services.pod_hash_chain_writer import PodHashChainWriter  # noqa: E402

TENANT_ID = "t1"
DRIVER_ID = "drv_1"
JOB_ID = "JOB_1"

_SIGNATURE_REF = (
    f"tenants/{TENANT_ID}/signature/2024/01/15/"
    "11111111-1111-1111-1111-111111111111.png"
)
_PHOTO_REF = (
    f"tenants/{TENANT_ID}/photo/2024/01/15/"
    "22222222-2222-2222-2222-222222222222.jpg"
)
_METER_TICKET_REF = (
    f"tenants/{TENANT_ID}/meter_ticket/2024/01/15/"
    "44444444-4444-4444-4444-444444444444.jpg"
)
_CROSS_TENANT_PHOTO_REF = (
    "tenants/other-tenant/photo/2024/01/15/"
    "33333333-3333-3333-3333-333333333333.jpg"
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_es(tenant_policies: Optional[dict] = None) -> MagicMock:
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    if tenant_policies is not None:
        # The policy lookup and the chain-state lookup share this mock; the
        # chain writer treats an unexpected hit shape as an empty chain, which
        # is what these tests want.
        es.search_documents = AsyncMock(
            return_value={
                "hits": {"hits": [{"_source": tenant_policies}], "total": {"value": 1}}
            }
        )
    else:
        # No tenant policies and no prior POD in the chain.
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [], "total": {"value": 0}}}
        )
    return es


def _otp_es(otp_required: bool = True) -> MagicMock:
    return _make_es(
        tenant_policies={
            "tenant_id": TENANT_ID,
            "pod_required": True,
            "pod_radius_meters": 500,
            "otp_required": otp_required,
        }
    )


def _make_ocr_service(
    *,
    extracted_gallons: Optional[float] = None,
    confidence: float = 0.0,
    requires_manual_review: bool = True,
    error_details: Optional[str] = None,
) -> MagicMock:
    result = MagicMock()
    result.ocr_result_id = "ocr-result-1"
    result.confidence = confidence
    result.extracted_gallons = extracted_gallons
    result.requires_manual_review = requires_manual_review
    result.error_details = error_details

    svc = MagicMock()
    svc.extract = AsyncMock(return_value=result)
    return svc


def _make_job_service(job_doc: Optional[dict] = None) -> MagicMock:
    svc = MagicMock()
    svc._append_event = AsyncMock(return_value="evt-1")
    svc._get_job_doc = AsyncMock(
        return_value=job_doc
        or {"job_id": JOB_ID, "tenant_id": TENANT_ID, "order_id": "ORD_1"}
    )
    return svc


def _make_file_storage() -> MagicMock:
    def _validate_ref(tenant_id: str, file_ref: str, actor=None) -> bool:
        if not file_ref or not file_ref.startswith(f"tenants/{tenant_id}/"):
            raise PermissionError("cross_tenant_file_ref")
        return True

    svc = MagicMock()
    svc.validate_ref = MagicMock(side_effect=_validate_ref)
    return svc


class FakeOrderRepository:
    """Minimal stand-in for ``FuelOrderRepository.get``."""

    def __init__(self, order: Optional[dict] = None) -> None:
        self._order = order
        self.calls: list[tuple[str, str]] = []

    async def get(self, tenant_id: str, order_id: str):
        self.calls.append((tenant_id, order_id))
        if self._order is None or self._order.get("order_id") != order_id:
            return None
        return dict(self._order)


def _order_doc(status: str = "in_transit", order_id: str = "ORD_1") -> dict:
    return {
        "order_id": order_id,
        "tenant_id": TENANT_ID,
        "assigned_driver_id": DRIVER_ID,
        "status": status,
    }


def _make_order_service(*, raises: Optional[Exception] = None) -> MagicMock:
    """``OrderService`` stand-in that records the transition it was asked for."""
    svc = MagicMock()

    async def _apply(**kwargs):
        if raises is not None:
            raise raises
        order = kwargs["order"]
        order["status"] = kwargs["new_status"]
        return order

    svc.apply_status_transition = AsyncMock(side_effect=_apply)

    async def _reconcile(**kwargs):
        order = kwargs["order"]
        order["delivery_result"] = kwargs["delivery_result"]
        return order

    svc.reconcile_delivery_result = AsyncMock(side_effect=_reconcile)
    return svc


def _make_service(
    *,
    es=None,
    job_service=None,
    driver_ws=None,
    scheduling_ws=None,
    ocr_service=None,
    order_service=None,
    order_repository=None,
) -> PODSubmissionService:
    es = es or _make_es()
    return PODSubmissionService(
        es_service=es,
        job_service=job_service if job_service is not None else _make_job_service(),
        order_service=order_service,
        order_repository=order_repository,
        file_storage_service=_make_file_storage(),
        pod_hash_chain_writer=PodHashChainWriter(es_service=es, redis_client=None),
        driver_ws_manager=driver_ws,
        scheduling_ws_manager=scheduling_ws,
        ocr_service=ocr_service,
    )


def _job_ref(job_doc: Optional[dict] = None) -> WorkRef:
    return WorkRef(
        tenant_id=TENANT_ID,
        driver_id=DRIVER_ID,
        kind="job",
        work_id=JOB_ID,
        job_doc=job_doc or {"job_id": JOB_ID, "tenant_id": TENANT_ID, "order_id": "ORD_1"},
    )


def _order_ref(order_doc: Optional[dict] = None) -> WorkRef:
    doc = order_doc or _order_doc()
    return WorkRef(
        tenant_id=TENANT_ID,
        driver_id=DRIVER_ID,
        kind="order",
        work_id=doc["order_id"],
        order_doc=doc,
    )


def _body(**overrides) -> PODRequest:
    """A complete non-refusal submission.

    ``delivered_gallons`` is present by default: a non-refusal that resolves no
    gallon count is rejected rather than chained with a null (R5.11, R5.12).
    """
    payload = {
        "recipient_name": "John Doe",
        "signature_ref": _SIGNATURE_REF,
        "photo_refs": [_PHOTO_REF],
        "delivered_gallons": 500.0,
        "geotag": {"lat": -33.8688, "lng": 151.2093},
        "timestamp": "2024-01-15T10:30:00Z",
    }
    payload.update(overrides)
    return PODRequest(**payload)


# ---------------------------------------------------------------------------
# Artifact validation
# ---------------------------------------------------------------------------


class TestArtifactValidation:
    """R5.7 / R5.14 — artifact requirements per branch."""

    async def test_persists_pod_with_hash_chain_fields(self):
        """A complete submission is persisted and chained. Validates: Req 5.16"""
        es = _make_es()
        service = _make_service(es=es)

        result = await service.submit(_job_ref(), _body(), request_id="req-1")

        doc = result["data"]
        assert result["request_id"] == "req-1"
        assert doc["job_id"] == JOB_ID
        assert doc["order_id"] == "ORD_1"
        assert doc["status"] == "submitted"
        assert doc["previous_pod_hash"] == "0" * 64
        assert len(doc["pod_hash"]) == 64
        assert doc["chain_sequence"] == 1
        es.index_document.assert_called_once()
        assert es.index_document.call_args.args[0] == "proof_of_delivery"

    async def test_non_refusal_requires_signature(self):
        """Validates: Requirement 5.7"""
        service = _make_service()

        with pytest.raises(AppException) as exc:
            await service.submit(
                _job_ref(), _body(signature_ref=None), request_id="req-1"
            )

        assert exc.value.details["missing"] == ["signature_ref"]

    async def test_non_refusal_requires_at_least_one_photo(self):
        """Validates: Requirement 5.7"""
        service = _make_service()

        with pytest.raises(AppException) as exc:
            await service.submit(_job_ref(), _body(photo_refs=[]), request_id="req-1")

        assert exc.value.details["missing"] == ["photo_refs"]

    async def test_refusal_needs_neither_signature_nor_photo(self):
        """The moved refusal-branch bug. Validates: Requirement 5.14"""
        es = _make_es()
        service = _make_service(es=es)

        result = await service.submit(
            _job_ref(),
            _body(
                signature_ref=None,
                photo_refs=[],
                refused_delivery=True,
                refusal_reason_code="unsafe_site",
            ),
            request_id="req-1",
        )

        doc = result["data"]
        assert doc["status"] == "refused"
        assert doc["refused_delivery"] is True
        assert doc["refusal_reason_code"] == "unsafe_site"
        assert doc["signature_ref"] is None
        assert doc["photo_refs"] == []
        es.index_document.assert_called_once()

    async def test_refusal_requires_reason_code(self):
        """Validates: Requirement 5.14"""
        service = _make_service()

        with pytest.raises(AppException) as exc:
            await service.submit(
                _job_ref(), _body(refused_delivery=True), request_id="req-1"
            )

        assert exc.value.details["missing"] == ["refusal_reason_code"]


# ---------------------------------------------------------------------------
# Tenant prefix enforcement
# ---------------------------------------------------------------------------


class TestFileRefTenantPrefix:
    """R5.17 / R15.14 — foreign file_refs are rejected before any write."""

    async def test_cross_tenant_photo_ref_rejected_without_persisting(self):
        es = _make_es()
        service = _make_service(es=es)

        with pytest.raises(AppException) as exc:
            await service.submit(
                _job_ref(),
                _body(photo_refs=[_PHOTO_REF, _CROSS_TENANT_PHOTO_REF]),
                request_id="req-1",
            )

        assert exc.value.status_code == 403
        assert exc.value.details["reason"] == "cross_tenant_file_ref"
        assert exc.value.details["field"] == "photo_refs[1]"
        es.index_document.assert_not_called()


# ---------------------------------------------------------------------------
# Acting identity
# ---------------------------------------------------------------------------


class TestActingIdentity:
    """The acting driver is the canonical driver_id on every derived value."""

    async def test_event_and_broadcast_use_canonical_driver_id(self):
        job_service = _make_job_service()
        driver_ws = MagicMock()
        driver_ws.send_to_driver = AsyncMock()
        scheduling_ws = MagicMock()
        scheduling_ws.broadcast = AsyncMock()
        service = _make_service(
            job_service=job_service,
            driver_ws=driver_ws,
            scheduling_ws=scheduling_ws,
        )

        result = await service.submit(_job_ref(), _body(), request_id="req-1")

        assert result["data"]["driver_id"] == DRIVER_ID
        assert job_service._append_event.call_args.kwargs["actor_id"] == DRIVER_ID
        # A DriverWSManager connection is keyed on driver_id, so the identity
        # passed here is what decides whether the driver's device is reached.
        assert driver_ws.send_to_driver.call_args.args[0] == DRIVER_ID
        assert scheduling_ws.broadcast.call_args.args[0] == "pod_submitted"

    async def test_broadcast_failure_does_not_fail_a_persisted_pod(self):
        driver_ws = MagicMock()
        driver_ws.send_to_driver = AsyncMock(side_effect=Exception("WS down"))
        service = _make_service(driver_ws=driver_ws)

        result = await service.submit(_job_ref(), _body(), request_id="req-1")

        assert result["data"]["status"] == "submitted"


# ---------------------------------------------------------------------------
# The four POD corrections
# ---------------------------------------------------------------------------


class TestOrderReference:
    """R5.22 — a POD that names no order is rejected, never chained."""

    async def test_blank_job_order_id_is_422(self):
        """Validates: Requirement 5.22"""
        es = _make_es()
        service = _make_service(
            es=es,
            job_service=_make_job_service(
                job_doc={"job_id": JOB_ID, "tenant_id": TENANT_ID}
            ),
        )
        ref = _job_ref(job_doc={"job_id": JOB_ID, "tenant_id": TENANT_ID})

        with pytest.raises(AppException) as exc:
            await service.submit(ref, _body(), request_id="req-1")

        assert exc.value.status_code == 422
        assert exc.value.error_code.value == "POD_ORDER_REFERENCE_REQUIRED"
        assert exc.value.details["missing"] == ["order_id"]
        es.index_document.assert_not_called()

    async def test_order_keyed_path_uses_the_path_parameter(self):
        """No job document is consulted on the order-keyed path.

        Validates: Requirement 5.21
        """
        es = _make_es()
        job_service = _make_job_service()
        service = _make_service(es=es, job_service=job_service)
        ref = WorkRef(
            tenant_id=TENANT_ID,
            driver_id=DRIVER_ID,
            kind="order",
            work_id="ORD_9",
            order_doc={"order_id": "ORD_9", "tenant_id": TENANT_ID},
        )

        result = await service.submit(ref, _body(), request_id="req-1")

        assert result["data"]["order_id"] == "ORD_9"
        job_service._get_job_doc.assert_not_called()


class TestDeliveredGallons:
    """R5.11 / R5.12 / R5.15 — no null gallon count reaches the chain."""

    async def test_no_gallons_and_no_meter_ticket_is_422(self):
        """Validates: Requirement 5.12"""
        es = _make_es()
        service = _make_service(es=es)

        with pytest.raises(AppException) as exc:
            await service.submit(
                _job_ref(), _body(delivered_gallons=None), request_id="req-1"
            )

        assert exc.value.status_code == 422
        assert exc.value.error_code.value == "DELIVERED_GALLONS_REQUIRED"
        es.index_document.assert_not_called()

    async def test_unusable_ocr_value_is_409_with_the_diagnostic(self):
        """Validates: Requirement 5.11"""
        es = _make_es()
        ocr = _make_ocr_service(
            extracted_gallons=None,
            error_details="textract_error:ThrottlingException",
        )
        service = _make_service(es=es, ocr_service=ocr)

        with pytest.raises(AppException) as exc:
            await service.submit(
                _job_ref(),
                _body(delivered_gallons=None, meter_ticket_ref=_METER_TICKET_REF),
                request_id="req-1",
            )

        assert exc.value.status_code == 409
        assert exc.value.error_code.value == "POD_GALLONS_CONFIRMATION_REQUIRED"
        assert exc.value.details["ocr_error"] == "textract_error:ThrottlingException"
        es.index_document.assert_not_called()

    async def test_usable_ocr_value_is_accepted_with_source_ocr(self):
        """Validates: Requirement 5.10"""
        ocr = _make_ocr_service(
            extracted_gallons=812.5,
            confidence=0.92,
            requires_manual_review=False,
        )
        service = _make_service(ocr_service=ocr)

        result = await service.submit(
            _job_ref(),
            _body(delivered_gallons=None, meter_ticket_ref=_METER_TICKET_REF),
            request_id="req-1",
        )

        assert result["data"]["delivered_gallons"] == pytest.approx(812.5)
        assert result["data"]["delivered_gallons_source"] == "ocr"

    async def test_refusal_records_zero_gallons(self):
        """Validates: Requirement 5.15"""
        ocr = _make_ocr_service(extracted_gallons=999.0, requires_manual_review=False)
        service = _make_service(ocr_service=ocr)

        result = await service.submit(
            _job_ref(),
            _body(
                signature_ref=None,
                photo_refs=[],
                delivered_gallons=None,
                meter_ticket_ref=_METER_TICKET_REF,
                refused_delivery=True,
                refusal_reason_code="unsafe_site",
            ),
            request_id="req-1",
        )

        doc = result["data"]
        assert doc["delivered_gallons"] == 0
        assert doc["delivered_gallons_source"] == "refused"
        # No meter ticket is read for a delivery that never happened, so OCR
        # cannot contradict the refusal.
        ocr.extract.assert_not_called()
        assert len(doc["pod_hash"]) == 64


class TestOtpStatusCodes:
    """R5.9 / R15.10 — the OTP failures carry real status codes."""

    async def test_missing_otp_is_422(self):
        """Validates: Requirements 5.9, 15.10"""
        service = _make_service(es=_otp_es())

        with pytest.raises(AppException) as exc:
            await service.submit(_job_ref(), _body(), request_id="req-1")

        assert exc.value.status_code == 422
        assert exc.value.error_code.value == "OTP_REQUIRED"

    async def test_unprovisioned_otp_is_409_fail_closed(self):
        """Validates: Requirements 5.9, 15.10"""
        service = _make_service(es=_otp_es())

        with pytest.raises(AppException) as exc:
            await service.submit(
                _job_ref(), _body(otp="123456"), request_id="req-1"
            )

        assert exc.value.status_code == 409
        assert exc.value.error_code.value == "OTP_NOT_PROVISIONED"

    async def test_mismatched_otp_is_403(self):
        """Validates: Requirements 5.9, 15.10"""
        job_doc = {
            "job_id": JOB_ID,
            "tenant_id": TENANT_ID,
            "order_id": "ORD_1",
            "pod_otp": "123456",
        }
        service = _make_service(es=_otp_es())

        with pytest.raises(AppException) as exc:
            await service.submit(
                _job_ref(job_doc=job_doc), _body(otp="999999"), request_id="req-1"
            )

        assert exc.value.status_code == 403
        assert exc.value.error_code.value == "OTP_VERIFICATION_FAILED"
        # Neither operand of the comparison may appear in the rejection.
        assert "123456" not in str(exc.value.details)
        assert "999999" not in str(exc.value.details)

    async def test_matching_otp_is_verified(self):
        """Validates: Requirement 5.9"""
        job_doc = {
            "job_id": JOB_ID,
            "tenant_id": TENANT_ID,
            "order_id": "ORD_1",
            "pod_otp": "123456",
        }
        service = _make_service(es=_otp_es())

        result = await service.submit(
            _job_ref(job_doc=job_doc), _body(otp="123456"), request_id="req-1"
        )

        assert result["data"]["otp_verified"] is True


class TestOtpValidityWindow:
    """R5.29 — the right code, presented too late, is 409."""

    def _job_doc(self, window_end: str) -> dict:
        return {
            "job_id": JOB_ID,
            "tenant_id": TENANT_ID,
            "order_id": "ORD_1",
            "pod_otp": "123456",
            "delivery_window_end": window_end,
        }

    async def test_code_presented_after_the_window_is_409(self):
        """Validates: Requirement 5.29"""
        es = _otp_es()
        service = _make_service(es=es)

        with pytest.raises(AppException) as exc:
            await service.submit(
                _job_ref(job_doc=self._job_doc("2020-01-01T00:00:00Z")),
                _body(otp="123456"),
                request_id="req-1",
            )

        assert exc.value.status_code == 409
        assert exc.value.error_code.value == "OTP_WINDOW_EXPIRED"
        assert exc.value.details["delivery_window_end"] == "2020-01-01T00:00:00Z"
        # An expired window is a rejection, so nothing is chained.
        es.index_document.assert_not_called()

    async def test_code_presented_inside_the_window_is_accepted(self):
        """Validates: Requirement 5.29"""
        service = _make_service(es=_otp_es())

        result = await service.submit(
            _job_ref(job_doc=self._job_doc("2999-01-01T00:00:00Z")),
            _body(otp="123456"),
            request_id="req-1",
        )

        assert result["data"]["otp_verified"] is True

    async def test_a_wrong_code_is_403_even_when_the_window_is_closed(self):
        """The window is evaluated only once the code itself verifies.

        Validates: Requirements 5.9, 5.29
        """
        service = _make_service(es=_otp_es())

        with pytest.raises(AppException) as exc:
            await service.submit(
                _job_ref(job_doc=self._job_doc("2020-01-01T00:00:00Z")),
                _body(otp="999999"),
                request_id="req-1",
            )

        assert exc.value.error_code.value == "OTP_VERIFICATION_FAILED"


class TestOrderTransition:
    """R4.5 / R4.6 / R4.7 — the transition is last, and never undoes the POD."""

    async def test_successful_submission_transitions_the_order_to_delivered(self):
        """Validates: Requirement 4.5"""
        order_service = _make_order_service()
        repo = FakeOrderRepository(_order_doc())
        service = _make_service(order_service=order_service, order_repository=repo)

        await service.submit(_job_ref(), _body(), request_id="req-1")

        kwargs = order_service.apply_status_transition.call_args.kwargs
        assert kwargs["new_status"] == "delivered"
        assert kwargs["actor_user_id"] == DRIVER_ID
        assert kwargs["client_event_timestamp"] == "2024-01-15T10:30:00Z"

    async def test_order_keyed_path_transitions_the_resolved_order_document(self):
        """The resolver's document is reused — no second read. Validates: Req 4.5"""
        order_service = _make_order_service()
        repo = FakeOrderRepository(_order_doc())
        service = _make_service(order_service=order_service, order_repository=repo)

        await service.submit(_order_ref(), _body(), request_id="req-1")

        assert repo.calls == []
        kwargs = order_service.apply_status_transition.call_args.kwargs
        assert kwargs["order"]["order_id"] == "ORD_1"
        assert kwargs["new_status"] == "delivered"

    async def test_refusal_transitions_to_failed_and_stamps_the_reason(self):
        """Validates: Requirement 4.6"""
        order_service = _make_order_service()
        repo = FakeOrderRepository(_order_doc())
        service = _make_service(order_service=order_service, order_repository=repo)

        await service.submit(
            _job_ref(),
            _body(
                signature_ref=None,
                photo_refs=[],
                delivered_gallons=None,
                refused_delivery=True,
                refusal_reason_code="unsafe_site",
            ),
            request_id="req-1",
        )

        kwargs = order_service.apply_status_transition.call_args.kwargs
        assert kwargs["new_status"] == "failed"
        assert kwargs["order"]["refusal_reason_code"] == "unsafe_site"
        assert kwargs["reason"] == "unsafe_site"

    async def test_transition_runs_after_the_pod_is_persisted(self):
        """The ordering guarantee itself. Validates: Requirements 4.7, 5.16"""
        calls: list[str] = []
        es = _make_es()

        async def _index(index, doc_id, document):
            calls.append("persist")
            return {"result": "created"}

        es.index_document = AsyncMock(side_effect=_index)

        job_service = _make_job_service()

        async def _append_event(**kwargs):
            calls.append("event")
            return "evt-1"

        job_service._append_event = AsyncMock(side_effect=_append_event)

        order_service = MagicMock()

        async def _apply(**kwargs):
            calls.append("transition")
            return kwargs["order"]

        order_service.apply_status_transition = AsyncMock(side_effect=_apply)

        service = _make_service(
            es=es,
            job_service=job_service,
            order_service=order_service,
            order_repository=FakeOrderRepository(_order_doc()),
        )

        await service.submit(_job_ref(), _body(), request_id="req-1")

        assert calls == ["persist", "event", "transition"]

    async def test_failed_transition_keeps_the_pod_and_returns_409(self):
        """Validates: Requirement 4.7"""
        from errors.exceptions import invalid_status_transition

        es = _make_es()
        order_service = _make_order_service(
            raises=invalid_status_transition(
                message="Invalid transition",
                details={"old_status": "dispatched", "new_status": "delivered"},
            )
        )
        service = _make_service(
            es=es,
            order_service=order_service,
            order_repository=FakeOrderRepository(_order_doc(status="dispatched")),
        )

        with pytest.raises(AppException) as exc:
            await service.submit(_job_ref(), _body(), request_id="req-1")

        # The POD was written before the transition was attempted and is not
        # rolled back.
        es.index_document.assert_called_once()
        persisted_pod = es.index_document.call_args.args[2]

        assert exc.value.status_code == 409
        assert exc.value.error_code.value == "INVALID_STATUS_TRANSITION"
        assert exc.value.details["pod_id"] == persisted_pod["pod_id"]
        assert exc.value.details["target_status"] == "delivered"
        assert exc.value.details["transition_error_code"] == (
            "INVALID_STATUS_TRANSITION"
        )

        # And the persisted POD is marked for reconciliation.
        patch_args = es.update_document.call_args.args
        assert patch_args[1] == persisted_pod["pod_id"]
        assert patch_args[2] == {
            "pod_status_transition": "pending",
            "pod_status_transition_error": "INVALID_STATUS_TRANSITION",
        }

    async def test_no_order_service_leaves_the_pod_persisted(self):
        """An unwired transition never fails a POD. Validates: Requirement 4.7"""
        es = _make_es()
        service = _make_service(es=es)

        result = await service.submit(_job_ref(), _body(), request_id="req-1")

        es.index_document.assert_called_once()
        assert result["data"]["status"] == "submitted"

    async def test_order_already_delivered_attaches_missing_delivery_result(self):
        """``X → X`` is repaired without an invalid status transition."""
        order_service = _make_order_service()
        service = _make_service(
            order_service=order_service,
            order_repository=FakeOrderRepository(_order_doc(status="delivered")),
        )

        result = await service.submit(_job_ref(), _body(), request_id="req-1")

        order_service.apply_status_transition.assert_not_called()
        order_service.reconcile_delivery_result.assert_awaited_once()
        delivery_result = (
            order_service.reconcile_delivery_result.call_args.kwargs[
                "delivery_result"
            ]
        )
        assert delivery_result["actual_gallons"] == 500
        assert delivery_result["pod_id"] == result["data"]["pod_id"]
        assert result["data"]["pod_id"]
