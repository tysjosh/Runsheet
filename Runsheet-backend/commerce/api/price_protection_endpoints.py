"""Price Protection Contract REST endpoints for the Fuel Compliance Backbone.

Exposes CRUD over sell-side price-protection contracts plus a
portfolio-variance report surface (Task 4.7 of the
fuel-compliance-backbone spec). Mounted under
``/api/commerce/price-protection-contracts``.

Endpoints:

* ``POST   /api/commerce/price-protection-contracts``           — create contract
* ``GET    /api/commerce/price-protection-contracts``           — list / filter
* ``GET    /api/commerce/price-protection-contracts/{id}``      — fetch single
* ``PUT    /api/commerce/price-protection-contracts/{id}``      — update mutable
                                                                   fields (notes,
                                                                   cancellation)
* ``GET    /api/commerce/price-protection-contracts/{id}/variance``
                                                                 — portfolio
                                                                   variance
                                                                   report

Wiring pattern mirrors :mod:`compliance.api.tax_endpoints` (Task 3.9):

1. Module-level ``_es_service`` is populated by
   :func:`configure_price_protection_api` at application startup
   (``bootstrap/compliance.py``).
2. Each handler constructs a per-request, tenant-scoped
   :class:`PriceProtectionService` from the
   :class:`TenantContext` resolved by :func:`get_tenant_context`.
3. ``tenant_id`` on new contracts is stamped from the verified JWT
   claim so clients cannot seed cross-tenant rows (Constraint C3).
4. The PUT handler only accepts a small allowlist of mutable fields
   (``notes`` and a status transition to ``cancelled``) so business
   invariants stay inside :meth:`PriceProtectionService.decrement_gallons`
   and :meth:`PriceProtectionService.check_and_transition_contract`.

The variance endpoint feeds
:meth:`PriceProtectionService.iter_contract_invoice_events` directly
into :meth:`PriceProtectionService.compute_portfolio_variance` so
callers receive the same report shape documented in Task 4.6 without
assembling the delivery list by hand.

Validates: Requirement 3 — full CRUD surface for contracts plus the
variance report (Req 3.1, 3.2, 3.6, 3.7).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from commerce.models.price_protection_contract import (
    ContractType,
    PriceProtectionContract,
)
from commerce.services.price_protection_service import PriceProtectionService
from compliance.services.compliance_es_mappings import (
    PRICE_PROTECTION_CONTRACTS_INDEX,
)
from ops.middleware.tenant_guard import (
    TenantContext,
    get_tenant_context,
    inject_tenant_filter,
)
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_price_protection_api()
# ---------------------------------------------------------------------------

_es_service: Optional[Any] = None

router = APIRouter(
    prefix="/api/commerce/price-protection-contracts",
    tags=["Commerce - Price Protection"],
)


def configure_price_protection_api(*, es_service: Any) -> None:
    """Wire the Elasticsearch service into this module.

    Called once during application startup
    (``bootstrap/compliance.py``) so per-request handlers can
    construct a tenant-scoped :class:`PriceProtectionService` plus
    issue reads/writes against the ``price_protection_contracts``
    index without taking a hard import dependency on the container.

    Args:
        es_service: The application-scoped Elasticsearch service
            (``container.es_service``). Typed as :class:`typing.Any`
            because the handlers only use ``search_documents``,
            ``index_document``, ``get_document``, and
            ``update_document`` — both the live service and the
            in-memory test fakes expose those.
    """
    global _es_service
    _es_service = es_service


def _get_es_service() -> Any:
    """Return the configured Elasticsearch service or raise."""
    if _es_service is None:
        raise RuntimeError(
            "Price Protection API not configured. "
            "Call configure_price_protection_api() during startup."
        )
    return _es_service


def _build_service(tenant_id: str) -> PriceProtectionService:
    """Construct a per-request, tenant-scoped :class:`PriceProtectionService`."""
    return PriceProtectionService(
        es_service=_get_es_service(), tenant_id=tenant_id
    )


def _get_request_id(request: Request) -> str:
    """Extract the RequestIDMiddleware-assigned id, with a safe default."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PriceProtectionContractCreateRequest(BaseModel):
    """Body for ``POST /api/commerce/price-protection-contracts``.

    Mirrors :class:`PriceProtectionContract` but omits auto-generated
    fields (``contract_id``, ``tenant_id``, ``status``, ``version``,
    ``created_at``, ``updated_at``). The router stamps ``tenant_id``
    from the verified JWT claim so clients cannot seed cross-tenant
    contracts.
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(
        ..., description="Customer identifier the contract is written for."
    )
    account_id: str = Field(
        ..., description="Billing account the contract applies to."
    )
    product_code: str = Field(
        ...,
        description=(
            "Canonical product code the contract covers "
            "(exact match — resolution does not canonicalize)."
        ),
    )
    contract_type: ContractType = Field(
        ...,
        description="fixed_price | cap_price | collar (Req 3.1).",
    )
    start_date: date = Field(
        ...,
        description="First date (inclusive) the contract is in force.",
    )
    end_date: date = Field(
        ...,
        description="Last date (inclusive) the contract is in force.",
    )
    contracted_gallons: float = Field(
        ...,
        gt=0,
        description="Total gallons reserved under the contract (>0).",
    )
    remaining_gallons: Optional[float] = Field(
        default=None,
        description=(
            "Remaining gallons available. Defaults to contracted_gallons "
            "when omitted so a new contract starts with its full allotment."
        ),
    )
    price_cap_cents: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Max sell price in integer cents. Required for cap_price / "
            "collar; rejected for fixed_price."
        ),
    )
    price_floor_cents: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Min sell price in integer cents. Required for collar; "
            "rejected for fixed_price / cap_price."
        ),
    )
    fixed_price_cents: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Locked price in integer cents. Required for fixed_price; "
            "rejected for cap_price / collar."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description="Free-form operator notes for contract context.",
    )


class PriceProtectionContractUpdateRequest(BaseModel):
    """Body for ``PUT /api/commerce/price-protection-contracts/{id}``.

    Only a small allowlist of mutable fields is accepted so business
    invariants stay inside :meth:`PriceProtectionService.decrement_gallons`
    (OCC-guarded remaining_gallons writes) and the lifecycle cron
    (Task 4.5 transitions to ``exhausted`` / ``expired``). Operators
    may:

    * edit ``notes`` — free-form context, no invariant,
    * transition ``status`` to ``cancelled`` — operator-initiated
      termination before ``end_date`` (allowed only from ``active``).

    Every other mutation path is explicitly disallowed: remaining
    gallons is writable only via the decrement API, and transitions
    to ``exhausted`` / ``expired`` are the cron's responsibility.
    """

    model_config = ConfigDict(extra="forbid")

    notes: Optional[str] = Field(
        default=None,
        description="Updated free-form notes. Pass empty string to clear.",
    )
    status: Optional[str] = Field(
        default=None,
        description=(
            "Only the transition to 'cancelled' is accepted via this "
            "endpoint. Other status values are rejected with HTTP 422."
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/commerce/price-protection-contracts — create
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_price_protection_contract(
    request: Request,
    body: PriceProtectionContractCreateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Create a new price-protection contract.

    Stamps ``tenant_id`` from the verified JWT context so clients
    cannot seed cross-tenant rows, then lets
    :class:`PriceProtectionContract`'s validators enforce the shape
    (contract_type / pricing-field coherence, positive gallons,
    remaining_gallons within bounds, start/end date ordering —
    Req 3.1, 3.2). The validated row is persisted to the
    ``price_protection_contracts`` index and echoed back to the
    client with server-assigned ``contract_id`` and timestamps.

    Validates: Requirement 3.1, 3.2
    """
    payload = body.model_dump(exclude_none=False)
    payload["tenant_id"] = tenant.tenant_id

    try:
        contract = PriceProtectionContract.model_validate(payload)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "price_protection_contract.invalid_payload",
                "message": str(exc),
            },
        )

    document = contract.model_dump(mode="json")

    es = _get_es_service()
    await es.index_document(
        PRICE_PROTECTION_CONTRACTS_INDEX,
        contract.contract_id,
        document,
    )

    logger.info(
        "price_protection_contracts.create: tenant=%s contract=%s "
        "customer=%s product=%s type=%s",
        tenant.tenant_id,
        contract.contract_id,
        contract.customer_id,
        contract.product_code,
        contract.contract_type,
    )

    return {
        "data": document,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/price-protection-contracts — list
# ---------------------------------------------------------------------------


@router.get("")
async def list_price_protection_contracts(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    customer_id: Optional[str] = Query(
        default=None,
        description="Restrict results to a single customer.",
    ),
    product_code: Optional[str] = Query(
        default=None,
        description="Restrict results to a single product code.",
    ),
    status: Optional[str] = Query(
        default=None,
        description="active | exhausted | expired | cancelled.",
    ),
    active_on_date: Optional[date] = Query(
        default=None,
        description=(
            "When supplied, restricts results to contracts whose "
            "[start_date, end_date] window contains this date. Does "
            "not enforce status — combine with status=active for the "
            "classic 'in-force' query."
        ),
    ),
    size: int = Query(
        default=200,
        ge=1,
        le=1000,
        description="Page size (max 1000).",
    ),
) -> Dict[str, Any]:
    """List price-protection contracts for the tenant.

    Filters compose (``AND``-ed together) and every query is wrapped
    in :func:`inject_tenant_filter` so cross-tenant contracts never
    leak. The ``active_on_date`` filter matches contracts whose
    ``start_date <= value <= end_date``; it does not also filter on
    status so UIs can render expired / exhausted rows that were in
    force on a historical date.

    Validates: Requirement 3.1
    """
    es = _get_es_service()

    filters: List[Dict[str, Any]] = []
    if customer_id is not None:
        filters.append({"term": {"customer_id": customer_id.strip()}})
    if product_code is not None:
        filters.append({"term": {"product_code": product_code.strip()}})
    if status is not None:
        filters.append({"term": {"status": status.strip()}})
    if active_on_date is not None:
        iso = active_on_date.isoformat()
        filters.append({"range": {"start_date": {"lte": iso}}})
        filters.append({"range": {"end_date": {"gte": iso}}})

    base_query: Dict[str, Any] = {
        "query": (
            {"bool": {"filter": filters}} if filters else {"match_all": {}}
        ),
        "size": size,
    }
    query = inject_tenant_filter(base_query, tenant.tenant_id)

    response = await es.search_documents(
        PRICE_PROTECTION_CONTRACTS_INDEX,
        query,
        size=size,
    )

    hits = ((response or {}).get("hits") or {}).get("hits") or []
    items: List[Dict[str, Any]] = []
    for hit in hits:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            continue
        items.append(source)

    return {
        "data": items,
        "count": len(items),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/price-protection-contracts/{id} — fetch single
# ---------------------------------------------------------------------------


async def _fetch_contract_or_404(
    contract_id: str, tenant_id: str
) -> Dict[str, Any]:
    """Return the raw contract document or raise HTTP 404.

    Uses ``search_documents`` with a tenant-scoped ``contract_id``
    filter (rather than a direct ``get_document`` by id) so the
    tenant guard stays in effect even if the id is guessed across
    tenants. A missing row returns HTTP 404; a mismatched tenant id
    is indistinguishable from "not found" so the handler does not
    leak existence across tenants.
    """
    es = _get_es_service()

    base_query: Dict[str, Any] = {
        "query": {
            "bool": {
                "filter": [{"term": {"contract_id": contract_id}}],
            }
        },
        "size": 1,
    }
    query = inject_tenant_filter(base_query, tenant_id)
    response = await es.search_documents(
        PRICE_PROTECTION_CONTRACTS_INDEX, query, size=1
    )

    hits = ((response or {}).get("hits") or {}).get("hits") or []
    for hit in hits:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if isinstance(source, dict):
            return source

    raise HTTPException(
        status_code=404,
        detail={
            "error_code": "price_protection_contract.not_found",
            "message": (
                f"No price-protection contract with id {contract_id!r} "
                "is visible to the requesting tenant."
            ),
        },
    )


@router.get("/{contract_id}")
async def get_price_protection_contract(
    request: Request,
    contract_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Fetch a single price-protection contract by id.

    Returns HTTP 404 when the contract does not exist or belongs to
    a different tenant.

    Validates: Requirement 3.1
    """
    document = await _fetch_contract_or_404(
        contract_id=contract_id, tenant_id=tenant.tenant_id
    )
    return {
        "data": document,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# PUT /api/commerce/price-protection-contracts/{id} — update mutable fields
# ---------------------------------------------------------------------------


# Only this transition is accepted via the REST surface. Transitions
# to ``exhausted`` and ``expired`` are the lifecycle cron's
# responsibility (Task 4.5) and a direct client-initiated jump to
# those terminal states would bypass the invariants the cron
# enforces.
_ALLOWED_STATUS_TRANSITIONS: Dict[str, set] = {
    "active": {"cancelled"},
}


@router.put("/{contract_id}")
async def update_price_protection_contract(
    request: Request,
    contract_id: str,
    body: PriceProtectionContractUpdateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Update the small allowlist of mutable contract fields.

    Accepts:

    * ``notes`` — free-form context. An empty string clears the
      field (the model validator collapses whitespace-only to
      ``None``).
    * ``status`` — only the transition ``active → cancelled`` is
      accepted. Any other value (including valid lifecycle states
      like ``exhausted`` / ``expired`` that the cron owns) returns
      HTTP 422.

    Remaining gallons is deliberately *not* exposed here — that
    invariant is enforced by
    :meth:`PriceProtectionService.decrement_gallons` with a
    compare-and-swap retry loop (Task 4.3). Clients should go
    through the invoice finalization path that owns the decrement.

    Returns HTTP 404 when the contract is not visible to the
    requesting tenant, HTTP 422 when no mutable fields were supplied
    or the status transition is disallowed.

    Validates: Requirement 3.6
    """
    if body.notes is None and body.status is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "price_protection_contract.no_mutable_fields",
                "message": (
                    "At least one of 'notes' or 'status' must be "
                    "provided."
                ),
            },
        )

    # Fetch the current state so we can evaluate the status transition
    # rules and reuse the validated document shape on the merge.
    existing = await _fetch_contract_or_404(
        contract_id=contract_id, tenant_id=tenant.tenant_id
    )

    updates: Dict[str, Any] = {}

    if body.notes is not None:
        # Pydantic already stripped leading/trailing whitespace on the
        # contract model itself — mirror that here so callers that
        # send "   " to clear the field end up with ``None`` rather
        # than a whitespace-only string on disk.
        stripped = body.notes.strip()
        updates["notes"] = stripped or None

    if body.status is not None:
        desired = body.status.strip()
        current = existing.get("status")
        allowed = _ALLOWED_STATUS_TRANSITIONS.get(current, set())
        if desired not in allowed:
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": (
                        "price_protection_contract.invalid_status_transition"
                    ),
                    "message": (
                        f"Transition from {current!r} to {desired!r} is "
                        "not allowed via this endpoint. Only "
                        "'active → cancelled' is accepted; "
                        "'exhausted' / 'expired' are applied by the "
                        "lifecycle cron."
                    ),
                    "current_status": current,
                    "requested_status": desired,
                },
            )
        updates["status"] = desired

    updates["updated_at"] = utcnow().isoformat()

    # Merge and re-validate so the stored document always round-trips
    # through :class:`PriceProtectionContract`'s model validator.
    merged = dict(existing)
    merged.update(updates)
    try:
        validated = PriceProtectionContract.model_validate(merged)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "price_protection_contract.invalid_payload",
                "message": str(exc),
            },
        )

    document = validated.model_dump(mode="json")

    es = _get_es_service()
    # Use index_document (full replacement) rather than
    # update_document so the stored row always matches the
    # validated model exactly. The OCC write path in
    # :meth:`PriceProtectionService.decrement_gallons` covers
    # remaining_gallons / version atomicity; these metadata fields
    # are low-contention.
    await es.index_document(
        PRICE_PROTECTION_CONTRACTS_INDEX,
        validated.contract_id,
        document,
    )

    logger.info(
        "price_protection_contracts.update: tenant=%s contract=%s "
        "fields=%s",
        tenant.tenant_id,
        contract_id,
        sorted(updates.keys()),
    )

    return {
        "data": document,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/price-protection-contracts/{id}/variance — portfolio report
# ---------------------------------------------------------------------------


@router.get("/{contract_id}/variance")
async def get_price_protection_contract_variance(
    request: Request,
    contract_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Compute a portfolio-variance report for a single contract.

    Streams every invoice event tagged with ``contract_id`` through
    :meth:`PriceProtectionService.iter_contract_invoice_events` into
    :meth:`PriceProtectionService.compute_portfolio_variance` and
    returns the aggregated breakdown. Variance is expressed from the
    customer's perspective — positive = customer saved money under
    the contract (Req 3.7).

    The contract itself is fetched first so 404 is returned cleanly
    when the id is unknown to the tenant; the report would otherwise
    silently return zero variance for a missing contract.

    Returns a payload shaped::

        {
            "data": {
                "contract_id": "...",
                "total_variance_cents": <int>,
                "total_gallons": <float>,
                "delivery_count": <int>,
                "breakdown": [...],
                "contract": { ...full contract document... },
            },
            "request_id": "..."
        }

    Validates: Requirement 3.7
    """
    contract_document = await _fetch_contract_or_404(
        contract_id=contract_id, tenant_id=tenant.tenant_id
    )

    service = _build_service(tenant.tenant_id)

    deliveries: List[Dict[str, Any]] = []
    async for event in service.iter_contract_invoice_events(contract_id):
        deliveries.append(event)

    report = await service.compute_portfolio_variance(
        contract_id=contract_id,
        deliveries=deliveries,
    )
    # Enrich the report with the contract so the UI can render the
    # portfolio row without a separate fetch.
    report["contract"] = contract_document

    return {
        "data": report,
        "request_id": _get_request_id(request),
    }


__all__ = [
    "configure_price_protection_api",
    "router",
    "PriceProtectionContractCreateRequest",
    "PriceProtectionContractUpdateRequest",
]
