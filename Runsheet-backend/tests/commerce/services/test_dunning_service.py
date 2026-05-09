"""Unit tests for DunningService.

Tests cover:
- Threshold crossings: invoice at 7, 14, 30 days overdue triggers correct
  notifications with the right template keys.
- Duplicate prevention: same (invoice_id, threshold) doesn't trigger twice.
- Cancellation on payment: cancel_for_invoice marks dunning_events as
  cancelled and notifies the notification pipeline.
- Feature flag gating: no-op when dunning_enabled is off.
- Multiple invoices processed in one cycle.

Validates: Requirements 7.3, 7.4, 7.5
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from commerce.services.dunning_service import (
    DEFAULT_DUNNING_THRESHOLDS_DAYS,
    DunningService,
)


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_dunning_test"
_INVOICE_ID_1 = "inv_overdue_001"
_INVOICE_ID_2 = "inv_overdue_002"
_INVOICE_ID_3 = "inv_overdue_003"
_ACCOUNT_ID = "acct_dunning_abc"
_FIXED_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(return_value=_es_empty_response())
    return es


def _make_notification_service() -> AsyncMock:
    """Create a mocked notification service."""
    ns = AsyncMock()
    ns.enqueue = AsyncMock(return_value=None)
    ns.cancel_queued = AsyncMock(return_value=None)
    return ns


def _make_feature_flag_service(*, enabled: bool = True) -> AsyncMock:
    """Create a mocked feature flag service."""
    ffs = AsyncMock()
    state = "active" if enabled else "disabled"
    ffs.get_overlay_state = AsyncMock(return_value=state)
    return ffs


def _es_empty_response() -> Dict[str, Any]:
    """Build an empty ES search response."""
    return {
        "hits": {
            "hits": [],
            "total": {"value": 0},
        }
    }


def _es_search_response(hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a mock ES search response with hits."""
    return {
        "hits": {
            "hits": [{"_source": h} for h in hits],
            "total": {"value": len(hits)},
        }
    }


def _make_invoice(
    *,
    invoice_id: str = _INVOICE_ID_1,
    tenant_id: str = _TENANT_ID,
    account_id: str = _ACCOUNT_ID,
    status: str = "overdue",
    due_date: str = "2026-07-10",
    total_cents: int = 500000,
    remaining_cents: int = 500000,
    invoice_number: str = "INV-2026-0042",
    customer_id: str = "cust_xyz",
) -> Dict[str, Any]:
    """Build a sample invoice document."""
    return {
        "invoice_id": invoice_id,
        "tenant_id": tenant_id,
        "account_id": account_id,
        "customer_id": customer_id,
        "status": status,
        "due_date": due_date,
        "total_cents": total_cents,
        "remaining_cents": remaining_cents,
        "invoice_number": invoice_number,
    }


def _make_dunning_event(
    *,
    event_id: str = "dun_existing_001",
    invoice_id: str = _INVOICE_ID_1,
    threshold_days: int = 7,
    cancelled_at=None,
) -> Dict[str, Any]:
    """Build a sample dunning_events document."""
    return {
        "event_id": event_id,
        "invoice_id": invoice_id,
        "account_id": _ACCOUNT_ID,
        "tenant_id": _TENANT_ID,
        "threshold_days": threshold_days,
        "template_key": f"dunning_level_{threshold_days}",
        "queued_at": "2026-07-17T12:00:00+00:00",
        "cancelled_at": cancelled_at,
        "cancellation_reason": None,
    }


# ---------------------------------------------------------------------------
# Tests: Threshold crossings
# ---------------------------------------------------------------------------


