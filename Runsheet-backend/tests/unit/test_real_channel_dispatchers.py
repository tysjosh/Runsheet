"""
Unit tests for real channel dispatchers (Twilio SMS, Twilio WhatsApp, SendGrid Email).

Tests cover:
- Credential validation (ValueError on missing env vars)
- Channel name correctness
- Successful dispatch with provider_message_id capture
- Rate-limit (429) handling
- General error handling
- WhatsApp prefix logic
- Bootstrap fallback logic

All provider SDKs are mocked — these tests do not make real API calls.
The twilio/sendgrid packages are optional runtime dependencies and may
not be installed in the test environment.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6
"""

import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from notifications.services.channel_dispatchers import ChannelDispatcher


# ---------------------------------------------------------------------------
# Helpers — install fake twilio / sendgrid modules so imports succeed
# ---------------------------------------------------------------------------


def _install_fake_twilio():
    """Install a minimal fake ``twilio`` package into sys.modules."""
    twilio = types.ModuleType("twilio")
    twilio_rest = types.ModuleType("twilio.rest")
    twilio_base = types.ModuleType("twilio.base")
    twilio_base_exc = types.ModuleType("twilio.base.exceptions")

    class FakeClient:
        def __init__(self, sid, token):
            self.messages = MagicMock()

    class FakeTwilioRestException(Exception):
        def __init__(self, status=500, msg="error"):
            self.status = status
            self.msg = msg
            super().__init__(msg)

    twilio_rest.Client = FakeClient
    twilio_base_exc.TwilioRestException = FakeTwilioRestException

    sys.modules.setdefault("twilio", twilio)
    sys.modules.setdefault("twilio.rest", twilio_rest)
    sys.modules.setdefault("twilio.base", twilio_base)
    sys.modules.setdefault("twilio.base.exceptions", twilio_base_exc)

    return FakeClient, FakeTwilioRestException


def _install_fake_sendgrid():
    """Install a minimal fake ``sendgrid`` package into sys.modules."""
    sendgrid_mod = types.ModuleType("sendgrid")
    sendgrid_helpers = types.ModuleType("sendgrid.helpers")
    sendgrid_helpers_mail = types.ModuleType("sendgrid.helpers.mail")

    class FakeSendGridAPIClient:
        def __init__(self, api_key):
            self.send = MagicMock()

    class FakeMail:
        def __init__(self, from_email=None, to_emails=None, subject=None,
                     plain_text_content=None):
            self.from_email = from_email
            self.to_emails = to_emails
            self.subject = subject
            self.plain_text_content = plain_text_content

    sendgrid_mod.SendGridAPIClient = FakeSendGridAPIClient
    sendgrid_helpers_mail.Mail = FakeMail

    sys.modules.setdefault("sendgrid", sendgrid_mod)
    sys.modules.setdefault("sendgrid.helpers", sendgrid_helpers)
    sys.modules.setdefault("sendgrid.helpers.mail", sendgrid_helpers_mail)

    return FakeSendGridAPIClient, FakeMail


# Install fakes before importing dispatchers
FakeClient, FakeTwilioRestException = _install_fake_twilio()
FakeSendGridAPIClient, FakeMail = _install_fake_sendgrid()


# ---------------------------------------------------------------------------
# Twilio SMS Dispatcher
# ---------------------------------------------------------------------------


