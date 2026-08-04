"""
Unit tests for ``ExceptionReportService`` (`driver/services/exception_service.py`).

Covers persistence of the acting ``driver_id`` and both work keys, the job
timeline event, ``RiskSignal`` publication on the existing SignalBus path, the
``exception_escalation`` broadcast on ``high``/``critical`` only, the
escalation push emission point, and the best-effort behaviour of every step
downstream of persistence.

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.13, 7.16, 9.7
"""

import pytest

from Agents.overlay.data_contracts import RiskSignal, Severity
from driver.models import ExceptionRequest, ExceptionType, GeoPoint
from driver.services.exception_service import ExceptionReportService
from driver.services.work_ref import WorkRef

TENANT_ID = "t1"
DRIVER_ID = "drv-1"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeES:
    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple] = []
        self._error = error

    async def index_document(self, index, doc_id, document):
        self.calls.append((index, doc_id, document))
        if self._error is not None:
            raise self._error
        return {"result": "created"}


class _FakeJobService:
    def __init__(self, error: Exception | None = None):
        self.events: list[dict] = []
        self._error = error

    async def _append_event(self, **kwargs):
        self.events.append(kwargs)
        if self._error is not None:
            raise self._error
        return "evt-1"


class _FakeSignalBus:
    def __init__(self, error: Exception | None = None):
        self.published: list[RiskSignal] = []
        self._error = error

    async def publish(self, signal):
        self.published.append(signal)
        if self._error is not None:
            raise self._error
        return 1


class _FakeSchedulingWS:
    def __init__(self, error: Exception | None = None):
        self.broadcasts: list[tuple] = []
        self._error = error

    async def broadcast(self, event_type, event_data):
        self.broadcasts.append((event_type, event_data))
        if self._error is not None:
            raise self._error


class _FakeDriverWS:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_to_driver(self, driver_id, message):
        self.sent.append((driver_id, message))


class _FakePushNotifier:
    def __init__(self):
        self.calls: list[dict] = []

    async def notify_exception_escalation(self, *, driver_id, payload):
        self.calls.append({"driver_id": driver_id, "payload": payload})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job_ref(job_doc: dict | None = None) -> WorkRef:
    return WorkRef(
        tenant_id=TENANT_ID,
        driver_id=DRIVER_ID,
        kind="job",
        work_id="JOB_1",
        job_doc={} if job_doc is None else job_doc,
    )


def _order_ref() -> WorkRef:
    return WorkRef(
        tenant_id=TENANT_ID,
        driver_id=DRIVER_ID,
        kind="order",
        work_id="ORD_9",
        order_doc={"tenant_id": TENANT_ID, "assigned_driver_id": DRIVER_ID},
    )


def _body(
    *,
    exception_type: ExceptionType = ExceptionType.ROAD_CLOSURE,
    severity: Severity = Severity.MEDIUM,
    note: str = "Road blocked",
    location: GeoPoint | None = None,
    media_refs: list[str] | None = None,
) -> ExceptionRequest:
    return ExceptionRequest(
        exception_type=exception_type,
        severity=severity,
        note=note,
        location=location,
        media_refs=media_refs,
    )


def _service(**overrides) -> tuple[ExceptionReportService, dict]:
    collaborators = {
        "es_service": _FakeES(),
        "job_service": _FakeJobService(),
        "signal_bus": _FakeSignalBus(),
        "scheduling_ws_manager": _FakeSchedulingWS(),
        "driver_ws_manager": _FakeDriverWS(),
        "push_notifier": _FakePushNotifier(),
    }
    collaborators.update(overrides)
    return ExceptionReportService(**collaborators), collaborators


