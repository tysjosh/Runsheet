"""
Unit tests for ``PODOTPService`` — the dispatch-time POD one-time code.

Covers the default-off policy gate, code generation and persistence on the
order document, the single ``pod_otp`` notification, the validity window
evaluated at submission, and the guarantee that the code never reaches a log
record or a driver-facing response.

Validates: Requirements 5.25, 5.26, 5.27, 5.28, 5.29, 5.30, 5.31
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

from driver.services.pod_otp_service import (
    POD_OTP_DEFAULT_VALIDITY_HOURS,
    POD_OTP_FIELD,
    POD_OTP_GENERATED_AT_FIELD,
    PODOTPService,
    assert_otp_window_open,
    otp_validity_end,
)
from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

TENANT = "tenant-a"
ORDER = "order-1"
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


class _FakeES:
    """Records partial updates and answers the tenant-policy read."""

    def __init__(self, *, otp_required: bool = True, fail_update: bool = False):
        self._otp_required = otp_required
        self._fail_update = fail_update
        self.updates: list[tuple[str, str, dict]] = []
        self.policy_reads = 0

    async def search_documents(self, index, query, size=10):
        self.policy_reads += 1
        return {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "tenant_id": TENANT,
                            "otp_required": self._otp_required,
                        }
                    }
                ]
            }
        }

    async def update_document(self, index, doc_id, partial_doc):
        if self._fail_update:
            raise RuntimeError("es down")
        self.updates.append((index, doc_id, dict(partial_doc)))
        return {"result": "updated"}


class _FakeNotifications:
    def __init__(self, *, fail: bool = False):
        self.calls: list[dict] = []
        self._fail = fail

    async def notify_event(self, *, event_type, event_data, tenant_id):
        if self._fail:
            raise RuntimeError("dispatcher down")
        self.calls.append(
            {
                "event_type": event_type,
                "event_data": dict(event_data),
                "tenant_id": tenant_id,
            }
        )
        return [{"notification_id": "n-1"}]


def _order(**overrides) -> dict:
    doc = {
        "order_id": ORDER,
        "tenant_id": TENANT,
        "customer_id": "cust-1",
        "customer_name": "Acme Fuel",
        "status": "dispatched",
        "delivery_window_end": "2026-06-01T18:00:00+00:00",
    }
    doc.update(overrides)
    return doc


def _service(es, notifications=None) -> PODOTPService:
    return PODOTPService(
        es_service=es,
        notification_service=notifications,
        clock=lambda: NOW,
    )


# ---------------------------------------------------------------------------
# R5.31 — otp_required defaults to false
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_code_when_tenant_does_not_require_one():
    es = _FakeES(otp_required=False)
    notifications = _FakeNotifications()

    await _service(es, notifications).on_order_dispatched(_order())

    assert es.updates == []
    assert notifications.calls == []


@pytest.mark.asyncio
async def test_no_code_when_no_policy_document_exists():
    class _EmptyES(_FakeES):
        async def search_documents(self, index, query, size=10):
            return {"hits": {"hits": []}}

    es = _EmptyES()
    notifications = _FakeNotifications()

    await _service(es, notifications).on_order_dispatched(_order())

    assert es.updates == []
    assert notifications.calls == []


# ---------------------------------------------------------------------------
# R5.25 — generation and persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persists_a_six_digit_code_under_pod_otp():
    es = _FakeES()

    await _service(es).on_order_dispatched(_order())

    assert len(es.updates) == 1
    index, doc_id, fields = es.updates[0]
    assert index == FUEL_ORDERS_CURRENT_INDEX
    assert doc_id == ORDER
    assert len(fields[POD_OTP_FIELD]) == 6
    assert fields[POD_OTP_FIELD].isdigit()
    assert fields[POD_OTP_GENERATED_AT_FIELD] == NOW.isoformat()


@pytest.mark.asyncio
async def test_does_not_mutate_the_order_dict_it_is_handed():
    """The transition response is built from this dict — it must stay clean."""
    es = _FakeES()
    order = _order()

    await _service(es).on_order_dispatched(order)

    assert POD_OTP_FIELD not in order
    assert POD_OTP_GENERATED_AT_FIELD not in order


@pytest.mark.asyncio
async def test_a_redispatch_keeps_the_code_the_customer_already_holds():
    es = _FakeES()
    notifications = _FakeNotifications()

    await _service(es, notifications).on_order_dispatched(
        _order(pod_otp="123456")
    )

    assert es.updates == []
    assert notifications.calls == []


@pytest.mark.asyncio
async def test_missing_identifiers_provision_nothing():
    es = _FakeES()

    await _service(es).on_order_dispatched({"status": "dispatched"})

    assert es.updates == []
    assert es.policy_reads == 0


# ---------------------------------------------------------------------------
# R5.27 — one notification, through Notification_Pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submits_exactly_one_pod_otp_notification_with_the_code():
    es = _FakeES()
    notifications = _FakeNotifications()

    await _service(es, notifications).on_order_dispatched(_order())

    assert len(notifications.calls) == 1
    call = notifications.calls[0]
    assert call["event_type"] == "pod_otp"
    assert call["tenant_id"] == TENANT
    assert call["event_data"]["customer_id"] == "cust-1"
    # The persisted code and the delivered code are the same value.
    assert call["event_data"]["otp_code"] == es.updates[0][2][POD_OTP_FIELD]
    assert call["event_data"]["valid_until"] == "2026-06-01T18:00:00+00:00"


@pytest.mark.asyncio
async def test_a_failed_persist_delivers_nothing():
    """A code the server cannot verify is worse than no code."""
    es = _FakeES(fail_update=True)
    notifications = _FakeNotifications()

    await _service(es, notifications).on_order_dispatched(_order())

    assert notifications.calls == []


@pytest.mark.asyncio
async def test_a_failed_notification_does_not_propagate():
    es = _FakeES()
    notifications = _FakeNotifications(fail=True)

    await _service(es, notifications).on_order_dispatched(_order())

    assert len(es.updates) == 1  # the code is still provisioned


@pytest.mark.asyncio
async def test_setter_injection_completes_the_wiring():
    es = _FakeES()
    notifications = _FakeNotifications()
    service = PODOTPService(es_service=es, clock=lambda: NOW)

    service.set_notification_service(notifications)
    await service.on_order_dispatched(_order())

    assert len(notifications.calls) == 1


# ---------------------------------------------------------------------------
# R5.26 — the code never reaches a log record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_code_appears_in_no_log_record(caplog):
    es = _FakeES()
    notifications = _FakeNotifications()

    with caplog.at_level(logging.DEBUG, logger="driver.services.pod_otp_service"):
        await _service(es, notifications).on_order_dispatched(_order())

    code = es.updates[0][2][POD_OTP_FIELD]
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert code not in logged


# ---------------------------------------------------------------------------
# R5.28, R5.30 — the validity window
# ---------------------------------------------------------------------------


def test_validity_runs_through_the_delivery_window_end():
    end = otp_validity_end(
        {
            POD_OTP_GENERATED_AT_FIELD: "2026-06-01T08:00:00+00:00",
            "delivery_window_end": "2026-06-01T18:00:00+00:00",
        }
    )

    assert end == datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)


def test_validity_is_24_hours_without_a_window():
    generated = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)

    end = otp_validity_end({POD_OTP_GENERATED_AT_FIELD: generated})

    assert end == generated + timedelta(hours=POD_OTP_DEFAULT_VALIDITY_HOURS)


def test_no_window_and_no_generation_instant_is_unbounded():
    assert otp_validity_end({"order_id": ORDER}) is None
    assert otp_validity_end(None) is None


def test_a_code_received_hours_early_is_still_valid_on_arrival():
    doc = {
        POD_OTP_GENERATED_AT_FIELD: "2026-06-01T06:00:00+00:00",
        "delivery_window_end": "2026-06-01T18:00:00+00:00",
    }

    assert_otp_window_open(doc, now=datetime(2026, 6, 1, 17, 30, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# R5.29 — a late submission is 409 OTP_WINDOW_EXPIRED
# ---------------------------------------------------------------------------


def test_a_late_submission_is_conflict_with_the_window_in_details():
    doc = {
        POD_OTP_GENERATED_AT_FIELD: "2026-06-01T06:00:00+00:00",
        "delivery_window_end": "2026-06-01T18:00:00+00:00",
    }

    with pytest.raises(AppException) as exc_info:
        assert_otp_window_open(
            doc, now=datetime(2026, 6, 1, 18, 0, 1, tzinfo=timezone.utc)
        )

    exc = exc_info.value
    assert exc.status_code == 409
    assert exc.error_code == ErrorCode.OTP_WINDOW_EXPIRED
    assert exc.details["delivery_window_end"] == "2026-06-01T18:00:00+00:00"


def test_a_windowless_order_expires_24_hours_after_generation():
    doc = {POD_OTP_GENERATED_AT_FIELD: "2026-06-01T06:00:00+00:00"}

    with pytest.raises(AppException) as exc_info:
        assert_otp_window_open(
            doc, now=datetime(2026, 6, 2, 6, 0, 1, tzinfo=timezone.utc)
        )

    assert exc_info.value.error_code == ErrorCode.OTP_WINDOW_EXPIRED
    assert exc_info.value.details["delivery_window_end"] is None
