"""
Unit tests for schemas.common — unified request/response schemas.

Validates: Requirements 4.1, 4.2, 4.3
"""

import pytest
from pydantic import ValidationError

from schemas.common import (
    ErrorResponse,
    ListEnvelope,
    PaginatedResponse,
    TenantScopedRequest,
)


# ---------------------------------------------------------------------------
# PaginatedResponse tests
# ---------------------------------------------------------------------------


class TestPaginatedResponse:
    """Tests for PaginatedResponse[T] generic model."""

    def test_create_basic(self):
        """PaginatedResponse.create computes has_next correctly."""
        resp = PaginatedResponse.create(
            items=["a", "b", "c"],
            total=10,
            page=1,
            page_size=3,
        )
        assert resp.items == ["a", "b", "c"]
        assert resp.total == 10
        assert resp.page == 1
        assert resp.page_size == 3
        assert resp.has_next is True

    def test_last_page_has_next_false(self):
        """has_next is False when on the last page."""
        resp = PaginatedResponse.create(
            items=["x"],
            total=5,
            page=5,
            page_size=1,
        )
        assert resp.has_next is False

    def test_single_page(self):
        """has_next is False when all items fit on one page."""
        resp = PaginatedResponse.create(
            items=[1, 2, 3],
            total=3,
            page=1,
            page_size=10,
        )
        assert resp.has_next is False

    def test_empty_items(self):
        """PaginatedResponse works with an empty items list."""
        resp = PaginatedResponse.create(
            items=[],
            total=0,
            page=1,
            page_size=20,
        )
        assert resp.items == []
        assert resp.total == 0
        assert resp.has_next is False

    def test_serialization_roundtrip(self):
        """PaginatedResponse serializes and deserializes correctly."""
        resp = PaginatedResponse.create(
            items=[{"id": 1}, {"id": 2}],
            total=50,
            page=2,
            page_size=2,
        )
        data = resp.model_dump()
        assert data["items"] == [{"id": 1}, {"id": 2}]
        assert data["total"] == 50
        assert data["page"] == 2
        assert data["page_size"] == 2
        assert data["has_next"] is True

        # Deserialize back
        restored = PaginatedResponse.model_validate(data)
        assert restored.items == resp.items
        assert restored.total == resp.total

    def test_page_must_be_positive(self):
        """page must be >= 1."""
        with pytest.raises(ValidationError):
            PaginatedResponse(
                items=[], total=0, page=0, page_size=10, has_next=False
            )

    def test_total_must_be_non_negative(self):
        """total must be >= 0."""
        with pytest.raises(ValidationError):
            PaginatedResponse(
                items=[], total=-1, page=1, page_size=10, has_next=False
            )

    def test_page_size_must_be_positive(self):
        """page_size must be >= 1."""
        with pytest.raises(ValidationError):
            PaginatedResponse(
                items=[], total=0, page=1, page_size=0, has_next=False
            )

    def test_with_dict_items(self):
        """PaginatedResponse works with dict items (common in API responses)."""
        items = [{"name": "Alice"}, {"name": "Bob"}]
        resp = PaginatedResponse.create(items=items, total=100, page=3, page_size=2)
        assert len(resp.items) == 2
        assert resp.has_next is True

    def test_boundary_exact_page_fill(self):
        """has_next is False when total is exactly page * page_size."""
        resp = PaginatedResponse.create(items=[1, 2], total=4, page=2, page_size=2)
        assert resp.has_next is False


# ---------------------------------------------------------------------------
# ErrorResponse tests
# ---------------------------------------------------------------------------


class TestErrorResponse:
    """Tests for ErrorResponse model."""

    def test_required_fields(self):
        """ErrorResponse requires error_code, message, and request_id."""
        resp = ErrorResponse(
            error_code="VALIDATION_ERROR",
            message="Invalid input",
            request_id="req-123",
        )
        assert resp.error_code == "VALIDATION_ERROR"
        assert resp.message == "Invalid input"
        assert resp.request_id == "req-123"
        assert resp.details is None

    def test_with_details(self):
        """ErrorResponse accepts optional details dict."""
        resp = ErrorResponse(
            error_code="VALIDATION_ERROR",
            message="Bad field",
            details={"field": "name", "reason": "too short"},
            request_id="req-456",
        )
        assert resp.details == {"field": "name", "reason": "too short"}

    def test_serialization(self):
        """ErrorResponse serializes to dict correctly."""
        resp = ErrorResponse(
            error_code="INTERNAL_ERROR",
            message="Something went wrong",
            request_id="req-789",
        )
        data = resp.model_dump()
        assert data["error_code"] == "INTERNAL_ERROR"
        assert data["message"] == "Something went wrong"
        assert data["request_id"] == "req-789"

    def test_missing_required_field_raises(self):
        """ErrorResponse raises ValidationError when required fields are missing."""
        with pytest.raises(ValidationError):
            ErrorResponse(error_code="ERR", message="msg")  # missing request_id

    def test_details_none_excluded(self):
        """details=None can be excluded from serialization."""
        resp = ErrorResponse(
            error_code="ERR",
            message="msg",
            request_id="r1",
        )
        data = resp.model_dump(exclude_none=True)
        assert "details" not in data


# ---------------------------------------------------------------------------
# ListEnvelope tests
# ---------------------------------------------------------------------------


class TestListEnvelope:
    """Tests for ListEnvelope[T] generic model."""

    def test_create_computes_count(self):
        """ListEnvelope.create sets count from items length."""
        env = ListEnvelope.create(items=[1, 2, 3])
        assert env.items == [1, 2, 3]
        assert env.count == 3

    def test_empty_list(self):
        """ListEnvelope works with an empty list."""
        env = ListEnvelope.create(items=[])
        assert env.items == []
        assert env.count == 0

    def test_serialization(self):
        """ListEnvelope serializes correctly."""
        env = ListEnvelope.create(items=["a", "b"])
        data = env.model_dump()
        assert data["items"] == ["a", "b"]
        assert data["count"] == 2

    def test_count_must_be_non_negative(self):
        """count must be >= 0."""
        with pytest.raises(ValidationError):
            ListEnvelope(items=[], count=-1)


# ---------------------------------------------------------------------------
# TenantScopedRequest tests
# ---------------------------------------------------------------------------


class TestTenantScopedRequest:
    """Tests for TenantScopedRequest model."""

    def test_valid_tenant_id(self):
        """TenantScopedRequest accepts a non-empty tenant_id."""
        req = TenantScopedRequest(tenant_id="tenant-abc")
        assert req.tenant_id == "tenant-abc"

    def test_empty_tenant_id_raises(self):
        """TenantScopedRequest rejects empty tenant_id (min_length=1)."""
        with pytest.raises(ValidationError):
            TenantScopedRequest(tenant_id="")

    def test_missing_tenant_id_raises(self):
        """TenantScopedRequest requires tenant_id."""
        with pytest.raises(ValidationError):
            TenantScopedRequest()

    def test_serialization(self):
        """TenantScopedRequest serializes correctly."""
        req = TenantScopedRequest(tenant_id="t1")
        data = req.model_dump()
        assert data["tenant_id"] == "t1"
