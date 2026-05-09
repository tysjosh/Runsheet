"""
Unit tests for commerce.services.sync_pull_bridge (Task 9.3).

Tests the SyncPullBridge, QBOPullSubscriber, StripePullSubscriber,
and register_pull_subscribers functionality.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from commerce.services.sync_pull_bridge import (
    QBOPullSubscriber,
    StripePullSubscriber,
    SyncPullBridge,
    register_pull_subscribers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_external_sync():
    """Mock CommerceExternalSync with async handlers."""
    sync = MagicMock()
    sync.on_qbo_payment_observed = AsyncMock()
    sync.on_stripe_charge_observed = AsyncMock()
    return sync


@pytest.fixture
def mock_es_service():
    """Mock Elasticsearch service."""
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": []}
    })
    return es


@pytest.fixture
def mock_qbo_connector():
    """Mock QBO connector with sync_pull."""
    connector = MagicMock()
    connector._tenant_id = "tenant_001"
    run = MagicMock()
    run.status = "success"
    run.record_counts = {"payments_processed": 2, "invoices_processed": 3}
    connector.sync_pull = AsyncMock(return_value=run)
    return connector


@pytest.fixture
def mock_stripe_connector():
    """Mock Stripe connector with sync_pull."""
    connector = MagicMock()
    connector._tenant_id = "tenant_001"
    run = MagicMock()
    run.status = "success"
    run.record_counts = {"payment_intents_processed": 1}
    connector.sync_pull = AsyncMock(return_value=run)
    return connector


# ---------------------------------------------------------------------------
# QBOPullSubscriber tests
# ---------------------------------------------------------------------------


class TestQBOPullSubscriber:
    """Tests for QBOPullSubscriber."""

    @pytest.mark.asyncio
    async def test_forwards_qbo_payment_event(self, mock_external_sync):
        """Subscriber forwards a QBO payment event to external_sync."""
        subscriber = QBOPullSubscriber(
            external_sync=mock_external_sync,
            tenant_id="tenant_001",
        )

        event = {
            "Id": "pay_123",
            "TotalAmt": 150.00,
            "TxnDate": "2025-01-15",
            "PaymentMethodRef": {"value": "1", "name": "Check"},
            "LinkedTxn": [{"TxnId": "456", "TxnType": "Invoice"}],
            "tenant_id": "tenant_001",
            "matched_invoice_id": "inv_abc",
        }

        await subscriber(event)

        mock_external_sync.on_qbo_payment_observed.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_adds_tenant_id_if_missing(self, mock_external_sync):
        """Subscriber adds tenant_id to event if not present."""
        subscriber = QBOPullSubscriber(
            external_sync=mock_external_sync,
            tenant_id="tenant_001",
        )

        event = {
            "Id": "pay_123",
            "TotalAmt": 100.00,
            "matched_invoice_id": "inv_abc",
        }

        await subscriber(event)

        call_args = mock_external_sync.on_qbo_payment_observed.call_args[0][0]
        assert call_args["tenant_id"] == "tenant_001"

    @pytest.mark.asyncio
    async def test_skips_when_no_external_sync(self):
        """Subscriber is a no-op when external_sync is None."""
        subscriber = QBOPullSubscriber(
            external_sync=None,
            tenant_id="tenant_001",
        )

        event = {"Id": "pay_123", "TotalAmt": 100.00}
        # Should not raise
        await subscriber(event)

    @pytest.mark.asyncio
    async def test_resolves_invoice_from_es(self, mock_external_sync, mock_es_service):
        """Subscriber resolves invoice_id from ES when not in event."""
        mock_es_service.search_documents = AsyncMock(return_value={
            "hits": {"hits": [{"_source": {"invoice_id": "inv_resolved"}}]}
        })

        subscriber = QBOPullSubscriber(
            external_sync=mock_external_sync,
            es_service=mock_es_service,
            tenant_id="tenant_001",
        )

        event = {
            "Id": "pay_456",
            "TotalAmt": 200.00,
            "tenant_id": "tenant_001",
            "LinkedTxn": [{"TxnId": "789", "TxnType": "Invoice"}],
        }

        await subscriber(event)

        call_args = mock_external_sync.on_qbo_payment_observed.call_args[0][0]
        assert call_args["matched_invoice_id"] == "inv_resolved"

    @pytest.mark.asyncio
    async def test_handles_handler_error_gracefully(self, mock_external_sync):
        """Subscriber logs but does not raise on handler error."""
        mock_external_sync.on_qbo_payment_observed = AsyncMock(
            side_effect=RuntimeError("handler failed")
        )

        subscriber = QBOPullSubscriber(
            external_sync=mock_external_sync,
            tenant_id="tenant_001",
        )

        event = {
            "Id": "pay_123",
            "TotalAmt": 100.00,
            "tenant_id": "tenant_001",
            "matched_invoice_id": "inv_abc",
        }

        # Should not raise
        await subscriber(event)


# ---------------------------------------------------------------------------
# StripePullSubscriber tests
# ---------------------------------------------------------------------------


class TestStripePullSubscriber:
    """Tests for StripePullSubscriber."""

    @pytest.mark.asyncio
    async def test_forwards_succeeded_stripe_event(self, mock_external_sync):
        """Subscriber forwards a succeeded Stripe event."""
        subscriber = StripePullSubscriber(
            external_sync=mock_external_sync,
            tenant_id="tenant_001",
        )

        event = {
            "id": "pi_abc123",
            "amount": 15000,
            "status": "succeeded",
            "payment_method_types": ["card"],
            "metadata": {"invoice_id": "inv_xyz", "tenant_id": "tenant_001"},
        }

        await subscriber(event)

        mock_external_sync.on_stripe_charge_observed.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_non_succeeded_events(self, mock_external_sync):
        """Subscriber skips events that are not succeeded."""
        subscriber = StripePullSubscriber(
            external_sync=mock_external_sync,
            tenant_id="tenant_001",
        )

        event = {
            "id": "pi_abc123",
            "amount": 15000,
            "status": "requires_payment_method",
            "payment_method_types": ["card"],
        }

        await subscriber(event)

        mock_external_sync.on_stripe_charge_observed.assert_not_called()

    @pytest.mark.asyncio
    async def test_adds_tenant_id_if_missing(self, mock_external_sync):
        """Subscriber adds tenant_id to event if not present."""
        subscriber = StripePullSubscriber(
            external_sync=mock_external_sync,
            tenant_id="tenant_001",
        )

        event = {
            "id": "pi_abc123",
            "amount": 15000,
            "status": "succeeded",
            "payment_method_types": ["card"],
            "metadata": {"invoice_id": "inv_xyz"},
        }

        await subscriber(event)

        call_args = mock_external_sync.on_stripe_charge_observed.call_args[0][0]
        assert call_args["tenant_id"] == "tenant_001"

    @pytest.mark.asyncio
    async def test_skips_when_no_external_sync(self):
        """Subscriber is a no-op when external_sync is None."""
        subscriber = StripePullSubscriber(
            external_sync=None,
            tenant_id="tenant_001",
        )

        event = {"id": "pi_abc", "amount": 100, "status": "succeeded"}
        # Should not raise
        await subscriber(event)

    @pytest.mark.asyncio
    async def test_handles_handler_error_gracefully(self, mock_external_sync):
        """Subscriber logs but does not raise on handler error."""
        mock_external_sync.on_stripe_charge_observed = AsyncMock(
            side_effect=RuntimeError("handler failed")
        )

        subscriber = StripePullSubscriber(
            external_sync=mock_external_sync,
            tenant_id="tenant_001",
        )

        event = {
            "id": "pi_abc123",
            "amount": 15000,
            "status": "succeeded",
            "metadata": {"invoice_id": "inv_xyz", "tenant_id": "tenant_001"},
        }

        # Should not raise
        await subscriber(event)


# ---------------------------------------------------------------------------
# SyncPullBridge tests
# ---------------------------------------------------------------------------


class TestSyncPullBridge:
    """Tests for SyncPullBridge."""

    @pytest.mark.asyncio
    async def test_forwards_qbo_payments(self, mock_external_sync):
        """Bridge forwards QBO payment events to external_sync."""
        bridge = SyncPullBridge(
            external_sync=mock_external_sync,
        )

        payments = [
            {"Id": "1", "TotalAmt": 100, "tenant_id": "t1", "matched_invoice_id": "inv_1"},
            {"Id": "2", "TotalAmt": 200, "tenant_id": "t1", "matched_invoice_id": "inv_2"},
        ]

        count = await bridge.on_qbo_sync_pull_complete(
            sync_run=MagicMock(), raw_payments=payments
        )

        assert count == 2
        assert mock_external_sync.on_qbo_payment_observed.call_count == 2

    @pytest.mark.asyncio
    async def test_forwards_stripe_intents(self, mock_external_sync):
        """Bridge forwards Stripe succeeded intents to external_sync."""
        bridge = SyncPullBridge(
            external_sync=mock_external_sync,
        )

        intents = [
            {"id": "pi_1", "amount": 1000, "status": "succeeded",
             "metadata": {"invoice_id": "inv_1", "tenant_id": "t1"}},
            {"id": "pi_2", "amount": 2000, "status": "requires_action",
             "metadata": {"invoice_id": "inv_2", "tenant_id": "t1"}},
        ]

        count = await bridge.on_stripe_sync_pull_complete(
            sync_run=MagicMock(), raw_intents=intents
        )

        # Only the succeeded intent should be forwarded
        assert count == 1
        mock_external_sync.on_stripe_charge_observed.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_events(self, mock_external_sync):
        """Bridge returns 0 when no raw events are provided."""
        bridge = SyncPullBridge(external_sync=mock_external_sync)

        count = await bridge.on_qbo_sync_pull_complete(
            sync_run=MagicMock(), raw_payments=None
        )
        assert count == 0

        count = await bridge.on_stripe_sync_pull_complete(
            sync_run=MagicMock(), raw_intents=None
        )
        assert count == 0


# ---------------------------------------------------------------------------
# register_pull_subscribers tests
# ---------------------------------------------------------------------------


class TestRegisterPullSubscribers:
    """Tests for register_pull_subscribers."""

    def test_registers_qbo_subscriber(
        self, mock_external_sync, mock_qbo_connector, mock_es_service
    ):
        """Registers a QBO subscriber on the connector."""
        result = register_pull_subscribers(
            external_sync=mock_external_sync,
            qbo_connector=mock_qbo_connector,
            stripe_connector=None,
            es_service=mock_es_service,
        )

        assert result["qbo_subscriber"] is not None
        assert result["stripe_subscriber"] is None
        assert hasattr(mock_qbo_connector, "_commerce_pull_subscriber")

    def test_registers_stripe_subscriber(
        self, mock_external_sync, mock_stripe_connector, mock_es_service
    ):
        """Registers a Stripe subscriber on the connector."""
        result = register_pull_subscribers(
            external_sync=mock_external_sync,
            qbo_connector=None,
            stripe_connector=mock_stripe_connector,
            es_service=mock_es_service,
        )

        assert result["qbo_subscriber"] is None
        assert result["stripe_subscriber"] is not None
        assert hasattr(mock_stripe_connector, "_commerce_pull_subscriber")

    def test_registers_both_subscribers(
        self, mock_external_sync, mock_qbo_connector, mock_stripe_connector, mock_es_service
    ):
        """Registers both QBO and Stripe subscribers."""
        result = register_pull_subscribers(
            external_sync=mock_external_sync,
            qbo_connector=mock_qbo_connector,
            stripe_connector=mock_stripe_connector,
            es_service=mock_es_service,
        )

        assert result["qbo_subscriber"] is not None
        assert result["stripe_subscriber"] is not None

    def test_handles_no_connectors(self, mock_external_sync, mock_es_service):
        """Returns None subscribers when no connectors available."""
        result = register_pull_subscribers(
            external_sync=mock_external_sync,
            qbo_connector=None,
            stripe_connector=None,
            es_service=mock_es_service,
        )

        assert result["qbo_subscriber"] is None
        assert result["stripe_subscriber"] is None

    @pytest.mark.asyncio
    async def test_patched_qbo_sync_pull_still_works(
        self, mock_external_sync, mock_qbo_connector, mock_es_service
    ):
        """Patched QBO sync_pull still returns the original SyncRun."""
        from datetime import datetime, timezone

        register_pull_subscribers(
            external_sync=mock_external_sync,
            qbo_connector=mock_qbo_connector,
            stripe_connector=None,
            es_service=mock_es_service,
        )

        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        result = await mock_qbo_connector.sync_pull(since)

        # The patched sync_pull should still return the original run
        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_patched_stripe_sync_pull_still_works(
        self, mock_external_sync, mock_stripe_connector, mock_es_service
    ):
        """Patched Stripe sync_pull still returns the original SyncRun."""
        from datetime import datetime, timezone

        register_pull_subscribers(
            external_sync=mock_external_sync,
            qbo_connector=None,
            stripe_connector=mock_stripe_connector,
            es_service=mock_es_service,
        )

        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        result = await mock_stripe_connector.sync_pull(since)

        # The patched sync_pull should still return the original run
        assert result.status == "success"
