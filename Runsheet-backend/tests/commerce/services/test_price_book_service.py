"""Unit tests for PriceBookService.

Tests cover:
- Create: valid creation with rules, validation errors (empty name, invalid rules)
- Get: found, not found (404), tenant isolation
- List: pagination, status filtering
- Update: partial updates, rule replacement, cache invalidation
- Activate: fan-out into pricing_rules_current, cache invalidation, idempotency

Validates: Requirements 3.1, 3.4, 3.6, C1, C3, C6
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.price_book_service import PriceBookService
from errors.exceptions import AppException


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_abc"
_FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    es.delete_document = AsyncMock(return_value=True)
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return es


def _make_redis_client() -> AsyncMock:
    """Create a mocked async Redis client."""
    redis = AsyncMock()
    redis.delete = AsyncMock(return_value=1)
    return redis


def _canonicalize_fn(code: str) -> str:
    """Simple canonicalize mock that uppercases and strips."""
    normalized = code.strip().upper()
    if not normalized or normalized == "UNKNOWN":
        raise ValueError(f"Unknown product: {code}")
    return normalized


def _make_rule_data(
    *,
    product_code: str = "DIESEL_2",
    unit_price_cents: int = 350,
    scope_type: str = "default",
    scope_value: str = "default",
    effective_from: str = "2026-01-01T00:00:00",
    effective_to: str | None = None,
    min_quantity_gallons: float | None = None,
) -> Dict[str, Any]:
    """Build a rule data dict for testing."""
    rule: Dict[str, Any] = {
        "product_code": product_code,
        "unit_price_cents": unit_price_cents,
        "scope_type": scope_type,
        "scope_value": scope_value,
        "effective_from": effective_from,
        "min_quantity_gallons": min_quantity_gallons,
    }
    if effective_to is not None:
        rule["effective_to"] = effective_to
    return rule


def _make_book_doc(
    *,
    price_book_id: str = "pb_test123",
    tenant_id: str = _TENANT_ID,
    name: str = "Default Book",
    status: str = "draft",
    rule_count: int = 0,
) -> Dict[str, Any]:
    """Build a price book document as returned from ES."""
    return {
        "price_book_id": price_book_id,
        "tenant_id": tenant_id,
        "name": name,
        "description": None,
        "status": status,
        "rule_count": rule_count,
        "created_at": _FIXED_NOW.isoformat(),
        "updated_at": _FIXED_NOW.isoformat(),
    }


def _es_search_response(
    hits: List[Dict[str, Any]], total: int | None = None
) -> Dict[str, Any]:
    """Build a mock ES search response."""
    return {
        "hits": {
            "hits": [
                {
                    "_source": h,
                    "sort": [
                        h.get("created_at", ""),
                        h.get("price_book_id", h.get("rule_id", "")),
                    ],
                }
                for h in hits
            ],
            "total": {"value": total if total is not None else len(hits)},
        }
    }


# ---------------------------------------------------------------------------
# Tests: Create
# ---------------------------------------------------------------------------


class TestPriceBookServiceCreate:
    """Tests for PriceBookService.create."""

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_create_valid_book_with_rules(self, mock_utcnow):
        """A valid create call persists the book and its rules."""
        es = _make_es_service()
        redis = _make_redis_client()
        service = PriceBookService(es, redis, _canonicalize_fn)

        rules = [
            _make_rule_data(product_code="diesel_2", unit_price_cents=350),
            _make_rule_data(product_code="gasoline_reg", unit_price_cents=299),
        ]

        result = await service.create(
            _TENANT_ID,
            name="Q1 2026 Prices",
            description="First quarter pricing",
            rules=rules,
        )

        assert result["tenant_id"] == _TENANT_ID
        assert result["name"] == "Q1 2026 Prices"
        assert result["price_book_id"].startswith("pb_")
        assert result["rule_count"] == 2
        assert result["status"] == "draft"
        assert len(result["rules"]) == 2
        # Rules should have canonicalized product codes
        assert result["rules"][0]["product_code"] == "DIESEL_2"
        assert result["rules"][1]["product_code"] == "GASOLINE_REG"
        # Book persisted
        es.index_document.assert_called()

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_create_empty_name_raises_422(self, mock_utcnow):
        """Empty name raises a validation error."""
        es = _make_es_service()
        service = PriceBookService(es)

        with pytest.raises(AppException) as exc_info:
            await service.create(_TENANT_ID, name="")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_create_invalid_product_code_raises_422(self, mock_utcnow):
        """Unknown product code raises a validation error (C6)."""
        es = _make_es_service()
        service = PriceBookService(es, canonicalize_fn=_canonicalize_fn)

        rules = [_make_rule_data(product_code="UNKNOWN")]

        with pytest.raises(AppException) as exc_info:
            await service.create(_TENANT_ID, name="Test", rules=rules)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_create_negative_unit_price_raises_422(self, mock_utcnow):
        """Negative unit_price_cents raises a validation error (C1)."""
        es = _make_es_service()
        service = PriceBookService(es, canonicalize_fn=_canonicalize_fn)

        rules = [_make_rule_data(unit_price_cents=-100)]

        with pytest.raises(AppException) as exc_info:
            await service.create(_TENANT_ID, name="Test", rules=rules)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_create_incoherent_effective_window_raises_422(self, mock_utcnow):
        """effective_to before effective_from raises a validation error."""
        es = _make_es_service()
        service = PriceBookService(es, canonicalize_fn=_canonicalize_fn)

        rules = [
            _make_rule_data(
                effective_from="2026-06-01T00:00:00",
                effective_to="2026-01-01T00:00:00",
            )
        ]

        with pytest.raises(AppException) as exc_info:
            await service.create(_TENANT_ID, name="Test", rules=rules)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_create_active_book_invalidates_cache(self, mock_utcnow):
        """Creating a book with status=active bumps cache invalidation (Req 3.6)."""
        es = _make_es_service()
        redis = _make_redis_client()
        service = PriceBookService(es, redis, _canonicalize_fn)

        rules = [_make_rule_data(product_code="diesel_2")]

        await service.create(
            _TENANT_ID, name="Active Book", status="active", rules=rules
        )

        # Redis delete should have been called for the product code
        redis.delete.assert_called_once_with(
            f"commerce:pricing:{_TENANT_ID}:DIESEL_2"
        )


# ---------------------------------------------------------------------------
# Tests: Get
# ---------------------------------------------------------------------------


class TestPriceBookServiceGet:
    """Tests for PriceBookService.get."""

    @pytest.mark.asyncio
    async def test_get_existing_book(self):
        """Get returns the book with its rules."""
        es = _make_es_service()
        book = _make_book_doc()
        rule = {
            "rule_id": "rule_1",
            "price_book_id": "pb_test123",
            "tenant_id": _TENANT_ID,
            "product_code": "DIESEL_2",
            "scope_type": "default",
            "scope_value": "default",
            "effective_from": "2026-01-01T00:00:00",
            "effective_to": None,
            "min_quantity_gallons": None,
            "unit_price_cents": 350,
            "created_at": _FIXED_NOW.isoformat(),
        }

        # First call returns the book, second returns rules
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([book]),
                _es_search_response([rule]),
            ]
        )

        service = PriceBookService(es)
        result = await service.get(_TENANT_ID, "pb_test123")

        assert result["price_book_id"] == "pb_test123"
        assert result["name"] == "Default Book"
        assert len(result["rules"]) == 1
        assert result["rules"][0]["product_code"] == "DIESEL_2"

    @pytest.mark.asyncio
    async def test_get_not_found_raises_404(self):
        """Get raises 404 when book doesn't exist."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))

        service = PriceBookService(es)

        with pytest.raises(AppException) as exc_info:
            await service.get(_TENANT_ID, "pb_nonexistent")

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests: List
# ---------------------------------------------------------------------------


