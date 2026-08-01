"""
Unit tests for ``Driver_Push_Service``: ``driver/services/driver_push_notifier.py``.

The notifier is exercised over a **real** :class:`DeviceRegistry` on a fake
Elasticsearch and a fake ``ChannelDispatcher``, so each assertion covers the whole
emission path: the duty-status read, the device fan-out, the dict handed to the
dispatcher, and the identifier allow-list.

Two of the four emission points are asserted through their real callers —
``ExceptionReportService.report`` for the escalation and
``ThreadMessageService.send`` for the thread message — because in both cases the
wiring is the thing under test as much as the notifier is.

The sharpest assertions are the negative ones. An assignment for an ``off_duty``
or ``inactive`` driver reaches no dispatcher; an escalation for the same driver
does. A thread message for a driver holding an open realtime connection is not
pushed, because the driver already has it.

Validates: Requirements 7.11, 9.5, 9.6, 9.7, 9.15, 13.8
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from driver.models import ExceptionRequest, ExceptionType, MessageRequest
from driver.services.device_registry import DeviceRegistry
from driver.services.driver_es_mappings import (
    DRIVER_DEVICES_INDEX,
    DRIVER_EXCEPTIONS_INDEX,
    JOB_MESSAGES_INDEX,
)
from driver.services.driver_push_notifier import (
    ASSIGNMENT_NOTIFICATION,
    ASSIGNMENT_REVOKED_NOTIFICATION,
    EXCEPTION_ESCALATION_NOTIFICATION,
    THREAD_MESSAGE_NOTIFICATION,
    DriverPushNotifier,
)
from driver.services.exception_service import ExceptionReportService
from driver.services.message_service import ThreadMessageService
from driver.services.work_ref import WorkRef
from fuel.services.order_es_mappings import DRIVERS_CURRENT_INDEX

TENANT = "t1"
DRIVER = "drv_1"
DEVICE = "device-abc"
SECOND_DEVICE = "device-def"
TOKEN = "OpaqueToken[AbC-123]"
SECOND_TOKEN = "OpaqueToken[def-456]"
ORDER = "ord_4821"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _terms(node: Any, collected: Optional[List[tuple]] = None) -> List[tuple]:
    """Collect every ``{"term": {field: value}}`` pair anywhere in a query."""
    collected = [] if collected is None else collected
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "term" and isinstance(value, dict):
                for field, wanted in value.items():
                    collected.append((field, wanted))
            else:
                _terms(value, collected)
    elif isinstance(node, list):
        for item in node:
            _terms(item, collected)
    return collected


class FakeES:
    """In-memory store supporting ``index_document`` and term-filter search."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Dict[str, Any]]] = {}

    # -- writes ---------------------------------------------------------

    async def index_document(self, index, doc_id, document):
        self.docs.setdefault(index, {})[doc_id] = dict(document)
        return {"result": "created"}

    async def get_document(self, index, doc_id):
        record = self.docs.get(index, {}).get(doc_id)
        return dict(record) if record is not None else None

    async def delete_document(self, index, doc_id):
        return self.docs.get(index, {}).pop(doc_id, None) is not None

    # -- reads ----------------------------------------------------------

    async def search_documents(self, index, query, size=10):
        wanted = _terms(query)
        hits = [
            {"_source": dict(doc)}
            for doc in self.docs.get(index, {}).values()
            if all(doc.get(field) == value for field, value in wanted)
        ]
        return {"hits": {"total": {"value": len(hits)}, "hits": hits[:size]}}

    # -- helpers --------------------------------------------------------

    def seed_driver(self, status: str, *, tenant_id: str = TENANT) -> None:
        self.docs.setdefault(DRIVERS_CURRENT_INDEX, {})[
            f"{tenant_id}:{DRIVER}"
        ] = {
            "driver_id": DRIVER,
            "tenant_id": tenant_id,
            "driver_name": "Ada",
            "status": status,
        }


