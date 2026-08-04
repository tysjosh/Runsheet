"""Tax Engine REST endpoints for the Fuel Compliance Backbone.

Exposes the ``TaxEngine`` compute surface plus CRUD for the two ES-backed
rate tables (``tax_jurisdictions``, ``tax_exemptions``) under the
``/api/compliance`` prefix (design §"REST API Endpoints (New)").

Endpoints:

* ``POST /api/compliance/tax/compute`` — invoice-time tax breakdown
  (Req 1.1, 1.2, 1.3, 1.4, 1.7, 1.8, 1.9, 1.10).
* ``GET  /api/compliance/tax-jurisdictions`` — list / filter
  jurisdictional rate rows (Req 1.5).
* ``POST /api/compliance/tax-jurisdictions`` — create a new rate row
  (Req 1.5).
* ``GET  /api/compliance/exemptions`` — list / filter customer
  exemption certificates (Req 1.6, 1.7, 1.8).
* ``POST /api/compliance/exemptions`` — create an exemption
  certificate.

Wiring pattern mirrors ``commerce/api/price_book_endpoints.py`` and
``commerce/api/invoice_endpoints.py``:

1. A module-level ``_es_service`` is populated by
   :func:`configure_tax_api` at application startup (see
   ``bootstrap/compliance.py``).
2. Each handler constructs a ``TaxEngine`` on demand using the
   requesting tenant from :func:`get_tenant_context`, so the engine
   stays tenant-scoped for every request (design §"Bootstrap Wiring",
   Constraint C3).
3. ``TaxJurisdictionNotFoundError`` (error code
   ``tax.jurisdiction_not_found``) is translated to HTTP 400 with the
   structured error envelope expected by the commerce / fuel-ops
   endpoints (``{"error_code": ..., "message": ...}``) — Req 1.9.

Validates: Requirements 1.1, 1.5
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from compliance.api._authz import compliance_ops_dependency
from compliance.models.jurisdiction_rate import JurisdictionRate
from compliance.models.tax_exemption import TaxExemption
from compliance.services.compliance_es_mappings import (
    TAX_EXEMPTIONS_INDEX,
    TAX_JURISDICTIONS_INDEX,
)
from compliance.services.tax_engine import (
    ERROR_CODE_JURISDICTION_NOT_FOUND,
    TaxBreakdown,
    TaxEngine,
    TaxJurisdictionNotFoundError,
)
from ops.middleware.tenant_guard import (
    TenantContext,
    get_tenant_context,
    inject_tenant_filter,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_tax_api()
# ---------------------------------------------------------------------------

_es_service: Optional[Any] = None

# DOT / IRS records, gated to the operations roles. Attached to the router
# rather than to each handler so a route added later inherits it: every module
# in this package previously had no role check at all.
router = APIRouter(
    prefix="/api/compliance", tags=["Compliance"],
    dependencies=[Depends(compliance_ops_dependency)],
)


def configure_tax_api(*, es_service: Any) -> None:
    """Wire the Elasticsearch service into this module.

    Called once during application startup (``bootstrap/compliance.py``)
    so that per-request handlers can construct a tenant-scoped
    :class:`TaxEngine` plus issue reads/writes against the
    ``tax_jurisdictions`` and ``tax_exemptions`` indices without taking
    a hard import dependency on the container.

    Args:
        es_service: The application-scoped Elasticsearch service
            (``container.es_service``). Typed as :class:`typing.Any`
            because the module only uses the ``search_documents`` and
            ``index_document`` surface, which both the live service
            and the in-memory test fakes expose.
    """

    global _es_service
    _es_service = es_service


def _get_es_service() -> Any:
    """Return the configured Elasticsearch service or raise."""

    if _es_service is None:
        raise RuntimeError(
            "Tax API not configured. Call configure_tax_api() during startup."
        )
    return _es_service


def _build_tax_engine(tenant_id: str) -> TaxEngine:
    """Construct a per-request, tenant-scoped :class:`TaxEngine`.

    The engine is cheap to build (two attribute assignments) and binds
    to a single tenant id for the duration of the request so
    :func:`inject_tenant_filter` is applied consistently across the
    jurisdiction / exemption lookups.
    """

    return TaxEngine(es_service=_get_es_service(), tenant_id=tenant_id)


def _get_request_id(request: Request) -> str:
    """Extract the RequestIDMiddleware-assigned id, with a safe default."""

    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class TaxComputeRequest(BaseModel):
    """Body for ``POST /api/compliance/tax/compute`` (Req 1.1)."""

    model_config = ConfigDict(extra="forbid")

    product_code: str = Field(
        ...,
        description=(
            "Canonical or alias fuel product code (e.g. DIESEL_2, "
            "GASOLINE_REG). Aliases are resolved by the TaxEngine."
        ),
    )
    net_gallons: float = Field(
        ...,
        ge=0,
        description=(
            "Temperature-corrected net gallons at 60°F (Req 2.3). "
            "Must be non-negative."
        ),
    )
    destination_fips: str = Field(
        ...,
        description=(
            "FIPS code of the delivery destination: 2 digits (state), "
            "5 digits (state+county), or 7 digits (state+county+city)."
        ),
    )
    customer_id: str = Field(
        ...,
        description="Customer identifier the invoice is being computed for.",
    )
    effective_date: Optional[date] = Field(
        default=None,
        description=(
            "Invoice / delivery date used to select rate rows and "
            "honor exemption windows. Defaults to today when omitted."
        ),
    )


class JurisdictionRateCreateRequest(BaseModel):
    """Body for ``POST /api/compliance/tax-jurisdictions`` (Req 1.5).

    Mirrors :class:`JurisdictionRate` but omits auto-generated fields
    (``jurisdiction_id``, ``tenant_id``, ``created_at``, ``updated_at``)
    — the router stamps those from context so clients cannot spoof
    them.
    """

    model_config = ConfigDict(extra="forbid")

    fips_code: str = Field(..., description="FIPS code (2/5/7 digits).")
    jurisdiction_level: str = Field(
        ...,
        description="federal | state | county | city.",
    )
    jurisdiction_name: Optional[str] = Field(
        default=None,
        description="Optional human-readable jurisdiction name.",
    )
    tax_type: str = Field(
        ...,
        description="excise | ust | spcc | environmental.",
    )
    product_codes: List[str] = Field(
        ...,
        description="Non-empty list of product codes this rate applies to.",
    )
    rate_cents_per_gallon: int = Field(
        ...,
        ge=0,
        description=(
            "Rate in RATE_SCALE units (tenths of a cent per gallon — "
            "see compliance/services/tax_engine.py::RATE_SCALE)."
        ),
    )
    effective_date: date = Field(
        ...,
        description="First date (inclusive) the rate applies.",
    )
    expiry_date: Optional[date] = Field(
        default=None,
        description="Last date (inclusive) the rate applies; None is open-ended.",
    )
    source: Optional[str] = Field(
        default=None,
        description="Optional provenance label (e.g. 'irs_form_720').",
    )


class TaxExemptionCreateRequest(BaseModel):
    """Body for ``POST /api/compliance/exemptions``.

    Mirrors :class:`TaxExemption` but omits auto-generated fields
    (``exemption_id``, ``tenant_id``, ``created_at``, ``updated_at``).
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(..., description="Customer holding the exemption.")
    account_id: Optional[str] = Field(
        default=None,
        description="Optional billing-account scope.",
    )
    exemption_type: str = Field(
        ...,
        description=(
            "dyed_diesel | off_road | farm | 637M | government | resale."
        ),
    )
    certificate_number: str = Field(
        ...,
        description="Certificate number as issued by the authority.",
    )
    letter_suffix: Optional[str] = Field(
        default=None,
        description="IRS 637 letter suffix (e.g. 'M').",
    )
    issuing_authority: Optional[str] = Field(
        default=None,
        description="Authority that issued the certificate.",
    )
    product_codes: Optional[List[str]] = Field(
        default=None,
        description="Scoped product codes; None means all products.",
    )
    jurisdiction_fips: Optional[str] = Field(
        default=None,
        description="Optional FIPS scope for the exemption.",
    )
    issued_date: Optional[date] = Field(
        default=None,
        description="Date the certificate was issued.",
    )
    expiry_date: date = Field(
        ...,
        description="Last date on which the certificate is honored.",
    )
    status: Optional[str] = Field(
        default="valid",
        description="valid | expired | revoked.",
    )
    document_ref: Optional[str] = Field(
        default=None,
        description="Optional reference to the scanned certificate document.",
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _jurisdiction_not_found_to_http(
    exc: TaxJurisdictionNotFoundError,
) -> HTTPException:
    """Translate :class:`TaxJurisdictionNotFoundError` to HTTP 400.

    Req 1.9 requires the caller to receive a structured rejection when
    a jurisdiction row is missing. We return HTTP 400 with a body of
    the shape::

        {
            "error_code": "tax.jurisdiction_not_found",
            "message": "...",
            "fips_code": "06",
            "jurisdiction_level": "state",
            "tax_type": "excise",
            "product_code": "DIESEL_2",
            "effective_date": "2026-01-15",
        }

    so UI / invoice-generation callers can surface the specific
    missing row to operators.
    """

    return HTTPException(
        status_code=400,
        detail={
            "error_code": exc.error_code,
            "message": str(exc),
            "fips_code": exc.fips_code,
            "jurisdiction_level": exc.jurisdiction_level,
            "tax_type": exc.tax_type,
            "product_code": exc.product_code,
            "effective_date": (
                exc.effective_date.isoformat()
                if exc.effective_date is not None
                else None
            ),
        },
    )


# ---------------------------------------------------------------------------
# POST /api/compliance/tax/compute
# ---------------------------------------------------------------------------


@router.post("/tax/compute")
async def compute_tax(
    request: Request,
    body: TaxComputeRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Compute an invoice-time :class:`TaxBreakdown`.

    Delegates to :meth:`TaxEngine.compute_tax`, which orchestrates
    jurisdiction lookup, federal excise computation, state/local fees,
    and exemption application. Returns HTTP 400 with
    ``error_code == "tax.jurisdiction_not_found"`` when a required
    state excise row is missing and no exemption zeros that component
    (Req 1.9).

    Validates: Requirement 1.1
    """

    engine = _build_tax_engine(tenant.tenant_id)

    try:
        breakdown: TaxBreakdown = await engine.compute_tax(
            product_code=body.product_code,
            net_gallons=body.net_gallons,
            destination_fips=body.destination_fips,
            customer_id=body.customer_id,
            effective_date=body.effective_date,
        )
    except TaxJurisdictionNotFoundError as exc:
        logger.info(
            "tax.compute: missing jurisdiction for tenant=%s product=%s "
            "destination_fips=%s: %s",
            tenant.tenant_id,
            body.product_code,
            body.destination_fips,
            exc,
        )
        raise _jurisdiction_not_found_to_http(exc)
    except ValueError as exc:
        # Input-validation failures (negative gallons, unknown product
        # without a statutory rate, malformed FIPS, etc.) are surfaced
        # as HTTP 422 so the client can distinguish them from the
        # missing-row case above.
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "tax.compute_invalid_input",
                "message": str(exc),
            },
        )

    return {
        "data": breakdown.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/tax-jurisdictions
# ---------------------------------------------------------------------------


@router.get("/tax-jurisdictions")
async def list_tax_jurisdictions(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    fips_code: Optional[str] = Query(
        default=None,
        description=(
            "Exact FIPS code filter (2/5/7 digits). When omitted, all "
            "jurisdictional rows for the tenant are returned."
        ),
    ),
    tax_type: Optional[str] = Query(
        default=None,
        description="excise | ust | spcc | environmental.",
    ),
    effective_date: Optional[date] = Query(
        default=None,
        description=(
            "When supplied, restricts results to rows active on this "
            "date (effective_date <= value AND "
            "(expiry_date >= value OR expiry_date missing))."
        ),
    ),
    size: int = Query(
        default=200,
        ge=1,
        le=1000,
        description="Page size (max 1000).",
    ),
) -> Dict[str, Any]:
    """List jurisdictional rate rows for the tenant.

    Filters compose (``AND``-ed together) and all queries are wrapped
    in :func:`inject_tenant_filter` so cross-tenant rows never leak.

    Validates: Requirement 1.5
    """

    es = _get_es_service()

    # Read-cutover: serve from Postgres when enabled. The fips_code / tax_type
    # term filters + effective_date<=iso map directly; the "expiry >= iso OR
    # missing" open-ended-row rule is applied in Python (awkward in portable
    # SQL), mirroring tax_engine.get_jurisdiction_rates. Byte-identical result
    # set to the ES query.
    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER,
        read_hybrid_fetch_for_aggregation,
    )

    _iso = effective_date.isoformat() if effective_date is not None else None
    _term_filters: Dict[str, Any] = {}
    if fips_code is not None:
        _term_filters["fips_code"] = fips_code.strip()
    if tax_type is not None:
        _term_filters["tax_type"] = tax_type.strip()

    pg_docs = await read_hybrid_fetch_for_aggregation(
        "tax_jurisdiction", tenant.tenant_id,
        term_filters=_term_filters or None,
        range_field="effective_date" if _iso is not None else None,
        range_lte=_iso,
    )
    if pg_docs is not _NOT_CUT_OVER:
        items = []
        for source in pg_docs:
            if _iso is not None:
                expiry = source.get("expiry_date")
                if expiry is not None and str(expiry) < _iso:
                    continue  # expired before the requested date
            items.append(source)
            if len(items) >= size:
                break
        return {
            "data": items,
            "count": len(items),
            "request_id": _get_request_id(request),
        }

    filters: List[Dict[str, Any]] = []
    if fips_code is not None:
        filters.append({"term": {"fips_code": fips_code.strip()}})
    if tax_type is not None:
        filters.append({"term": {"tax_type": tax_type.strip()}})
    if effective_date is not None:
        iso = effective_date.isoformat()
        filters.append({"range": {"effective_date": {"lte": iso}}})
        filters.append(
            {
                "bool": {
                    "should": [
                        {"range": {"expiry_date": {"gte": iso}}},
                        {
                            "bool": {
                                "must_not": [
                                    {"exists": {"field": "expiry_date"}}
                                ]
                            }
                        },
                    ],
                    "minimum_should_match": 1,
                }
            }
        )

    base_query: Dict[str, Any] = {
        "query": (
            {"bool": {"filter": filters}} if filters else {"match_all": {}}
        ),
        "size": size,
    }
    query = inject_tenant_filter(base_query, tenant.tenant_id)

    response = await es.search_documents(
        TAX_JURISDICTIONS_INDEX,
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
# POST /api/compliance/tax-jurisdictions
# ---------------------------------------------------------------------------


@router.post("/tax-jurisdictions", status_code=201)
async def create_tax_jurisdiction(
    request: Request,
    body: JurisdictionRateCreateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Create a new jurisdictional rate row.

    The router stamps ``tenant_id`` from the verified JWT context so
    clients cannot seed cross-tenant rows, and then lets
    :class:`JurisdictionRate`'s validators enforce the shape
    (FIPS length × level, non-empty product_codes, non-negative rate,
    effective window coherence).

    The created row is persisted to the ``tax_jurisdictions`` index
    (indexed to ES) and returned to the client with server-assigned
    ``jurisdiction_id`` and timestamps.

    Validates: Requirement 1.5
    """

    payload = body.model_dump(exclude_none=False)
    payload["tenant_id"] = tenant.tenant_id

    try:
        rate = JurisdictionRate.model_validate(payload)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "tax_jurisdiction.invalid_payload",
                "message": str(exc),
            },
        )

    document = rate.model_dump(mode="json")

    es = _get_es_service()
    await es.index_document(
        TAX_JURISDICTIONS_INDEX,
        rate.jurisdiction_id,
        document,
    )

    # Dual-write the tax jurisdiction config to the Postgres source-of-truth.
    from commerce.services.commerce_persistence_bridge import (
        mirror_compliance_config_upsert,
    )
    await mirror_compliance_config_upsert("tax_jurisdiction", document)

    logger.info(
        "tax_jurisdictions.create: tenant=%s jurisdiction=%s fips=%s "
        "level=%s tax_type=%s",
        tenant.tenant_id,
        rate.jurisdiction_id,
        rate.fips_code,
        rate.jurisdiction_level,
        rate.tax_type,
    )

    return {
        "data": document,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/exemptions
# ---------------------------------------------------------------------------


@router.get("/exemptions")
async def list_exemptions(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    customer_id: Optional[str] = Query(
        default=None,
        description="Restrict results to a single customer.",
    ),
    product_code: Optional[str] = Query(
        default=None,
        description=(
            "Match certificates whose ``product_codes`` list contains "
            "this code. Blanket exemptions (no product_codes set) are "
            "also returned so callers can see every applicable row."
        ),
    ),
    effective_date: Optional[date] = Query(
        default=None,
        description=(
            "When supplied, restricts results to certificates not "
            "expired as of this date and with status == 'valid'."
        ),
    ),
    size: int = Query(
        default=200,
        ge=1,
        le=1000,
        description="Page size (max 1000).",
    ),
) -> Dict[str, Any]:
    """List customer exemption certificates for the tenant.

    Validates: Requirements 1.6, 1.7, 1.8
    """

    es = _get_es_service()

    # Read-cutover: serve from Postgres when enabled. customer_id + (when
    # effective_date supplied) status=valid map to term filters, and
    # expiry_date>=iso maps to a range; the "product_codes contains X OR the
    # field is missing (blanket)" rule is applied in Python (list-membership +
    # missing-field OR is awkward in portable SQL). Byte-identical result set.
    from commerce.services.commerce_persistence_bridge import (
        _NOT_CUT_OVER,
        read_hybrid_fetch_for_aggregation,
    )

    _iso = effective_date.isoformat() if effective_date is not None else None
    _term_filters: Dict[str, Any] = {}
    if customer_id is not None:
        _term_filters["customer_id"] = customer_id.strip()
    if effective_date is not None:
        _term_filters["status"] = "valid"
    _pc = product_code.strip() if product_code is not None else None

    pg_docs = await read_hybrid_fetch_for_aggregation(
        "tax_exemption", tenant.tenant_id,
        term_filters=_term_filters or None,
        range_field="expiry_date" if _iso is not None else None,
        range_gte=_iso,
    )
    if pg_docs is not _NOT_CUT_OVER:
        items = []
        for source in pg_docs:
            if _pc is not None:
                codes = source.get("product_codes")
                # Blanket exemption (no/empty product_codes) applies to all;
                # otherwise the list must contain the requested code.
                if codes and _pc not in codes:
                    continue
            items.append(source)
            if len(items) >= size:
                break
        return {
            "data": items,
            "count": len(items),
            "request_id": _get_request_id(request),
        }

    filters: List[Dict[str, Any]] = []
    if customer_id is not None:
        filters.append({"term": {"customer_id": customer_id.strip()}})
    if product_code is not None:
        # A blanket exemption (no product_codes field) applies to all
        # products, so we OR the explicit-match against the
        # field-missing case.
        filters.append(
            {
                "bool": {
                    "should": [
                        {"term": {"product_codes": product_code.strip()}},
                        {
                            "bool": {
                                "must_not": [
                                    {"exists": {"field": "product_codes"}}
                                ]
                            }
                        },
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    if effective_date is not None:
        iso = effective_date.isoformat()
        filters.append({"term": {"status": "valid"}})
        filters.append({"range": {"expiry_date": {"gte": iso}}})

    base_query: Dict[str, Any] = {
        "query": (
            {"bool": {"filter": filters}} if filters else {"match_all": {}}
        ),
        "size": size,
    }
    query = inject_tenant_filter(base_query, tenant.tenant_id)

    response = await es.search_documents(
        TAX_EXEMPTIONS_INDEX,
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
# POST /api/compliance/exemptions
# ---------------------------------------------------------------------------


@router.post("/exemptions", status_code=201)
async def create_exemption(
    request: Request,
    body: TaxExemptionCreateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Create a new customer exemption certificate.

    The router stamps ``tenant_id`` from the verified JWT context so
    clients cannot seed cross-tenant certificates, and then relies on
    :class:`TaxExemption`'s validators to enforce the shape
    (non-empty certificate number, expiry window coherence, FIPS
    length, etc.). The created row is persisted to the
    ``tax_exemptions`` index.

    Validates: Requirements 1.6, 1.7, 1.8
    """

    payload = body.model_dump(exclude_none=False)
    payload["tenant_id"] = tenant.tenant_id
    # Pydantic strips None for optional fields when the request uses
    # the default; reconstruct the dict so the model validator sees
    # exactly what the client sent (letting status default to "valid"
    # when omitted).
    if payload.get("status") is None:
        payload["status"] = "valid"

    try:
        exemption = TaxExemption.model_validate(payload)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "tax_exemption.invalid_payload",
                "message": str(exc),
            },
        )

    document = exemption.model_dump(mode="json")

    es = _get_es_service()
    await es.index_document(
        TAX_EXEMPTIONS_INDEX,
        exemption.exemption_id,
        document,
    )

    # Dual-write the tax exemption config to the Postgres source-of-truth.
    from commerce.services.commerce_persistence_bridge import (
        mirror_compliance_config_upsert,
    )
    await mirror_compliance_config_upsert("tax_exemption", document)

    logger.info(
        "tax_exemptions.create: tenant=%s exemption=%s customer=%s type=%s",
        tenant.tenant_id,
        exemption.exemption_id,
        exemption.customer_id,
        exemption.exemption_type,
    )

    return {
        "data": document,
        "request_id": _get_request_id(request),
    }


__all__ = [
    "configure_tax_api",
    "router",
    "TaxComputeRequest",
    "JurisdictionRateCreateRequest",
    "TaxExemptionCreateRequest",
]