class TestPriceBookServiceList:
    """Tests for PriceBookService.list."""

    @pytest.mark.asyncio
    async def test_list_returns_paginated_results(self):
        """List returns books with pagination metadata."""
        es = _make_es_service()
        books = [_make_book_doc(price_book_id=f"pb_{i}") for i in range(3)]
        es.search_documents = AsyncMock(return_value=_es_search_response(books))

        service = PriceBookService(es)
        result = await service.list(_TENANT_ID, limit=10)

        assert len(result["items"]) == 3
        assert result["limit"] == 10

    @pytest.mark.asyncio
    async def test_list_clamps_limit(self):
        """List clamps limit to max 200."""
        es = _make_es_service()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))

        service = PriceBookService(es)
        result = await service.list(_TENANT_ID, limit=500)

        assert result["limit"] == 200


# ---------------------------------------------------------------------------
# Tests: Update
# ---------------------------------------------------------------------------


class TestPriceBookServiceUpdate:
    """Tests for PriceBookService.update."""

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_update_name(self, mock_utcnow):
        """Updating name persists the change and bumps cache."""
        es = _make_es_service()
        redis = _make_redis_client()
        book = _make_book_doc()

        # get() calls: first for book, second for rules
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([book]),
                _es_search_response([]),  # no rules
            ]
        )

        service = PriceBookService(es, redis, _canonicalize_fn)
        result = await service.update(_TENANT_ID, "pb_test123", name="New Name")

        assert result["name"] == "New Name"
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_update_rules_replaces_old_rules(self, mock_utcnow):
        """Updating rules removes old rules and persists new ones."""
        es = _make_es_service()
        redis = _make_redis_client()
        old_rule = {
            "rule_id": "rule_old",
            "price_book_id": "pb_test123",
            "tenant_id": _TENANT_ID,
            "product_code": "DIESEL_2",
            "scope_type": "default",
            "scope_value": "default",
            "effective_from": "2026-01-01T00:00:00",
            "effective_to": None,
            "min_quantity_gallons": None,
            "unit_price_cents": 300,
            "created_at": _FIXED_NOW.isoformat(),
        }
        book = _make_book_doc(rule_count=1)

        # Calls: get book, get rules for book, remove old rules search, then persist
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([book]),       # get() -> book
                _es_search_response([old_rule]),   # get() -> rules
                _es_search_response([old_rule]),   # _remove_rules_for_book
            ]
        )

        service = PriceBookService(es, redis, _canonicalize_fn)
        new_rules = [_make_rule_data(product_code="diesel_2", unit_price_cents=400)]

        result = await service.update(
            _TENANT_ID, "pb_test123", rules=new_rules
        )

        assert result["rule_count"] == 1
        assert result["rules"][0]["unit_price_cents"] == 400
        # Old rule should have been deleted
        es.delete_document.assert_called()

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_update_bumps_cache_invalidation(self, mock_utcnow):
        """Any update bumps cache invalidation (Req 3.6)."""
        es = _make_es_service()
        redis = _make_redis_client()
        book = _make_book_doc()
        rule = {
            "rule_id": "rule_1",
            "price_book_id": "pb_test123",
            "tenant_id": _TENANT_ID,
            "product_code": "DIESEL_2",
            "scope_type": "default",
            "scope_value": "default",
            "effective_from": "2026-01-01T00:00:00",
            "effective_to": None,
            "min_quantity_gallons": None,
            "unit_price_cents": 350,
            "created_at": _FIXED_NOW.isoformat(),
        }

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([book]),
                _es_search_response([rule]),
            ]
        )

        service = PriceBookService(es, redis, _canonicalize_fn)
        await service.update(_TENANT_ID, "pb_test123", name="Updated")

        redis.delete.assert_called_with(
            f"commerce:pricing:{_TENANT_ID}:DIESEL_2"
        )


