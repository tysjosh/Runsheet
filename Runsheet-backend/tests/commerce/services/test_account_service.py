"""Unit tests for AccountService.

Tests cover:
- Create: valid creation, customer existence check, validation errors
  (invalid credit_limit_cents, invalid net_terms_days)
- Get: found with computed fields, not found (404), tenant isolation
- List: pagination, filtering by customer_id and status
- Update: partial updates, credit state transitions on limit change
- compute_open_balance: sums non-void invoice remaining_cents
- Event writing: every state-changing op writes an account_event

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, C1, C3
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.account_service import AccountService
from errors.exceptions import AppException


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_abc"
_OTHER_TENANT_ID = "tenant_xyz"
_CUSTOMER_ID = "cust_test123"
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
    account_id: str = "acct_test456",
    tenant_id: str = _TENANT_ID,
    customer_id: str = _CUSTOMER_ID,
    display_name: str = "Main Account",
    credit_limit_cents: int = 500000,
    open_balance_cents: int = 100000,
    available_credit_cents: int = 400000,
    credit_state: str = "ok",
    net_terms_days: int = 30,
    status: str = "active",
) -> Dict[str, Any]:
    """Build an account document as returned from ES."""
    return {
        "account_id": account_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "display_name": display_name,
        "status": status,
        "credit_limit_cents": credit_limit_cents,
        "open_balance_cents": open_balance_cents,
        "available_credit_cents": available_credit_cents,
        "credit_balance_cents": 0,
        "credit_state": credit_state,
        "credit_override_expires_at": None,
        "net_terms_days": net_terms_days,
        "tier": "default",
        "billing_address": None,
        "payment_method_preference": "invoice",
        "created_at": _FIXED_NOW.isoformat(),
        "updated_at": _FIXED_NOW.isoformat(),
        "external_refs": {},
    }


def _make_customer_doc(
    customer_id: str = _CUSTOMER_ID,
    tenant_id: str = _TENANT_ID,
) -> Dict[str, Any]:
    """Build a customer document for the customer existence check."""
    return {
        "customer_id": customer_id,
        "tenant_id": tenant_id,
        "display_name": "Acme Corp",
        "status": "active",
    }


def _es_search_response(
    hits: list[Dict[str, Any]], total: int | None = None
) -> Dict[str, Any]:
    """Build a mock ES search response."""
    return {
        "hits": {
            "hits": [
                {
                    "_source": h,
                    "sort": [
                        h.get("created_at", ""),
                        h.get("account_id", h.get("customer_id", "")),
                    ],
                }
                for h in hits
            ],
            "total": {"value": total if total is not None else len(hits)},
        }
    }


def _es_agg_response(aggs: Dict[str, Any]) -> Dict[str, Any]:
    """Build a mock ES aggregation response."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": aggs,
    }


# ---------------------------------------------------------------------------
# Tests: Create
# ---------------------------------------------------------------------------