class FakeDispatcher:
    """A ``ChannelDispatcher`` on the ``push`` channel that records its input."""

    def __init__(self, outcome: str = "sent") -> None:
        self.outcome = outcome
        self.dispatched: List[Dict[str, Any]] = []

    @property
    def channel_name(self) -> str:
        return "push"

    async def dispatch(self, notification: dict) -> str:
        self.dispatched.append(dict(notification))
        if self.outcome == "sent":
            notification["provider_message_id"] = "ticket-1"
        else:
            notification["failure_reason"] = "provider down"
        return self.outcome


class FakeWSManager:
    """Realtime manager stub: connection state plus recorded sends."""

    def __init__(self, *, connected: bool = False) -> None:
        self._connected = connected
        self.sent: List[tuple] = []

    def is_driver_connected(self, driver_id: str) -> bool:
        return self._connected

    async def send_to_driver(self, driver_id, message):
        self.sent.append((driver_id, message))
        return True

    async def broadcast(self, event_type, data):
        self.sent.append((event_type, data))
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def es() -> FakeES:
    store = FakeES()
    store.seed_driver("active")
    return store


@pytest.fixture
def registry(es: FakeES) -> DeviceRegistry:
    return DeviceRegistry(es_service=es)


@pytest.fixture
def dispatcher() -> FakeDispatcher:
    return FakeDispatcher()


async def _register(
    registry: DeviceRegistry, device_id: str, token: str
) -> None:
    await registry.register(
        TENANT, DRIVER, device_id, push_token=token, platform="ios"
    )


def _notifier(
    es: FakeES,
    registry: DeviceRegistry,
    dispatcher: FakeDispatcher,
    *,
    ws_manager=None,
) -> DriverPushNotifier:
    return DriverPushNotifier(
        es_service=es,
        device_registry=registry,
        push_dispatcher=dispatcher,
        driver_ws_manager=ws_manager,
    )


# ---------------------------------------------------------------------------
# Emission point 1 — order assigned to a driver (R9.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assignment_reaches_every_registered_device(es, registry, dispatcher):
    """An assignment sends to every device the driver has registered.

    Validates: Requirements 9.5
    """
    await _register(registry, DEVICE, TOKEN)
    await _register(registry, SECOND_DEVICE, SECOND_TOKEN)

    delivered = await _notifier(es, registry, dispatcher).notify_assignment(
        driver_id=DRIVER,
        payload={
            "tenant_id": TENANT,
            "order_id": ORDER,
            "delivery_window_start": "2026-05-08T14:00:00Z",
            "delivery_window_end": "2026-05-08T16:00:00Z",
        },
    )

    assert delivered is True
    assert len(dispatcher.dispatched) == 1
    notification = dispatcher.dispatched[0]
    assert notification["notification_type"] == ASSIGNMENT_NOTIFICATION
    assert notification["channel"] == "push"
    assert notification["tenant_id"] == TENANT
    assert notification["driver_id"] == DRIVER
    assert notification["order_id"] == ORDER
    assert {
        (device["device_id"], device["push_token"])
        for device in notification["devices"]
    } == {(DEVICE, TOKEN), (SECOND_DEVICE, SECOND_TOKEN)}


@pytest.mark.asyncio
async def test_order_dispatched_emits_the_assignment(es, registry, dispatcher):
    """``order.dispatched`` is the assignment emission point.

    Validates: Requirements 9.5
    """
    await _register(registry, DEVICE, TOKEN)

    await _notifier(es, registry, dispatcher).on_order_dispatched(
        {
            "order_id": ORDER,
            "tenant_id": TENANT,
            "assigned_driver_id": DRIVER,
            "status": "dispatched",
            # PII the notifier must not forward (R9.8).
            "customer_name": "Acme Fuel Depot",
            "customer_phone": "+15555550123",
            "ship_to_address": "14 Mill Lane",
        }
    )

    assert len(dispatcher.dispatched) == 1
    notification = dispatcher.dispatched[0]
    assert notification["notification_type"] == ASSIGNMENT_NOTIFICATION
    assert notification["order_id"] == ORDER
    serialized = repr(notification)
    assert "Acme Fuel Depot" not in serialized
    assert "+15555550123" not in serialized
    assert "Mill Lane" not in serialized