class TestThresholdCrossings:
    """Invoice at 7, 14, 30 days overdue triggers correct notifications."""

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_7_day_threshold_triggers_notification(self, mock_utcnow):
        """Invoice 7 days overdue triggers dunning_level_7 notification."""
        mock_utcnow.return_value = _FIXED_NOW

        # Invoice due 7 days ago
        invoice = _make_invoice(
            due_date=(_FIXED_NOW.date() - timedelta(days=7)).isoformat()
        )

        es = _make_es_service()
        ns = _make_notification_service()

        # First call: query overdue invoices → returns our invoice
        # Second call: check dunning_events for (invoice, 7) → empty (no dup)
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # overdue invoices query
                _es_empty_response(),  # no existing dunning event for threshold 7
            ]
        )

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["skipped"] is False
        assert result["invoices_scanned"] == 1
        assert result["notifications_enqueued"] == 1
        assert result["duplicates_skipped"] == 0

        # Verify dunning_events record was written
        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args[0][0] == "dunning_events"
        doc = call_args[0][2]
        assert doc["invoice_id"] == _INVOICE_ID_1
        assert doc["threshold_days"] == 7
        assert doc["template_key"] == "dunning_level_7"

        # Verify notification was enqueued
        ns.enqueue.assert_called_once()
        enqueue_kwargs = ns.enqueue.call_args[1]
        assert enqueue_kwargs["template_key"] == "dunning_level_7"

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_14_day_threshold_triggers_both_7_and_14(self, mock_utcnow):
        """Invoice 14 days overdue triggers both level_7 and level_14."""
        mock_utcnow.return_value = _FIXED_NOW

        invoice = _make_invoice(
            due_date=(_FIXED_NOW.date() - timedelta(days=14)).isoformat()
        )

        es = _make_es_service()
        ns = _make_notification_service()

        # Overdue query → invoice; then two threshold checks (7 and 14) → both empty
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # overdue invoices
                _es_empty_response(),  # no event for threshold 7
                _es_empty_response(),  # no event for threshold 14
            ]
        )

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["notifications_enqueued"] == 2
        assert result["duplicates_skipped"] == 0

        # Two dunning_events records written
        assert es.index_document.call_count == 2

        # Two notifications enqueued
        assert ns.enqueue.call_count == 2

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_30_day_threshold_triggers_all_three(self, mock_utcnow):
        """Invoice 30 days overdue triggers level_7, level_14, and level_30."""
        mock_utcnow.return_value = _FIXED_NOW

        invoice = _make_invoice(
            due_date=(_FIXED_NOW.date() - timedelta(days=30)).isoformat()
        )

        es = _make_es_service()
        ns = _make_notification_service()

        # Overdue query → invoice; then three threshold checks → all empty
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # overdue invoices
                _es_empty_response(),  # no event for threshold 7
                _es_empty_response(),  # no event for threshold 14
                _es_empty_response(),  # no event for threshold 30
            ]
        )

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["notifications_enqueued"] == 3
        assert result["duplicates_skipped"] == 0
        assert es.index_document.call_count == 3
        assert ns.enqueue.call_count == 3

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_invoice_not_yet_at_threshold_no_notification(self, mock_utcnow):
        """Invoice only 5 days overdue does not trigger any notification."""
        mock_utcnow.return_value = _FIXED_NOW

        # Invoice due 5 days ago — below minimum threshold of 7
        invoice = _make_invoice(
            due_date=(_FIXED_NOW.date() - timedelta(days=5)).isoformat()
        )

        es = _make_es_service()
        ns = _make_notification_service()

        # The query uses cutoff_date = now - 7 days, so this invoice
        # shouldn't even appear in results. But if it does, no threshold
        # should fire.
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # overdue invoices (edge case)
            ]
        )

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["notifications_enqueued"] == 0
        ns.enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Duplicate prevention
# ---------------------------------------------------------------------------