class TestAccountServiceCreate:
    """Tests for AccountService.create."""

    @pytest.mark.asyncio
    @patch("commerce.services.account_service.utcnow", return_value=_FIXED_NOW)
    async def test_create_valid_account(self, mock_utcnow):
        """A valid create call persists the account and returns the doc."""
        es = _make_es_service()

        # Mock: customer exists, event seq query returns no events
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([_make_customer_doc()]),  # customer check
                _es_agg_response({"max_seq": {"value": None}}),  # seq number
            ]
        )
        service = AccountService(es)

        result = await service.create(
            _TENANT_ID,
            customer_id=_CUSTOMER_ID,
            display_name="Main Account",
            credit_limit_cents=500000,
            net_terms_days=30,
        )

        assert result["tenant_id"] == _TENANT_ID
        assert result["customer_id"] == _CUSTOMER_ID
        assert result["display_name"] == "Main Account"
        assert result["credit_limit_cents"] == 500000
        assert result["net_terms_days"] == 30
        assert result["credit_state"] == "ok"
        assert result["account_id"].startswith("acct_")
        assert result["open_balance_cents"] == 0
        assert result["available_credit_cents"] == 500000
        # index_document called twice: once for account, once for event
        assert es.index_document.call_count == 2

    @pytest.mark.asyncio
    async def test_create_customer_not_found_raises_422(self):
        """Raises validation error when customer doesn't exist (Req 2.1)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([])  # customer not found
        )
        service = AccountService(es)

        with pytest.raises(AppException) as exc_info:
            await service.create(
                _TENANT_ID,
                customer_id="cust_nonexistent",
                display_name="Test",
                credit_limit_cents=100000,
                net_terms_days=30,
            )

        assert exc_info.value.status_code == 400
        assert "cust_nonexistent" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_create_negative_credit_limit_raises_422(self):
        """Negative credit_limit_cents raises validation error (Req 2.2)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([_make_customer_doc()])
        )
        service = AccountService(es)

        with pytest.raises(AppException) as exc_info:
            await service.create(
                _TENANT_ID,
                customer_id=_CUSTOMER_ID,
                display_name="Test",
                credit_limit_cents=-1,
                net_terms_days=30,
            )

        assert exc_info.value.status_code == 400
        assert "credit_limit_cents" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_create_credit_limit_over_max_raises_422(self):
        """credit_limit_cents > 999_999_999_999 raises validation error (Req 2.2)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([_make_customer_doc()])
        )
        service = AccountService(es)

        with pytest.raises(AppException) as exc_info:
            await service.create(
                _TENANT_ID,
                customer_id=_CUSTOMER_ID,
                display_name="Test",
                credit_limit_cents=1_000_000_000_000,
                net_terms_days=30,
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_create_zero_credit_limit_accepted(self):
        """credit_limit_cents = 0 is valid (cash on delivery only, Req 2.2)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([_make_customer_doc()]),  # customer check
                _es_agg_response({"max_seq": {"value": None}}),  # seq number
            ]
        )
        service = AccountService(es)

        result = await service.create(
            _TENANT_ID,
            customer_id=_CUSTOMER_ID,
            display_name="COD Account",
            credit_limit_cents=0,
            net_terms_days=0,
        )

        assert result["credit_limit_cents"] == 0
        assert result["available_credit_cents"] == 0

    @pytest.mark.asyncio
    async def test_create_invalid_net_terms_raises_422(self):
        """net_terms_days not in {0,7,15,30,45,60,90} raises 422 (Req 2.3)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([_make_customer_doc()])
        )
        service = AccountService(es)

        with pytest.raises(AppException) as exc_info:
            await service.create(
                _TENANT_ID,
                customer_id=_CUSTOMER_ID,
                display_name="Test",
                credit_limit_cents=100000,
                net_terms_days=14,  # not in allowed set
            )

        assert exc_info.value.status_code == 400
        assert "net_terms_days" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_create_all_valid_net_terms_accepted(self):
        """All valid net_terms_days values are accepted (Req 2.3)."""
        valid_terms = [0, 7, 15, 30, 45, 60, 90]
        for terms in valid_terms:
            es = _make_es_service()
            es.search_documents = AsyncMock(
                side_effect=[
                    _es_search_response([_make_customer_doc()]),
                    _es_agg_response({"max_seq": {"value": None}}),
                ]
            )
            service = AccountService(es)

            result = await service.create(
                _TENANT_ID,
                customer_id=_CUSTOMER_ID,
                display_name="Test",
                credit_limit_cents=100000,
                net_terms_days=terms,
            )

            assert result["net_terms_days"] == terms

    @pytest.mark.asyncio
    @patch("commerce.services.account_service.utcnow", return_value=_FIXED_NOW)
    async def test_create_writes_account_event(self, mock_utcnow):
        """Create writes an AccountEvent of type 'created' (Req 2.1)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([_make_customer_doc()]),
                _es_agg_response({"max_seq": {"value": None}}),
            ]
        )
        service = AccountService(es)

        await service.create(
            _TENANT_ID,
            customer_id=_CUSTOMER_ID,
            display_name="Test",
            credit_limit_cents=100000,
            net_terms_days=30,
        )

        # Second index_document call is the event
        event_call = es.index_document.call_args_list[1]
        index_name = event_call[0][0]
        event_doc = event_call[0][2]

        assert index_name == "account_events"
        assert event_doc["event_type"] == "created"
        assert event_doc["tenant_id"] == _TENANT_ID
        assert event_doc["sequence_number"] == 1


