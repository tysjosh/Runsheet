"""CRUD + projections for Customer.

Implements the CustomerService with create, get, list, update, archive,
and get_with_projections methods. Every method takes tenant_id as its
first parameter and every ES query passes through inject_tenant_filter.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.6, C3
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

from commerce.models.customer import Customer, CustomerStatus
from commerce.services.commerce_es_mappings import (
    ACCOUNTS_CURRENT_INDEX,
    CUSTOMERS_CURRENT_INDEX,
    INVOICES_CURRENT_INDEX,
)
from errors.exceptions import conflict, resource_not_found, validation_error
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TAX_ID_PATTERN = re.compile(r"^[A-Z0-9-]{1,64}$")
_DEFAULT_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 200


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CustomerService:
    """Service layer for Customer CRUD and aggregate projections.

    Every public method takes ``tenant_id`` as its first positional argument
    and every Elasticsearch query is wrapped with ``inject_tenant_filter``
    to enforce strict tenant isolation (Constraint C3).
    """

    def __init__(self, es_service: ElasticsearchService) -> None:
        self._es = es_service

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_display_name(display_name: str) -> str:
        """Validate and normalize display_name.

        Rejects empty/whitespace-only or >255 chars (Req 1.2).
        """
        if not display_name or not display_name.strip():
            raise validation_error(
                "display_name must not be empty or whitespace-only",
                details={"display_name": display_name},
            )
        stripped = display_name.strip()
        if len(stripped) > 255:
            raise validation_error(
                "display_name must not exceed 255 characters",
                details={"display_name_length": len(stripped)},
            )
        return stripped

    @staticmethod
    def _validate_tax_id(tax_id: Optional[str]) -> Optional[str]:
        """Validate tax_id against ^[A-Z0-9-]{1,64}$ when present (Req 1.2)."""
        if tax_id is None:
            return None
        if not _TAX_ID_PATTERN.match(tax_id):
            raise validation_error(
                "tax_id must match ^[A-Z0-9-]{1,64}$",
                details={"tax_id": tax_id},
            )
        return tax_id

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        tenant_id: str,
        *,
        display_name: str,
        legal_name: Optional[str] = None,
        primary_email: Optional[str] = None,
        tax_id: Optional[str] = None,
        status: str = "active",
        external_refs: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a new Customer record.

        Assigns a server-generated ``customer_id`` of shape ``cust_<uuid4>``,
        stamps ``created_at`` via ``utcnow()``, and persists to
        ``customers_current``.

        Validates: Requirements 1.1, 1.2, C3
        """
        # Validate inputs
        display_name = self._validate_display_name(display_name)
        self._validate_tax_id(tax_id)

        now = utcnow()
        customer_id = f"cust_{uuid4()}"

        doc: Dict[str, Any] = {
            "customer_id": customer_id,
            "tenant_id": tenant_id,
            "display_name": display_name,
            "legal_name": legal_name,
            "primary_email": primary_email,
            "tax_id": tax_id,
            "status": status,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "external_refs": external_refs or {},
            "metadata": metadata or {},
        }

        await self._es.index_document(CUSTOMERS_CURRENT_INDEX, customer_id, doc)

        logger.info(
            "Created customer %s for tenant %s",
            customer_id,
            tenant_id,
        )
        return doc

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    async def get(self, tenant_id: str, customer_id: str) -> Dict[str, Any]:
        """Retrieve a single Customer by ID, scoped to tenant.

        Validates: Requirement C3
        """
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"customer_id": customer_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            CUSTOMERS_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Customer '{customer_id}' not found",
                details={"customer_id": customer_id},
            )

        return hits[0]["_source"]

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list(
        self,
        tenant_id: str,
        *,
        cursor: Optional[str] = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List Customers for a tenant with cursor/limit pagination.

        Default limit is 50, max 200 (Req 1.3). Cursor is the
        ``customer_id`` of the last item on the previous page (keyset
        pagination via ``search_after``).

        Validates: Requirements 1.3, C3
        """
        # Clamp limit
        if limit < 1:
            limit = _DEFAULT_PAGE_LIMIT
        if limit > _MAX_PAGE_LIMIT:
            limit = _MAX_PAGE_LIMIT

        must_clauses: List[Dict[str, Any]] = []
        if status:
            must_clauses.append({"term": {"status": status}})

        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": must_clauses if must_clauses else [{"match_all": {}}],
                }
            },
            "size": limit,
            "sort": [
                {"created_at": {"order": "desc"}},
                {"customer_id": {"order": "asc"}},
            ],
        }

        # Cursor-based pagination using search_after
        if cursor:
            base_query["search_after"] = [cursor, cursor]

        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            CUSTOMERS_CURRENT_INDEX, query, size=limit
        )

        hits = response["hits"]["hits"]
        items = [hit["_source"] for hit in hits]

        # Determine next cursor
        next_cursor: Optional[str] = None
        if hits and len(hits) == limit:
            last_sort = hits[-1].get("sort")
            if last_sort and len(last_sort) >= 2:
                next_cursor = hits[-1]["_source"]["customer_id"]

        return {
            "items": items,
            "next_cursor": next_cursor,
            "limit": limit,
        }

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(
        self,
        tenant_id: str,
        customer_id: str,
        *,
        display_name: Optional[str] = None,
        legal_name: Optional[str] = None,
        primary_email: Optional[str] = None,
        tax_id: Optional[str] = ...,  # type: ignore[assignment]
        status: Optional[str] = None,
        external_refs: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Update an existing Customer record.

        Only non-None fields are applied. Validates display_name and tax_id
        when provided (Req 1.2).

        Validates: Requirements 1.2, C3
        """
        # Ensure the customer exists and belongs to this tenant
        existing = await self.get(tenant_id, customer_id)
        doc_id = existing["customer_id"]

        partial: Dict[str, Any] = {}

        if display_name is not None:
            partial["display_name"] = self._validate_display_name(display_name)

        if legal_name is not None:
            partial["legal_name"] = legal_name

        if primary_email is not None:
            partial["primary_email"] = primary_email

        # tax_id uses sentinel to distinguish "not provided" from "set to None"
        if tax_id is not ...:
            self._validate_tax_id(tax_id)
            partial["tax_id"] = tax_id

        if status is not None:
            partial["status"] = status

        if external_refs is not None:
            partial["external_refs"] = external_refs

        if metadata is not None:
            partial["metadata"] = metadata

        if not partial:
            return existing

        partial["updated_at"] = utcnow().isoformat()

        await self._es.update_document(CUSTOMERS_CURRENT_INDEX, doc_id, partial)

        merged = {**existing, **partial}
        logger.info("Updated customer %s for tenant %s", customer_id, tenant_id)
        return merged

    # ------------------------------------------------------------------
    # Archive
    # ------------------------------------------------------------------

    async def archive(self, tenant_id: str, customer_id: str) -> Dict[str, Any]:
        """Soft-delete a Customer by setting status to archived.

        Rejects with HTTP 409 if the customer has any open invoices,
        including the list of blocking invoice_ids in the error payload
        (Req 1.6).

        Validates: Requirements 1.6, C3
        """
        # Ensure the customer exists
        existing = await self.get(tenant_id, customer_id)

        # Check for open invoices (status in open, partial, overdue, draft)
        open_statuses = ["open", "partial", "overdue", "draft"]
        invoice_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"customer_id": customer_id}},
                        {"terms": {"status": open_statuses}},
                    ]
                }
            },
            "size": 100,
            "_source": ["invoice_id"],
        }
        invoice_query = inject_tenant_filter(invoice_query, tenant_id)

        invoice_response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, invoice_query, size=100
        )

        invoice_hits = invoice_response["hits"]["hits"]
        if invoice_hits:
            blocking_ids = [
                hit["_source"]["invoice_id"] for hit in invoice_hits
            ]
            raise conflict(
                "Cannot archive customer with open invoices",
                error_code="INVALID_STATUS_TRANSITION",
                details={
                    "customer_id": customer_id,
                    "blocking_invoice_ids": blocking_ids,
                },
            )

        # Perform the archive
        partial = {
            "status": CustomerStatus.ARCHIVED.value,
            "updated_at": utcnow().isoformat(),
        }
        await self._es.update_document(
            CUSTOMERS_CURRENT_INDEX, customer_id, partial
        )

        merged = {**existing, **partial}
        logger.info("Archived customer %s for tenant %s", customer_id, tenant_id)
        return merged

    # ------------------------------------------------------------------
    # Get with projections
    # ------------------------------------------------------------------

    async def get_with_projections(
        self, tenant_id: str, customer_id: str
    ) -> Dict[str, Any]:
        """Retrieve a Customer with aggregate projections.

        Computes:
        - open_invoice_count: number of non-void, non-paid invoices
        - open_balance_cents: sum of remaining_cents on open invoices
        - lifetime_revenue_cents: sum of amount_paid_cents across all invoices
        - account_count: number of accounts linked to this customer

        All aggregations are tenant-scoped via inject_tenant_filter (Req 1.4, C3).

        Validates: Requirements 1.4, C3
        """
        # Get the base customer record
        customer = await self.get(tenant_id, customer_id)

        # --- Account count ---
        account_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"customer_id": customer_id}},
                    ]
                }
            },
            "size": 0,
            "track_total_hits": True,
        }
        account_query = inject_tenant_filter(account_query, tenant_id)

        account_response = await self._es.search_documents(
            ACCOUNTS_CURRENT_INDEX, account_query, size=0
        )
        account_count = account_response["hits"]["total"]["value"]

        # --- Invoice aggregations ---
        # We need: open_invoice_count, open_balance_cents, lifetime_revenue_cents
        invoice_agg_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"customer_id": customer_id}},
                    ]
                }
            },
            "size": 0,
            "aggs": {
                "open_invoices": {
                    "filter": {
                        "terms": {"status": ["open", "partial", "overdue", "draft"]}
                    },
                    "aggs": {
                        "count": {"value_count": {"field": "invoice_id"}},
                        "total_remaining": {"sum": {"field": "remaining_cents"}},
                    },
                },
                "lifetime_revenue": {
                    "sum": {"field": "amount_paid_cents"},
                },
            },
        }
        invoice_agg_query = inject_tenant_filter(invoice_agg_query, tenant_id)

        invoice_response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, invoice_agg_query, size=0
        )

        aggs = invoice_response.get("aggregations", {})
        open_invoices_agg = aggs.get("open_invoices", {})
        open_invoice_count = open_invoices_agg.get("count", {}).get("value", 0)
        open_balance_cents = int(
            open_invoices_agg.get("total_remaining", {}).get("value", 0)
        )
        lifetime_revenue_cents = int(
            aggs.get("lifetime_revenue", {}).get("value", 0)
        )

        # Assemble the response with projections
        customer["open_invoice_count"] = open_invoice_count
        customer["open_balance_cents"] = open_balance_cents
        customer["lifetime_revenue_cents"] = lifetime_revenue_cents
        customer["account_count"] = account_count

        return customer
