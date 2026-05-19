"""Unit tests for CustomerService.

Tests cover:
- Create: valid creation, validation errors (empty display_name, long display_name, invalid tax_id)
- Get: found, not found (404), tenant isolation (can't access other tenant's customer)
- List: pagination, status filtering, tenant scoping
- Update: partial updates, validation on update
- Archive: successful archive, blocked by open invoices (409)
- Projections: correct computation of aggregates

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.6, C3
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.customer_service import CustomerService
from errors.exceptions import AppException


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_abc"
_OTHER_TENANT_ID = "tenant_xyz"
_FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return es


def _make_customer_doc(
    *,
    customer_id: str = "cust_test123",
    tenant_id: str = _TENANT_ID,
    display_name: str = "Acme Corp",
    status: str = "active",
) -> Dict[str, Any]:
    """Build a customer document as returned from ES."""
    return {
        "customer_id": customer_id,
        "tenant_id": tenant_id,
        "display_name": display_name,
        "legal_name": None,
        "primary_email": "acme@example.com",
        "tax_id": "US-12345",
        "status": status,
        "created_at": _FIXED_NOW.isoformat(),
        "updated_at": _FIXED_NOW.isoformat(),
        "external_refs": {},
        "metadata": {},
    }


def _es_search_response(hits: list[Dict[str, Any]], total: int | None = None) -> Dict[str, Any]:
    """Build a mock ES search response."""
    return {
        "hits": {
            "hits": [{"_source": h, "sort": [h.get("created_at", ""), h.get("customer_id", "")]} for h in hits],
            "total": {"value": total if total is not None else len(hits)},
        }
    }


# ---------------------------------------------------------------------------
# Tests: Create
# ---------------------------------------------------------------------------


class TestCustomerServiceCreate:
    """Tests for CustomerService.create."""

    @pytest.mark.asyncio
    @patch("commerce.services.customer_service.utcnow", return_value=_FIXED_NOW)
    async def test_create_valid_customer(self, mock_utcnow):
        """A valid create call persists the customer and returns the doc."""
        es = _make_es_service()
        service = CustomerService(es)

        result = await service.create(
            _TENANT_ID,
            display_name="Acme Corp",
            legal_name="Acme Corporation LLC",
            primary_email="billing@acme.com",
            tax_id="US-12345",
        )

        assert result["tenant_id"] == _TENANT_ID
        assert result["display_name"] == "Acme Corp"
        assert result["legal_name"] == "Acme Corporation LLC"
        assert result["primary_email"] == "billing@acme.com"
        assert result["tax_id"] == "US-12345"
        assert result["status"] == "active"
        assert result["customer_id"].startswith("cust_")
        assert result["created_at"] == _FIXED_NOW.isoformat()
        es.index_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_empty_display_name_raises_422(self):
        """Empty display_name raises a validation error (Req 1.2)."""
        es = _make_es_service()
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.create(_TENANT_ID, display_name="")

        assert exc_info.value.status_code == 400
        assert "display_name" in exc_info.value.message.lower()

    @pytest.mark.asyncio
    async def test_create_whitespace_display_name_raises_422(self):
        """Whitespace-only display_name raises a validation error (Req 1.2)."""
        es = _make_es_service()
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.create(_TENANT_ID, display_name="   \t  ")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_create_long_display_name_raises_422(self):
        """display_name > 255 chars raises a validation error (Req 1.2)."""
        es = _make_es_service()
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.create(_TENANT_ID, display_name="A" * 256)

        assert exc_info.value.status_code == 400
        assert "255" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_create_invalid_tax_id_raises_422(self):
        """Invalid tax_id pattern raises a validation error (Req 1.2)."""
        es = _make_es_service()
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.create(_TENANT_ID, display_name="Acme", tax_id="invalid!@#")

        assert exc_info.value.status_code == 400
        assert "tax_id" in exc_info.value.message.lower()

    @pytest.mark.asyncio
    async def test_create_lowercase_tax_id_raises_422(self):
        """Lowercase tax_id fails the ^[A-Z0-9-]{1,64}$ pattern."""
        es = _make_es_service()
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.create(_TENANT_ID, display_name="Acme", tax_id="us-12345")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_create_null_tax_id_is_valid(self):
        """tax_id=None is accepted (optional field)."""
        es = _make_es_service()
        service = CustomerService(es)

        result = await service.create(_TENANT_ID, display_name="Acme", tax_id=None)

        assert result["tax_id"] is None

    @pytest.mark.asyncio
    async def test_create_strips_display_name_whitespace(self):
        """Leading/trailing whitespace is stripped from display_name."""
        es = _make_es_service()
        service = CustomerService(es)

        result = await service.create(_TENANT_ID, display_name="  Acme Corp  ")

        assert result["display_name"] == "Acme Corp"


# ---------------------------------------------------------------------------
# Tests: Get
# ---------------------------------------------------------------------------


class TestCustomerServiceGet:
    """Tests for CustomerService.get — tenant isolation and not-found."""

    @pytest.mark.asyncio
    async def test_get_existing_customer(self):
        """Returns the customer when found under the correct tenant."""
        es = _make_es_service()
        customer_doc = _make_customer_doc()
        es.search_documents = AsyncMock(return_value=_es_search_response([customer_doc]))
        service = CustomerService(es)

        result = await service.get(_TENANT_ID, "cust_test123")

        assert result["customer_id"] == "cust_test123"
        assert result["display_name"] == "Acme Corp"

    @pytest.mark.asyncio
    async def test_get_not_found_raises_404(self):
        """Raises 404 when no customer matches the ID under the tenant."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.get(_TENANT_ID, "cust_nonexistent")

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_tenant_isolation(self):
        """Cannot access a customer belonging to another tenant (C3).

        The query passes through inject_tenant_filter, so even if the
        customer_id exists, it won't be found under the wrong tenant.
        """
        es = _make_es_service()
        # Simulate: customer exists for tenant_abc but query is for tenant_xyz
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.get(_OTHER_TENANT_ID, "cust_test123")

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_query_includes_tenant_filter(self):
        """The ES query passed to search_documents includes a tenant_id filter."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([_make_customer_doc()]))
        service = CustomerService(es)

        await service.get(_TENANT_ID, "cust_test123")

        # Verify the query was called with tenant filter
        call_args = es.search_documents.call_args
        query = call_args[0][1]  # second positional arg is the query
        # inject_tenant_filter wraps with a bool filter containing tenant_id term
        query_str = str(query)
        assert "tenant_id" in query_str
        assert _TENANT_ID in query_str


# ---------------------------------------------------------------------------
# Tests: List
# ---------------------------------------------------------------------------


class TestCustomerServiceList:
    """Tests for CustomerService.list — pagination, filtering, tenant scoping."""

    @pytest.mark.asyncio
    async def test_list_returns_items(self):
        """Returns a list of customers for the tenant."""
        es = _make_es_service()
        customers = [_make_customer_doc(customer_id=f"cust_{i}") for i in range(3)]
        es.search_documents = AsyncMock(return_value=_es_search_response(customers))
        service = CustomerService(es)

        result = await service.list(_TENANT_ID)

        assert len(result["items"]) == 3
        assert result["limit"] == 50  # default

    @pytest.mark.asyncio
    async def test_list_respects_limit(self):
        """Limit parameter is passed to the ES query."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = CustomerService(es)

        await service.list(_TENANT_ID, limit=10)

        call_args = es.search_documents.call_args
        # size is passed as keyword arg
        assert call_args[1]["size"] == 10 or call_args[0][1].get("size") == 10

    @pytest.mark.asyncio
    async def test_list_clamps_limit_to_max_200(self):
        """Limit > 200 is clamped to 200 (Req 1.3)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = CustomerService(es)

        result = await service.list(_TENANT_ID, limit=500)

        assert result["limit"] == 200

    @pytest.mark.asyncio
    async def test_list_status_filter(self):
        """Status filter is included in the query."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = CustomerService(es)

        await service.list(_TENANT_ID, status="archived")

        call_args = es.search_documents.call_args
        query_str = str(call_args[0][1])
        assert "archived" in query_str

    @pytest.mark.asyncio
    async def test_list_tenant_scoped(self):
        """The list query includes tenant_id filter (C3)."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = CustomerService(es)

        await service.list(_TENANT_ID)

        call_args = es.search_documents.call_args
        query_str = str(call_args[0][1])
        assert _TENANT_ID in query_str

    @pytest.mark.asyncio
    async def test_list_next_cursor_when_full_page(self):
        """next_cursor is set when the page is full."""
        es = _make_es_service()
        # Return exactly `limit` items to trigger next_cursor
        customers = [_make_customer_doc(customer_id=f"cust_{i}") for i in range(5)]
        es.search_documents = AsyncMock(return_value=_es_search_response(customers))
        service = CustomerService(es)

        result = await service.list(_TENANT_ID, limit=5)

        assert result["next_cursor"] is not None

    @pytest.mark.asyncio
    async def test_list_no_cursor_when_partial_page(self):
        """next_cursor is None when fewer items than limit are returned."""
        es = _make_es_service()
        customers = [_make_customer_doc(customer_id=f"cust_{i}") for i in range(3)]
        es.search_documents = AsyncMock(return_value=_es_search_response(customers))
        service = CustomerService(es)

        result = await service.list(_TENANT_ID, limit=10)

        assert result["next_cursor"] is None

    @pytest.mark.asyncio
    async def test_list_with_account_counts(self):
        """When include_account_counts=True, each customer includes account_count."""
        es = _make_es_service()
        customers = [
            _make_customer_doc(customer_id="cust_001"),
            _make_customer_doc(customer_id="cust_002"),
            _make_customer_doc(customer_id="cust_003"),
        ]
        
        # Mock account aggregation response
        account_agg_response = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_customer": {
                    "buckets": [
                        {"key": "cust_001", "doc_count": 2},
                        {"key": "cust_002", "doc_count": 3},
                        {"key": "cust_003", "doc_count": 1},
                    ]
                }
            },
        }
        
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response(customers),  # list query
                account_agg_response,  # account counts aggregation
            ]
        )
        service = CustomerService(es)

        result = await service.list(_TENANT_ID, include_account_counts=True)

        assert len(result["items"]) == 3
        assert result["items"][0]["account_count"] == 2
        assert result["items"][1]["account_count"] == 3
        assert result["items"][2]["account_count"] == 1

    @pytest.mark.asyncio
    async def test_list_without_account_counts(self):
        """When include_account_counts=False, no account_count field is added."""
        es = _make_es_service()
        customers = [_make_customer_doc(customer_id="cust_001")]
        es.search_documents = AsyncMock(return_value=_es_search_response(customers))
        service = CustomerService(es)

        result = await service.list(_TENANT_ID, include_account_counts=False)

        assert len(result["items"]) == 1
        assert "account_count" not in result["items"][0]
        # Only one ES call (no account aggregation)
        assert es.search_documents.call_count == 1

    @pytest.mark.asyncio
    async def test_list_account_counts_defaults_to_zero_when_missing(self):
        """Customers with no accounts get account_count=0."""
        es = _make_es_service()
        customers = [
            _make_customer_doc(customer_id="cust_001"),
            _make_customer_doc(customer_id="cust_002"),
        ]
        
        # Only cust_001 has accounts in the aggregation
        account_agg_response = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_customer": {
                    "buckets": [
                        {"key": "cust_001", "doc_count": 5},
                    ]
                }
            },
        }
        
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response(customers),
                account_agg_response,
            ]
        )
        service = CustomerService(es)

        result = await service.list(_TENANT_ID, include_account_counts=True)

        assert result["items"][0]["account_count"] == 5
        assert result["items"][1]["account_count"] == 0  # defaults to 0


# ---------------------------------------------------------------------------
# Tests: Update
# ---------------------------------------------------------------------------


class TestCustomerServiceUpdate:
    """Tests for CustomerService.update — partial updates and validation."""

    @pytest.mark.asyncio
    async def test_update_partial_fields(self):
        """Only provided fields are updated."""
        es = _make_es_service()
        existing = _make_customer_doc()
        es.search_documents = AsyncMock(return_value=_es_search_response([existing]))
        service = CustomerService(es)

        result = await service.update(
            _TENANT_ID, "cust_test123", display_name="New Name"
        )

        assert result["display_name"] == "New Name"
        # Other fields remain unchanged
        assert result["primary_email"] == "acme@example.com"
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_validates_display_name(self):
        """Validation runs on display_name during update (Req 1.2)."""
        es = _make_es_service()
        existing = _make_customer_doc()
        es.search_documents = AsyncMock(return_value=_es_search_response([existing]))
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.update(_TENANT_ID, "cust_test123", display_name="")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_update_validates_tax_id(self):
        """Validation runs on tax_id during update (Req 1.2)."""
        es = _make_es_service()
        existing = _make_customer_doc()
        es.search_documents = AsyncMock(return_value=_es_search_response([existing]))
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.update(_TENANT_ID, "cust_test123", tax_id="bad!id")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_update_nonexistent_customer_raises_404(self):
        """Updating a non-existent customer raises 404."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.update(_TENANT_ID, "cust_ghost", display_name="X")

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_update_no_changes_returns_existing(self):
        """When no fields are provided, returns existing without ES update."""
        es = _make_es_service()
        existing = _make_customer_doc()
        es.search_documents = AsyncMock(return_value=_es_search_response([existing]))
        service = CustomerService(es)

        result = await service.update(_TENANT_ID, "cust_test123")

        assert result == existing
        es.update_document.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Archive
