"""Unit tests for CreditService.

Tests cover:
- check: approved when within limit, hold when over limit, override active
- apply_override: transitions to override state, writes event, validates inputs
- expire_override: re-evaluates state, transitions to ok or hold
- on_payment_applied: transitions hold → ok when under limit, idempotent

Validates: Requirements 2.5, 2.6, 4.3, 4.4, C1, C3
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.credit_service import CreditDecision, CreditService
from errors.exceptions import AppException


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_abc"
_ACCOUNT_ID = "acct_test456"
_FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return es


def _make_account_doc(
    *,
    account_id: str = _ACCOUNT_ID,
    tenant_id: str = _TENANT_ID,
    credit_limit_cents: int = 500000,
    open_balance_cents: int = 100000,
    credit_state: str = "ok",
    credit_override_expires_at: str | None = None,
) -> Dict[str, Any]:
    """Build an account document as returned from ES."""
    return {
        "account_id": account_id,
        "tenant_id": tenant_id,
        "customer_id": "cust_test123",
        "display_name": "Main Account",
        "status": "active",
        "credit_limit_cents": credit_limit_cents,
        "open_balance_cents": open_balance_cents,
        "available_credit_cents": credit_limit_cents - open_balance_cents,
        "credit_balance_cents": 0,
        "credit_state": credit_state,
        "credit_override_expires_at": credit_override_expires_at,
        "net_terms_days": 30,
        "tier": "default",
        "billing_address": None,
        "payment_method_preference": "invoice",
        "created_at": _FIXED_NOW.isoformat(),
        "updated_at": _FIXED_NOW.isoformat(),
        "external_refs": {},
    }


def _es_search_response(hits: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a mock ES search response."""
    return {
        "hits": {
            "hits": [{"_source": h} for h in hits],
            "total": {"value": len(hits)},
        }
    }


def _es_agg_response(aggs: Dict[str, Any]) -> Dict[str, Any]:
    """Build a mock ES aggregation response."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": aggs,
    }


# ---------------------------------------------------------------------------
# Tests: check
# ---------------------------------------------------------------------------


class TestCreditServiceCheck:
    """Tests for CreditService.check."""

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_check_approved_within_limit(self, mock_utcnow):
        """Returns approved=True when projected balance is within limit."""
        es = _make_es_service()
        account = _make_account_doc(credit_limit_cents=500000)

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account]),  # _get_account
                _es_agg_response({"total_remaining": {"value": 100000}}),  # open balance
            ]
        )
        service = CreditService(es)

        result = await service.check(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
            order_total_cents=50000,
        )

        assert result.approved is True
        assert result.hold_required is False
        assert result.override_active is False
        assert result.reason == "within_credit_limit"

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_check_hold_when_over_limit(self, mock_utcnow):
        """Returns hold_required=True when order would push over limit."""
        es = _make_es_service()
        account = _make_account_doc(credit_limit_cents=200000)

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account]),  # _get_account
                _es_agg_response({"total_remaining": {"value": 150000}}),  # open balance
            ]
        )
        service = CreditService(es)

        result = await service.check(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
            order_total_cents=100000,  # 150000 + 100000 = 250000 > 200000
        )

        assert result.approved is False
        assert result.hold_required is True
        assert result.reason == "credit_limit_exceeded"

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_check_hold_when_already_on_hold(self, mock_utcnow):
        """Returns hold_required=True when account is already on hold."""
        es = _make_es_service()
        account = _make_account_doc(credit_state="hold")

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account]),  # _get_account
                _es_agg_response({"total_remaining": {"value": 300000}}),  # open balance
            ]
        )
        service = CreditService(es)

        result = await service.check(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
            order_total_cents=10000,
        )

        assert result.approved is False
        assert result.hold_required is True
        assert result.reason == "credit_limit_exceeded"

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_check_approved_with_active_override(self, mock_utcnow):
        """Returns approved=True with override_active when override is valid."""
        es = _make_es_service()
        future_expiry = (_FIXED_NOW + timedelta(hours=24)).isoformat()
        account = _make_account_doc(
            credit_state="override",
            credit_override_expires_at=future_expiry,
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account]),  # _get_account
                _es_agg_response({"total_remaining": {"value": 900000}}),  # over limit
            ]
        )
        service = CreditService(es)

        result = await service.check(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
            order_total_cents=500000,
        )

        assert result.approved is True
        assert result.override_active is True
        assert result.hold_required is False
        assert result.reason == "credit_override_active"

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_check_hold_when_zero_credit_limit(self, mock_utcnow):
        """credit_limit_cents=0 means COD only — always hold."""
        es = _make_es_service()
        account = _make_account_doc(credit_limit_cents=0)

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account]),  # _get_account
                _es_agg_response({"total_remaining": {"value": 0}}),  # no balance
            ]
        )
        service = CreditService(es)

        result = await service.check(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
            order_total_cents=10000,
        )

        assert result.approved is False
        assert result.hold_required is True

    @pytest.mark.asyncio
    async def test_check_account_not_found_raises_404(self):
        """Raises 404 when account doesn't exist."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = CreditService(es)

        with pytest.raises(AppException) as exc_info:
            await service.check(
                tenant_id=_TENANT_ID,
                account_id="acct_nonexistent",
                order_total_cents=10000,
            )

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests: apply_override
# ---------------------------------------------------------------------------