class TestTwilioSmsDispatcher:
    """Tests for TwilioSmsDispatcher."""

    def test_missing_credentials_raises(self):
        """ValueError raised when Twilio SMS env vars are missing."""
        with patch.dict(os.environ, {}, clear=True):
            from notifications.services.twilio_sms_dispatcher import (
                TwilioSmsDispatcher,
            )

            with pytest.raises(ValueError, match="Missing Twilio SMS credentials"):
                TwilioSmsDispatcher()

    def test_partial_credentials_raises(self):
        """ValueError raised when only some Twilio SMS env vars are set."""
        env = {"TWILIO_ACCOUNT_SID": "AC123", "TWILIO_AUTH_TOKEN": "tok"}
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.twilio_sms_dispatcher import (
                TwilioSmsDispatcher,
            )

            with pytest.raises(ValueError, match="Missing Twilio SMS credentials"):
                TwilioSmsDispatcher()

    def test_valid_credentials_creates_instance(self):
        """Instance created when all Twilio SMS env vars are present."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_FROM_NUMBER": "+15551234567",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.twilio_sms_dispatcher import (
                TwilioSmsDispatcher,
            )

            d = TwilioSmsDispatcher()
            assert d.channel_name == "sms"
            assert isinstance(d, ChannelDispatcher)

    async def test_dispatch_success(self):
        """Successful dispatch captures provider_message_id."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_FROM_NUMBER": "+15551234567",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.twilio_sms_dispatcher import (
                TwilioSmsDispatcher,
            )

            d = TwilioSmsDispatcher()

        mock_message = MagicMock()
        mock_message.sid = "SM_test_sid_123"
        d._client.messages.create = MagicMock(return_value=mock_message)

        notification = {
            "recipient_reference": "+254700000000",
            "message_body": "Your delivery is on the way",
        }
        result = await d.dispatch(notification)

        assert result == "sent"
        assert notification["provider_message_id"] == "SM_test_sid_123"
        d._client.messages.create.assert_called_once_with(
            body="Your delivery is on the way",
            from_="+15551234567",
            to="+254700000000",
        )

    async def test_dispatch_rate_limit_429(self):
        """Rate-limit (429) response is captured in failure_reason."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_FROM_NUMBER": "+15551234567",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.twilio_sms_dispatcher import (
                TwilioSmsDispatcher,
            )

            d = TwilioSmsDispatcher()

        exc = FakeTwilioRestException(status=429, msg="Too many requests")
        d._client.messages.create = MagicMock(side_effect=exc)

        notification = {
            "recipient_reference": "+254700000000",
            "message_body": "Hello",
        }
        result = await d.dispatch(notification)

        assert result == "failed"
        assert "Rate limited" in notification["failure_reason"]

    async def test_dispatch_non_429_twilio_error(self):
        """Non-429 Twilio error is captured in failure_reason."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_FROM_NUMBER": "+15551234567",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.twilio_sms_dispatcher import (
                TwilioSmsDispatcher,
            )

            d = TwilioSmsDispatcher()

        exc = FakeTwilioRestException(status=400, msg="Invalid number")
        d._client.messages.create = MagicMock(side_effect=exc)

        notification = {
            "recipient_reference": "+254700000000",
            "message_body": "Hello",
        }
        result = await d.dispatch(notification)

        assert result == "failed"
        assert "Invalid number" in notification["failure_reason"]
        assert "Rate limited" not in notification["failure_reason"]

    async def test_dispatch_general_exception(self):
        """Unexpected exception is captured in failure_reason."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_FROM_NUMBER": "+15551234567",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.twilio_sms_dispatcher import (
                TwilioSmsDispatcher,
            )

            d = TwilioSmsDispatcher()

        d._client.messages.create = MagicMock(
            side_effect=RuntimeError("Connection failed")
        )

        notification = {
            "recipient_reference": "+254700000000",
            "message_body": "Hello",
        }
        result = await d.dispatch(notification)

        assert result == "failed"
        assert "Connection failed" in notification["failure_reason"]


# ---------------------------------------------------------------------------
# Twilio WhatsApp Dispatcher
# ---------------------------------------------------------------------------


class TestTwilioWhatsAppDispatcher:
    """Tests for TwilioWhatsAppDispatcher."""

    def test_missing_credentials_raises(self):
        """ValueError raised when Twilio WhatsApp env vars are missing."""
        with patch.dict(os.environ, {}, clear=True):
            from notifications.services.twilio_whatsapp_dispatcher import (
                TwilioWhatsAppDispatcher,
            )

            with pytest.raises(ValueError, match="Missing Twilio WhatsApp credentials"):
                TwilioWhatsAppDispatcher()

    def test_valid_credentials_creates_instance(self):
        """Instance created when all Twilio WhatsApp env vars are present."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_WHATSAPP_FROM_NUMBER": "+15559876543",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.twilio_whatsapp_dispatcher import (
                TwilioWhatsAppDispatcher,
            )

            d = TwilioWhatsAppDispatcher()
            assert d.channel_name == "whatsapp"
            assert isinstance(d, ChannelDispatcher)

    async def test_dispatch_adds_whatsapp_prefix(self):
        """Dispatch adds whatsapp: prefix to from and to numbers."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_WHATSAPP_FROM_NUMBER": "+15559876543",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.twilio_whatsapp_dispatcher import (
                TwilioWhatsAppDispatcher,
            )

            d = TwilioWhatsAppDispatcher()

        mock_message = MagicMock()
        mock_message.sid = "WA_test_sid_456"
        d._client.messages.create = MagicMock(return_value=mock_message)

        notification = {
            "recipient_reference": "+254700000000",
            "message_body": "ETA updated",
        }
        result = await d.dispatch(notification)

        assert result == "sent"
        assert notification["provider_message_id"] == "WA_test_sid_456"
        d._client.messages.create.assert_called_once_with(
            body="ETA updated",
            from_="whatsapp:+15559876543",
            to="whatsapp:+254700000000",
        )

    async def test_dispatch_preserves_existing_prefix(self):
        """Dispatch does not double-prefix numbers already prefixed."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_WHATSAPP_FROM_NUMBER": "whatsapp:+15559876543",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.twilio_whatsapp_dispatcher import (
                TwilioWhatsAppDispatcher,
            )

            d = TwilioWhatsAppDispatcher()

        mock_message = MagicMock()
        mock_message.sid = "WA_test_sid_789"
        d._client.messages.create = MagicMock(return_value=mock_message)

        notification = {
            "recipient_reference": "whatsapp:+254700000000",
            "message_body": "Hello",
        }
        result = await d.dispatch(notification)

        assert result == "sent"
        d._client.messages.create.assert_called_once_with(
            body="Hello",
            from_="whatsapp:+15559876543",
            to="whatsapp:+254700000000",
        )

    async def test_dispatch_rate_limit_429(self):
        """Rate-limit (429) response is captured in failure_reason."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_WHATSAPP_FROM_NUMBER": "+15559876543",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.twilio_whatsapp_dispatcher import (
                TwilioWhatsAppDispatcher,
            )

            d = TwilioWhatsAppDispatcher()

        exc = FakeTwilioRestException(status=429, msg="Too many requests")
        d._client.messages.create = MagicMock(side_effect=exc)

        notification = {
            "recipient_reference": "+254700000000",
            "message_body": "Hello",
        }
        result = await d.dispatch(notification)

        assert result == "failed"
        assert "Rate limited" in notification["failure_reason"]

    async def test_dispatch_general_exception(self):
        """Unexpected exception is captured in failure_reason."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_WHATSAPP_FROM_NUMBER": "+15559876543",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.twilio_whatsapp_dispatcher import (
                TwilioWhatsAppDispatcher,
            )

            d = TwilioWhatsAppDispatcher()

        d._client.messages.create = MagicMock(
            side_effect=RuntimeError("Network error")
        )

        notification = {
            "recipient_reference": "+254700000000",
            "message_body": "Hello",
        }
        result = await d.dispatch(notification)

        assert result == "failed"
        assert "Network error" in notification["failure_reason"]