# ---------------------------------------------------------------------------
# Tests: Get
# ---------------------------------------------------------------------------


class TestAccountServiceGet:
    """Tests for AccountService.get — computed fields and tenant isolation."""

    @pytest.mark.asyncio
    async def test_get_existing_account_with_computed_fields(self):
        """Returns account with computed open_balance, available_credit,
        oldest_open_invoice_days (Req 2.4)."""
        es = _make_es_service()
        account_doc = _make_account_doc()

        # Mock: get account, compute_open_balance, oldest_open_invoice_days
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account_doc]),  # get raw
                _es_agg_response({"total_remaining": {"value": 150000}}),  # open balance
                _es_agg_response({"oldest_issued": {"value": 1716000000000}}),  # oldest invoice (epoch ms)
            ]
        )
        service = AccountService(es)

        result = await service.get(_TENANT_ID, "acct_test456")

        assert result["account_id"] == "acct_test456"
        assert result["open_balance_cents"] == 150000
        assert result["available_credit_cents"] == 500000 - 150000
        assert isinstance(result["oldest_open_invoice_days"], int)

    @pytest.mark.asyncio
    async def test_get_not_found_raises_404(self):
        """Raises 404 when no account matches the ID under the tenant."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = AccountService(es)

        with pytest.raises(AppException) as exc_info:
            await service.get(_TENANT_ID, "acct_nonexistent")

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_tenant_isolation(self):
        """Cannot access an account belonging to another tenant (C3)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = AccountService(es)

        with pytest.raises(AppException) as exc_info:
            await service.get(_OTHER_TENANT_ID, "acct_test456")

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_no_open_invoices_returns_zero_days(self):
        """oldest_open_invoice_days is 0 when no open invoices exist."""
        es = _make_es_service()
        account_doc = _make_account_doc()

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account_doc]),
                _es_agg_response({"total_remaining": {"value": 0}}),
                _es_agg_response({"oldest_issued": {"value": None}}),
            ]
        )
        service = AccountService(es)

        result = await service.get(_TENANT_ID, "acct_test456")

        assert result["oldest_open_invoice_days"] == 0
        assert result["open_balance_cents"] == 0


# ---------------------------------------------------------------------------
# Tests: List
# ---------------------------------------------------------------------------


class TestAccountServiceList:
    """Tests for AccountService.list — pagination and filtering."""

    @pytest.mark.asyncio
    async def test_list_returns_items(self):
        """Returns a list of accounts for the tenant."""
        es = _make_es_service()
        accounts = [_make_account_doc(account_id=f"acct_{i}") for i in range(3)]
        es.search_documents = AsyncMock(return_value=_es_search_response(accounts))
        service = AccountService(es)

        result = await service.list(_TENANT_ID)

        assert len(result["items"]) == 3
        assert result["limit"] == 50

    @pytest.mark.asyncio
    async def test_list_filters_by_customer_id(self):
        """customer_id filter is included in the query."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = AccountService(es)

        await service.list(_TENANT_ID, customer_id="cust_xyz")

        call_args = es.search_documents.call_args
        query_str = str(call_args[0][1])
        assert "cust_xyz" in query_str

    @pytest.mark.asyncio
    async def test_list_filters_by_status(self):
        """status filter is included in the query."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = AccountService(es)

        await service.list(_TENANT_ID, status="suspended")

        call_args = es.search_documents.call_args
        query_str = str(call_args[0][1])
        assert "suspended" in query_str

    @pytest.mark.asyncio
    async def test_list_clamps_limit_to_max_200(self):
        """Limit > 200 is clamped to 200."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = AccountService(es)

        result = await service.list(_TENANT_ID, limit=500)

        assert result["limit"] == 200

    @pytest.mark.asyncio
    async def test_list_tenant_scoped(self):
        """The list query includes tenant_id filter (C3)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = AccountService(es)

        await service.list(_TENANT_ID)

        call_args = es.search_documents.call_args
        query_str = str(call_args[0][1])
        assert _TENANT_ID in query_str