@pytest.mark.asyncio
async def test_order_dispatched_without_an_assigned_driver_emits_nothing(
    es, registry, dispatcher
):
    """An unassigned order has nobody to alert."""
    await _register(registry, DEVICE, TOKEN)

    await _notifier(es, registry, dispatcher).on_order_dispatched(
        {"order_id": ORDER, "tenant_id": TENANT, "status": "dispatched"}
    )

    assert dispatcher.dispatched == []


# ---------------------------------------------------------------------------
# Emission point 2 — assignment revoked (R9.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revocation_identifies_the_revoked_order(es, registry, dispatcher):
    """A revocation names the order that is no longer the driver's.

    Validates: Requirements 9.6
    """
    await _register(registry, DEVICE, TOKEN)

    delivered = await _notifier(
        es, registry, dispatcher
    ).notify_assignment_revoked(
        driver_id=DRIVER, payload={"tenant_id": TENANT, "order_id": ORDER}
    )

    assert delivered is True
    notification = dispatcher.dispatched[0]
    assert notification["notification_type"] == ASSIGNMENT_REVOKED_NOTIFICATION
    assert notification["order_id"] == ORDER


# ---------------------------------------------------------------------------
# Emission point 3 — exception escalation, through its real caller (R9.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_from_the_exception_service(es, registry, dispatcher):
    """A ``high`` severity report emits the escalation push.

    Validates: Requirements 9.7
    """
    await _register(registry, DEVICE, TOKEN)
    notifier = _notifier(es, registry, dispatcher)
    service = ExceptionReportService(es_service=es, push_notifier=notifier)

    result = await service.report(
        WorkRef(
            tenant_id=TENANT,
            driver_id=DRIVER,
            kind="order",
            work_id=ORDER,
            order_doc={"order_id": ORDER, "tenant_id": TENANT},
        ),
        ExceptionRequest(
            exception_type=ExceptionType.VEHICLE_BREAKDOWN,
            severity="high",
            note="Coolant leak on the hard shoulder",
        ),
        request_id="req-1",
    )

    assert result["data"]["severity"] == "high"
    assert es.docs[DRIVER_EXCEPTIONS_INDEX]
    notification = dispatcher.dispatched[0]
    assert notification["notification_type"] == EXCEPTION_ESCALATION_NOTIFICATION
    assert notification["order_id"] == ORDER
    assert notification["exception_type"] == "vehicle_breakdown"


@pytest.mark.asyncio
async def test_low_severity_report_emits_no_push(es, registry, dispatcher):
    """Only ``high`` and ``critical`` escalate.

    Validates: Requirements 9.7
    """
    await _register(registry, DEVICE, TOKEN)
    service = ExceptionReportService(
        es_service=es, push_notifier=_notifier(es, registry, dispatcher)
    )

    await service.report(
        WorkRef(tenant_id=TENANT, driver_id=DRIVER, kind="order", work_id=ORDER),
        ExceptionRequest(
            exception_type=ExceptionType.WEATHER, severity="low", note="Drizzle"
        ),
        request_id="req-2",
    )

    assert dispatcher.dispatched == []


# ---------------------------------------------------------------------------
# Emission point 4 — thread message, through its real caller (R7.11)
# ---------------------------------------------------------------------------


def _message_service(es, notifier, ws_manager) -> ThreadMessageService:
    return ThreadMessageService(
        es_service=es,
        driver_ws_manager=ws_manager,
        push_notifier=notifier,
    )


def _thread_ref() -> WorkRef:
    return WorkRef(
        tenant_id=TENANT,
        driver_id=DRIVER,
        kind="order",
        work_id=ORDER,
        order_doc={
            "order_id": ORDER,
            "tenant_id": TENANT,
            "assigned_driver_id": DRIVER,
        },
    )