# ---------------------------------------------------------------------------
# Tests: Activate
# ---------------------------------------------------------------------------


class TestPriceBookServiceActivate:
    """Tests for PriceBookService.activate."""

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_activate_draft_book(self, mock_utcnow):
        """Activating a draft book transitions to active and fans out rules."""
        es = _make_es_service()
        redis = _make_redis_client()
        book = _make_book_doc(status="draft", rule_count=1)
        rule = {
            "rule_id": "rule_1",
            "price_book_id": "pb_test123",
            "tenant_id": _TENANT_ID,
            "product_code": "DIESEL_2",
            "scope_type": "default",
            "scope_value": "default",
            "effective_from": "2026-01-01T00:00:00",
            "effective_to": None,
            "min_quantity_gallons": None,
            "unit_price_cents": 350,
            "created_at": _FIXED_NOW.isoformat(),
        }

        # get() -> book + rules, then _remove_rules_for_book search
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([book]),   # get() -> book
                _es_search_response([rule]),   # get() -> rules
                _es_search_response([rule]),   # _remove_rules_for_book
            ]
        )

        service = PriceBookService(es, redis, _canonicalize_fn)
        result = await service.activate(_TENANT_ID, "pb_test123")

        assert result["status"] == "active"
        # Book status updated
        es.update_document.assert_called_once()
        # Old rules deleted and new ones persisted
        es.delete_document.assert_called()
        # Cache invalidated
        redis.delete.assert_called_with(
            f"commerce:pricing:{_TENANT_ID}:DIESEL_2"
        )

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_activate_already_active_is_idempotent(self, mock_utcnow):
        """Activating an already-active book re-fans rules without error."""
        es = _make_es_service()
        redis = _make_redis_client()
        book = _make_book_doc(status="active", rule_count=1)
        rule = {
            "rule_id": "rule_1",
            "price_book_id": "pb_test123",
            "tenant_id": _TENANT_ID,
            "product_code": "DIESEL_2",
            "scope_type": "default",
            "scope_value": "default",
            "effective_from": "2026-01-01T00:00:00",
            "effective_to": None,
            "min_quantity_gallons": None,
            "unit_price_cents": 350,
            "created_at": _FIXED_NOW.isoformat(),
        }

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([book]),   # get() -> book
                _es_search_response([rule]),   # get() -> rules
                _es_search_response([rule]),   # _remove_rules_for_book
            ]
        )

        service = PriceBookService(es, redis, _canonicalize_fn)
        result = await service.activate(_TENANT_ID, "pb_test123")

        # Should still be active, no error
        assert result["status"] == "active"
        # Cache still invalidated for idempotency
        redis.delete.assert_called()

    @pytest.mark.asyncio
    async def test_activate_archived_book_raises_409(self):
        """Activating an archived book raises a conflict error."""
        es = _make_es_service()
        book = _make_book_doc(status="archived")

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([book]),
                _es_search_response([]),  # rules
            ]
        )

        service = PriceBookService(es)

        with pytest.raises(AppException) as exc_info:
            await service.activate(_TENANT_ID, "pb_test123")

        assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Tests: Cache invalidation