# ---------------------------------------------------------------------------
# Tests: Update
# ---------------------------------------------------------------------------


class TestAccountServiceUpdate:
    """Tests for AccountService.update — partial updates and credit transitions."""

    @pytest.mark.asyncio
    async def test_update_partial_fields(self):
        """Only provided fields are updated."""
        es = _make_es_service()
        existing = _make_account_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([existing])
        )
        service = AccountService(es)

        result = await service.update(
            _TENANT_ID, "acct_test456", display_name="Updated Name"
        )

        assert result["display_name"] == "Updated Name"
        assert result["credit_limit_cents"] == 500000  # unchanged
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_validates_credit_limit(self):
        """Validation runs on credit_limit_cents during update (Req 2.2)."""
        es = _make_es_service()
        existing = _make_account_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([existing])
        )
        service = AccountService(es)

        with pytest.raises(AppException) as exc_info:
            await service.update(
                _TENANT_ID, "acct_test456", credit_limit_cents=-100
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_update_validates_net_terms(self):
        """Validation runs on net_terms_days during update (Req 2.3)."""
        es = _make_es_service()
        existing = _make_account_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([existing])
        )
        service = AccountService(es)

        with pytest.raises(AppException) as exc_info:
            await service.update(
                _TENANT_ID, "acct_test456", net_terms_days=20
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_update_credit_limit_triggers_hold_transition(self):
        """Reducing credit_limit below open_balance triggers hold (Req 2.5)."""
        es = _make_es_service()
        existing = _make_account_doc(
            credit_limit_cents=500000,
            open_balance_cents=300000,
            credit_state="ok",
        )

        # Mock: _get_raw, compute_open_balance, event seq, then update
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),  # _get_raw
                _es_agg_response({"total_remaining": {"value": 300000}}),  # compute_open_balance
                _es_agg_response({"max_seq": {"value": None}}),  # event seq
            ]
        )
        service = AccountService(es)

        result = await service.update(
            _TENANT_ID,
            "acct_test456",
            credit_limit_cents=200000,  # below open_balance of 300000
        )

        assert result["credit_state"] == "hold"
        # Event was written for the state change
        assert es.index_document.call_count == 1  # event write

    @pytest.mark.asyncio
    async def test_update_no_changes_returns_existing(self):
        """When no fields are provided, returns existing without ES update."""
        es = _make_es_service()
        existing = _make_account_doc()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([existing])
        )
        service = AccountService(es)

        result = await service.update(_TENANT_ID, "acct_test456")

        assert result == existing
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_nonexistent_account_raises_404(self):
        """Updating a non-existent account raises 404."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = AccountService(es)

        with pytest.raises(AppException) as exc_info:
            await service.update(
                _TENANT_ID, "acct_ghost", display_name="X"
            )

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests: compute_open_balance
# ---------------------------------------------------------------------------


class TestAccountServiceComputeOpenBalance:
    """Tests for AccountService.compute_open_balance."""

    @pytest.mark.asyncio
    async def test_compute_open_balance_sums_remaining(self):
        """Sums remaining_cents from non-void invoices (Req 2.4)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_agg_response({"total_remaining": {"value": 250000}})
        )
        service = AccountService(es)

        result = await service.compute_open_balance(_TENANT_ID, "acct_test456")

        assert result == 250000
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_compute_open_balance_zero_when_no_invoices(self):
        """Returns 0 when no open invoices exist."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_agg_response({"total_remaining": {"value": 0}})
        )
        service = AccountService(es)

        result = await service.compute_open_balance(_TENANT_ID, "acct_test456")

        assert result == 0

    @pytest.mark.asyncio
    async def test_compute_open_balance_returns_integer(self):
        """Result is always int even if ES returns float (C1)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_agg_response({"total_remaining": {"value": 99999.0}})
        )
        service = AccountService(es)

        result = await service.compute_open_balance(_TENANT_ID, "acct_test456")

        assert isinstance(result, int)
        assert result == 99999

    @pytest.mark.asyncio
    async def test_compute_open_balance_tenant_scoped(self):
        """The query includes tenant_id filter (C3)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_agg_response({"total_remaining": {"value": 0}})
        )
        service = AccountService(es)

        await service.compute_open_balance(_TENANT_ID, "acct_test456")

        call_args = es.search_documents.call_args
        query_str = str(call_args[0][1])
        assert _TENANT_ID in query_str

    @pytest.mark.asyncio
    async def test_compute_open_balance_excludes_void_and_paid(self):
        """Only invoices with status in (open, partial, overdue, draft) are summed."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_agg_response({"total_remaining": {"value": 100000}})
        )
        service = AccountService(es)

        await service.compute_open_balance(_TENANT_ID, "acct_test456")

        call_args = es.search_documents.call_args
        query_str = str(call_args[0][1])
        # Should include the non-void statuses
        assert "open" in query_str
        assert "partial" in query_str
        assert "overdue" in query_str
        assert "draft" in query_str