# ---------------------------------------------------------------------------
# SendGrid Email Dispatcher
# ---------------------------------------------------------------------------


class TestSendGridEmailDispatcher:
    """Tests for SendGridEmailDispatcher."""

    def test_missing_credentials_raises(self):
        """ValueError raised when SendGrid env vars are missing."""
        with patch.dict(os.environ, {}, clear=True):
            from notifications.services.sendgrid_email_dispatcher import (
                SendGridEmailDispatcher,
            )

            with pytest.raises(ValueError, match="Missing SendGrid credentials"):
                SendGridEmailDispatcher()

    def test_partial_credentials_raises(self):
        """ValueError raised when only API key is set."""
        env = {"SENDGRID_API_KEY": "SG.test"}
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.sendgrid_email_dispatcher import (
                SendGridEmailDispatcher,
            )

            with pytest.raises(ValueError, match="Missing SendGrid credentials"):
                SendGridEmailDispatcher()

    def test_valid_credentials_creates_instance(self):
        """Instance created when all SendGrid env vars are present."""
        env = {
            "SENDGRID_API_KEY": "SG.test_key",
            "SENDGRID_FROM_EMAIL": "noreply@example.com",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.sendgrid_email_dispatcher import (
                SendGridEmailDispatcher,
            )

            d = SendGridEmailDispatcher()
            assert d.channel_name == "email"
            assert isinstance(d, ChannelDispatcher)

    async def test_dispatch_success_with_message_id(self):
        """Successful dispatch captures provider_message_id from header."""
        env = {
            "SENDGRID_API_KEY": "SG.test_key",
            "SENDGRID_FROM_EMAIL": "noreply@example.com",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.sendgrid_email_dispatcher import (
                SendGridEmailDispatcher,
            )

            d = SendGridEmailDispatcher()

        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.headers = {"X-Message-Id": "sg_msg_abc123"}
        d._client.send = MagicMock(return_value=mock_response)

        notification = {
            "recipient_reference": "user@example.com",
            "subject": "Delivery Update",
            "message_body": "Your order is on the way.",
        }
        result = await d.dispatch(notification)

        assert result == "sent"
        assert notification["provider_message_id"] == "sg_msg_abc123"

    async def test_dispatch_rate_limit_429(self):
        """Rate-limit (429) response is captured in failure_reason."""
        env = {
            "SENDGRID_API_KEY": "SG.test_key",
            "SENDGRID_FROM_EMAIL": "noreply@example.com",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.sendgrid_email_dispatcher import (
                SendGridEmailDispatcher,
            )

            d = SendGridEmailDispatcher()

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        d._client.send = MagicMock(return_value=mock_response)

        notification = {
            "recipient_reference": "user@example.com",
            "subject": "Test",
            "message_body": "Hello",
        }
        result = await d.dispatch(notification)

        assert result == "failed"
        assert "Rate limited" in notification["failure_reason"]

    async def test_dispatch_server_error(self):
        """Non-success status code is captured in failure_reason."""
        env = {
            "SENDGRID_API_KEY": "SG.test_key",
            "SENDGRID_FROM_EMAIL": "noreply@example.com",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.sendgrid_email_dispatcher import (
                SendGridEmailDispatcher,
            )

            d = SendGridEmailDispatcher()

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        d._client.send = MagicMock(return_value=mock_response)

        notification = {
            "recipient_reference": "user@example.com",
            "subject": "Test",
            "message_body": "Hello",
        }
        result = await d.dispatch(notification)

        assert result == "failed"
        assert "500" in notification["failure_reason"]

    async def test_dispatch_exception(self):
        """Unexpected exception is captured in failure_reason."""
        env = {
            "SENDGRID_API_KEY": "SG.test_key",
            "SENDGRID_FROM_EMAIL": "noreply@example.com",
        }
        with patch.dict(os.environ, env, clear=True):
            from notifications.services.sendgrid_email_dispatcher import (
                SendGridEmailDispatcher,
            )

            d = SendGridEmailDispatcher()

        d._client.send = MagicMock(side_effect=RuntimeError("Connection refused"))

        notification = {
            "recipient_reference": "user@example.com",
            "subject": "Test",
            "message_body": "Hello",
        }
        result = await d.dispatch(notification)

        assert result == "failed"
        assert "Connection refused" in notification["failure_reason"]


# ---------------------------------------------------------------------------
# Bootstrap fallback logic
# ---------------------------------------------------------------------------


class TestBootstrapDispatcherFallback:
    """Tests for the _create_dispatchers fallback logic in bootstrap."""

    def test_stubs_when_no_env_vars(self):
        """All stubs are returned when no provider env vars are set."""
        with patch.dict(os.environ, {}, clear=True):
            from bootstrap.notifications import _create_dispatchers

            dispatchers = _create_dispatchers()

        assert len(dispatchers) == 3
        names = {d.channel_name for d in dispatchers}
        assert names == {"sms", "whatsapp", "email"}

        from notifications.services.channel_dispatchers import (
            StubSmsDispatcher,
            StubEmailDispatcher,
            StubWhatsAppDispatcher,
        )

        types_set = {type(d) for d in dispatchers}
        assert StubSmsDispatcher in types_set
        assert StubEmailDispatcher in types_set
        assert StubWhatsAppDispatcher in types_set

    def test_real_dispatchers_when_env_vars_present(self):
        """Real dispatchers are returned when all env vars are set."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_FROM_NUMBER": "+15551234567",
            "TWILIO_WHATSAPP_FROM_NUMBER": "+15559876543",
            "SENDGRID_API_KEY": "SG.test_key",
            "SENDGRID_FROM_EMAIL": "noreply@example.com",
        }
        with patch.dict(os.environ, env, clear=True):
            from bootstrap.notifications import _create_dispatchers

            dispatchers = _create_dispatchers()

        assert len(dispatchers) == 3
        names = {d.channel_name for d in dispatchers}
        assert names == {"sms", "whatsapp", "email"}

        from notifications.services.twilio_sms_dispatcher import TwilioSmsDispatcher
        from notifications.services.twilio_whatsapp_dispatcher import (
            TwilioWhatsAppDispatcher,
        )
        from notifications.services.sendgrid_email_dispatcher import (
            SendGridEmailDispatcher,
        )

        types_set = {type(d) for d in dispatchers}
        assert TwilioSmsDispatcher in types_set
        assert TwilioWhatsAppDispatcher in types_set
        assert SendGridEmailDispatcher in types_set

    def test_partial_env_vars_mixed_dispatchers(self):
        """Only channels with full credentials get real dispatchers."""
        env = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "tok_test",
            "TWILIO_FROM_NUMBER": "+15551234567",
            # WhatsApp from number missing
            # SendGrid vars missing
        }
        with patch.dict(os.environ, env, clear=True):
            from bootstrap.notifications import _create_dispatchers

            dispatchers = _create_dispatchers()

        assert len(dispatchers) == 3
        dispatcher_map = {d.channel_name: d for d in dispatchers}

        from notifications.services.twilio_sms_dispatcher import TwilioSmsDispatcher
        from notifications.services.channel_dispatchers import (
            StubWhatsAppDispatcher,
            StubEmailDispatcher,
        )

        assert isinstance(dispatcher_map["sms"], TwilioSmsDispatcher)
        assert isinstance(dispatcher_map["whatsapp"], StubWhatsAppDispatcher)
        assert isinstance(dispatcher_map["email"], StubEmailDispatcher)