# ---------------------------------------------------------------------------


class TestCustomerServiceArchive:
    """Tests for CustomerService.archive — success and blocked by open invoices."""

    @pytest.mark.asyncio
    async def test_archive_success_no_open_invoices(self):
        """Archive succeeds when no open invoices exist (Req 1.6)."""
        es = _make_es_service()
        existing = _make_customer_doc()

        # First call: get customer; Second call: check invoices (empty)
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),  # get() call
                _es_search_response([]),  # invoice check
            ]
        )
        service = CustomerService(es)

        result = await service.archive(_TENANT_ID, "cust_test123")

        assert result["status"] == "archived"
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_archive_blocked_by_open_invoices_raises_409(self):
        """Archive is rejected with 409 when open invoices exist (Req 1.6)."""
        es = _make_es_service()
        existing = _make_customer_doc()
        open_invoices = [
            {"invoice_id": "inv_001"},
            {"invoice_id": "inv_002"},
        ]

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),  # get() call
                _es_search_response(open_invoices),  # invoice check
            ]
        )
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.archive(_TENANT_ID, "cust_test123")

        assert exc_info.value.status_code == 409
        assert "inv_001" in str(exc_info.value.details["blocking_invoice_ids"])
        assert "inv_002" in str(exc_info.value.details["blocking_invoice_ids"])

    @pytest.mark.asyncio
    async def test_archive_nonexistent_customer_raises_404(self):
        """Archiving a non-existent customer raises 404."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.archive(_TENANT_ID, "cust_ghost")

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Get with projections
# ---------------------------------------------------------------------------


class TestCustomerServiceProjections:
    """Tests for CustomerService.get_with_projections — aggregate computation."""

    @pytest.mark.asyncio
    async def test_projections_correct_computation(self):
        """Projections compute open_invoice_count, open_balance_cents,
        lifetime_revenue_cents, and account_count correctly (Req 1.4)."""
        es = _make_es_service()
        existing = _make_customer_doc()

        # Mock responses for: get(), account count, invoice aggregations
        account_response = {
            "hits": {"hits": [], "total": {"value": 3}},
        }
        invoice_agg_response = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "open_invoices": {
                    "count": {"value": 5},
                    "total_remaining": {"value": 125000},
                },
                "lifetime_revenue": {"value": 500000},
            },
        }

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),  # get() call
                account_response,  # account count
                invoice_agg_response,  # invoice aggregations
            ]
        )
        service = CustomerService(es)

        result = await service.get_with_projections(_TENANT_ID, "cust_test123")

        assert result["open_invoice_count"] == 5
        assert result["open_balance_cents"] == 125000
        assert result["lifetime_revenue_cents"] == 500000
        assert result["account_count"] == 3

    @pytest.mark.asyncio
    async def test_projections_zero_values(self):
        """Projections return 0 when no invoices or accounts exist."""
        es = _make_es_service()
        existing = _make_customer_doc()

        account_response = {
            "hits": {"hits": [], "total": {"value": 0}},
        }
        invoice_agg_response = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "open_invoices": {
                    "count": {"value": 0},
                    "total_remaining": {"value": 0},
                },
                "lifetime_revenue": {"value": 0},
            },
        }

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),  # get() call
                account_response,  # account count
                invoice_agg_response,  # invoice aggregations
            ]
        )
        service = CustomerService(es)

        result = await service.get_with_projections(_TENANT_ID, "cust_test123")

        assert result["open_invoice_count"] == 0
        assert result["open_balance_cents"] == 0
        assert result["lifetime_revenue_cents"] == 0
        assert result["account_count"] == 0

    @pytest.mark.asyncio
    async def test_projections_missing_aggregations_defaults_to_zero(self):
        """When aggregations key is missing, projections default to 0."""
        es = _make_es_service()
        existing = _make_customer_doc()

        account_response = {
            "hits": {"hits": [], "total": {"value": 2}},
        }
        # No aggregations key in response
        invoice_agg_response = {
            "hits": {"hits": [], "total": {"value": 0}},
        }

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),  # get() call
                account_response,  # account count
                invoice_agg_response,  # invoice aggregations
            ]
        )
        service = CustomerService(es)

        result = await service.get_with_projections(_TENANT_ID, "cust_test123")

        assert result["open_invoice_count"] == 0
        assert result["open_balance_cents"] == 0
        assert result["lifetime_revenue_cents"] == 0
        assert result["account_count"] == 2

    @pytest.mark.asyncio
    async def test_projections_tenant_scoped(self):
        """All projection queries include tenant_id filter (C3)."""
        es = _make_es_service()
        existing = _make_customer_doc()

        account_response = {
            "hits": {"hits": [], "total": {"value": 0}},
        }
        invoice_agg_response = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "open_invoices": {
                    "count": {"value": 0},
                    "total_remaining": {"value": 0},
                },
                "lifetime_revenue": {"value": 0},
            },
        }

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),
                account_response,
                invoice_agg_response,
            ]
        )
        service = CustomerService(es)

        await service.get_with_projections(_TENANT_ID, "cust_test123")

        # All three search calls should include tenant_id
        assert es.search_documents.call_count == 3
        for call in es.search_documents.call_args_list:
            query_str = str(call[0][1])
            assert _TENANT_ID in query_str

    @pytest.mark.asyncio
    async def test_projections_nonexistent_customer_raises_404(self):
        """get_with_projections raises 404 for non-existent customer."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        service = CustomerService(es)

        with pytest.raises(AppException) as exc_info:
            await service.get_with_projections(_TENANT_ID, "cust_ghost")

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_projections_values_are_integers(self):
        """All _cents projection values are integers, not floats (C1)."""
        es = _make_es_service()
        existing = _make_customer_doc()

        account_response = {
            "hits": {"hits": [], "total": {"value": 1}},
        }
        # ES may return floats for sum aggregations
        invoice_agg_response = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "open_invoices": {
                    "count": {"value": 2},
                    "total_remaining": {"value": 99999.0},
                },
                "lifetime_revenue": {"value": 250000.0},
            },
        }

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([existing]),
                account_response,
                invoice_agg_response,
            ]
        )
        service = CustomerService(es)

        result = await service.get_with_projections(_TENANT_ID, "cust_test123")

        assert isinstance(result["open_balance_cents"], int)
        assert isinstance(result["lifetime_revenue_cents"], int)
        assert result["open_balance_cents"] == 99999
        assert result["lifetime_revenue_cents"] == 250000
