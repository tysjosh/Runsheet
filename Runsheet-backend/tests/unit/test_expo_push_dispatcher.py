"""
Unit tests for the driver push channel dispatchers.

Covers the ``ExpoPushDispatcher`` contract required by task 13.1 of the
driver-mobile-app spec:

- missing ``EXPO_ACCESS_TOKEN`` raises ``ValueError`` (R9.17)
- ``channel_name`` is ``push`` and the class subclasses
  ``ChannelDispatcher`` (R9.13)
- retryable provider errors retry 3 times at 1 s / 2 s / 4 s (R9.9)
- ``DeviceNotRegistered`` / ``InvalidCredentials`` prune the
  ``driver_devices`` record (R9.4)
- the payload carries identifiers only — no customer name, phone number,
  or street address (R9.8)
- one ``driver_push_attempts`` document per attempt (R9.10)
- ``StubPushDispatcher`` needs no credential (R9.14)

No network calls are made: the lazily-built HTTP client is replaced with
a recording fake and ``asyncio.sleep`` is patched so the retry ladder is
asserted rather than waited out.

Requirements: 9.4, 9.8, 9.9, 9.10, 9.13, 9.14, 9.16, 9.17
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from driver.services.driver_es_mappings import (
    DRIVER_DEVICES_INDEX,
    DRIVER_PUSH_ATTEMPTS_INDEX,
)
from notifications.services.channel_dispatchers import (
    ChannelDispatcher,
    StubPushDispatcher,
)
from notifications.services.expo_push_dispatcher import ExpoPushDispatcher

ENV = {"EXPO_ACCESS_TOKEN": "expo_test_token"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttpClient:
    """Records every POST and returns queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


class FakeEs:
    def __init__(self):
        self.indexed: list[tuple[str, str, dict]] = []
        self.deleted: list[tuple[str, str]] = []

    async def index_document(self, index, doc_id, document):
        self.indexed.append((index, doc_id, document))
        return {"result": "created"}

    async def delete_document(self, index, doc_id):
        self.deleted.append((index, doc_id))
        return True


def _ok_response(message_id="ticket-1"):
    return FakeResponse(200, {"data": {"status": "ok", "id": message_id}})


def _error_response(error_name, message="provider said no", status=200):
    return FakeResponse(
        status,
        {
            "data": {
                "status": "error",
                "message": message,
                "details": {"error": error_name},
            }
        },
    )


def _make_dispatcher(responses, es=None):
    with patch.dict(os.environ, ENV, clear=True):
        dispatcher = ExpoPushDispatcher(es_service=es)
    client = FakeHttpClient(responses)
    dispatcher._client = client
    return dispatcher, client


def _notification(**overrides):
    notification = {
        "notification_id": "notif-1",
        "notification_type": "driver_assignment",
        "channel": "push",
        "tenant_id": "acme",
        "driver_id": "drv-1",
        "order_id": "ord-4821",
        "delivery_window_start": "2026-06-01T14:00:00Z",
        "delivery_window_end": "2026-06-01T16:00:00Z",
        "devices": [{"device_id": "dev-1", "push_token": "ExponentPushToken[aaa]"}],
    }
    notification.update(overrides)
    return notification


# ---------------------------------------------------------------------------
# Construction — R9.17, R9.13
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_missing_access_token_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="Missing Expo push credentials"):
                ExpoPushDispatcher()

    def test_blank_access_token_raises(self):
        with patch.dict(os.environ, {"EXPO_ACCESS_TOKEN": ""}, clear=True):
            with pytest.raises(ValueError, match="Missing Expo push credentials"):
                ExpoPushDispatcher()

    def test_valid_credential_builds_instance_with_default_endpoint(self):
        with patch.dict(os.environ, ENV, clear=True):
            dispatcher = ExpoPushDispatcher()

        assert dispatcher.channel_name == "push"
        assert isinstance(dispatcher, ChannelDispatcher)
        assert dispatcher._endpoint == "https://exp.host/--/api/v2/push/send"
        # HTTP client is built lazily — nothing constructed at init time.
        assert dispatcher._client is None

    def test_endpoint_override_from_environment(self):
        env = dict(ENV, EXPO_PUSH_ENDPOINT="https://push.internal/send")
        with patch.dict(os.environ, env, clear=True):
            dispatcher = ExpoPushDispatcher()

        assert dispatcher._endpoint == "https://push.internal/send"


