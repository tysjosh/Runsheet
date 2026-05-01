"""
Unified request/response schemas for the Runsheet logistics platform.

Provides shared base models for consistent API responses across all
domain routers (ops, fuel, scheduling, agents).

Schemas:
- PaginatedResponse[T]: Generic paginated list response
- ErrorResponse: Structured error response
- ListEnvelope[T]: Simple list wrapper with count
- TenantScopedRequest: Base model for tenant-scoped requests

Validates: Requirements 4.1, 4.2, 4.3
"""

import math
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response envelope.

    Used by all list endpoints that support pagination. The ``has_next``
    field is computed from ``total``, ``page``, and ``page_size``.

    Validates: Requirement 4.2
    """

    items: list[T] = Field(default_factory=list, description="List of result items")
    total: int = Field(..., ge=0, description="Total number of matching items")
    page: int = Field(..., ge=1, description="Current page number (1-indexed)")
    page_size: int = Field(..., ge=1, description="Number of items per page")
    has_next: bool = Field(..., description="Whether there are more pages after the current one")

    @classmethod
    def create(
        cls,
        items: list,
        total: int,
        page: int,
        page_size: int,
    ) -> "PaginatedResponse":
        """Factory method that computes ``has_next`` automatically."""
        total_pages = math.ceil(total / page_size) if page_size > 0 else 0
        return cls(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            has_next=page < total_pages,
        )


class ErrorResponse(BaseModel):
    """Structured error response model.

    All error responses from the API follow this format for consistency
    and to enable programmatic error handling by clients.

    Validates: Requirement 4.3
    """

    error_code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error message")
    details: Optional[dict[str, Any]] = Field(
        default=None, description="Additional error context"
    )
    request_id: str = Field(..., description="Unique request identifier for tracing")


class ListEnvelope(BaseModel, Generic[T]):
    """Simple list wrapper with a count field.

    Used for non-paginated list responses where only the items and
    their count are needed.

    Validates: Requirement 4.1
    """

    items: list[T] = Field(default_factory=list, description="List of result items")
    count: int = Field(..., ge=0, description="Number of items in the list")

    @classmethod
    def create(cls, items: list) -> "ListEnvelope":
        """Factory method that computes ``count`` from the items list."""
        return cls(items=items, count=len(items))


class TenantScopedRequest(BaseModel):
    """Base model for requests that require a tenant context.

    Validates: Requirement 4.1
    """

    tenant_id: str = Field(
        ..., min_length=1, description="Tenant identifier (must not be empty)"
    )


def paginated_response_dict(
    items: list,
    total: int,
    page: int,
    page_size: int,
    *,
    request_id: str = "unknown",
) -> dict:
    """Build a dual-field paginated response dict for the deprecation window.

    Returns a dict containing both the new unified ``PaginatedResponse``
    fields (``items``, ``total``, ``page``, ``page_size``, ``has_next``)
    and the old domain-specific fields (``data``, ``pagination``,
    ``request_id``) so that existing consumers continue to work during
    the 60-day deprecation window.

    Validates: Requirements 4.4, 4.6
    """
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0
    has_next = page < total_pages

    return {
        # --- New unified fields (PaginatedResponse) ---
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": has_next,
        # --- Old fields (deprecated, removed after 60-day window) ---
        "data": items,
        "pagination": {
            "page": page,
            "size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
        "request_id": request_id,
    }