class TestDuplicatePrevention:
    """Same (invoice_id, threshold) doesn't trigger twice (Req 7.4)."""

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_existing_dunning_event_prevents_duplicate(self, mock_utcnow):
        """If dunning_events record exists for (invoice, threshold), skip."""
        mock_utcnow.return_value = _FIXED_NOW

        invoice = _make_invoice(
            due_date=(_FIXED_NOW.date() - timedelta(days=10)).isoformat()
        )

        es = _make_es_service()
        ns = _make_notification_service()

        # Overdue query → invoice; threshold 7 check → already exists
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # overdue invoices
                # Existing dunning event for threshold 7 (count > 0)
                {
                    "hits": {
                        "hits": [],
                        "total": {"value": 1},
                    }
                },
            ]
        )

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["notifications_enqueued"] == 0
        assert result["duplicates_skipped"] == 1

        # No dunning_events record written
        es.index_document.assert_not_called()
        # No notification enqueued
        ns.enqueue.assert_not_called()

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_partial_duplicate_only_enqueues_new_threshold(self, mock_utcnow):
        """Invoice 14 days overdue with existing 7-day event only enqueues 14."""
        mock_utcnow.return_value = _FIXED_NOW

        invoice = _make_invoice(
            due_date=(_FIXED_NOW.date() - timedelta(days=14)).isoformat()
        )

        es = _make_es_service()
        ns = _make_notification_service()

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # overdue invoices
                # Threshold 7: already exists
                {"hits": {"hits": [], "total": {"value": 1}}},
                # Threshold 14: does not exist
                _es_empty_response(),
            ]
        )

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["notifications_enqueued"] == 1
        assert result["duplicates_skipped"] == 1

        # Only one dunning_events record written (for threshold 14)
        es.index_document.assert_called_once()
        doc = es.index_document.call_args[0][2]
        assert doc["threshold_days"] == 14
        assert doc["template_key"] == "dunning_level_14"

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_all_thresholds_already_sent_no_action(self, mock_utcnow):
        """Invoice 30+ days overdue with all events already recorded → no-op."""
        mock_utcnow.return_value = _FIXED_NOW

        invoice = _make_invoice(
            due_date=(_FIXED_NOW.date() - timedelta(days=35)).isoformat()
        )

        es = _make_es_service()
        ns = _make_notification_service()

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # overdue invoices
                # All three thresholds already exist
                {"hits": {"hits": [], "total": {"value": 1}}},  # 7
                {"hits": {"hits": [], "total": {"value": 1}}},  # 14
                {"hits": {"hits": [], "total": {"value": 1}}},  # 30
            ]
        )

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["notifications_enqueued"] == 0
        assert result["duplicates_skipped"] == 3
        es.index_document.assert_not_called()
        ns.enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Cancellation on payment
# ---------------------------------------------------------------------------


class TestCancellationOnPayment:
    """cancel_for_invoice marks dunning_events as cancelled (Req 7.5)."""

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_cancel_marks_pending_events_as_cancelled(self, mock_utcnow):
        """Paying an invoice cancels all pending dunning events."""
        mock_utcnow.return_value = _FIXED_NOW

        pending_events = [
            _make_dunning_event(event_id="dun_001", threshold_days=7),
            _make_dunning_event(event_id="dun_002", threshold_days=14),
        ]

        es = _make_es_service()
        ns = _make_notification_service()

        # Query for pending events returns two
        es.search_documents = AsyncMock(
            return_value=_es_search_response(pending_events)
        )

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.cancel_for_invoice(
            _TENANT_ID, _INVOICE_ID_1, reason="invoice_paid"
        )

        assert result["cancelled_count"] == 2
        assert result["reason"] == "invoice_paid"

        # Two update calls to mark events as cancelled
        assert es.update_document.call_count == 2

        # Verify cancellation fields
        for call in es.update_document.call_args_list:
            args = call[0]
            assert args[0] == "dunning_events"
            partial = args[2]
            assert partial["cancelled_at"] == _FIXED_NOW.isoformat()
            assert partial["cancellation_reason"] == "invoice_paid"

        # Notification service cancel_queued called
        ns.cancel_queued.assert_called_once_with(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID_1,
            reason="invoice_paid",
        )

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_cancel_with_void_reason(self, mock_utcnow):
        """Voiding an invoice cancels dunning with 'invoice_voided' reason."""
        mock_utcnow.return_value = _FIXED_NOW

        pending_events = [
            _make_dunning_event(event_id="dun_003", threshold_days=7),
        ]

        es = _make_es_service()
        ns = _make_notification_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response(pending_events)
        )

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.cancel_for_invoice(
            _TENANT_ID, _INVOICE_ID_1, reason="invoice_voided"
        )

        assert result["cancelled_count"] == 1
        assert result["reason"] == "invoice_voided"

        partial = es.update_document.call_args[0][2]
        assert partial["cancellation_reason"] == "invoice_voided"

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_cancel_no_pending_events_is_noop(self, mock_utcnow):
        """If no pending dunning events exist, cancellation is a no-op."""
        mock_utcnow.return_value = _FIXED_NOW

        es = _make_es_service()
        ns = _make_notification_service()
        es.search_documents = AsyncMock(return_value=_es_empty_response())

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.cancel_for_invoice(
            _TENANT_ID, _INVOICE_ID_1, reason="invoice_paid"
        )

        assert result["cancelled_count"] == 0
        es.update_document.assert_not_called()
        ns.cancel_queued.assert_not_called()

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_cancel_handles_notification_service_error(self, mock_utcnow):
        """Notification service failure doesn't break cancellation."""
        mock_utcnow.return_value = _FIXED_NOW

        pending_events = [
            _make_dunning_event(event_id="dun_004", threshold_days=7),
        ]

        es = _make_es_service()
        ns = _make_notification_service()
        ns.cancel_queued = AsyncMock(side_effect=RuntimeError("queue down"))
        es.search_documents = AsyncMock(
            return_value=_es_search_response(pending_events)
        )

        service = DunningService(es_service=es, notification_service=ns)
        # Should not raise despite notification service failure
        result = await service.cancel_for_invoice(
            _TENANT_ID, _INVOICE_ID_1, reason="invoice_paid"
        )

        # ES update still happened
        assert result["cancelled_count"] == 1
        es.update_document.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Feature flag gating
