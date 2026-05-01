"""
Shared schemas package for the Runsheet logistics platform.

Provides unified request/response models used consistently across
all domain routers (ops, fuel, scheduling, agents).

Validates: Requirement 4.1
"""

from schemas.common import (
    ErrorResponse,
    ListEnvelope,
    PaginatedResponse,
    TenantScopedRequest,
    paginated_response_dict,
)

__all__ = [
    "PaginatedResponse",
    "ErrorResponse",
    "ListEnvelope",
    "TenantScopedRequest",
    "paginated_response_dict",
]