@pytest.mark.asyncio
async def test_thread_message_pushes_when_the_driver_is_offline(
    es, registry, dispatcher
):
    """A message for a disconnected driver becomes a push.

    Validates: Requirements 7.11
    """
    await _register(registry, DEVICE, TOKEN)
    ws_manager = FakeWSManager(connected=False)
    notifier = _notifier(es, registry, dispatcher, ws_manager=ws_manager)

    await _message_service(es, notifier, ws_manager).send(
        _thread_ref(),
        MessageRequest(body="Gate code changed", sender_id=DRIVER, sender_role="driver"),
        sender_id=DRIVER,
        sender_role="driver",
        request_id="req-3",
    )

    assert es.docs[JOB_MESSAGES_INDEX]
    notification = dispatcher.dispatched[0]
    assert notification["notification_type"] == THREAD_MESSAGE_NOTIFICATION
    assert notification["order_id"] == ORDER
    # The body is the thing the app fetches over an authenticated request.
    assert "Gate code changed" not in repr(notification)


@pytest.mark.asyncio
async def test_thread_message_suppressed_while_the_driver_is_connected(
    es, registry, dispatcher
):
    """A connected driver already has the message, so no push is emitted.

    Validates: Requirements 7.11
    """
    await _register(registry, DEVICE, TOKEN)
    ws_manager = FakeWSManager(connected=True)
    notifier = _notifier(es, registry, dispatcher, ws_manager=ws_manager)

    await _message_service(es, notifier, ws_manager).send(
        _thread_ref(),
        MessageRequest(body="Gate code changed", sender_id=DRIVER, sender_role="driver"),
        sender_id=DRIVER,
        sender_role="driver",
        request_id="req-4",
    )

    assert es.docs[JOB_MESSAGES_INDEX]  # the message is still persisted
    assert ws_manager.sent  # and still delivered over the realtime channel
    assert dispatcher.dispatched == []


# ---------------------------------------------------------------------------
# R13.8 — duty-status suppression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["off_duty", "inactive"])
@pytest.mark.asyncio
async def test_assignment_suppressed_while_unavailable(
    es, registry, dispatcher, status
):
    """An assignment is dropped while the driver is off duty or inactive.

    Validates: Requirements 13.8
    """
    await _register(registry, DEVICE, TOKEN)
    es.seed_driver(status)

    delivered = await _notifier(es, registry, dispatcher).notify_assignment(
        driver_id=DRIVER, payload={"tenant_id": TENANT, "order_id": ORDER}
    )

    assert delivered is False
    assert dispatcher.dispatched == []


@pytest.mark.parametrize("status", ["off_duty", "inactive"])
@pytest.mark.asyncio
async def test_revocation_suppressed_while_unavailable(
    es, registry, dispatcher, status
):
    """A revocation is an assignment-lifecycle alert and is dropped too.

    Validates: Requirements 13.8
    """
    await _register(registry, DEVICE, TOKEN)
    es.seed_driver(status)

    delivered = await _notifier(
        es, registry, dispatcher
    ).notify_assignment_revoked(
        driver_id=DRIVER, payload={"tenant_id": TENANT, "order_id": ORDER}
    )

    assert delivered is False
    assert dispatcher.dispatched == []


@pytest.mark.parametrize("status", ["off_duty", "inactive"])
@pytest.mark.asyncio
async def test_escalation_not_suppressed_while_unavailable(
    es, registry, dispatcher, status
):
    """An escalation for work already assigned still goes out.

    Validates: Requirements 13.8
    """
    await _register(registry, DEVICE, TOKEN)
    es.seed_driver(status)

    delivered = await _notifier(
        es, registry, dispatcher
    ).notify_exception_escalation(
        driver_id=DRIVER,
        payload={
            "tenant_id": TENANT,
            "order_id": ORDER,
            "exception_type": "vehicle_breakdown",
        },
    )

    assert delivered is True
    assert (
        dispatcher.dispatched[0]["notification_type"]
        == EXCEPTION_ESCALATION_NOTIFICATION
    )


@pytest.mark.asyncio
async def test_assignment_delivered_while_on_break(es, registry, dispatcher):
    """``on_break`` is not one of the two suppressing statuses.

    Validates: Requirements 13.8
    """
    await _register(registry, DEVICE, TOKEN)
    es.seed_driver("on_break")

    delivered = await _notifier(es, registry, dispatcher).notify_assignment(
        driver_id=DRIVER, payload={"tenant_id": TENANT, "order_id": ORDER}
    )

    assert delivered is True