# ---------------------------------------------------------------------------


class TestFeatureFlagGating:
    """No-op when dunning_enabled is off."""

    @pytest.mark.asyncio
    async def test_evaluate_skips_when_flag_disabled(self):
        """evaluate_and_enqueue returns early when dunning_enabled is off."""
        es = _make_es_service()
        ns = _make_notification_service()
        ffs = _make_feature_flag_service(enabled=False)

        service = DunningService(
            es_service=es,
            notification_service=ns,
            feature_flag_service=ffs,
        )
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["skipped"] is True
        assert result["reason"] == "dunning_disabled"
        assert result["invoices_scanned"] == 0
        assert result["notifications_enqueued"] == 0

        # No ES queries made
        es.search_documents.assert_not_called()
        ns.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_skips_when_flag_disabled(self):
        """cancel_for_invoice returns early when dunning_enabled is off."""
        es = _make_es_service()
        ns = _make_notification_service()
        ffs = _make_feature_flag_service(enabled=False)

        service = DunningService(
            es_service=es,
            notification_service=ns,
            feature_flag_service=ffs,
        )
        result = await service.cancel_for_invoice(
            _TENANT_ID, _INVOICE_ID_1, reason="invoice_paid"
        )

        assert result["cancelled_count"] == 0
        assert result["reason"] == "dunning_disabled"
        es.search_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluate_runs_when_flag_enabled(self):
        """evaluate_and_enqueue proceeds when dunning_enabled is active."""
        es = _make_es_service()
        ns = _make_notification_service()
        ffs = _make_feature_flag_service(enabled=True)

        # No overdue invoices
        es.search_documents = AsyncMock(return_value=_es_empty_response())

        service = DunningService(
            es_service=es,
            notification_service=ns,
            feature_flag_service=ffs,
        )
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["skipped"] is False
        assert result["invoices_scanned"] == 0

        # ES was queried (flag check passed)
        es.search_documents.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_feature_flag_service_defaults_to_enabled(self):
        """When no feature_flag_service is provided, dunning runs."""
        es = _make_es_service()
        ns = _make_notification_service()

        es.search_documents = AsyncMock(return_value=_es_empty_response())

        # No feature_flag_service → defaults to enabled
        service = DunningService(es_service=es, notification_service=ns)
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["skipped"] is False
        es.search_documents.assert_called_once()

    @pytest.mark.asyncio
    async def test_feature_flag_error_fails_closed(self):
        """If feature flag check raises, dunning is skipped (fail-closed)."""
        es = _make_es_service()
        ns = _make_notification_service()
        ffs = AsyncMock()
        ffs.get_overlay_state = AsyncMock(
            side_effect=ConnectionError("Redis down")
        )

        service = DunningService(
            es_service=es,
            notification_service=ns,
            feature_flag_service=ffs,
        )
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["skipped"] is True
        assert result["reason"] == "dunning_disabled"
        es.search_documents.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Multiple invoices in one cycle
# ---------------------------------------------------------------------------


