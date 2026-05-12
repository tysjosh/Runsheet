"""
Customer Tank domain model and tenant-scoped repository.

Capability 1 (Requirement 1.1) of the fuel-ops hardening spec introduces a
US-market ``Customer_Tank`` entity so the forecaster can run per-residential,
per-commercial, per-keep-full customer instead of only per-retail-station.
This module is the source of truth for the :class:`CustomerTank` Pydantic
model and the :class:`CustomerTankRepository` that reads/writes it against
the ``customer_tanks`` Elasticsearch index (mapping defined in
:mod:`Agents.support.fuel_ops_es_mappings`).

Key responsibilities:

* Expose :class:`CustomerTank`, a strict Pydantic model whose fields mirror
  the ES mapping 1:1 (Requirement 1.1.1). Coordinates are bounded to valid
  WGS84 ranges, volumes are non-negative, and the ``customer_type`` /
  ``fuel_type`` / ``status`` / ``use_case`` enumerations are enforced at
  construction time.
* Canonicalize fuel-product inputs through
  :func:`services.fuel_product_catalog.canonicalize` on every write so
  legacy aliases (AGO → DIESEL_2, LPG → PROPANE, etc.) are normalized to
  the canonical US ``product_code`` before persistence (Requirement 6.1.4
  applied to the new ``fuel_product_code`` column).
* Expose :class:`CustomerTankRepository` with async ``create``, ``get``,
  ``list_for_tenant``, ``update``, and ``delete`` methods. Every method is
  tenant-scoped: cross-tenant ``get`` returns ``None``, cross-tenant
  ``update`` / ``delete`` either raise :class:`CrossTenantAccessError` or
  return ``None`` / ``False`` respectively so callers can translate
  cleanly into HTTP 404s (Requirement 1.1.6).

Tenant isolation is enforced at two points for defense-in-depth:
    1. Every ES query includes a ``term`` clause on ``tenant_id``.
    2. Every returned document is re-validated against the caller's
       ``tenant_id`` before it crosses the repository boundary, so a
       mis-labelled document never leaks across tenants.

Validates: Requirements 1.1.1, 1.1.6.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fuel.services.fuel_ops_es_mappings import CUSTOMER_TANKS_INDEX
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


#: Customer-segment enum used by the forecaster to pick a consumption model
#: multiplier. Matches Requirement 1.1.1.
CustomerType = Literal[
    "residential",
    "commercial",
    "keep_full",
    "will_call",
    "auto_fill",
]


#: Narrow fuel-family enum used by the forecaster to pick a Consumption_Model
#: strategy. Matches Requirement 1.1.1. Distinct from the catalog
#: ``product_code`` (which lives in ``fuel_product_code``) so downstream
#: model-selection code can switch on the family without re-implementing the
#: catalog taxonomy.
FuelType = Literal[
    "propane",
    "heating_oil",
    "diesel",
    "generator_fuel",
    "farm_fuel",
    "gasoline",
]


#: Source-of-truth status for a customer tank.
CustomerTankStatus = Literal["active", "inactive", "maintenance"]


#: Optional high-level use-case flag. The ``generator`` value is first used
#: by Phase 10 (Storm Mode) to boost priority for generator-fuel customers.
UseCase = Literal[
    "residential_heat",
    "commercial_heat",
    "generator",
    "farm",
    "other",
]


class CustomerTank(BaseModel):
    """A physical fuel tank owned by or serving an end customer.

    Field shapes, types, and value ranges mirror the ``customer_tanks`` ES
    mapping (Task 1.1) so a ``model_dump()`` payload can be indexed directly
    without transformation.

    Volumes are stored in **US gallons** (not liters) — that is the canonical
    unit for all US-market entities introduced by the fuel-ops hardening
    spec. Legacy NG data remains in the ``fuel_stations`` index in liters;
    :class:`services.delivery_destination_service.DeliveryDestinationService`
    normalizes the two into a single view.
    """

    model_config = ConfigDict(extra="forbid")

    customer_tank_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Stable, tenant-scoped identifier. If omitted at write time, the "
            "repository mints a uuid4-derived id — the model itself still "
            "requires one here so round-tripped records are lossless."
        ),
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Owning tenant; repositories re-assert this on every read.",
    )
    customer_id: str = Field(..., min_length=1, description="Owning customer_id.")
    customer_type: CustomerType = Field(
        ...,
        description=(
            "One of residential, commercial, keep_full, will_call, auto_fill. "
            "Drives the consumption-segmentation multiplier (Req 1.3)."
        ),
    )
    fuel_type: FuelType = Field(
        ...,
        description=(
            "Narrow fuel-family enum used to select a Consumption_Model "
            "strategy (Req 1.5). Distinct from ``fuel_product_code`` which "
            "holds the catalog product_code."
        ),
    )
    fuel_product_code: str = Field(
        ...,
        min_length=1,
        description=(
            "Canonical US catalog product_code (e.g. PROPANE, HEATING_OIL). "
            "Always persisted after canonicalization so legacy NG aliases "
            "(AGO/PMS/ATK/LPG) are normalized on write."
        ),
    )
    capacity_gallons: float = Field(
        ...,
        gt=0,
        description="Total tank capacity in US gallons; must be strictly positive.",
    )
    current_level_gallons: float = Field(
        ...,
        ge=0,
        description="Current fuel level in US gallons; may be zero.",
    )
    last_reading_at: Optional[datetime] = Field(
        None,
        description=(
            "Timestamp of the most recent reading that produced "
            "``current_level_gallons``. Nullable for tanks that have never "
            "been read."
        ),
    )
    location_lat: float = Field(
        ...,
        ge=-90.0,
        le=90.0,
        description="Latitude in degrees, WGS84.",
    )
    location_lon: float = Field(
        ...,
        ge=-180.0,
        le=180.0,
        description="Longitude in degrees, WGS84.",
    )
    zip_code: str = Field(
        ...,
        min_length=1,
        description=(
            "US ZIP code used to key weather lookups (Req 1.2) and cluster "
            "route groups. Stored as a string to preserve leading zeros."
        ),
    )
    k_factor: Optional[float] = Field(
        None,
        ge=0,
        description=(
            "Propane consumption coefficient (gallons/HDD). Populated by "
            "PropaneKFactorModel once ≥3 delivery intervals are available; "
            "nullable otherwise."
        ),
    )
    use_case: Optional[UseCase] = Field(
        None,
        description=(
            "Optional high-level use-case flag. ``generator`` is the "
            "Phase-10 Storm-Mode priority boost flag; other values are "
            "descriptive."
        ),
    )
    status: CustomerTankStatus = Field(
        default="active",
        description="active | inactive | maintenance. Forecasts run only on active tanks.",
    )
    updated_at: Optional[datetime] = Field(
        None, description="Last-modification timestamp written by the repository."
    )
    created_at: Optional[datetime] = Field(
        None, description="Creation timestamp written by the repository."
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("fuel_product_code", mode="before")
    @classmethod
    def _canonicalize_fuel_product_code(cls, value: Any) -> Any:
        """Canonicalize the catalog ``product_code`` at construction time.

        Accepts the canonical code (``PROPANE``) or any registered alias
        (``LPG``) and stores the canonical form. Unknown codes propagate as
        :class:`UnknownFuelProductError` which Pydantic wraps in a
        ValidationError so API layers surface a 422 with a useful message.
        """

        if value is None:
            return value
        if not isinstance(value, str):
            # Let Pydantic's built-in type check raise with a clear error.
            return value
        return canonicalize(value)

    @field_validator("customer_tank_id", "tenant_id", "customer_id", "zip_code")
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        """Collapse whitespace-only required strings into a validation error."""

        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @model_validator(mode="after")
    def _check_level_not_above_capacity(self) -> "CustomerTank":
        """Reject ``current_level_gallons > capacity_gallons``.

        The ES mapping cannot express this constraint so the model is the
        enforcement point. Forecasting math divides by ``capacity_gallons``
        and expects the level to never exceed it.
        """

        if self.current_level_gallons > self.capacity_gallons:
            raise ValueError(
                "current_level_gallons "
                f"({self.current_level_gallons}) cannot exceed capacity_gallons "
                f"({self.capacity_gallons})"
            )
        return self


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CrossTenantAccessError(PermissionError):
    """Raised when a write/delete targets a record owned by another tenant.

    Subclass of :class:`PermissionError` so middleware that maps exceptions
    to HTTP 403 does the right thing automatically. Reads degrade silently
    to ``None`` instead of raising because a 404 is the appropriate
    response for a missing-or-not-owned document.
    """

    def __init__(
        self,
        tenant_id: str,
        customer_tank_id: str,
        owning_tenant_id: Optional[str] = None,
    ) -> None:
        message = (
            f"customer_tank {customer_tank_id!r} does not belong to tenant "
            f"{tenant_id!r}"
        )
        super().__init__(message)
        self.tenant_id = tenant_id
        self.customer_tank_id = customer_tank_id
        self.owning_tenant_id = owning_tenant_id


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


# Fields the repository refuses to overwrite via ``update()``. Tenant_id and
# the primary key are immutable; timestamps are set by the repository itself.
_UPDATE_IMMUTABLE_FIELDS = frozenset(
    {"customer_tank_id", "tenant_id", "created_at"}
)


# Fields that must be canonicalized on every write path (both create and
# update). Today that is only ``fuel_product_code``; future catalog-backed
# fields can extend this set.
_CANONICALIZE_FIELDS = ("fuel_product_code",)


class CustomerTankRepository:
    """Tenant-scoped CRUD repository for the ``customer_tanks`` ES index.

    Dependencies are injected via the constructor so the repository is
    trivially testable with a recording mock. The only interface the
    repository relies on is:

        * ``await es.index_document(index, doc_id, document)``
        * ``await es.get_document(index, doc_id)`` — may raise / return None
        * ``await es.search_documents(index, query, size)``
        * ``await es.update_document(index, doc_id, partial_doc)``
        * ``await es.delete_document(index, doc_id)`` → bool

    which matches :class:`services.elasticsearch_service.ElasticsearchService`
    exactly.
    """

    #: Default per-query cap for ``list_for_tenant``. Callers that legitimately
    #: need more pass ``size=`` explicitly; this prevents accidental full-
    #: cluster scans from a misconfigured page size.
    DEFAULT_LIST_SIZE: int = 500

    def __init__(self, es_service: Any, *, index_name: str = CUSTOMER_TANKS_INDEX) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        if not index_name:
            raise ValueError("index_name must not be empty")
        self._es = es_service
        self._index = index_name

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        tenant_id: str,
        tank: CustomerTank | Dict[str, Any],
    ) -> CustomerTank:
        """Persist a new Customer_Tank and return the stored model.

        The repository enforces that the record's ``tenant_id`` matches the
        caller's ``tenant_id``. If the caller passes a dict without
        ``tenant_id`` the repository stamps it from the argument. If the
        caller passes a model / dict whose ``tenant_id`` differs, the
        repository raises :class:`CrossTenantAccessError`.

        The ``fuel_product_code`` is canonicalized through
        :func:`services.fuel_product_catalog.canonicalize` via the model
        validator on the way in, so legacy aliases are persisted as their
        canonical form without any extra plumbing at the call site.

        Args:
            tenant_id: Owning tenant. Required, non-empty.
            tank: Either a :class:`CustomerTank` or a raw dict that can be
                coerced into one.

        Returns:
            The persisted :class:`CustomerTank` including auto-generated
            ``customer_tank_id`` (if omitted), ``updated_at``, and
            ``created_at``.
        """

        self._require_tenant(tenant_id)

        payload = self._coerce_to_dict(tank)
        # Fill in tenant_id from the argument if the caller left it blank;
        # reject cross-tenant payloads outright.
        payload.setdefault("tenant_id", tenant_id)
        if payload["tenant_id"] != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                customer_tank_id=str(payload.get("customer_tank_id", "<new>")),
                owning_tenant_id=payload["tenant_id"],
            )

        # Mint a uuid4 id when one isn't supplied so callers posting from a UI
        # don't have to generate one themselves.
        payload.setdefault("customer_tank_id", f"tank_{uuid4()}")

        # Stamp bookkeeping timestamps; ``updated_at`` is also stamped by the
        # ES service, but we set it here too so the returned model carries it.
        # Use ``get`` + explicit None check instead of ``setdefault`` because
        # ``model_dump()`` returns ``created_at=None`` for freshly minted models
        # and setdefault would leave that None in place.
        now = _utcnow_iso()
        if not payload.get("created_at"):
            payload["created_at"] = now
        payload["updated_at"] = now

        # Canonicalize explicitly as well. The Pydantic validator handles the
        # same thing when the caller hands us a plain dict, but doing it here
        # means the early ``CrossTenantAccessError`` check above runs on a
        # canonical payload too (so error messages show the canonical code).
        self._canonicalize_in_place(payload)

        # Validate the full shape before touching ES so validation errors
        # bubble up as ValidationError rather than an ES mapping failure.
        model = CustomerTank(**payload)

        doc = model.model_dump(mode="json", exclude_none=False)
        await self._es.index_document(self._index, model.customer_tank_id, doc)

        return model

    # ------------------------------------------------------------------
    # Read (single + list)
    # ------------------------------------------------------------------

    async def get(
        self, tenant_id: str, customer_tank_id: str
    ) -> Optional[CustomerTank]:
        """Return the tank or ``None`` if it does not exist / is not owned.

        A cross-tenant fetch returns ``None`` rather than raising so the
        REST layer can translate the response into a uniform HTTP 404 — a
        403 would leak existence of tanks owned by other tenants.
        """

        self._require_tenant(tenant_id)
        if not customer_tank_id or not customer_tank_id.strip():
            raise ValueError("customer_tank_id must be a non-empty string")

        source = await self._fetch_source(customer_tank_id)
        if source is None:
            return None
        if source.get("tenant_id") != tenant_id:
            # Cross-tenant read — suppress existence so the caller sees 404.
            logger.info(
                "CustomerTankRepository.get: suppressing cross-tenant hit for "
                "tank=%s (owner=%s, requester=%s)",
                customer_tank_id,
                source.get("tenant_id"),
                tenant_id,
            )
            return None
        return _safe_model_load(source)

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        status: Optional[CustomerTankStatus] = None,
        customer_id: Optional[str] = None,
        customer_type: Optional[CustomerType] = None,
        fuel_type: Optional[FuelType] = None,
        zip_code: Optional[str] = None,
        size: int = DEFAULT_LIST_SIZE,
    ) -> List[CustomerTank]:
        """List tanks for the tenant with optional filters.

        Filters are ANDed together at the ES query layer, then the returned
        documents are re-validated against the caller's ``tenant_id`` so a
        mis-labelled record never crosses the repository boundary. Records
        that fail Pydantic validation (because the source schema drifted)
        are logged and dropped rather than raising, so a single corrupt
        record does not take out the whole list endpoint.
        """

        self._require_tenant(tenant_id)
        if size <= 0:
            raise ValueError("size must be a positive integer")

        must: List[Dict[str, Any]] = [{"term": {"tenant_id": tenant_id}}]
        if status is not None:
            must.append({"term": {"status": status}})
        if customer_id:
            must.append({"term": {"customer_id": customer_id}})
        if customer_type is not None:
            must.append({"term": {"customer_type": customer_type}})
        if fuel_type is not None:
            must.append({"term": {"fuel_type": fuel_type}})
        if zip_code:
            must.append({"term": {"zip_code": zip_code}})

        query = {
            "query": {"bool": {"must": must}},
            "size": size,
        }

        resp = await self._es.search_documents(self._index, query, size)
        sources = _extract_sources(resp)

        out: List[CustomerTank] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "CustomerTankRepository.list_for_tenant: dropping "
                    "customer_tanks doc with mismatched tenant_id %s "
                    "(expected %s)",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            model = _safe_model_load(source)
            if model is not None:
                out.append(model)
        return out

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(
        self,
        tenant_id: str,
        customer_tank_id: str,
        patch: Dict[str, Any],
    ) -> Optional[CustomerTank]:
        """Apply a partial update and return the refreshed model.

        The tenant guard runs **before** any ES write so an attacker cannot
        use an ``update`` to exfiltrate another tenant's record by probing
        existence. If the record does not exist this method returns
        ``None`` (→ HTTP 404). If it exists but belongs to a different
        tenant, :class:`CrossTenantAccessError` is raised (→ HTTP 403
        through the middleware).

        Immutable fields (``customer_tank_id``, ``tenant_id``, ``created_at``)
        are stripped from the patch before it is applied. Attempting to
        mutate them is silently ignored — we deliberately do not raise
        because clients frequently re-post the full model on update and
        rejecting the request for a no-op field would be user-hostile.
        """

        self._require_tenant(tenant_id)
        if not customer_tank_id or not customer_tank_id.strip():
            raise ValueError("customer_tank_id must be a non-empty string")
        if not isinstance(patch, dict):
            raise TypeError(
                f"patch must be a dict, got {type(patch).__name__}"
            )

        source = await self._fetch_source(customer_tank_id)
        if source is None:
            return None
        owner = source.get("tenant_id")
        if owner != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                customer_tank_id=customer_tank_id,
                owning_tenant_id=owner,
            )

        # Strip immutable fields. We do not raise because many clients POST
        # the whole record on update and we want to be tolerant of that.
        clean_patch = {
            k: v for k, v in patch.items() if k not in _UPDATE_IMMUTABLE_FIELDS
        }
        if not clean_patch:
            # Nothing to change — return the current model as-is.
            return _safe_model_load(source)

        # Canonicalize catalog-backed fields in the patch before merging.
        self._canonicalize_in_place(clean_patch)

        # Merge, then re-validate through the Pydantic model so we never
        # persist a payload that would have failed validation on create.
        merged = {**source, **clean_patch}
        merged["updated_at"] = _utcnow_iso()
        validated = CustomerTank(**merged)

        # Persist only the delta — ES _update merges into the live doc.
        partial = validated.model_dump(mode="json", include=set(clean_patch.keys()) | {"updated_at"})
        await self._es.update_document(self._index, customer_tank_id, partial)

        return validated

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, tenant_id: str, customer_tank_id: str) -> bool:
        """Delete a tank. Returns ``True`` if the row was removed.

        Semantics:
            * Not-found → ``False`` (callers translate to HTTP 404).
            * Cross-tenant → :class:`CrossTenantAccessError` (→ HTTP 403).
            * Owned + deleted → ``True`` (→ HTTP 204).
        """

        self._require_tenant(tenant_id)
        if not customer_tank_id or not customer_tank_id.strip():
            raise ValueError("customer_tank_id must be a non-empty string")

        source = await self._fetch_source(customer_tank_id)
        if source is None:
            return False
        owner = source.get("tenant_id")
        if owner != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                customer_tank_id=customer_tank_id,
                owning_tenant_id=owner,
            )

        return bool(await self._es.delete_document(self._index, customer_tank_id))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _require_tenant(tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

    @staticmethod
    def _coerce_to_dict(tank: CustomerTank | Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(tank, CustomerTank):
            return tank.model_dump(mode="python")
        if isinstance(tank, dict):
            # Shallow copy so the caller's dict is never mutated.
            return dict(tank)
        raise TypeError(
            f"tank must be a CustomerTank or dict, got {type(tank).__name__}"
        )

    @staticmethod
    def _canonicalize_in_place(payload: Dict[str, Any]) -> None:
        """Canonicalize every catalog-backed field present in ``payload``.

        Unknown product codes propagate as :class:`UnknownFuelProductError`
        so the API layer surfaces a 400/422 rather than silently persisting
        garbage. Keeping the error explicit here (rather than in a validator)
        also means repository-level unit tests can exercise the path without
        driving a full Pydantic validation cycle.
        """

        for field in _CANONICALIZE_FIELDS:
            if field in payload and payload[field] is not None:
                try:
                    payload[field] = canonicalize(payload[field])
                except UnknownFuelProductError:
                    # Re-raise so callers see a single, informative error.
                    raise

    async def _fetch_source(self, customer_tank_id: str) -> Optional[Dict[str, Any]]:
        """Return the raw ``_source`` or ``None`` if the document is missing.

        Uses a search-by-id rather than a direct ``get_document`` call
        because the ES service's ``get_document`` raises on 404 (it surfaces
        an :class:`AppException`). A search returns empty hits cleanly,
        which matches repository semantics.
        """

        query = {
            "query": {"term": {"customer_tank_id": customer_tank_id}},
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(self._index, query, 1)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "CustomerTankRepository._fetch_source: search failed for "
                "tank=%s: %s",
                customer_tank_id,
                exc,
            )
            return None
        sources = _extract_sources(resp)
        return sources[0] if sources else None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Return a timezone-aware UTC timestamp as an ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat()


def _extract_sources(resp: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` payloads from an ES-shaped response.

    Accepts both the canonical ``{"hits": {"hits": [{"_source": ...}]}}``
    shape and ``None`` so the helper is robust across the variety of mock
    shapes used by tests.
    """

    if not resp:
        return []
    # Handle both dict and ObjectApiResponse (which has .get() but isn't a dict)
    hits_outer = resp.get("hits") if hasattr(resp, 'get') else None
    if not hits_outer:
        return []
    hits = hits_outer.get("hits") or []
    out: List[Dict[str, Any]] = []
    for hit in hits:
        if hasattr(hit, 'get') and hit.get("_source"):
            out.append(hit["_source"])
    return out


def _safe_model_load(source: Dict[str, Any]) -> Optional[CustomerTank]:
    """Build a :class:`CustomerTank` from a raw ES source, logging on failure.

    A source document that fails Pydantic validation is logged at warning
    level and dropped so a single corrupt record does not kill an entire
    list response. This matches the pattern used by
    :class:`DeliveryDestinationService`.
    """

    try:
        return CustomerTank(**source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "CustomerTankRepository: dropping customer_tanks doc that failed "
            "model validation (tank_id=%s): %s",
            source.get("customer_tank_id"),
            exc,
        )
        return None


__all__ = [
    "CustomerType",
    "FuelType",
    "CustomerTankStatus",
    "UseCase",
    "CustomerTank",
    "CustomerTankRepository",
    "CrossTenantAccessError",
]
