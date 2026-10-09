"""
Compartment state domain model and tenant-scoped repository.

Capability 7 / Requirement 7.1 of the fuel-ops hardening spec adds
cross-contamination prevention to the Compartment_Loading_Agent. Safe
enforcement requires tracking per-compartment state beyond the static
configuration fields (``capacity_liters``, ``allowed_grades``) that the
``truck_compartments`` ES index already carries.

This module is the source of truth for the additive state fields
introduced by the Task 6.1 mapping extension:

* ``last_loaded_product`` (keyword, nullable) — canonical US catalog
  product_code written by the Loading_Plan commit (Task 6.6).
* ``last_loaded_at`` (date, nullable) — timestamp of the most recent load.
* ``last_cleaned_at`` (date, nullable) — timestamp of the most recent
  Cleaning_Event write (Task 6.2).
* ``state`` (keyword) — one of ``clean``, ``loaded``, ``needs_cleaning``.

The module exposes:

* :class:`CompartmentState`, a strict Pydantic model representing the
  state triple plus identity fields. Kept separate from the full
  compartment configuration so the repository can operate on the narrow
  subset it cares about without needing every configuration field to
  round-trip through Pydantic on every atomic update.
* :class:`CompartmentStateRepository` with tenant-scoped ``get``,
  ``mark_loaded``, ``mark_cleaned``, and ``mark_needs_cleaning`` helpers.
  Every write goes through :meth:`_atomic_update`, which uses
  Elasticsearch optimistic concurrency control (``if_seq_no`` /
  ``if_primary_term``) so concurrent Loading_Plan commits against the
  same compartment cannot overwrite each other silently (Req 7.1.2,
  7.1.3). A bounded retry-on-conflict loop degrades gracefully to a
  :class:`CompartmentStateConflictError` on persistent contention so
  callers can surface an HTTP 409 rather than silently losing writes.

The module deliberately does **not** define the ``CleaningEvent`` model
or ``CleaningEventService`` — those are added by Task 6.2 alongside the
``POST /api/fuel/mvp/compartments/{id}/cleaning-events`` endpoint (Task
6.3). What Task 6.1 provides is the primitive the Cleaning Event
service, the Compartment_Loading_Agent, and the load-eligibility
endpoint (Task 6.7) all sit on top of.

Validates: Requirements 7.1.1, 7.1.2, 7.1.3.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from Agents.support.mvp_es_mappings import TRUCK_COMPARTMENTS_INDEX
from commerce.services.commerce_persistence_bridge import (
    mirror_current_state_upsert,
)
from fuel.services.fuel_ops_es_mappings import COMPARTMENT_CLEANING_EVENTS_INDEX
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


#: Allowed values for the compartment lifecycle ``state`` flag.
#:
#:     * ``clean`` — compartment is empty and safe to load any allowed grade.
#:     * ``loaded`` — compartment currently holds the product recorded in
#:       ``last_loaded_product`` (loading plan committed; delivery may still
#:       be pending).
#:     * ``needs_cleaning`` — the last load triggered a ``requires_cleaning``
#:       transition rule and a Cleaning_Event must be recorded before the
#:       compartment may accept a new product.
CompartmentLifecycleState = Literal["clean", "loaded", "needs_cleaning"]


class CompartmentState(BaseModel):
    """Narrow view over the four lifecycle fields of a truck compartment.

    Constructed and persisted by :class:`CompartmentStateRepository`; the
    full compartment configuration (``capacity_liters``, ``allowed_grades``,
    ``position_index``, depot fields, etc.) lives in the same document but
    is intentionally out of scope for this model because the repository's
    job is to mutate only the state triple.
    """

    model_config = ConfigDict(extra="forbid")

    compartment_id: str = Field(
        ...,
        min_length=1,
        description="Compartment identifier, tenant-scoped and truck-qualified.",
    )
    truck_id: str = Field(
        ...,
        min_length=1,
        description="Parent truck identifier for the compartment.",
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Owning tenant; repositories re-assert this on every read.",
    )
    state: CompartmentLifecycleState = Field(
        default="clean",
        description="Lifecycle flag: clean | loaded | needs_cleaning.",
    )
    last_loaded_product: Optional[str] = Field(
        default=None,
        description=(
            "Canonical US catalog product_code of the most recent load. "
            "Persisted only after canonicalization so legacy NG aliases "
            "land as their US equivalent (Req 6.1.4)."
        ),
    )
    last_loaded_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp of the most recent Loading_Plan commit.",
    )
    last_cleaned_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp of the most recent Cleaning_Event write.",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("last_loaded_product", mode="before")
    @classmethod
    def _canonicalize_last_loaded_product(cls, value: Any) -> Any:
        """Canonicalize the catalog product_code at construction time.

        Accepts the canonical code, any registered alias, or ``None``
        (the initial/cleaned state). Unknown codes propagate as
        :class:`UnknownFuelProductError` which Pydantic wraps in a
        :class:`ValidationError` so API callers surface a 422.
        """

        if value is None or value == "":
            return None
        if not isinstance(value, str):
            # Let Pydantic's built-in type check raise with a clear error.
            return value
        return canonicalize(value)

    @field_validator("compartment_id", "truck_id", "tenant_id")
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CompartmentStateError(Exception):
    """Base exception for compartment-state repository failures."""


class CompartmentNotFoundError(CompartmentStateError):
    """Raised when a tenant-scoped lookup cannot find the compartment.

    Separate from :class:`CrossTenantCompartmentAccessError` so callers can
    translate cleanly into HTTP 404 vs 403 without leaking existence
    across tenants (cross-tenant reads are downgraded to ``None``; this
    error is only raised on writes when the caller asserts the
    compartment must exist).
    """

    def __init__(self, tenant_id: str, compartment_doc_id: str) -> None:
        super().__init__(
            f"compartment {compartment_doc_id!r} not found for tenant {tenant_id!r}"
        )
        self.tenant_id = tenant_id
        self.compartment_doc_id = compartment_doc_id


class CrossTenantCompartmentAccessError(PermissionError, CompartmentStateError):
    """Raised when a write targets a compartment owned by another tenant.

    Subclass of :class:`PermissionError` so middleware that maps
    exceptions to HTTP 403 does the right thing automatically.
    """

    def __init__(
        self,
        tenant_id: str,
        compartment_doc_id: str,
        owning_tenant_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            f"compartment {compartment_doc_id!r} does not belong to tenant "
            f"{tenant_id!r}"
        )
        self.tenant_id = tenant_id
        self.compartment_doc_id = compartment_doc_id
        self.owning_tenant_id = owning_tenant_id


class CompartmentStateConflictError(CompartmentStateError):
    """Raised when optimistic concurrency control repeatedly loses the race.

    The repository retries a bounded number of times on
    ``version_conflict`` before giving up; if every retry still collides
    with a concurrent write, callers see this exception so they can
    surface an HTTP 409 and prompt the user to retry.
    """

    def __init__(self, tenant_id: str, compartment_doc_id: str, attempts: int) -> None:
        super().__init__(
            f"concurrent modification detected for compartment "
            f"{compartment_doc_id!r} (tenant={tenant_id!r}) after "
            f"{attempts} attempts"
        )
        self.tenant_id = tenant_id
        self.compartment_doc_id = compartment_doc_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


# Fields the repository is allowed to set on an atomic update. Any other
# field slipping into the partial payload would either violate the strict
# mapping or overwrite unrelated compartment configuration.
_STATE_FIELDS = frozenset(
    {"state", "last_loaded_product", "last_loaded_at", "last_cleaned_at"}
)


class CompartmentStateRepository:
    """Tenant-scoped state-mutation helpers for ``truck_compartments``.

    The repository exposes four public async operations:

        * :meth:`get` — read the current state triple for a compartment.
        * :meth:`mark_loaded` — record a successful Loading_Plan commit.
        * :meth:`mark_cleaned` — record a Cleaning_Event completion.
        * :meth:`mark_needs_cleaning` — flag a compartment as requiring
          cleaning before its next load (typically invoked by the
          cross-contamination guard).

    Every write goes through :meth:`_atomic_update`, which delegates to
    :meth:`services.elasticsearch_service.ElasticsearchService.atomic_update`.
    How that call stays safe under concurrency depends on the backend, and the
    difference matters to callers: on Elasticsearch it is ``_seq_no`` /
    ``_primary_term`` optimistic concurrency with bounded jittered retries, so
    persistent contention surfaces as :class:`CompartmentStateConflictError` and
    the caller returns a 409. On Postgres the row is locked, so a concurrent
    writer waits instead of colliding and that 409 becomes unreachable.

    Dependencies are injected via the constructor so the repository is
    trivially testable with a recording mock. The only interface the
    repository relies on is:

        * ``await es_service.get_document(index, doc_id)`` — the stored
          document, or ``None`` when it does not exist.
        * ``await es_service.atomic_update(index, doc_id, transform, ...)`` —
          returns ``(document, applied)``; ``transform`` returns ``None`` to
          leave the document untouched.

    Neither reaches ``es_service.client``, so both follow
    ``DOCUMENT_STORE_BACKEND``. That is not incidental here: this repository
    writes ``last_loaded_product``, the history the cross-contamination guard
    reads, so a read and a write that disagreed about which store to use would
    let a contaminated load through.
    """

    #: Maximum number of retry attempts on ``version_conflict`` before a
    #: :class:`CompartmentStateConflictError` is raised. Exposed as a
    #: class attribute so tests can tune it without monkey-patching.
    MAX_OCC_RETRIES: int = 3

    #: Baseline backoff between retries (seconds). Actual sleep is
    #: ``base * 2 ** attempt * random.uniform(0.5, 1.5)`` to spread
    #: thundering-herd retries across concurrent callers.
    OCC_BACKOFF_BASE_SECONDS: float = 0.01

    def __init__(
        self,
        es_service: Any,
        *,
        index_name: str = TRUCK_COMPARTMENTS_INDEX,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        if not index_name:
            raise ValueError("index_name must not be empty")
        self._es = es_service
        self._index = index_name

    # ------------------------------------------------------------------
    # Public read
    # ------------------------------------------------------------------

    async def get(
        self, tenant_id: str, compartment_doc_id: str
    ) -> Optional[CompartmentState]:
        """Return the current state triple, or ``None`` if not found / not owned.

        A cross-tenant fetch returns ``None`` rather than raising so the
        REST layer can translate the response into a uniform HTTP 404 — a
        403 would leak existence of compartments owned by other tenants.

        Args:
            tenant_id: Requesting tenant. Required, non-empty.
            compartment_doc_id: ES document id. Truck configuration writes
                use the composite key ``{truck_id}_{compartment_id}`` (see
                ``mvp_endpoints.configure_compartments``); callers pass
                that same composite id here.

        Returns:
            A :class:`CompartmentState` or ``None`` when the document is
            missing or owned by a different tenant.
        """

        self._require_tenant(tenant_id)
        self._require_doc_id(compartment_doc_id)

        source = await self._fetch_source(compartment_doc_id)
        if source is None:
            return None
        if source.get("tenant_id") != tenant_id:
            logger.info(
                "CompartmentStateRepository.get: suppressing cross-tenant hit "
                "for compartment=%s (owner=%s, requester=%s)",
                compartment_doc_id,
                source.get("tenant_id"),
                tenant_id,
            )
            return None
        return _safe_state_load(source)

    # ------------------------------------------------------------------
    # Public writes
    # ------------------------------------------------------------------

    async def mark_loaded(
        self,
        tenant_id: str,
        compartment_doc_id: str,
        *,
        product_code: str,
        loaded_at: Optional[datetime] = None,
    ) -> CompartmentState:
        """Atomically record a Loading_Plan commit.

        Validates: Requirement 7.1.2.

        On success the compartment's ``last_loaded_product`` is set to the
        canonical form of ``product_code``, ``last_loaded_at`` is stamped
        with ``loaded_at`` (default: now), and ``state`` is set to
        ``loaded``. ``last_cleaned_at`` is deliberately left untouched —
        the cleaning history is append-only from this method's point of
        view and is only reset by :meth:`mark_cleaned`.

        Args:
            tenant_id: Owning tenant.
            compartment_doc_id: ES document id for the compartment.
            product_code: Fuel product code or legacy alias; canonicalized
                before persistence.
            loaded_at: Timestamp to record. Defaults to the current UTC
                time so the Loading_Plan commit path does not need to
                plumb a clock through.

        Returns:
            The refreshed :class:`CompartmentState`.

        Raises:
            CompartmentNotFoundError: if the compartment does not exist.
            CrossTenantCompartmentAccessError: if owned by a different tenant.
            CompartmentStateConflictError: if every retry lost the OCC race.
            UnknownFuelProductError: if ``product_code`` is not in the catalog.
        """

        self._require_tenant(tenant_id)
        self._require_doc_id(compartment_doc_id)
        if not isinstance(product_code, str) or not product_code.strip():
            raise ValueError("product_code must be a non-empty string")

        canonical = canonicalize(product_code)
        stamp = _ensure_utc(loaded_at) if loaded_at is not None else datetime.now(timezone.utc)
        patch = {
            "state": "loaded",
            "last_loaded_product": canonical,
            "last_loaded_at": stamp.isoformat(),
        }
        return await self._atomic_update(tenant_id, compartment_doc_id, patch)

    async def mark_cleaned(
        self,
        tenant_id: str,
        compartment_doc_id: str,
        *,
        cleaned_at: Optional[datetime] = None,
    ) -> CompartmentState:
        """Atomically record a Cleaning_Event completion.

        Validates: Requirement 7.1.3.

        On success the compartment's ``last_cleaned_at`` is stamped,
        ``state`` is reset to ``clean``, and ``last_loaded_product`` is
        cleared so the compatibility engine treats the compartment as
        empty on its next evaluation. ``last_loaded_at`` is preserved
        because compatibility rules consult it when deciding whether a
        :attr:`last_cleaned_at` is newer than the last load.

        Args:
            tenant_id: Owning tenant.
            compartment_doc_id: ES document id for the compartment.
            cleaned_at: Timestamp to record. Defaults to the current UTC
                time.

        Returns:
            The refreshed :class:`CompartmentState`.
        """

        self._require_tenant(tenant_id)
        self._require_doc_id(compartment_doc_id)

        stamp = _ensure_utc(cleaned_at) if cleaned_at is not None else datetime.now(timezone.utc)
        patch = {
            "state": "clean",
            "last_cleaned_at": stamp.isoformat(),
            # Explicitly clear so the compatibility engine does not
            # consult a stale product on the next evaluation.
            "last_loaded_product": None,
        }
        return await self._atomic_update(tenant_id, compartment_doc_id, patch)

    async def mark_needs_cleaning(
        self,
        tenant_id: str,
        compartment_doc_id: str,
    ) -> CompartmentState:
        """Atomically flag a compartment as requiring cleaning.

        Validates: Requirement 7.1.2 (state transition path).

        This helper is invoked by the cross-contamination guard (Task 6.5)
        when a ``requires_cleaning`` rule is triggered on a proposed
        assignment so the next compatibility check immediately blocks
        further loads until a Cleaning_Event is recorded.

        The method only mutates ``state``; ``last_loaded_*`` and
        ``last_cleaned_at`` are preserved so the audit trail remains
        intact.
        """

        self._require_tenant(tenant_id)
        self._require_doc_id(compartment_doc_id)

        patch = {"state": "needs_cleaning"}
        return await self._atomic_update(tenant_id, compartment_doc_id, patch)

    # ------------------------------------------------------------------
    # Internals — atomic update with OCC
    # ------------------------------------------------------------------

    async def _atomic_update(
        self,
        tenant_id: str,
        compartment_doc_id: str,
        patch: Dict[str, Any],
    ) -> CompartmentState:
        """Apply ``patch`` to a compartment doc using ``_seq_no`` OCC.

        The update loop follows this sequence:

            1. Fetch the current document with ``_seq_no`` and
               ``_primary_term`` via the raw ES client.
            2. Verify the document belongs to the caller's tenant (the
               guard fires **before** any mutation so an attacker cannot
               probe existence of cross-tenant records).
            3. Attempt the update with ``if_seq_no`` / ``if_primary_term``
               asserted against the values from step 1.
            4. On ``version_conflict`` (409) retry with jittered
               exponential backoff up to :attr:`MAX_OCC_RETRIES` times.
            5. Read back the document to return a refreshed model.

        ``refresh=True`` is passed to the update call so the read-back in
        step 5 sees the new values immediately — compartment updates are
        low-volume per tenant so the refresh cost is acceptable and the
        caller observes a consistent view.
        """

        if not patch:
            raise ValueError("patch must not be empty")
        invalid_fields = set(patch) - _STATE_FIELDS
        if invalid_fields:
            raise ValueError(
                f"patch contains non-state fields: {sorted(invalid_fields)}"
            )

        # The tenant guard fires BEFORE any mutation so an attacker cannot probe
        # the existence of another tenant's compartment by watching for a write.
        # Checked outside the transform because the transform may not run at all
        # (a missing document) and because raising from inside it would surface as
        # whatever the backend does with an exception mid-update.
        owner_holder: Dict[str, Any] = {}

        def _apply(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            owner_holder["tenant_id"] = current.get("tenant_id")
            if current.get("tenant_id") != tenant_id:
                # Signalled rather than raised so the guard's error is built
                # outside the transform; returning None leaves the document
                # untouched, which is the safe direction.
                owner_holder["cross_tenant"] = True
                return None
            # ``{"doc": patch}`` semantics: a shallow merge, with ``None`` values
            # permitted so :meth:`mark_cleaned` can clear ``last_loaded_product``.
            # Callers outside this module cannot construct a null-bearing patch
            # because the public methods control the allowed shape.
            merged = dict(current)
            merged.update(patch)
            return merged

        # One call replaces the read / assert-seq-no / write / retry-on-409 loop.
        # On Elasticsearch the facade still does exactly that; on Postgres it
        # takes a row lock, so a concurrent writer waits instead of colliding and
        # :class:`CompartmentStateConflictError` becomes unreachable. Reaching the
        # raw client here would also have kept this write on Elasticsearch after
        # the document plane moved — and this is the write that records
        # ``last_loaded_product``, which the cross-contamination guard reads.
        try:
            refreshed_source, applied = await self._es.atomic_update(
                self._index,
                compartment_doc_id,
                _apply,
                max_retries=self.MAX_OCC_RETRIES,
                backoff_base_seconds=self.OCC_BACKOFF_BASE_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — narrowed immediately below
            # The facade signals exhausted optimistic-concurrency retries with a
            # generic AppException. This repository's documented contract is
            # ``CompartmentStateConflictError``, which callers translate to a 409,
            # so it is preserved rather than leaked — a caller that starts seeing
            # a 503 where it handled a 409 would retry the wrong way.
            #
            # Unreachable on the Postgres backend: a row lock makes the concurrent
            # writer wait instead of colliding, so retries are never exhausted.
            if "concurrent modification" in str(exc):
                raise CompartmentStateConflictError(
                    tenant_id=tenant_id,
                    compartment_doc_id=compartment_doc_id,
                    attempts=self.MAX_OCC_RETRIES,
                ) from exc
            raise

        if refreshed_source is None:
            raise CompartmentNotFoundError(tenant_id, compartment_doc_id)
        if owner_holder.get("cross_tenant"):
            raise CrossTenantCompartmentAccessError(
                tenant_id=tenant_id,
                compartment_doc_id=compartment_doc_id,
                owning_tenant_id=owner_holder.get("tenant_id"),
            )

        # Postgres source of truth. ``truck_compartments`` was Elasticsearch-only
        # until the fuel-asset migration, so a recreated cluster silently erased
        # ``last_loaded_product`` — the history the cross-contamination guard reads
        # before allowing a product into a compartment. The FULL post-update
        # document is mirrored, not the patch, because a partial merge would leave
        # Postgres missing anything the update recomputed.
        await mirror_current_state_upsert(
            "truck_compartment",
            dict(refreshed_source),
            doc_id=compartment_doc_id,
        )

        state = _safe_state_load(refreshed_source)
        if state is None:
            # The source is too malformed to validate. Raise a not-found rather
            # than a ValidationError so the caller observes a clean error mode —
            # the write still persisted, and an out-of-band re-read can recover.
            raise CompartmentNotFoundError(tenant_id, compartment_doc_id)
        return state

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _fetch_source(self, compartment_doc_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the stored document for a read path, returning ``None`` if absent.

        Routed through the facade rather than ``es.client.get`` so the read
        follows the same backend as the write. A raw ``client.get`` here would
        have kept this read on Elasticsearch after the document plane moved to
        Postgres, and the value it returns is ``last_loaded_product`` — the
        history the cross-contamination guard checks before allowing a product
        into a compartment. A stale read there approves a contaminated load.

        ``get_document`` already maps a 404 to ``None``, so the not-found
        sentinel this used to need is gone with it.
        """

        return await self._es.get_document(self._index, compartment_doc_id)

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require_tenant(tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

    @staticmethod
    def _require_doc_id(compartment_doc_id: str) -> None:
        if (
            not isinstance(compartment_doc_id, str)
            or not compartment_doc_id.strip()
        ):
            raise ValueError("compartment_doc_id must be a non-empty string")


# The 404/409 sniffing helpers and the jittered-backoff calculator that used to
# live here are gone: the read-modify-write loop they served moved into
# ``ElasticsearchService.atomic_update``, which owns the retry policy for both
# backends. ``MAX_OCC_RETRIES`` and ``OCC_BACKOFF_BASE_SECONDS`` survive as the
# values this repository passes into it.


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _ensure_utc(stamp: datetime) -> datetime:
    """Return ``stamp`` converted to UTC, assuming UTC when naive.

    Callers that construct timestamps locally occasionally hand us naive
    datetimes; rather than reject those we treat them as UTC so the
    persisted value is always timezone-aware ISO-8601.
    """

    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _safe_state_load(source: Dict[str, Any]) -> Optional[CompartmentState]:
    """Construct a :class:`CompartmentState` from a raw source, logging on failure.

    Corrupt or legacy-shape source documents (e.g. a compartment that was
    written before Task 6.1 shipped and therefore lacks any of the four
    state fields) are coerced into a default ``clean`` state rather than
    raising a ValidationError — the whole point of the additive mapping
    is that pre-existing compartments keep working.
    """

    # Synthesize a minimal payload so pre-Task-6.1 documents that lack
    # ``state`` still yield a valid model.
    payload = {
        "compartment_id": source.get("compartment_id"),
        "truck_id": source.get("truck_id"),
        "tenant_id": source.get("tenant_id"),
        "state": source.get("state") or "clean",
        "last_loaded_product": source.get("last_loaded_product"),
        "last_loaded_at": source.get("last_loaded_at"),
        "last_cleaned_at": source.get("last_cleaned_at"),
    }
    try:
        return CompartmentState(**payload)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "CompartmentStateRepository: dropping truck_compartments doc "
            "that failed state validation (compartment_id=%s): %s",
            source.get("compartment_id"),
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Cleaning events (Task 6.2)
# ---------------------------------------------------------------------------


#: Permitted values for :attr:`CleaningEvent.method`. The three methods map to
#: the cleaning regimes called out by Requirement 7.1.4:
#:
#:     * ``flush``    — a rinse of the compartment with the next product so a
#:                      compatible product can take over (e.g. GASOLINE_REG
#:                      following GASOLINE_PREM).
#:     * ``purge``    — an inert-gas or vapor purge used when vapor residue
#:                      would otherwise contaminate the next load.
#:     * ``sanitize`` — a full wash + dry + certification, required before a
#:                      DEF load or when a compatibility rule flagged the
#:                      transition as ``requires_cleaning``.
CleaningMethod = Literal["flush", "purge", "sanitize"]


class CleaningEvent(BaseModel):
    """Record of a completed compartment cleaning.

    Produced by :class:`CleaningEventService` (Task 6.2) in response to
    ``POST /api/fuel/mvp/compartments/{compartment_id}/cleaning-events``
    (Task 6.3). Writing a CleaningEvent is what unblocks a compartment
    that the cross-contamination guard flagged as ``needs_cleaning`` —
    the service persists this record **and** resets the compartment's
    :class:`CompartmentState` to ``clean`` so the next Loading_Plan
    commit sees a fresh slate (Requirement 7.1.3).

    Field shapes mirror the ``compartment_cleaning_events`` ES mapping
    (Task 1.1) 1:1 so :meth:`model_dump` output can be indexed without
    transformation. ``evidence_refs`` carry :class:`FileStorageService`
    file_refs (photos of the cleaning, certification PDFs, etc.); the
    service re-validates each ref against the tenant before persisting.

    The model enforces:

        * non-empty tenant / compartment / actor identifiers
        * ``method`` restricted to flush | purge | sanitize
        * ``cleaned_at`` present and timezone-aware (naive datetimes are
          coerced to UTC so persistence is always ISO-8601 with an
          offset)
        * ``evidence_refs`` deduplicated in place while preserving input
          order
    """

    model_config = ConfigDict(extra="forbid")

    cleaning_event_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Stable, tenant-scoped identifier. The service mints a uuid4 "
            "when the caller omits one so the REST layer never has to."
        ),
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Owning tenant; the service re-asserts this on every write.",
    )
    compartment_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Compartment document id in the ``truck_compartments`` index. "
            "Composite form ``{truck_id}_{compartment_id}`` as used by the "
            "loading agent, matching the id the CompartmentStateRepository "
            "operates on."
        ),
    )
    truck_id: str = Field(
        ...,
        min_length=1,
        description="Parent truck identifier for downstream reporting.",
    )
    method: CleaningMethod = Field(
        ...,
        description="Cleaning regime applied. See :data:`CleaningMethod`.",
    )
    actor_id: str = Field(
        ...,
        min_length=1,
        description=(
            "User or driver id that recorded the cleaning — required for "
            "audit. Platforms that use service accounts still pass a "
            "deterministic principal here. DEPRECATED as a canonical "
            "reference: prefer the resolvable ``driver_id`` below. Retained "
            "as a free-text alias for backward compatibility (never removed; "
            "cross-module-entity-linkage Req 8.2)."
        ),
    )
    driver_id: Optional[str] = Field(
        default=None,
        description=(
            "Canonical, resolvable driver reference for the actor that "
            "performed the cleaning (cross-module-entity-linkage Req 8.2). "
            "Supersedes the free-text ``actor_id`` alias. Nullable/additive "
            "so legacy events without it remain valid; when present it is "
            "validated against the Drivers module at write time."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description="Free-text notes captured by the operator. Nullable.",
    )
    evidence_refs: List[str] = Field(
        default_factory=list,
        description=(
            "Optional File_Storage_Service refs (photos, certification "
            "PDFs). The service validates each ref against the tenant "
            "before persistence so cross-tenant refs are rejected."
        ),
    )
    cleaned_at: datetime = Field(
        ...,
        description=(
            "When the cleaning was completed. Timezone-aware; naive "
            "datetimes are coerced to UTC on ingest so the persisted "
            "value is always offset-qualified."
        ),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="Stamped by the service on write; echoed here for round-trip fidelity.",
    )
    created_at: Optional[datetime] = Field(
        default=None,
        description="Stamped by the service on write; echoed here for round-trip fidelity.",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator(
        "cleaning_event_id",
        "tenant_id",
        "compartment_id",
        "truck_id",
        "actor_id",
    )
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("notes", mode="before")
    @classmethod
    def _normalize_notes(cls, value: Any) -> Any:
        # Collapse empty/whitespace-only notes to ``None`` so the persisted
        # field is either meaningful text or null — downstream search does
        # not have to distinguish "" from missing.
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("driver_id", mode="before")
    @classmethod
    def _normalize_driver_id(cls, value: Any) -> Any:
        # Optional canonical reference: strip surrounding whitespace and
        # collapse blanks to ``None`` so an empty string is treated as
        # "absent" rather than a dangling reference (Req 8.2 / 6.1).
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _dedupe_evidence_refs(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            # Let Pydantic's type check raise with a clear message.
            return value
        seen: set[str] = set()
        out: List[Any] = []
        for entry in value:
            if isinstance(entry, str):
                cleaned = entry.strip()
                if not cleaned:
                    raise ValueError("evidence_refs must not contain blank entries")
                if cleaned in seen:
                    continue
                seen.add(cleaned)
                out.append(cleaned)
            else:
                out.append(entry)
        return out

    @field_validator("cleaned_at")
    @classmethod
    def _ensure_cleaned_at_utc(cls, value: datetime) -> datetime:
        return _ensure_utc(value)


# ---------------------------------------------------------------------------
# Cleaning event errors
# ---------------------------------------------------------------------------


class CleaningEventPersistenceError(Exception):
    """Raised when the cleaning-event write succeeds but the state reset fails.

    The service writes to ``compartment_cleaning_events`` first and then
    calls :meth:`CompartmentStateRepository.mark_cleaned` to reset the
    compartment's lifecycle state. If the state reset raises after the
    event has been indexed, callers see a
    :class:`CleaningEventPersistenceError` that carries the original
    exception so they can surface a coherent 5xx and queue a reconcile
    job rather than silently leaving the compartment in
    ``needs_cleaning``. The exception exposes ``cleaning_event_id`` so
    the caller can look up the persisted event and retry the reset
    step idempotently.
    """

    def __init__(
        self,
        tenant_id: str,
        compartment_id: str,
        cleaning_event_id: str,
        cause: BaseException,
    ) -> None:
        super().__init__(
            f"cleaning event {cleaning_event_id!r} persisted for compartment "
            f"{compartment_id!r} (tenant={tenant_id!r}) but the state reset "
            f"failed: {cause}"
        )
        self.tenant_id = tenant_id
        self.compartment_id = compartment_id
        self.cleaning_event_id = cleaning_event_id
        self.__cause__ = cause


# ---------------------------------------------------------------------------
# Cleaning event service
# ---------------------------------------------------------------------------


class CleaningEventService:
    """Record a Cleaning_Event and atomically reset the compartment state.

    Implements Task 6.2 / Requirement 7.1.3: writing a cleaning event to
    the ``compartment_cleaning_events`` ES index and then resetting the
    target compartment's lifecycle state (``state -> clean``,
    ``last_cleaned_at``, ``last_loaded_product -> None``) via
    :meth:`CompartmentStateRepository.mark_cleaned`.

    The service is deliberately thin:

        1. Validate the event (via :class:`CleaningEvent` construction).
        2. Optionally validate each ``evidence_refs`` entry belongs to the
           tenant using an injected :class:`FileStorageService` so the
           REST endpoint (Task 6.3) can rely on a single rejection path.
        3. Persist the event to ``compartment_cleaning_events``.
        4. Reset the compartment state (via the state repository), using
           ``cleaned_at`` as the reset timestamp so the persisted
           ``last_cleaned_at`` matches the event record.

    Dependencies are injected via the constructor so the service is
    trivially testable:

        * ``es_service`` — provides ``await index_document(...)``,
          matching :class:`services.elasticsearch_service.ElasticsearchService`.
        * ``state_repository`` — a :class:`CompartmentStateRepository`
          instance used for the final ``mark_cleaned`` call. The state
          repo is an explicit constructor arg rather than being built
          internally so a single repo can be shared with the loading
          agent and the REST endpoint without reconstructing ES clients.
        * ``file_storage`` (optional) — when supplied, every
          ``evidence_refs`` entry is validated via
          ``file_storage.validate_ref(tenant_id, ref)`` before the event
          is persisted; a cross-tenant ref raises ``PermissionError``.
          When omitted, evidence refs are accepted as-is (used by the
          compartment_loading_agent path where refs have already been
          validated upstream).
    """

    def __init__(
        self,
        es_service: Any,
        state_repository: CompartmentStateRepository,
        *,
        file_storage: Any = None,
        index_name: str = COMPARTMENT_CLEANING_EVENTS_INDEX,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        if state_repository is None:
            raise ValueError("state_repository must not be None")
        if not index_name:
            raise ValueError("index_name must not be empty")
        self._es = es_service
        self._state_repo = state_repository
        self._file_storage = file_storage
        self._index = index_name

    async def record(
        self,
        tenant_id: str,
        compartment_id: str,
        *,
        truck_id: str,
        method: str,
        actor_id: str,
        notes: Optional[str] = None,
        evidence_refs: Optional[Sequence[str]] = None,
        cleaned_at: Optional[datetime] = None,
        cleaning_event_id: Optional[str] = None,
        driver_id: Optional[str] = None,
    ) -> CleaningEvent:
        """Persist a Cleaning_Event and reset the compartment to ``clean``.

        Validates: Requirement 7.1.3.

        The argument list is kept explicit (rather than accepting a raw
        :class:`CleaningEvent`) so the REST router can pass its parsed
        request body through without another intermediate model — the
        service owns id minting and timestamp defaulting, which the
        endpoint should not replicate.

        Args:
            tenant_id: Owning tenant. Re-asserted on both the event and
                the state-reset call; cross-tenant evidence refs raise
                :class:`PermissionError`.
            compartment_id: ES document id for the compartment (composite
                ``{truck_id}_{compartment_id}`` form as used by the
                loading agent).
            truck_id: Parent truck id. Required because it is part of the
                ES mapping and downstream reports filter on it.
            method: Cleaning regime; must be one of flush | purge |
                sanitize.
            actor_id: User or service principal that recorded the
                cleaning.
            notes: Optional free-text notes.
            evidence_refs: Optional File_Storage_Service refs. Each is
                validated against the tenant when a file_storage
                dependency was injected.
            cleaned_at: Timestamp to record. Defaults to the current UTC
                time so the REST endpoint does not have to synthesize
                one server-side.
            cleaning_event_id: Optional caller-supplied event id. Useful
                for idempotent retries; the service otherwise mints a
                uuid4-derived id.
            driver_id: Optional canonical driver reference for the actor
                that performed the cleaning (cross-module-entity-linkage
                Req 8.2). Supersedes the free-text ``actor_id`` alias.
                Nullable/additive; callers validate it against the
                Drivers module before invoking this method.

        Returns:
            The validated :class:`CleaningEvent` with ``updated_at`` and
            ``created_at`` populated to reflect the stored document.

        Raises:
            ValueError: if any required argument is blank.
            ValidationError: if Pydantic validation fails.
            PermissionError: if an ``evidence_refs`` entry does not
                belong to ``tenant_id`` (only raised when a
                file_storage dependency was injected).
            CleaningEventPersistenceError: if the event was indexed but
                the subsequent state reset failed.
            CompartmentNotFoundError / CrossTenantCompartmentAccessError
            / CompartmentStateConflictError: propagated from
                :meth:`CompartmentStateRepository.mark_cleaned` **before**
                the event is persisted — the service fetches the state
                row first so a missing or foreign compartment never
                ends up with an orphan cleaning-event row.
        """

        # --- argument validation ------------------------------------------------
        self._require_string("tenant_id", tenant_id)
        self._require_string("compartment_id", compartment_id)
        self._require_string("truck_id", truck_id)
        self._require_string("actor_id", actor_id)
        self._require_string("method", method)

        # Default the timestamp up front so the event record and the
        # ``mark_cleaned`` state reset share a single canonical value.
        stamp = (
            _ensure_utc(cleaned_at)
            if cleaned_at is not None
            else datetime.now(timezone.utc)
        )

        # --- pre-flight: compartment must exist and belong to tenant ------------
        # Reading the state before writing means we fail fast on missing or
        # cross-tenant compartments without leaving an orphan cleaning-event
        # row. ``get`` returns ``None`` for either case (no existence leak
        # across tenants) so we translate to CompartmentNotFoundError to give
        # the REST layer a clean 404.
        existing_state = await self._state_repo.get(tenant_id, compartment_id)
        if existing_state is None:
            raise CompartmentNotFoundError(tenant_id, compartment_id)

        # --- build the event ----------------------------------------------------
        event_id = (cleaning_event_id or "").strip() or f"ce_{uuid4()}"
        normalized_refs = list(evidence_refs or [])
        self._validate_evidence_refs(tenant_id, normalized_refs, actor_id)

        now_iso = datetime.now(timezone.utc)
        event = CleaningEvent(
            cleaning_event_id=event_id,
            tenant_id=tenant_id,
            compartment_id=compartment_id,
            truck_id=truck_id,
            method=method,
            actor_id=actor_id,
            driver_id=driver_id,
            notes=notes,
            evidence_refs=normalized_refs,
            cleaned_at=stamp,
            created_at=now_iso,
            updated_at=now_iso,
        )

        # --- persist event ------------------------------------------------------
        document = event.model_dump(mode="json", exclude_none=False)
        await self._es.index_document(self._index, event.cleaning_event_id, document)

        # --- reset compartment state -------------------------------------------
        # Use the same ``cleaned_at`` we persisted on the event so the
        # compartment row and the event row carry identical timestamps
        # for auditability (Requirement 7.1.3 — the state reset IS the
        # downstream effect of the event).
        try:
            await self._state_repo.mark_cleaned(
                tenant_id,
                compartment_id,
                cleaned_at=stamp,
            )
        except Exception as exc:
            # The event is durable; surface a specific error so the
            # caller can retry the state reset idempotently without
            # writing a duplicate event.
            logger.error(
                "CleaningEventService.record: event %s persisted but state "
                "reset for compartment=%s (tenant=%s) failed: %s",
                event.cleaning_event_id,
                compartment_id,
                tenant_id,
                exc,
            )
            raise CleaningEventPersistenceError(
                tenant_id=tenant_id,
                compartment_id=compartment_id,
                cleaning_event_id=event.cleaning_event_id,
                cause=exc,
            ) from exc

        return event

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _validate_evidence_refs(
        self,
        tenant_id: str,
        refs: Iterable[str],
        actor_id: str,
    ) -> None:
        """Re-check every ``evidence_refs`` entry against the tenant prefix.

        Delegates to ``file_storage.validate_ref`` when a file storage
        dependency was injected; a cross-tenant ref surfaces as
        ``PermissionError`` from the storage service and propagates
        unchanged so middleware maps it to an HTTP 403.

        When no storage dependency is configured (unit tests, callers
        that already validated upstream) this is a no-op — the service
        still shapes/dedupes the list via the Pydantic validator.
        """

        if self._file_storage is None:
            return
        for ref in refs:
            if not isinstance(ref, str):
                continue
            # FileStorageService.validate_ref raises PermissionError on a
            # tenant-prefix mismatch; we deliberately let that bubble up.
            self._file_storage.validate_ref(tenant_id, ref, actor=actor_id)

    @staticmethod
    def _require_string(name: str, value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")


# ---------------------------------------------------------------------------
# Cross-contamination violations (Task 6.5 / Requirement 7.2.6)
# ---------------------------------------------------------------------------


#: Stable entity_type value used on the cross-contamination RiskSignal. The
#: Compartment_Loading_Agent publishes a RiskSignal with this ``entity_type``
#: whenever :func:`fuel.services.compatibility_matrix.check_compatibility`
#: returns a non-``allowed`` decision, so downstream consumers can filter on
#: a stable string without parsing the signal context.
CROSS_CONTAMINATION_VIOLATION_ENTITY_TYPE: str = "cross_contamination_violation"


class CrossContaminationViolation(BaseModel):
    """Audit record of a rejected compartment assignment.

    Written to the ``cross_contamination_events`` ES index by the
    Compartment_Loading_Agent (Task 6.5) whenever the compatibility rule
    engine blocks or gates a proposed assignment. Field shapes mirror the
    index mapping 1:1 (see
    :data:`fuel.services.fuel_ops_es_mappings.CROSS_CONTAMINATION_EVENTS_MAPPING`)
    so :meth:`model_dump` output can be indexed without transformation.

    ``decision`` and ``reason`` are the literals surfaced by
    :func:`check_compatibility`:

        * ``decision`` ∈ {``blocked``, ``requires_cleaning``}
        * ``reason`` ∈ {``cross_contamination_blocked``,
          ``cleaning_required``}

    ``governing_rule`` records the matrix rule value that drove the
    decision, preserving the same three-valued enum the engine uses
    internally.

    ``previous_product`` and ``attempted_product`` are canonicalized on
    construction so legacy NG aliases (``AGO``, ``PMS``, ``ATK``, ``LPG``)
    land as their US equivalents in the audit trail.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Stable, tenant-scoped identifier. Compartment_Loading_Agent "
            "mints a uuid4 when omitted so the agent callers never have "
            "to."
        ),
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Owning tenant; re-asserted on every write.",
    )
    compartment_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Compartment document id that the engine rejected. The agent "
            "passes the composite ``{truck_id}_{compartment_id}`` form "
            "used by ``configure_compartments``."
        ),
    )
    truck_id: str = Field(
        ...,
        min_length=1,
        description="Parent truck id for downstream reporting.",
    )
    previous_product: Optional[str] = Field(
        default=None,
        description=(
            "Canonical product_code of the compartment's last load, or "
            "``None`` when the compartment has never been loaded."
        ),
    )
    attempted_product: str = Field(
        ...,
        min_length=1,
        description="Canonical product_code that the agent tried to load.",
    )
    governing_rule: Literal["allowed", "blocked", "requires_cleaning"] = Field(
        ...,
        description=(
            "Rule value returned by the compatibility engine that drove "
            "the decision. ``allowed`` is still valid here so callers can "
            "record gated/downgrade transitions without surprising the "
            "ES mapping."
        ),
    )
    decision: Literal["blocked", "requires_cleaning"] = Field(
        ...,
        description=(
            "The engine's final decision. Only rejection decisions are "
            "persisted — allowed assignments never produce a violation."
        ),
    )
    reason: Literal["cross_contamination_blocked", "cleaning_required"] = Field(
        ...,
        description="Machine-readable reason code (Req 7.2.2 / 7.2.3).",
    )
    actor_id: Optional[str] = Field(
        default=None,
        description=(
            "Agent or user that surfaced the violation. The "
            "Compartment_Loading_Agent stamps its ``agent_id`` here for "
            "traceability."
        ),
    )
    plan_id: Optional[str] = Field(
        default=None,
        description="Loading_Plan that contained the rejected assignment.",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the engine returned the rejection.",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="Stamped by the agent on write; echoed here for round-trip fidelity.",
    )
    created_at: Optional[datetime] = Field(
        default=None,
        description="Stamped by the agent on write; echoed here for round-trip fidelity.",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator(
        "event_id",
        "tenant_id",
        "compartment_id",
        "truck_id",
        "attempted_product",
    )
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("previous_product", mode="before")
    @classmethod
    def _canonicalize_previous_product(cls, value: Any) -> Any:
        # Same shape as ``CompartmentState.last_loaded_product`` —
        # canonicalize on construction so the audit trail always carries
        # the US catalog code even when the caller passed a legacy alias.
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            return value
        return canonicalize(value)

    @field_validator("attempted_product", mode="before")
    @classmethod
    def _canonicalize_attempted_product(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return value
        return canonicalize(stripped)

    @field_validator("timestamp")
    @classmethod
    def _ensure_timestamp_utc(cls, value: datetime) -> datetime:
        return _ensure_utc(value)


__all__ = [
    "CompartmentLifecycleState",
    "CompartmentState",
    "CompartmentStateRepository",
    "CompartmentStateError",
    "CompartmentNotFoundError",
    "CrossTenantCompartmentAccessError",
    "CompartmentStateConflictError",
    "CleaningMethod",
    "CleaningEvent",
    "CleaningEventService",
    "CleaningEventPersistenceError",
    "CrossContaminationViolation",
    "CROSS_CONTAMINATION_VIOLATION_ENTITY_TYPE",
]