class TestMultipleInvoices:
    """Multiple invoices processed in one evaluation cycle."""

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_multiple_invoices_different_thresholds(self, mock_utcnow):
        """Three invoices at different overdue levels are all processed."""
        mock_utcnow.return_value = _FIXED_NOW

        # Invoice 1: 8 days overdue → triggers threshold 7
        inv1 = _make_invoice(
            invoice_id=_INVOICE_ID_1,
            due_date=(_FIXED_NOW.date() - timedelta(days=8)).isoformat(),
        )
        # Invoice 2: 15 days overdue → triggers thresholds 7 and 14
        inv2 = _make_invoice(
            invoice_id=_INVOICE_ID_2,
            due_date=(_FIXED_NOW.date() - timedelta(days=15)).isoformat(),
        )
        # Invoice 3: 31 days overdue → triggers thresholds 7, 14, and 30
        inv3 = _make_invoice(
            invoice_id=_INVOICE_ID_3,
            due_date=(_FIXED_NOW.date() - timedelta(days=31)).isoformat(),
        )

        es = _make_es_service()
        ns = _make_notification_service()

        # Build side effects:
        # 1. Overdue query → all three invoices
        # 2-7. Threshold checks → all empty (no prior events)
        side_effects = [
            _es_search_response([inv1, inv2, inv3]),  # overdue query
            # Invoice 1: threshold 7
            _es_empty_response(),
            # Invoice 2: threshold 7, threshold 14
            _es_empty_response(),
            _es_empty_response(),
            # Invoice 3: threshold 7, threshold 14, threshold 30
            _es_empty_response(),
            _es_empty_response(),
            _es_empty_response(),
        ]
        es.search_documents = AsyncMock(side_effect=side_effects)

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["invoices_scanned"] == 3
        # 1 + 2 + 3 = 6 notifications total
        assert result["notifications_enqueued"] == 6
        assert result["duplicates_skipped"] == 0

        # 6 dunning_events records written
        assert es.index_document.call_count == 6
        # 6 notifications enqueued
        assert ns.enqueue.call_count == 6

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_no_overdue_invoices_is_clean_noop(self, mock_utcnow):
        """When no invoices are overdue, evaluation completes with zero counts."""
        mock_utcnow.return_value = _FIXED_NOW

        es = _make_es_service()
        ns = _make_notification_service()
        es.search_documents = AsyncMock(return_value=_es_empty_response())

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.evaluate_and_enqueue(_TENANT_ID)

        assert result["invoices_scanned"] == 0
        assert result["notifications_enqueued"] == 0
        assert result["duplicates_skipped"] == 0
        es.index_document.assert_not_called()
        ns.enqueue.assert_not_called()

    @pytest.mark.asyncio
    @patch("commerce.services.dunning_service.utcnow")
    async def test_custom_thresholds_override_defaults(self, mock_utcnow):
        """Custom thresholds list overrides the default [7, 14, 30]."""
        mock_utcnow.return_value = _FIXED_NOW

        # Invoice 10 days overdue
        invoice = _make_invoice(
            due_date=(_FIXED_NOW.date() - timedelta(days=10)).isoformat()
        )

        es = _make_es_service()
        ns = _make_notification_service()

        # Custom thresholds: [5, 10]
        # Invoice is 10 days overdue → triggers both 5 and 10
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),
                _es_empty_response(),  # threshold 5
                _es_empty_response(),  # threshold 10
            ]
        )

        service = DunningService(es_service=es, notification_service=ns)
        result = await service.evaluate_and_enqueue(
            _TENANT_ID, thresholds=[5, 10]
        )

        assert result["notifications_enqueued"] == 2

        # Verify template keys use custom thresholds
        calls = es.index_document.call_args_list
        templates = [call[0][2]["template_key"] for call in calls]
        assert "dunning_level_5" in templates
        assert "dunning_level_10" in templates


# ---------------------------------------------------------------------------
# Tests: Default thresholds constant
# ---------------------------------------------------------------------------


class TestDefaults:
    """Verify default configuration values."""

    def test_default_thresholds_are_7_14_30(self):
        """Default dunning thresholds are [7, 14, 30] days."""
        assert DEFAULT_DUNNING_THRESHOLDS_DAYS == [7, 14, 30]

    def test_service_initializes_without_optional_deps(self):
        """DunningService can be created with only es_service."""
        es = _make_es_service()
        service = DunningService(es_service=es)
        assert service._es is es
        assert service._notification_service is None
        assert service._feature_flag_service is None
