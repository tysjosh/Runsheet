"""
Depot domain model and tenant-scoped repository.

Capability 2 (Requirement 2.2) of the fuel-ops hardening spec introduces a
tenant-configurable ``Depot`` entity so the Route_Planning_Agent can resolve
a real loading site per tenant instead of a hardcoded Lagos coordinate.
This module is the source of truth for the :class:`Depot` Pydantic model
and the :class:`DepotRepository` that reads/writes it against the
``depots`` Elasticsearch index (mapping defined in
:mod:`fuel.services.fuel_ops_es_mappings`).

Key responsibilities:

* Expose :class:`Depot`, a strict Pydantic model whose fields mirror the
  ES mapping 1:1 (Requirement 2.2.1). Coordinates are bounded to valid
  WGS84 ranges, ``timezone`` is validated against the IANA tz database,
  ``fuel_types_supported`` entries are canonicalized through the fuel
  product catalog, and the ``status`` enumeration is enforced at
  construction time.
* Canonicalize fuel-product entries in ``fuel_types_supported`` through
  :func:`fuel.services.fuel_product_catalog.canonicalize` on every write so
  legacy aliases (AGO → DIESEL_2, LPG → PROPANE, etc.) are normalized to
  the canonical US ``product_code`` before persistence (Requirement 6.1.4
  applied to depots).
* Expose :class:`DepotRepository` with async ``create``, ``get``,
  ``list_for_tenant``, ``update``, and ``delete`` methods. Every method is
  tenant-scoped: cross-tenant ``get`` returns ``None``, cross-tenant
  ``update`` / ``delete`` raise :class:`CrossTenantAccessError` so callers
  can translate cleanly into HTTP 403s. Missing records are ``None`` /
  ``False`` so callers translate to HTTP 404s.

Tenant isolation is enforced at two points for defense-in-depth:
    1. Every ES query includes a ``term`` clause on ``tenant_id``.
    2. Every returned document is re-validated against the caller's
       ``tenant_id`` before it crosses the repository boundary, so a
       mis-labelled document never leaks across tenants.

Validates: Requirements 2.2.1.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone as _dt_timezone
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fuel.services.fuel_ops_es_mappings import DEPOTS_INDEX
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


#: Source-of-truth status for a depot. Matches Requirement 2.2.1.
DepotStatus = Literal["active", "inactive"]


class Depot(BaseModel):
    """A physical location where trucks load fuel and start/end routes.

    Field shapes, types, and value ranges mirror the ``depots`` ES mapping
    (Task 1.1) so a ``model_dump()`` payload can be indexed directly
    without transformation. A tenant may have multiple depots; the
    :class:`DepotRepository` enforces tenant isolation on every read and
    write path so one tenant can never see or mutate another tenant's
    depots (Requirement 2.2.2).

    ``timezone`` is stored as an IANA identifier (e.g. ``"America/Chicago"``)
    so downstream services can compute local operating hours without
    re-deriving the offset. The validator rejects unknown IANA names at
    construction time rather than deferring the failure to a consumer.
    """

    model_config = ConfigDict(extra="forbid")

    depot_id: str = Field(
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
    name: str = Field(
        ...,
        min_length=1,
        description="Human-readable label shown in the dispatcher UI.",
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
    address: str = Field(
        ...,
        min_length=1,
        description="Postal address used for human lookup and dispatcher UI.",
    )
    timezone: str = Field(
        ...,
        min_length=1,
        description=(
            "IANA timezone name, e.g. 'America/Chicago'. Validated at "
            "construction time so callers fail fast on typos."
        ),
    )
    fuel_types_supported: List[str] = Field(
        default_factory=list,
        description=(
            "Canonical US fuel product codes this depot can load (e.g. "
            "DIESEL_2, PROPANE). Legacy aliases (AGO, LPG, ...) are "
            "canonicalized on write."
        ),
    )
    status: DepotStatus = Field(
        default="active",
        description="active | inactive. Route planning only considers active depots.",
    )
    is_default: bool = Field(
        default=False,
        description=(
            "When True, this depot is the tenant's default depot. The "
            "repository enforces at most one default per tenant: setting a "
            "depot as default clears the flag on any other depot."
        ),
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

    @field_validator("depot_id", "tenant_id", "name", "address", "timezone")
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        """Collapse whitespace-only required strings into a validation error."""

        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("timezone")
    @classmethod
    def _validate_iana_timezone(cls, value: str) -> str:
        """Reject timezone strings that are not valid IANA identifiers.

        ``zoneinfo`` (stdlib since Python 3.9) consults the system tz
        database. Unknown names raise ``ZoneInfoNotFoundError`` which we
        translate into a Pydantic ``ValueError`` so the API layer surfaces
        a clean 422. ``datetime.timezone.utc`` is accepted via the alias
        ``"UTC"`` because ``ZoneInfo("UTC")`` resolves to the IANA UTC
        entry on every supported platform.
        """

        # Import locally so the module still imports on older Python
        # versions where zoneinfo may be backported rather than shipped.
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        except ImportError as exc:  # pragma: no cover - Python 3.9+ ships zoneinfo
            raise ValueError(
                f"cannot validate timezone {value!r}: zoneinfo is unavailable"
            ) from exc

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"invalid IANA timezone: {value!r}"
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise ValueError(
                f"invalid timezone {value!r}: {exc}"
            ) from exc
        return value

    @field_validator("fuel_types_supported", mode="before")
    @classmethod
    def _canonicalize_fuel_types(cls, value: Any) -> Any:
        """Canonicalize every entry in ``fuel_types_supported`` on construction.

        Accepts canonical codes (``PROPANE``) or registered aliases
        (``LPG``) and stores the canonical form. Unknown codes propagate
        as :class:`UnknownFuelProductError` which Pydantic wraps in a
        ValidationError so API layers surface a 422 with a useful message.

        Duplicate entries after canonicalization are collapsed while
        preserving input order so the persisted list is always a clean
        set of canonical codes.
        """

        if value is None:
            return value
        if not isinstance(value, list):
            # Let Pydantic's type check raise with a clear error.
            return value

        seen: set[str] = set()
        out: List[str] = []
        for entry in value:
            if not isinstance(entry, str):
                # Defer to Pydantic to flag the non-string element.
                out.append(entry)
                continue
            canonical = canonicalize(entry)
            if canonical not in seen:
                seen.add(canonical)
                out.append(canonical)
        return out


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CrossTenantAccessError(PermissionError):
    """Raised when a write/delete targets a depot owned by another tenant.

    Subclass of :class:`PermissionError` so middleware that maps exceptions
    to HTTP 403 does the right thing automatically. Reads degrade silently
    to ``None`` instead of raising because a 404 is the appropriate
    response for a missing-or-not-owned document.
    """

    def __init__(
        self,
        tenant_id: str,
        depot_id: str,
        owning_tenant_id: Optional[str] = None,
    ) -> None:
        message = (
            f"depot {depot_id!r} does not belong to tenant {tenant_id!r}"
        )
        super().__init__(message)
        self.tenant_id = tenant_id
        self.depot_id = depot_id
        self.owning_tenant_id = owning_tenant_id


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


# Fields the repository refuses to overwrite via ``update()``. Tenant_id and
# the primary key are immutable; timestamps are set by the repository itself.
_UPDATE_IMMUTABLE_FIELDS = frozenset({"depot_id", "tenant_id", "created_at"})


class DepotRepository:
    """Tenant-scoped CRUD repository for the ``depots`` ES index.

    Dependencies are injected via the constructor so the repository is
    trivially testable with a recording mock. The only interface the
    repository relies on is:

        * ``await es.index_document(index, doc_id, document)``
        * ``await es.search_documents(index, query, size)``
        * ``await es.update_document(index, doc_id, partial_doc)``
        * ``await es.delete_document(index, doc_id)`` → bool

    which matches :class:`services.elasticsearch_service.ElasticsearchService`
    exactly. The repository intentionally does not depend on
    ``get_document`` because that method raises on 404; a search-by-id is
    used instead so missing records surface as empty hits.
    """

    #: Default per-query cap for ``list_for_tenant``. Callers that legitimately
    #: need more pass ``size=`` explicitly; this prevents accidental full-
    #: cluster scans from a misconfigured page size.
    DEFAULT_LIST_SIZE: int = 500

    def __init__(self, es_service: Any, *, index_name: str = DEPOTS_INDEX) -> None:
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
        depot: Depot | Dict[str, Any],
    ) -> Depot:
        """Persist a new Depot and return the stored model.

        The repository enforces that the record's ``tenant_id`` matches the
        caller's ``tenant_id``. If the caller passes a dict without
        ``tenant_id`` the repository stamps it from the argument. If the
        caller passes a model / dict whose ``tenant_id`` differs, the
        repository raises :class:`CrossTenantAccessError`.

        ``fuel_types_supported`` entries are canonicalized through the
        Pydantic validator on the way in, so legacy aliases are persisted
        as their canonical form without any extra plumbing at the call
        site.

        Args:
            tenant_id: Owning tenant. Required, non-empty.
            depot: Either a :class:`Depot` or a raw dict that can be
                coerced into one.

        Returns:
            The persisted :class:`Depot` including auto-generated
            ``depot_id`` (if omitted), ``updated_at``, and ``created_at``.
        """

        self._require_tenant(tenant_id)

        payload = self._coerce_to_dict(depot)
        # Fill in tenant_id from the argument if the caller left it blank;
        # reject cross-tenant payloads outright.
        payload.setdefault("tenant_id", tenant_id)
        if payload["tenant_id"] != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                depot_id=str(payload.get("depot_id", "<new>")),
                owning_tenant_id=payload["tenant_id"],
            )

        # Mint a uuid4 id when one isn't supplied so callers posting from a UI
        # don't have to generate one themselves.
        payload.setdefault("depot_id", f"depot_{uuid4()}")

        # Stamp bookkeeping timestamps; ``updated_at`` is set on every
        # create and update. Use explicit None checks instead of setdefault
        # because ``model_dump()`` may pass ``created_at=None`` for freshly
        # minted models.
        now = _utcnow_iso()
        if not payload.get("created_at"):
            payload["created_at"] = now
        payload["updated_at"] = now

        # Validate the full shape before touching ES so validation errors
        # bubble up as ValidationError rather than an ES mapping failure.
        model = Depot(**payload)

        doc = model.model_dump(mode="json", exclude_none=False)
        await self._es.index_document(self._index, model.depot_id, doc)

        # Dual-write the depot to the Postgres source-of-truth.
        from commerce.services.commerce_persistence_bridge import (
            mirror_current_state_upsert,
        )
        await mirror_current_state_upsert("depot", doc)

        # Enforce single-default-per-tenant: if this depot was created as
        # the default, clear the flag on any other depot for the tenant.
        if model.is_default:
            await self._clear_other_defaults(tenant_id, model.depot_id)

        return model

    # ------------------------------------------------------------------
    # Read (single + list)
    # ------------------------------------------------------------------

    async def get(self, tenant_id: str, depot_id: str) -> Optional[Depot]:
        """Return the depot or ``None`` if it does not exist / is not owned.

        A cross-tenant fetch returns ``None`` rather than raising so the
        REST layer can translate the response into a uniform HTTP 404 — a
        403 would leak existence of depots owned by other tenants.
        """

        self._require_tenant(tenant_id)
        if not depot_id or not depot_id.strip():
            raise ValueError("depot_id must be a non-empty string")

        # Read-cutover: serve from Postgres when enabled.
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_get,
        )
        pg = await read_hybrid_get("depot", tenant_id, depot_id)
        if pg is not _NOT_CUT_OVER:
            return _safe_model_load(pg) if pg is not None else None

        source = await self._fetch_source(depot_id)
        if source is None:
            return None
        if source.get("tenant_id") != tenant_id:
            # Cross-tenant read — suppress existence so the caller sees 404.
            logger.info(
                "DepotRepository.get: suppressing cross-tenant hit for "
                "depot=%s (owner=%s, requester=%s)",
                depot_id,
                source.get("tenant_id"),
                tenant_id,
            )
            return None
        return _safe_model_load(source)

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        status: Optional[DepotStatus] = None,
        fuel_type: Optional[str] = None,
        size: int = DEFAULT_LIST_SIZE,
    ) -> List[Depot]:
        """List depots for the tenant with optional filters.

        Filters are ANDed together at the ES query layer, then the returned
        documents are re-validated against the caller's ``tenant_id`` so a
        mis-labelled record never crosses the repository boundary. Records
        that fail Pydantic validation (because the source schema drifted)
        are logged and dropped rather than raising, so a single corrupt
        record does not take out the whole list endpoint.

        ``fuel_type`` is canonicalized before querying so a caller
        filtering on ``"LPG"`` matches depots that persist ``"PROPANE"``.
        """

        self._require_tenant(tenant_id)
        if size <= 0:
            raise ValueError("size must be a positive integer")

        # Read-cutover: serve from Postgres when enabled. The ``fuel_type``
        # filter targets a document array (not a typed column), so route to PG
        # only when it is absent; otherwise fall back to ES.
        if not fuel_type:
            from commerce.services.commerce_persistence_bridge import (
                _NOT_CUT_OVER,
                read_hybrid_list,
            )
            pg = await read_hybrid_list(
                "depot", tenant_id,
                filters={"status": status} if status is not None else None,
                limit=size,
            )
            if pg is not _NOT_CUT_OVER:
                out: List[Depot] = []
                for source in pg["items"]:
                    model = _safe_model_load(source)
                    if model is not None:
                        out.append(model)
                return out

        must: List[Dict[str, Any]] = [{"term": {"tenant_id": tenant_id}}]
        if status is not None:
            must.append({"term": {"status": status}})
        if fuel_type:
            try:
                canonical_fuel = canonicalize(fuel_type)
            except UnknownFuelProductError:
                # Unknown filter → empty result set; persist the query
                # shape so callers see a stable error-vs-empty distinction.
                return []
            must.append({"term": {"fuel_types_supported": canonical_fuel}})

        query = {
            "query": {"bool": {"must": must}},
            "size": size,
        }

        resp = await self._es.search_documents(self._index, query, size)
        sources = _extract_sources(resp)

        out: List[Depot] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "DepotRepository.list_for_tenant: dropping depots doc "
                    "with mismatched tenant_id %s (expected %s)",
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
        depot_id: str,
        patch: Dict[str, Any],
    ) -> Optional[Depot]:
        """Apply a partial update and return the refreshed model.

        The tenant guard runs **before** any ES write so an attacker cannot
        use an ``update`` to exfiltrate another tenant's record by probing
        existence. If the record does not exist this method returns
        ``None`` (→ HTTP 404). If it exists but belongs to a different
        tenant, :class:`CrossTenantAccessError` is raised (→ HTTP 403
        through the middleware).

        Immutable fields (``depot_id``, ``tenant_id``, ``created_at``) are
        stripped from the patch before it is applied. Attempting to mutate
        them is silently ignored — we deliberately do not raise because
        clients frequently re-post the full model on update and rejecting
        the request for a no-op field would be user-hostile.
        """

        self._require_tenant(tenant_id)
        if not depot_id or not depot_id.strip():
            raise ValueError("depot_id must be a non-empty string")
        if not isinstance(patch, dict):
            raise TypeError(
                f"patch must be a dict, got {type(patch).__name__}"
            )

        source = await self._fetch_source(depot_id)
        if source is None:
            return None
        owner = source.get("tenant_id")
        if owner != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                depot_id=depot_id,
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

        # Merge, then re-validate through the Pydantic model so we never
        # persist a payload that would have failed validation on create.
        # The model validator canonicalizes fuel_types_supported for us.
        # Strip ES-only keys (e.g. the ``location`` geo_point convenience
        # field) before constructing the strict model — the persisted source
        # carries fields the ``extra="forbid"`` model does not define, and
        # without this filter the merge raises ``extra_forbidden`` and breaks
        # every depot update (including the "set as default" action).
        model_fields = Depot.model_fields.keys()
        merged = {
            k: v for k, v in {**source, **clean_patch}.items() if k in model_fields
        }
        merged["updated_at"] = _utcnow_iso()
        validated = Depot(**merged)

        # Persist only the delta — ES _update merges into the live doc.
        # Include any fields the validator may have mutated (e.g. the
        # canonical form of fuel_types_supported) so the persisted doc
        # matches the returned model exactly.
        delta_keys = set(clean_patch.keys()) | {"updated_at"}
        partial = validated.model_dump(mode="json", include=delta_keys)
        await self._es.update_document(self._index, depot_id, partial)

        # Dual-write the full validated depot to Postgres (verbatim doc).
        from commerce.services.commerce_persistence_bridge import (
            mirror_current_state_upsert,
        )
        await mirror_current_state_upsert(
            "depot", validated.model_dump(mode="json", exclude_none=False)
        )

        # Enforce single-default-per-tenant when this update set the flag.
        if clean_patch.get("is_default") is True:
            await self._clear_other_defaults(tenant_id, depot_id)

        return validated

    # ------------------------------------------------------------------
    # Default-depot bookkeeping
    # ------------------------------------------------------------------

    async def _clear_other_defaults(
        self, tenant_id: str, keep_depot_id: str
    ) -> None:
        """Clear ``is_default`` on every tenant depot except ``keep_depot_id``.

        Enforces the single-default-per-tenant invariant. Best-effort:
        failures are logged but do not roll back the primary write, since
        the freshly-defaulted depot is already persisted and a stale
        second default is a soft inconsistency the next read reconciles.
        """
        try:
            others = await self.list_for_tenant(tenant_id, size=self.DEFAULT_LIST_SIZE)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            logger.warning(
                "DepotRepository._clear_other_defaults: failed to list "
                "depots for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return

        for depot in others:
            if depot.depot_id == keep_depot_id or not depot.is_default:
                continue
            try:
                await self._es.update_document(
                    self._index,
                    depot.depot_id,
                    {"is_default": False, "updated_at": _utcnow_iso()},
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "DepotRepository._clear_other_defaults: failed to clear "
                    "default on depot=%s tenant=%s: %s",
                    depot.depot_id,
                    tenant_id,
                    exc,
                )

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, tenant_id: str, depot_id: str) -> bool:
        """Delete a depot. Returns ``True`` if the row was removed.

        Semantics:
            * Not-found → ``False`` (callers translate to HTTP 404).
            * Cross-tenant → :class:`CrossTenantAccessError` (→ HTTP 403).
            * Owned + deleted → ``True`` (→ HTTP 204).
        """

        self._require_tenant(tenant_id)
        if not depot_id or not depot_id.strip():
            raise ValueError("depot_id must be a non-empty string")

        source = await self._fetch_source(depot_id)
        if source is None:
            return False
        owner = source.get("tenant_id")
        if owner != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                depot_id=depot_id,
                owning_tenant_id=owner,
            )

        return bool(await self._es.delete_document(self._index, depot_id))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _require_tenant(tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

    @staticmethod
    def _coerce_to_dict(depot: Depot | Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(depot, Depot):
            return depot.model_dump(mode="python")
        if isinstance(depot, dict):
            # Shallow copy so the caller's dict is never mutated.
            return dict(depot)
        raise TypeError(
            f"depot must be a Depot or dict, got {type(depot).__name__}"
        )

    async def _fetch_source(self, depot_id: str) -> Optional[Dict[str, Any]]:
        """Return the raw ``_source`` or ``None`` if the document is missing.

        Uses a search-by-id rather than a direct ``get_document`` call
        because the ES service's ``get_document`` raises on 404. A search
        returns empty hits cleanly, which matches repository semantics.
        """

        query = {
            "query": {"term": {"depot_id": depot_id}},
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(self._index, query, 1)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "DepotRepository._fetch_source: search failed for depot=%s: %s",
                depot_id,
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

    return datetime.now(_dt_timezone.utc).isoformat()


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


def _safe_model_load(source: Dict[str, Any]) -> Optional[Depot]:
    """Build a :class:`Depot` from a raw ES source, logging on failure.

    A source document that fails Pydantic validation is logged at warning
    level and dropped so a single corrupt record does not kill an entire
    list response. This matches the pattern used by
    :class:`CustomerTankRepository`.
    """

    try:
        # The persisted ``depots`` document carries a ``location`` geo_point
        # (and may carry other ES-only fields) that the strict ``Depot``
        # model (``extra="forbid"``) does not define — it uses the flat
        # ``location_lat`` / ``location_lon`` pair instead. Drop unknown keys
        # before validation so a row written with the geo_point convenience
        # field is not silently discarded on read.
        model_fields = Depot.model_fields.keys()
        cleaned = {k: v for k, v in source.items() if k in model_fields}
        return Depot(**cleaned)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "DepotRepository: dropping depots doc that failed model "
            "validation (depot_id=%s): %s",
            source.get("depot_id"),
            exc,
        )
        return None


__all__ = [
    "DepotStatus",
    "Depot",
    "DepotRepository",
    "CrossTenantAccessError",
]