class TestCreditServiceApplyOverride:
    """Tests for CreditService.apply_override."""

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_apply_override_transitions_to_override(self, mock_utcnow):
        """Transitions credit_state from ok to override and writes event."""
        es = _make_es_service()
        account = _make_account_doc(credit_state="ok")

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account]),  # _get_account
                _es_agg_response({"max_seq": {"value": None}}),  # event seq
            ]
        )
        service = CreditService(es)

        expires_at = _FIXED_NOW + timedelta(hours=24)
        await service.apply_override(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
            reason="VIP customer needs emergency delivery",
            authorized_by="admin_user_1",
            expires_at=expires_at,
        )

        # Account was updated to override state
        es.update_document.assert_called_once()
        update_call = es.update_document.call_args
        partial = update_call[0][2]
        assert partial["credit_state"] == "override"
        assert partial["credit_override_expires_at"] == expires_at.isoformat()

        # Event was written
        assert es.index_document.call_count == 1
        event_call = es.index_document.call_args
        event_doc = event_call[0][2]
        assert event_doc["event_type"] == "override_applied"
        assert event_doc["payload"]["authorized_by"] == "admin_user_1"
        assert event_doc["payload"]["reason"] == "VIP customer needs emergency delivery"

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_apply_override_from_hold_state(self, mock_utcnow):
        """Can apply override when account is on hold."""
        es = _make_es_service()
        account = _make_account_doc(credit_state="hold")

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account]),  # _get_account
                _es_agg_response({"max_seq": {"value": 3}}),  # event seq
            ]
        )
        service = CreditService(es)

        expires_at = _FIXED_NOW + timedelta(hours=12)
        await service.apply_override(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
            reason="Manager approval",
            authorized_by="manager_1",
            expires_at=expires_at,
        )

        # Event payload records old_state as hold
        event_call = es.index_document.call_args
        event_doc = event_call[0][2]
        assert event_doc["payload"]["old_state"] == "hold"
        assert event_doc["payload"]["new_state"] == "override"

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_apply_override_rejects_empty_reason(self, mock_utcnow):
        """Raises validation error when reason is empty."""
        es = _make_es_service()
        service = CreditService(es)

        with pytest.raises(AppException) as exc_info:
            await service.apply_override(
                tenant_id=_TENANT_ID,
                account_id=_ACCOUNT_ID,
                reason="",
                authorized_by="admin",
                expires_at=_FIXED_NOW + timedelta(hours=1),
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_apply_override_rejects_empty_authorized_by(self, mock_utcnow):
        """Raises validation error when authorized_by is empty."""
        es = _make_es_service()
        service = CreditService(es)

        with pytest.raises(AppException) as exc_info:
            await service.apply_override(
                tenant_id=_TENANT_ID,
                account_id=_ACCOUNT_ID,
                reason="Valid reason",
                authorized_by="",
                expires_at=_FIXED_NOW + timedelta(hours=1),
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_apply_override_rejects_past_expires_at(self, mock_utcnow):
        """Raises validation error when expires_at is in the past."""
        es = _make_es_service()
        service = CreditService(es)

        with pytest.raises(AppException) as exc_info:
            await service.apply_override(
                tenant_id=_TENANT_ID,
                account_id=_ACCOUNT_ID,
                reason="Valid reason",
                authorized_by="admin",
                expires_at=_FIXED_NOW - timedelta(hours=1),
            )

        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Tests: expire_override
# ---------------------------------------------------------------------------


class TestCreditServiceExpireOverride:
    """Tests for CreditService.expire_override."""

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_expire_override_transitions_to_ok_when_under_limit(self, mock_utcnow):
        """Expires override and transitions to ok when under limit."""
        es = _make_es_service()
        account = _make_account_doc(
            credit_state="override",
            credit_limit_cents=500000,
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account]),  # _get_account (first call)
                _es_search_response([account]),  # _get_account (in _evaluate_credit_state)
                _es_agg_response({"total_remaining": {"value": 100000}}),  # open balance (under limit)
                _es_agg_response({"max_seq": {"value": 2}}),  # event seq for override_expired
            ]
        )
        service = CreditService(es)

        await service.expire_override(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
        )

        # Account updated to ok state
        es.update_document.assert_called_once()
        update_call = es.update_document.call_args
        partial = update_call[0][2]
        assert partial["credit_state"] == "ok"
        assert partial["credit_override_expires_at"] is None

        # Override expired event written
        assert es.index_document.call_count == 1
        event_call = es.index_document.call_args
        event_doc = event_call[0][2]
        assert event_doc["event_type"] == "override_expired"
        assert event_doc["payload"]["new_state"] == "ok"

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_expire_override_transitions_to_hold_when_over_limit(self, mock_utcnow):
        """Expires override and transitions to hold when still over limit."""
        es = _make_es_service()
        account = _make_account_doc(
            credit_state="override",
            credit_limit_cents=200000,
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account]),  # _get_account (first call)
                _es_search_response([account]),  # _get_account (in _evaluate_credit_state)
                _es_agg_response({"total_remaining": {"value": 300000}}),  # over limit
                _es_agg_response({"max_seq": {"value": 1}}),  # event seq for override_expired
                _es_agg_response({"max_seq": {"value": 2}}),  # event seq for credit_state_changed
                _es_agg_response({"total_remaining": {"value": 300000}}),  # open balance for state_changed event
            ]
        )
        service = CreditService(es)

        await service.expire_override(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
        )

        # Account updated to hold state
        es.update_document.assert_called_once()
        update_call = es.update_document.call_args
        partial = update_call[0][2]
        assert partial["credit_state"] == "hold"

        # Two events written: override_expired + credit_state_changed
        assert es.index_document.call_count == 2

    @pytest.mark.asyncio
    async def test_expire_override_noop_when_not_in_override(self):
        """No-op when account is not in override state."""
        es = _make_es_service()
        account = _make_account_doc(credit_state="ok")

        es.search_documents = AsyncMock(
            return_value=_es_search_response([account])
        )
        service = CreditService(es)

        await service.expire_override(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
        )

        # No updates or events
        es.update_document.assert_not_called()
        es.index_document.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: on_payment_applied