# ---------------------------------------------------------------------------
# Persistence (R7.1, R7.13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persists_driver_id_type_severity_note_geotag_and_media():
    """The stored document carries R7.1's whole field set. Validates: Requirement 7.1"""
    svc, col = _service()

    result = await svc.report(
        _job_ref(),
        _body(
            exception_type=ExceptionType.CARGO_DAMAGE,
            severity=Severity.LOW,
            note="Seal broken",
            location=GeoPoint(lat=-33.8688, lng=151.2093),
            media_refs=["file-ref-1", "file-ref-2"],
        ),
        request_id="req-1",
    )

    index, doc_id, doc = col["es_service"].calls[0]
    assert index == "driver_exceptions"
    assert doc_id == doc["exception_id"]
    assert doc["driver_id"] == DRIVER_ID
    assert doc["exception_type"] == "cargo_damage"
    assert doc["severity"] == "low"
    assert doc["note"] == "Seal broken"
    assert doc["location"] == {"lat": -33.8688, "lng": 151.2093}
    assert doc["media_refs"] == ["file-ref-1", "file-ref-2"]
    assert doc["tenant_id"] == TENANT_ID
    assert result["data"] == doc
    assert result["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_job_keyed_document_carries_job_id_and_the_jobs_order_id():
    """Validates: Requirements 7.1, 7.13"""
    svc, col = _service()

    await svc.report(_job_ref({"order_id": "ORD_7"}), _body(), request_id="r")

    doc = col["es_service"].calls[0][2]
    assert doc["job_id"] == "JOB_1"
    assert doc["order_id"] == "ORD_7"


@pytest.mark.asyncio
async def test_order_keyed_document_carries_order_id_and_no_job_id():
    """Validates: Requirements 7.13, 7.16"""
    svc, col = _service()

    await svc.report(_order_ref(), _body(), request_id="r")

    doc = col["es_service"].calls[0][2]
    assert doc["order_id"] == "ORD_9"
    assert doc["job_id"] is None


@pytest.mark.asyncio
async def test_persistence_failure_propagates():
    """No stored record means no report. Validates: Requirement 7.1"""
    svc, _ = _service(es_service=_FakeES(error=RuntimeError("es down")))

    with pytest.raises(RuntimeError):
        await svc.report(_job_ref(), _body(), request_id="r")


# ---------------------------------------------------------------------------
# Job timeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_appends_exception_reported_event_on_the_job_path():
    """Validates: Requirement 7.1"""
    svc, col = _service()

    await svc.report(_job_ref(), _body(), request_id="r")

    event = col["job_service"].events[0]
    assert event["event_type"] == "exception_reported"
    assert event["job_id"] == "JOB_1"
    assert event["tenant_id"] == TENANT_ID
    assert event["actor_id"] == DRIVER_ID
    assert event["payload"]["exception_type"] == "road_closure"


@pytest.mark.asyncio
async def test_no_timeline_event_on_the_order_path():
    """An order-keyed report has no job timeline. Validates: Requirement 7.13"""
    svc, col = _service()

    await svc.report(_order_ref(), _body(), request_id="r")

    assert col["job_service"].events == []


@pytest.mark.asyncio
async def test_timeline_failure_does_not_fail_the_report():
    """Validates: Requirement 7.1"""
    svc, col = _service(job_service=_FakeJobService(error=RuntimeError("boom")))

    result = await svc.report(_job_ref(), _body(), request_id="r")

    assert result["data"]["exception_id"]


# ---------------------------------------------------------------------------
# RiskSignal (R7.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publishes_risk_signal_on_the_signal_bus():
    """Validates: Requirement 7.2"""
    svc, col = _service()

    await svc.report(
        _job_ref(),
        _body(exception_type=ExceptionType.VEHICLE_BREAKDOWN, severity=Severity.HIGH),
        request_id="r",
    )

    signal = col["signal_bus"].published[0]
    assert isinstance(signal, RiskSignal)
    assert signal.source_agent == "driver_exception_reporter"
    assert signal.entity_id == "JOB_1"
    assert signal.entity_type == "vehicle_breakdown"
    assert signal.severity == Severity.HIGH
    assert signal.tenant_id == TENANT_ID
    assert signal.confidence == 0.9
    assert signal.ttl_seconds == 3600
    assert signal.context["exception_id"] == col["es_service"].calls[0][1]


@pytest.mark.asyncio
async def test_order_keyed_risk_signal_names_the_order():
    """Validates: Requirements 7.2, 7.16"""
    svc, col = _service()

    await svc.report(_order_ref(), _body(), request_id="r")

    assert col["signal_bus"].published[0].entity_id == "ORD_9"


@pytest.mark.asyncio
async def test_signal_bus_failure_does_not_fail_the_report():
    """Validates: Requirement 7.2"""
    svc, _ = _service(signal_bus=_FakeSignalBus(error=RuntimeError("bus down")))

    result = await svc.report(_job_ref(), _body(), request_id="r")

    assert result["data"]["exception_id"]


@pytest.mark.asyncio
async def test_report_succeeds_without_a_signal_bus():
    """Validates: Requirement 7.2"""
    svc, _ = _service(signal_bus=None)

    result = await svc.report(_job_ref(), _body(), request_id="r")

    assert result["data"]["exception_id"]


# ---------------------------------------------------------------------------
# Escalation (R7.3, R9.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("severity", [Severity.HIGH, Severity.CRITICAL])
async def test_high_and_critical_broadcast_exception_escalation(severity):
    """Validates: Requirement 7.3"""
    svc, col = _service()

    await svc.report(_job_ref(), _body(severity=severity), request_id="r")

    event_type, data = col["scheduling_ws_manager"].broadcasts[0]
    assert event_type == "exception_escalation"
    assert data["severity"] == severity.value
    assert data["job_id"] == "JOB_1"
    assert col["driver_ws_manager"].sent[0][0] == DRIVER_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("severity", [Severity.LOW, Severity.MEDIUM])
async def test_low_and_medium_do_not_broadcast(severity):
    """Validates: Requirement 7.3"""
    svc, col = _service()

    await svc.report(_job_ref(), _body(severity=severity), request_id="r")

    assert col["scheduling_ws_manager"].broadcasts == []
    assert col["driver_ws_manager"].sent == []
    assert col["push_notifier"].calls == []


@pytest.mark.asyncio
async def test_broadcast_failure_does_not_fail_the_report():
    """Validates: Requirement 7.3"""
    svc, _ = _service(
        scheduling_ws_manager=_FakeSchedulingWS(error=RuntimeError("ws down"))
    )

    result = await svc.report(
        _job_ref(), _body(severity=Severity.CRITICAL), request_id="r"
    )

    assert result["data"]["exception_id"]


@pytest.mark.asyncio
async def test_escalation_push_carries_identifiers_and_type_only():
    """Validates: Requirements 9.7, 9.8"""
    svc, col = _service()

    await svc.report(
        _order_ref(),
        _body(exception_type=ExceptionType.WEATHER, severity=Severity.HIGH),
        request_id="r",
    )

    call = col["push_notifier"].calls[0]
    assert call["driver_id"] == DRIVER_ID
    assert call["payload"]["order_id"] == "ORD_9"
    assert call["payload"]["exception_type"] == "weather"
    assert set(call["payload"]) == {
        "tenant_id",
        "order_id",
        "job_id",
        "exception_id",
        "exception_type",
        "severity",
    }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_requires_an_es_service():
    with pytest.raises(ValueError):
        ExceptionReportService(es_service=None)