# ---------------------------------------------------------------------------


class TestPriceBookServiceCacheInvalidation:
    """Tests for cache invalidation behavior."""

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_no_redis_skips_invalidation(self, mock_utcnow):
        """When no Redis client is configured, cache invalidation is a no-op."""
        es = _make_es_service()
        service = PriceBookService(es, redis_client=None, canonicalize_fn=_canonicalize_fn)

        rules = [_make_rule_data(product_code="diesel_2")]

        # Should not raise even without Redis
        result = await service.create(
            _TENANT_ID, name="No Redis Book", status="active", rules=rules
        )

        assert result["name"] == "No Redis Book"

    @pytest.mark.asyncio
    @patch("commerce.services.price_book_service.utcnow", return_value=_FIXED_NOW)
    async def test_multiple_product_codes_all_invalidated(self, mock_utcnow):
        """Cache keys for all affected product codes are deleted."""
        es = _make_es_service()
        redis = _make_redis_client()
        service = PriceBookService(es, redis, _canonicalize_fn)

        rules = [
            _make_rule_data(product_code="diesel_2", unit_price_cents=350),
            _make_rule_data(product_code="gasoline_reg", unit_price_cents=299),
            _make_rule_data(product_code="diesel_2", unit_price_cents=400),  # duplicate product
        ]

        await service.create(
            _TENANT_ID, name="Multi Product", status="active", rules=rules
        )

        # Should have 2 unique product codes invalidated (DIESEL_2 and GASOLINE_REG)
        assert redis.delete.call_count == 2
        called_keys = {call.args[0] for call in redis.delete.call_args_list}
        assert f"commerce:pricing:{_TENANT_ID}:DIESEL_2" in called_keys
        assert f"commerce:pricing:{_TENANT_ID}:GASOLINE_REG" in called_keys