# ---------------------------------------------------------------------------


class TestCreditServiceOnPaymentApplied:
    """Tests for CreditService.on_payment_applied."""

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_on_payment_transitions_hold_to_ok(self, mock_utcnow):
        """Transitions from hold to ok when payment brings balance under limit."""
        es = _make_es_service()
        account = _make_account_doc(
            credit_state="hold",
            credit_limit_cents=500000,
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account]),  # _get_account
                _es_agg_response({"total_remaining": {"value": 200000}}),  # now under limit
                _es_agg_response({"max_seq": {"value": 3}}),  # event seq
            ]
        )
        service = CreditService(es)

        await service.on_payment_applied(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
        )

        # Account updated to ok
        es.update_document.assert_called_once()
        update_call = es.update_document.call_args
        partial = update_call[0][2]
        assert partial["credit_state"] == "ok"
        assert partial["open_balance_cents"] == 200000
        assert partial["available_credit_cents"] == 300000

        # Event written
        assert es.index_document.call_count == 1
        event_call = es.index_document.call_args
        event_doc = event_call[0][2]
        assert event_doc["event_type"] == "credit_state_changed"
        assert event_doc["payload"]["old_state"] == "hold"
        assert event_doc["payload"]["new_state"] == "ok"
        assert event_doc["payload"]["reason"] == "payment_applied"

    @pytest.mark.asyncio
    @patch("commerce.services.credit_service.utcnow", return_value=_FIXED_NOW)
    async def test_on_payment_stays_on_hold_when_still_over(self, mock_utcnow):
        """Stays on hold when payment doesn't bring balance under limit."""
        es = _make_es_service()
        account = _make_account_doc(
            credit_state="hold",
            credit_limit_cents=200000,
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account]),  # _get_account
                _es_agg_response({"total_remaining": {"value": 300000}}),  # still over
            ]
        )
        service = CreditService(es)

        await service.on_payment_applied(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
        )

        # Account balance updated but state stays hold
        es.update_document.assert_called_once()
        update_call = es.update_document.call_args
        partial = update_call[0][2]
        assert "credit_state" not in partial  # no state change
        assert partial["open_balance_cents"] == 300000

        # No state change event written
        es.index_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_payment_noop_when_not_on_hold(self):
        """No-op when account is not on hold (idempotent)."""
        es = _make_es_service()
        account = _make_account_doc(credit_state="ok")

        es.search_documents = AsyncMock(
            return_value=_es_search_response([account])
        )
        service = CreditService(es)

        await service.on_payment_applied(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
        )

        # No updates or events
        es.update_document.assert_not_called()
        es.index_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_payment_noop_when_override_active(self):
        """No-op when account is in override state."""
        es = _make_es_service()
        account = _make_account_doc(credit_state="override")

        es.search_documents = AsyncMock(
            return_value=_es_search_response([account])
        )
        service = CreditService(es)

        await service.on_payment_applied(
            tenant_id=_TENANT_ID,
            account_id=_ACCOUNT_ID,
        )

        # No updates or events
        es.update_document.assert_not_called()
        es.index_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_payment_account_not_found_raises_404(self):
        """Raises 404 when account doesn't exist."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = CreditService(es)

        with pytest.raises(AppException) as exc_info:
            await service.on_payment_applied(
                tenant_id=_TENANT_ID,
                account_id="acct_nonexistent",
            )

        assert exc_info.value.status_code == 404