# ---------------------------------------------------------------------------
# Failure containment and provider independence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_registered_device_is_not_an_error(es, registry, dispatcher):
    """A driver with no device reaches no dispatcher and raises nothing."""
    delivered = await _notifier(es, registry, dispatcher).notify_assignment(
        driver_id=DRIVER, payload={"tenant_id": TENANT, "order_id": ORDER}
    )

    assert delivered is False
    assert dispatcher.dispatched == []


@pytest.mark.asyncio
async def test_a_raising_dispatcher_never_reaches_the_caller(es, registry):
    """A provider failure is a logged event, never the caller's exception."""

    class ExplodingDispatcher(FakeDispatcher):
        async def dispatch(self, notification: dict) -> str:
            raise RuntimeError("provider unreachable")

    await _register(registry, DEVICE, TOKEN)

    delivered = await _notifier(
        es, registry, ExplodingDispatcher()
    ).notify_assignment(
        driver_id=DRIVER, payload={"tenant_id": TENANT, "order_id": ORDER}
    )

    assert delivered is False


@pytest.mark.asyncio
async def test_a_failed_dispatch_reports_not_delivered(es, registry):
    """``'failed'`` from the dispatcher is reported, not raised."""
    await _register(registry, DEVICE, TOKEN)

    delivered = await _notifier(
        es, registry, FakeDispatcher(outcome="failed")
    ).notify_assignment(
        driver_id=DRIVER, payload={"tenant_id": TENANT, "order_id": ORDER}
    )

    assert delivered is False


@pytest.mark.asyncio
async def test_no_dispatcher_wired_is_not_an_error(es, registry):
    """Absent a ``push`` dispatcher the emission is a no-op."""
    await _register(registry, DEVICE, TOKEN)

    notifier = DriverPushNotifier(es_service=es, device_registry=registry)

    assert (
        await notifier.notify_assignment(
            driver_id=DRIVER, payload={"tenant_id": TENANT, "order_id": ORDER}
        )
        is False
    )


@pytest.mark.asyncio
async def test_dispatcher_resolved_by_channel_name(es, registry, dispatcher):
    """The dispatcher is found by the channel identifier ``push`` alone.

    Validates: Requirements 9.15
    """
    await _register(registry, DEVICE, TOKEN)

    class FakePipeline:
        def __init__(self) -> None:
            self._dispatchers = {"sms": object(), "push": dispatcher}

    notifier = DriverPushNotifier(
        es_service=es,
        device_registry=registry,
        notification_service=FakePipeline(),
    )

    assert (
        await notifier.notify_assignment(
            driver_id=DRIVER, payload={"tenant_id": TENANT, "order_id": ORDER}
        )
        is True
    )


def test_the_notifier_names_no_push_provider():
    """The notifier module references no provider module, endpoint, or credential.

    The provider is named in exactly one module, and this is not it (R9.15).

    Validates: Requirements 9.15
    """
    module = (
        Path(__file__).resolve().parents[2]
        / "driver"
        / "services"
        / "driver_push_notifier.py"
    )
    source = module.read_text(encoding="utf-8").lower()

    for token in ("expo", "exp.host", "fcm", "apns", "onesignal"):
        assert token not in source, f"provider token '{token}' leaked into {module.name}"


@pytest.mark.asyncio
async def test_devices_are_scoped_to_the_driver_and_tenant(es, registry, dispatcher):
    """The fan-out reads only the subject driver's own registrations."""
    await _register(registry, DEVICE, TOKEN)
    await registry.register(
        TENANT, "drv_other", SECOND_DEVICE, push_token=SECOND_TOKEN, platform="android"
    )
    await registry.register(
        "t2", DRIVER, SECOND_DEVICE, push_token=SECOND_TOKEN, platform="android"
    )

    await _notifier(es, registry, dispatcher).notify_assignment(
        driver_id=DRIVER, payload={"tenant_id": TENANT, "order_id": ORDER}
    )

    devices = dispatcher.dispatched[0]["devices"]
    assert [device["device_id"] for device in devices] == [DEVICE]
    assert es.docs[DRIVER_DEVICES_INDEX]
