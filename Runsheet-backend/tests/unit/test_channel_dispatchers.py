"""
Unit tests for ChannelDispatcher ABC and stub implementations.

Requirements: 2.1, 2.4, 2.5, 2.6
"""

import pytest

from notifications.services.channel_dispatchers import (
    ChannelDispatcher,
    StubEmailDispatcher,
    StubSmsDispatcher,
    StubWhatsAppDispatcher,
)


# ---------------------------------------------------------------------------
# ABC contract tests
# ---------------------------------------------------------------------------


class TestChannelDispatcherABC:
    """Verify the abstract base class enforces the expected interface."""

    def test_cannot_instantiate_abc(self):
        """ChannelDispatcher cannot be instantiated directly."""
        with pytest.raises(TypeError):
            ChannelDispatcher()  # type: ignore[abstract]

    def test_subclass_must_implement_channel_name(self):
        """A subclass missing ``channel_name`` cannot be instantiated."""

        class Incomplete(ChannelDispatcher):
            async def dispatch(self, notification: dict) -> str:
                return "sent"

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_subclass_must_implement_dispatch(self):
        """A subclass missing ``dispatch`` cannot be instantiated."""

        class Incomplete(ChannelDispatcher):
            @property
            def channel_name(self) -> str:
                return "test"

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Stub dispatcher tests
# ---------------------------------------------------------------------------


class TestStubSmsDispatcher:
    """Tests for the SMS stub dispatcher."""

    def test_channel_name(self):
        assert StubSmsDispatcher().channel_name == "sms"

    async def test_dispatch_returns_sent(self):
        dispatcher = StubSmsDispatcher()
        result = await dispatcher.dispatch(
            {"recipient_reference": "+254700000000", "message_body": "Hello"}
        )
        assert result == "sent"

    async def test_dispatch_handles_missing_keys(self):
        dispatcher = StubSmsDispatcher()
        result = await dispatcher.dispatch({})
        assert result == "sent"

    def test_is_channel_dispatcher(self):
        assert isinstance(StubSmsDispatcher(), ChannelDispatcher)


class TestStubEmailDispatcher:
    """Tests for the email stub dispatcher."""

    def test_channel_name(self):
        assert StubEmailDispatcher().channel_name == "email"

    async def test_dispatch_returns_sent(self):
        dispatcher = StubEmailDispatcher()
        result = await dispatcher.dispatch(
            {
                "recipient_reference": "user@example.com",
                "subject": "Delivery Update",
                "message_body": "Your order is on the way.",
            }
        )
        assert result == "sent"

    async def test_dispatch_handles_missing_keys(self):
        dispatcher = StubEmailDispatcher()
        result = await dispatcher.dispatch({})
        assert result == "sent"

    def test_is_channel_dispatcher(self):
        assert isinstance(StubEmailDispatcher(), ChannelDispatcher)


class TestStubWhatsAppDispatcher:
    """Tests for the WhatsApp stub dispatcher."""

    def test_channel_name(self):
        assert StubWhatsAppDispatcher().channel_name == "whatsapp"

    async def test_dispatch_returns_sent(self):
        dispatcher = StubWhatsAppDispatcher()
        result = await dispatcher.dispatch(
            {"recipient_reference": "+254700000000", "message_body": "ETA updated"}
        )
        assert result == "sent"

    async def test_dispatch_handles_missing_keys(self):
        dispatcher = StubWhatsAppDispatcher()
        result = await dispatcher.dispatch({})
        assert result == "sent"

    def test_is_channel_dispatcher(self):
        assert isinstance(StubWhatsAppDispatcher(), ChannelDispatcher)