# ---------------------------------------------------------------------------
# Success path — R9.16
# ---------------------------------------------------------------------------


class TestSuccessfulDispatch:
    async def test_success_writes_provider_message_id(self):
        es = FakeEs()
        dispatcher, client = _make_dispatcher([_ok_response("ticket-abc")], es=es)
        notification = _notification()

        result = await dispatcher.dispatch(notification)

        assert result == "sent"
        assert notification["provider_message_id"] == "ticket-abc"
        assert "failure_reason" not in notification
        assert len(client.calls) == 1

    async def test_no_registered_device_fails_without_a_provider_call(self):
        dispatcher, client = _make_dispatcher([_ok_response()])
        notification = _notification(devices=[])

        result = await dispatcher.dispatch(notification)

        assert result == "failed"
        assert notification["failure_reason"] == "No registered device for driver"
        assert client.calls == []

    async def test_sent_when_one_of_two_devices_accepts(self):
        es = FakeEs()
        dispatcher, client = _make_dispatcher(
            [_ok_response("ticket-1"), _error_response("MessageTooBig")], es=es
        )
        notification = _notification(
            devices=[
                {"device_id": "dev-1", "push_token": "ExponentPushToken[aaa]"},
                {"device_id": "dev-2", "push_token": "ExponentPushToken[bbb]"},
            ]
        )

        result = await dispatcher.dispatch(notification)

        assert result == "sent"
        assert notification["provider_message_id"] == "ticket-1"
        assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# Retry ladder — R9.9
# ---------------------------------------------------------------------------