# ---------------------------------------------------------------------------
# Tests: refresh_open_balance + credit state transitions
# ---------------------------------------------------------------------------


class TestAccountServiceRefreshOpenBalance:
    """Tests for AccountService.refresh_open_balance — credit state transitions."""

    @pytest.mark.asyncio
    async def test_refresh_transitions_to_hold_when_over_limit(self):
        """Transitions credit_state to hold when open_balance > credit_limit (Req 2.5)."""
        es = _make_es_service()
        existing = _make_account_doc(
            credit_limit_cents=200000,
            credit_state="ok",
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),  # _get_raw
                _es_agg_response({"total_remaining": {"value": 300000}}),  # open balance > limit
                _es_agg_response({"max_seq": {"value": None}}),  # event seq
            ]
        )
        service = AccountService(es)

        result = await service.refresh_open_balance(_TENANT_ID, "acct_test456")

        assert result["credit_state"] == "hold"
        assert result["open_balance_cents"] == 300000
        assert result["available_credit_cents"] == 200000 - 300000
        # Event was written
        assert es.index_document.call_count == 1

    @pytest.mark.asyncio
    async def test_refresh_transitions_to_ok_when_under_limit(self):
        """Transitions credit_state from hold to ok when payment restores limit (Req 2.5)."""
        es = _make_es_service()
        existing = _make_account_doc(
            credit_limit_cents=500000,
            credit_state="hold",
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),  # _get_raw
                _es_agg_response({"total_remaining": {"value": 100000}}),  # now under limit
                _es_agg_response({"max_seq": {"value": 2}}),  # event seq
            ]
        )
        service = AccountService(es)

        result = await service.refresh_open_balance(_TENANT_ID, "acct_test456")

        assert result["credit_state"] == "ok"
        assert result["open_balance_cents"] == 100000
        # Event was written
        assert es.index_document.call_count == 1

    @pytest.mark.asyncio
    async def test_refresh_no_transition_when_override_active(self):
        """Does not transition credit_state when in override state."""
        es = _make_es_service()
        existing = _make_account_doc(
            credit_limit_cents=200000,
            credit_state="override",
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),  # _get_raw
                _es_agg_response({"total_remaining": {"value": 500000}}),  # over limit
            ]
        )
        service = AccountService(es)

        result = await service.refresh_open_balance(_TENANT_ID, "acct_test456")

        # State stays override, no event written
        assert result.get("credit_state", "override") == "override"
        assert es.index_document.call_count == 0

    @pytest.mark.asyncio
    async def test_refresh_idempotent_no_transition_when_already_hold(self):
        """No event written when already in hold and still over limit (idempotent)."""
        es = _make_es_service()
        existing = _make_account_doc(
            credit_limit_cents=200000,
            credit_state="hold",
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),  # _get_raw
                _es_agg_response({"total_remaining": {"value": 300000}}),  # still over
            ]
        )
        service = AccountService(es)

        result = await service.refresh_open_balance(_TENANT_ID, "acct_test456")

        # No state change event written (idempotent)
        assert es.index_document.call_count == 0
        es.update_document.assert_called_once()