class TestRetryLadder:
    @pytest.mark.parametrize(
        "error_name",
        ["TooManyRequests", "InternalServerError", "ServiceUnavailable"],
    )
    async def test_retryable_error_retries_three_times_at_1_2_4_seconds(
        self, error_name
    ):
        es = FakeEs()
        dispatcher, client = _make_dispatcher([_error_response(error_name)], es=es)
        notification = _notification()

        sleeps: list[float] = []

        async def record_sleep(delay):
            sleeps.append(delay)

        with patch(
            "notifications.services.expo_push_dispatcher.asyncio.sleep",
            new=AsyncMock(side_effect=record_sleep),
        ):
            result = await dispatcher.dispatch(notification)

        assert result == "failed"
        # 3 retries after the initial attempt → 4 provider calls.
        assert len(client.calls) == 4
        assert sleeps == [1.0, 2.0, 4.0]
        # One audit document per attempt.
        assert len(es.indexed) == 4

    async def test_retryable_error_that_then_succeeds_stops_retrying(self):
        es = FakeEs()
        dispatcher, client = _make_dispatcher(
            [_error_response("TooManyRequests"), _ok_response("ticket-2")], es=es
        )
        notification = _notification()

        sleeps: list[float] = []

        async def record_sleep(delay):
            sleeps.append(delay)

        with patch(
            "notifications.services.expo_push_dispatcher.asyncio.sleep",
            new=AsyncMock(side_effect=record_sleep),
        ):
            result = await dispatcher.dispatch(notification)

        assert result == "sent"
        assert notification["provider_message_id"] == "ticket-2"
        assert len(client.calls) == 2
        assert sleeps == [1.0]

    async def test_non_retryable_error_is_not_retried(self):
        es = FakeEs()
        dispatcher, client = _make_dispatcher([_error_response("MessageTooBig")], es=es)
        notification = _notification()

        with patch(
            "notifications.services.expo_push_dispatcher.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep_mock:
            result = await dispatcher.dispatch(notification)

        assert result == "failed"
        assert len(client.calls) == 1
        sleep_mock.assert_not_called()
        assert "failure_reason" in notification

    async def test_http_429_is_treated_as_retryable(self):
        es = FakeEs()
        dispatcher, client = _make_dispatcher([FakeResponse(429, {})], es=es)
        notification = _notification()

        with patch(
            "notifications.services.expo_push_dispatcher.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await dispatcher.dispatch(notification)

        assert result == "failed"
        assert len(client.calls) == 4


# ---------------------------------------------------------------------------
# Device pruning — R9.4
# ---------------------------------------------------------------------------


class TestDevicePruning:
    @pytest.mark.parametrize(
        "error_name", ["DeviceNotRegistered", "InvalidCredentials"]
    )
    async def test_invalid_token_prunes_the_registry_record(self, error_name):
        es = FakeEs()
        dispatcher, client = _make_dispatcher([_error_response(error_name)], es=es)
        notification = _notification()

        with patch(
            "notifications.services.expo_push_dispatcher.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep_mock:
            result = await dispatcher.dispatch(notification)

        assert result == "failed"
        assert es.deleted == [(DRIVER_DEVICES_INDEX, "acme:drv-1:dev-1")]
        # Never retried — the token is dead, not the provider.
        assert len(client.calls) == 1
        sleep_mock.assert_not_called()

    async def test_only_the_reported_device_is_pruned(self):
        es = FakeEs()
        dispatcher, _client = _make_dispatcher(
            [_error_response("DeviceNotRegistered"), _ok_response("ticket-9")], es=es
        )
        notification = _notification(
            devices=[
                {"device_id": "dev-dead", "push_token": "ExponentPushToken[dead]"},
                {"device_id": "dev-live", "push_token": "ExponentPushToken[live]"},
            ]
        )

        result = await dispatcher.dispatch(notification)

        assert result == "sent"
        assert es.deleted == [(DRIVER_DEVICES_INDEX, "acme:drv-1:dev-dead")]

    async def test_retryable_error_never_prunes(self):
        es = FakeEs()
        dispatcher, _client = _make_dispatcher(
            [_error_response("ServiceUnavailable")], es=es
        )

        with patch(
            "notifications.services.expo_push_dispatcher.asyncio.sleep",
            new=AsyncMock(),
        ):
            await dispatcher.dispatch(_notification())

        assert es.deleted == []


# ---------------------------------------------------------------------------
# Payload — R9.8
# ---------------------------------------------------------------------------


class TestPayloadExcludesPii:
    async def test_payload_carries_identifiers_only(self):
        es = FakeEs()
        dispatcher, client = _make_dispatcher([_ok_response()], es=es)
        notification = _notification(
            recipient_name="Jane Q. Customer",
            customer_name="Jane Q. Customer",
            customer_phone="+15551234567",
            delivery_address="221B Baker Street",
            message_body="Deliver to Jane Q. Customer at 221B Baker Street",
            subject="Customer-facing subject",
        )

        result = await dispatcher.dispatch(notification)

        assert result == "sent"
        payload = client.calls[0]["json"]
        serialized = repr(payload)
        for secret in (
            "Jane Q. Customer",
            "+15551234567",
            "221B Baker Street",
        ):
            assert secret not in serialized

        assert payload["data"] == {
            "type": "driver_assignment",
            "tenant_id": "acme",
            "order_id": "ord-4821",
            "notification_id": "notif-1",
            "delivery_window_start": "2026-06-01T14:00:00Z",
            "delivery_window_end": "2026-06-01T16:00:00Z",
        }
        assert payload["to"] == "ExponentPushToken[aaa]"

    async def test_title_and_body_render_from_the_default_template_entry(self):
        dispatcher, client = _make_dispatcher([_ok_response()])

        await dispatcher.dispatch(_notification())

        payload = client.calls[0]["json"]
        assert payload["title"] == "New assignment"
        assert payload["body"] == (
            "Order ord-4821 · window 2026-06-01T14:00:00Z–2026-06-01T16:00:00Z"
        )

    async def test_unknown_notification_type_still_carries_no_customer_data(self):
        dispatcher, client = _make_dispatcher([_ok_response()])
        notification = _notification(
            notification_type="something_new",
            customer_name="Jane Q. Customer",
        )

        await dispatcher.dispatch(notification)

        payload = client.calls[0]["json"]
        assert "Jane Q. Customer" not in repr(payload)
        assert payload["data"]["type"] == "something_new"


# ---------------------------------------------------------------------------
# Attempt audit — R9.10
# ---------------------------------------------------------------------------


class TestAttemptAudit:
    async def test_one_document_per_attempt_with_the_declared_fields(self):
        es = FakeEs()
        dispatcher, _client = _make_dispatcher(
            [_error_response("TooManyRequests"), _ok_response("ticket-7")], es=es
        )

        with patch(
            "notifications.services.expo_push_dispatcher.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await dispatcher.dispatch(_notification())

        assert result == "sent"
        assert len(es.indexed) == 2

        expected_fields = {
            "attempt_id",
            "tenant_id",
            "driver_id",
            "device_id",
            "notification_type",
            "outcome",
            "provider_message_id",
            "failure_reason",
            "attempt_number",
            "attempted_at",
        }
        for index, doc_id, document in es.indexed:
            assert index == DRIVER_PUSH_ATTEMPTS_INDEX
            assert doc_id == document["attempt_id"]
            assert set(document) == expected_fields
            assert document["driver_id"] == "drv-1"
            assert document["device_id"] == "dev-1"
            assert document["notification_type"] == "driver_assignment"

        first, second = (doc for _i, _d, doc in es.indexed)
        assert first["attempt_number"] == 1
        assert first["outcome"] == "failed"
        assert first["provider_message_id"] is None
        assert first["failure_reason"]
        assert second["attempt_number"] == 2
        assert second["outcome"] == "sent"
        assert second["provider_message_id"] == "ticket-7"
        assert second["failure_reason"] is None

    async def test_audit_document_carries_no_message_body_or_customer_field(self):
        es = FakeEs()
        dispatcher, _client = _make_dispatcher([_ok_response()], es=es)

        await dispatcher.dispatch(
            _notification(
                customer_name="Jane Q. Customer",
                message_body="Deliver to Jane at 221B Baker Street",
            )
        )

        (_index, _doc_id, document) = es.indexed[0]
        assert "message_body" not in document
        assert "customer_name" not in document
        assert "Jane" not in repr(document)

    async def test_one_document_per_device_per_attempt(self):
        es = FakeEs()
        dispatcher, _client = _make_dispatcher([_ok_response()], es=es)
        notification = _notification(
            devices=[
                {"device_id": "dev-1", "push_token": "ExponentPushToken[aaa]"},
                {"device_id": "dev-2", "push_token": "ExponentPushToken[bbb]"},
            ]
        )

        await dispatcher.dispatch(notification)

        assert len(es.indexed) == 2
        assert {doc["device_id"] for _i, _d, doc in es.indexed} == {"dev-1", "dev-2"}

    async def test_audit_failure_does_not_change_the_delivery_outcome(self):
        es = FakeEs()
        es.index_document = AsyncMock(side_effect=RuntimeError("es down"))
        dispatcher, _client = _make_dispatcher([_ok_response("ticket-x")], es=es)
        notification = _notification()

        result = await dispatcher.dispatch(notification)

        assert result == "sent"
        assert notification["provider_message_id"] == "ticket-x"


# ---------------------------------------------------------------------------
# Stub dispatcher — R9.14
# ---------------------------------------------------------------------------


class TestStubPushDispatcher:
    async def test_stub_needs_no_credential_and_reports_sent(self):
        with patch.dict(os.environ, {}, clear=True):
            dispatcher = StubPushDispatcher()

        assert dispatcher.channel_name == "push"
        assert isinstance(dispatcher, ChannelDispatcher)
        assert await dispatcher.dispatch(_notification()) == "sent"
