"""
Integration connector ABC, SyncRun model, and tenant-scoped repository.

Capability 5 of the fuel-ops hardening spec (Requirements 5.1.1, 5.1.2,
5.1.4) introduces a pluggable integration framework so new third-party
providers (QuickBooks Online, Veeder-Root, Geotab, Stripe, …) can be
wired into the platform without rewriting core services. This module is
the shared foundation every per-provider adapter builds on:

* :class:`IntegrationConnector` — the abstract base class every adapter
  implements. Declares the four async operations required by Requirement
  5.1.1 (``connect``, ``sync_pull``, ``sync_push``, ``disconnect``) plus
  the two ``ClassVar`` descriptors (``category``, ``provider_name``) the
  provider catalog and the Integration_Scheduler need to route calls.

* :class:`IntegrationInstance` — a strict Pydantic model mirroring the
  ``integration_instances`` ES mapping (Task 1.1) 1:1 so a
  ``model_dump(mode="json")`` payload can be indexed directly without any
  translation layer. Holds per-tenant, per-provider configuration: the
  enabled flag, the Tenant_Credentials_Vault reference, the cron
  schedule, and rolling health fields (``status``, ``last_sync_at``,
  ``last_error``, ``retry_count``) that the scheduler maintains
  (Requirements 5.1.2, 5.1.5, 5.1.6).

* :class:`SyncRun` — a strict Pydantic model mirroring the
  ``integration_sync_runs`` ES mapping 1:1, recording a single
  ``sync_pull`` / ``sync_push`` execution with its status, record counts,
  and error details (Requirement 5.1.4).

* :class:`IntegrationInstanceRepository` — tenant-scoped async CRUD
  against the ``integration_instances`` index. Mirrors the
  :class:`fuel.depot_models.DepotRepository` pattern so every new domain
  repository in this spec exposes the same five-method surface
  (``create``, ``get``, ``list_for_tenant``, ``update``, ``delete``) with
  identical tenant-isolation semantics:

    - Cross-tenant reads degrade silently to ``None`` so the REST layer
      translates them into a uniform HTTP 404 — returning 403 would leak
      existence of integrations owned by other tenants.
    - Cross-tenant writes / deletes raise
      :class:`CrossTenantAccessError` (a :class:`PermissionError`
      subclass) so middleware maps them to HTTP 403.
    - Records that fail Pydantic validation (schema drift) are logged
      and dropped rather than raising, so a single corrupt document
      never takes down the whole list endpoint.
    - Credentials themselves NEVER flow through this repository; only
      the opaque ``credentials_ref`` pointer into the
      Tenant_Credentials_Vault is stored and returned (Requirement
      5.1.8). Subclasses of :class:`IntegrationConnector` are
      responsible for dereferencing the pointer through the vault at
      call time.

The repository never touches the ``integration_sync_runs`` index — that
index is owned by the Integration_Scheduler (Task 9.2). A sibling
``SyncRunRepository`` can be introduced later without disturbing this
module.

Validates: Requirements 5.1.1, 5.1.2, 5.1.4.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone as _dt_timezone
from typing import Any, ClassVar, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fuel.services.fuel_ops_es_mappings import INTEGRATION_INSTANCES_INDEX

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public type aliases — match the ES mapping enumerations exactly so a
# ``model_dump()`` payload validates against the strict-dynamic index.
# ---------------------------------------------------------------------------


#: Coarse grouping used by the provider catalog and the Integration_
#: Scheduler to pick the right dispatch path. Values match Requirement
#: 5.1.1 verbatim.
IntegrationCategory = Literal[
    "accounting",
    "tank_monitor",
    "gps_eld",
    "payment",
    "tms",
    "terminal_pricing",
]

#: Rolling health status for a configured Integration_Instance.
#: "connected" is the happy path; "disconnected" is the clean-shutdown
#: state after an explicit disconnect call; "error" is set by the
#: Integration_Scheduler once ``max_retries`` is exhausted
#: (Requirement 5.1.6). "pending" lets the UI flag a freshly created
#: instance whose ``connect()`` call has not yet succeeded.
IntegrationStatus = Literal["pending", "connected", "disconnected", "error"]

#: Direction of a Sync_Run — matches Requirement 5.1.4.
SyncOperation = Literal["pull", "push"]

#: Terminal status for a completed Sync_Run. ``running`` is the
#: transient state the scheduler persists before awaiting the connector;
#: the four terminal states match Requirement 5.1.4 exactly.
SyncStatus = Literal["running", "success", "partial", "error"]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class IntegrationInstance(BaseModel):
    """A per-tenant, per-provider configured connector.

    Field shapes mirror the ``integration_instances`` ES mapping defined
    in :mod:`fuel.services.fuel_ops_es_mappings` (Task 1.1) so
    ``model_dump(mode="json")`` produces a valid indexing payload with
    no transformation. The ``credentials_ref`` is an opaque pointer into
    :class:`services.credentials_vault.TenantCredentialsVault`;
    plaintext credentials never appear on this model (Requirement
    5.1.8).
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Stable, tenant-scoped identifier. If omitted at write time "
            "the repository mints one derived from uuid4 — the model "
            "itself still requires one here so round-tripped records are "
            "lossless."
        ),
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Owning tenant; repositories re-assert this on every read.",
    )
    provider_name: str = Field(
        ...,
        min_length=1,
        description=(
            "Short provider identifier (``quickbooks_online``, "
            "``veeder_root``, …) matching the owning connector's "
            "``provider_name`` ClassVar."
        ),
    )
    category: IntegrationCategory = Field(
        ...,
        description=(
            "Coarse grouping used by the provider catalog and the "
            "Integration_Scheduler to dispatch calls."
        ),
    )
    status: IntegrationStatus = Field(
        default="pending",
        description=(
            "Rolling health status. Set to ``connected`` after a "
            "successful ``connect()``, ``error`` after max retries are "
            "exhausted, and ``disconnected`` by an explicit disconnect."
        ),
    )
    enabled: bool = Field(
        default=False,
        description=(
            "When false the Integration_Scheduler skips this instance "
            "entirely; manual sync-now calls also 400 with "
            "``instance_disabled``."
        ),
    )
    credentials_ref: Optional[str] = Field(
        default=None,
        description=(
            "Opaque Tenant_Credentials_Vault reference. Never a secret "
            "— dereferenced by the connector at call time."
        ),
    )
    schedule_cron: Optional[str] = Field(
        default=None,
        description=(
            "Standard 5-field cron expression consumed by APScheduler. "
            "Validation of cron semantics is deferred to the scheduler "
            "so this module has no dependency on APScheduler."
        ),
    )
    config: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-instance non-secret configuration (realm ids, "
            "database names, opt-in toggles). Secrets MUST live in the "
            "Tenant_Credentials_Vault."
        ),
    )
    last_sync_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp of the most recent terminal Sync_Run.",
    )
    last_error: Optional[str] = Field(
        default=None,
        description=(
            "Human-readable summary of the most recent failure. The "
            "full traceback lives on the owning Sync_Run record."
        ),
    )
    retry_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Consecutive failed Sync_Runs since the last success. The "
            "scheduler resets this to 0 after a successful run and "
            "marks ``status=error`` once it reaches ``max_retries``."
        ),
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="Last-modification timestamp written by the repository.",
    )
    created_at: Optional[datetime] = Field(
        default=None,
        description="Creation timestamp written by the repository.",
    )

    @field_validator(
        "instance_id",
        "tenant_id",
        "provider_name",
        mode="before",
    )
    @classmethod
    def _strip_required_strings(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("credentials_ref", "schedule_cron", "last_error", mode="before")
    @classmethod
    def _normalize_optional_strings(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None


class SyncRun(BaseModel):
    """A single execution of a connector's ``sync_pull`` or ``sync_push``.

    Field shapes mirror the ``integration_sync_runs`` ES mapping 1:1.
    Persistence is owned by the Integration_Scheduler (Task 9.2); this
    module only provides the model so connectors can construct it and
    return it from their ``sync_pull`` / ``sync_push`` implementations
    as mandated by Requirement 5.1.1.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    instance_id: str = Field(..., min_length=1)
    provider_name: str = Field(..., min_length=1)
    operation: SyncOperation = Field(...)
    started_at: datetime = Field(...)
    finished_at: Optional[datetime] = Field(default=None)
    status: SyncStatus = Field(default="running")
    record_counts: Dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-entity counts captured by the connector (e.g. "
            "``{'invoices': 12, 'payments': 3}``). Values MUST be "
            "non-negative integers."
        ),
    )
    error_details: Optional[str] = Field(default=None)
    duration_ms: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Wall-clock duration of the run in milliseconds. Optional "
            "so partially-completed runs can be persisted before the "
            "clock is stopped."
        ),
    )

    @field_validator(
        "run_id",
        "tenant_id",
        "instance_id",
        "provider_name",
        mode="before",
    )
    @classmethod
    def _strip_required_strings(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("error_details", mode="before")
    @classmethod
    def _normalize_error_details(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    @field_validator("record_counts", mode="before")
    @classmethod
    def _validate_record_counts(cls, value: Any) -> Dict[str, int]:
        """Reject negative counts, non-integer values, and booleans.

        Pydantic's type hint constrains values to ``int`` but would
        happily coerce a ``bool`` into 0/1 (since ``bool`` is an
        ``int`` subclass in Python). We run before coercion so the
        Integration_Scheduler never persists a nonsensical record like
        ``{"invoices": True}``.
        """

        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("record_counts must be a dict")
        for key, count in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"record_counts keys must be strings, got {type(key).__name__}"
                )
            if isinstance(count, bool) or not isinstance(count, int):
                raise ValueError(
                    f"record_counts[{key!r}] must be an int, got "
                    f"{type(count).__name__}"
                )
            if count < 0:
                raise ValueError(
                    f"record_counts[{key!r}] must be non-negative, got {count}"
                )
        return value


class ConnectionResult(BaseModel):
    """Return shape of :meth:`IntegrationConnector.connect`.

    Connectors report whether their credentials were accepted, the
    opaque ``credentials_ref`` they stored into the
    Tenant_Credentials_Vault (so the repository can persist it), and
    any non-secret metadata the UI needs (e.g. the QBO ``realm_id`` or
    the Geotab ``database``). Secrets MUST NOT appear on this object —
    Requirement 5.1.8 forbids exposing credentials through any API
    surface.
    """

    model_config = ConfigDict(extra="forbid")

    status: IntegrationStatus = Field(default="connected")
    credentials_ref: Optional[str] = Field(default=None)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = Field(default=None)

    @field_validator("credentials_ref", "message", mode="before")
    @classmethod
    def _normalize_optional_strings(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CrossTenantAccessError(PermissionError):
    """Raised when a write/delete targets an instance owned by another tenant.

    Subclass of :class:`PermissionError` so middleware that maps
    exceptions to HTTP 403 does the right thing automatically. Reads
    degrade silently to ``None`` instead of raising because a 404 is
    the appropriate response for a missing-or-not-owned document —
    returning 403 would leak existence.
    """

    def __init__(
        self,
        tenant_id: str,
        instance_id: str,
        owning_tenant_id: Optional[str] = None,
    ) -> None:
        message = (
            f"integration instance {instance_id!r} does not belong to "
            f"tenant {tenant_id!r}"
        )
        super().__init__(message)
        self.tenant_id = tenant_id
        self.instance_id = instance_id
        self.owning_tenant_id = owning_tenant_id


# ---------------------------------------------------------------------------
# Abstract connector
# ---------------------------------------------------------------------------


class IntegrationConnector(ABC):
    """Abstract base class for every third-party integration adapter.

    Concrete adapters (QuickBooks Online, Veeder-Root, Geotab, Stripe,
    …) subclass this class and implement the four async operations
    below. The ABC owns nothing more than the contract — every adapter
    is free to manage its own HTTP client, session, and credential
    caching internally. This keeps the ABC stable across provider-
    specific quirks (OAuth refresh rotation, TLS-401 TCP sessions,
    signed webhooks, …).

    Subclasses MUST override both :data:`category` and
    :data:`provider_name` as :class:`ClassVar` strings — the
    Integration_Scheduler and the provider catalog key off these at
    call time. The base class enforces the override in
    :meth:`__init_subclass__` so a missing or blank value surfaces at
    import time, not at runtime.
    """

    #: Coarse category used for dispatch and UI grouping. Subclasses
    #: MUST override with one of :data:`IntegrationCategory`.
    category: ClassVar[str] = ""

    #: Short provider identifier (``quickbooks_online``, ``veeder_root``,
    #: …). Subclasses MUST override with a non-empty lowercase snake
    #: case string.
    provider_name: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Validate ClassVar overrides when a concrete subclass is defined.

        Abstract intermediate classes — those that still have one or
        more methods marked ``@abstractmethod`` — are skipped so a
        multi-layer hierarchy (e.g. a shared ``OAuthConnectorBase``
        mixin) can declare abstract methods of its own without tripping
        this check.

        Note: we cannot rely on ``cls.__abstractmethods__`` here because
        :class:`ABCMeta` sets that attribute *after* ``__init_subclass__``
        runs. Instead we inspect the class (and its bases) for any
        attributes still flagged with ``__isabstractmethod__``.
        """

        super().__init_subclass__(**kwargs)

        # Walk the class attributes (including inherited ones) and skip
        # if any is still flagged as abstract. Concrete subclasses will
        # have overridden every abstract method defined on the base.
        for attr_name in dir(cls):
            try:
                value = getattr(cls, attr_name, None)
            except Exception:  # pragma: no cover - defensive
                continue
            if getattr(value, "__isabstractmethod__", False):
                return

        category_value = getattr(cls, "category", "")
        provider_value = getattr(cls, "provider_name", "")
        if not isinstance(category_value, str) or not category_value.strip():
            raise TypeError(
                f"{cls.__name__} must override ``category`` with a "
                "non-empty string (one of accounting, tank_monitor, "
                "gps_eld, payment, tms, terminal_pricing)."
            )
        if not isinstance(provider_value, str) or not provider_value.strip():
            raise TypeError(
                f"{cls.__name__} must override ``provider_name`` with "
                "a non-empty identifier string."
            )

    # ------------------------------------------------------------------
    # Abstract API
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(self, credentials: Dict[str, Any]) -> ConnectionResult:
        """Validate credentials and establish the integration.

        Implementations SHOULD:
            1. Exchange the supplied credentials with the provider (OAuth
               token exchange, TLS-401 session setup, Stripe API-key
               probe, …).
            2. Persist the long-lived secret material into the
               Tenant_Credentials_Vault and return its opaque
               ``credentials_ref``.
            3. Return a :class:`ConnectionResult` with
               ``status="connected"`` on success. On failure they may
               return ``status="error"`` with a ``message`` or raise —
               either is tolerated by the REST layer.

        ``credentials`` is a plaintext dict passed straight through from
        the REST handler. Implementations MUST NOT log its values.
        """

    @abstractmethod
    async def sync_pull(self, since: datetime) -> SyncRun:
        """Pull any records updated since ``since`` from the provider.

        Implementations return a :class:`SyncRun` with its terminal
        status, record counts, and (on failure) ``error_details`` set.
        The Integration_Scheduler persists the returned row to the
        ``integration_sync_runs`` ES index and updates the owning
        :class:`IntegrationInstance`'s ``last_sync_at`` and
        ``retry_count`` accordingly.
        """

    @abstractmethod
    async def sync_push(self, payload: Dict[str, Any]) -> SyncRun:
        """Push a payload (invoice, payment intent, …) to the provider.

        ``payload`` is provider-specific — the overlay agent that
        triggered the push (e.g. the POD finalization flow) is expected
        to have shaped it. Implementations return a :class:`SyncRun`
        the same way :meth:`sync_pull` does so the scheduler can
        persist it uniformly.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear down the integration gracefully.

        Implementations SHOULD revoke any long-lived tokens, close
        sockets, and release vault references. They MUST be idempotent
        — calling disconnect on an already-disconnected instance is a
        no-op.
        """


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


#: Fields the repository refuses to overwrite via ``update()``. The
#: primary key, the tenant id, the provider name, the category, and
#: the creation timestamp are all immutable once the instance has been
#: created — re-wiring them would effectively create a new instance and
#: is better handled by a delete + create.
_UPDATE_IMMUTABLE_FIELDS = frozenset(
    {
        "instance_id",
        "tenant_id",
        "provider_name",
        "category",
        "created_at",
    }
)


class IntegrationInstanceRepository:
    """Tenant-scoped CRUD repository for the ``integration_instances`` ES index.

    Dependencies are injected via the constructor so the repository is
    trivially testable with a recording mock. The only interface the
    repository relies on is:

        * ``await es.index_document(index, doc_id, document)``
        * ``await es.search_documents(index, query, size)``
        * ``await es.update_document(index, doc_id, partial_doc)``
        * ``await es.delete_document(index, doc_id)`` → ``bool``

    which matches :class:`services.elasticsearch_service.ElasticsearchService`
    exactly. The repository intentionally does not depend on
    ``get_document`` because that method raises on 404; a search-by-id
    is used instead so missing records surface as empty hits.

    Tenant isolation is enforced at two points for defense-in-depth:
        1. Every ES query includes a ``term`` clause on ``tenant_id``.
        2. Every returned document is re-validated against the
           caller's ``tenant_id`` before it crosses the repository
           boundary, so a mis-labelled document never leaks across
           tenants.
    """

    #: Default per-query cap for ``list_for_tenant``. Callers that
    #: legitimately need more pass ``size=`` explicitly; this prevents
    #: accidental full-cluster scans from a misconfigured page size.
    DEFAULT_LIST_SIZE: int = 500

    def __init__(
        self,
        es_service: Any,
        *,
        index_name: str = INTEGRATION_INSTANCES_INDEX,
    ) -> None:
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
        instance: IntegrationInstance | Dict[str, Any],
    ) -> IntegrationInstance:
        """Persist a new :class:`IntegrationInstance` and return the stored model.

        The repository enforces that the record's ``tenant_id`` matches
        the caller's ``tenant_id``. If the caller passes a dict without
        ``tenant_id`` the repository stamps it from the argument. If
        the caller passes a model / dict whose ``tenant_id`` differs,
        the repository raises :class:`CrossTenantAccessError`.

        Args:
            tenant_id: Owning tenant. Required, non-empty.
            instance: Either an :class:`IntegrationInstance` or a raw
                dict that can be coerced into one.

        Returns:
            The persisted :class:`IntegrationInstance` including the
            auto-generated ``instance_id`` (if omitted), ``updated_at``,
            and ``created_at`` timestamps.
        """

        self._require_tenant(tenant_id)

        payload = self._coerce_to_dict(instance)
        # Fill in tenant_id from the argument if the caller left it
        # blank; reject cross-tenant payloads outright.
        payload.setdefault("tenant_id", tenant_id)
        if payload["tenant_id"] != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                instance_id=str(payload.get("instance_id", "<new>")),
                owning_tenant_id=payload["tenant_id"],
            )

        # Mint a uuid4 id when one isn't supplied so callers posting
        # from a UI don't have to generate one themselves.
        payload.setdefault("instance_id", f"integration_{uuid4()}")

        # Stamp bookkeeping timestamps. Use explicit None checks instead
        # of setdefault because ``model_dump()`` may pass
        # ``created_at=None`` for freshly-minted models.
        now = _utcnow_iso()
        if not payload.get("created_at"):
            payload["created_at"] = now
        payload["updated_at"] = now

        # Validate the full shape before touching ES so validation
        # errors bubble up as ValidationError rather than an ES mapping
        # failure.
        model = IntegrationInstance(**payload)

        doc = model.model_dump(mode="json", exclude_none=False)
        await self._es.index_document(self._index, model.instance_id, doc)

        return model

    # ------------------------------------------------------------------
    # Read (single + list)
    # ------------------------------------------------------------------

    async def get(
        self, tenant_id: str, instance_id: str
    ) -> Optional[IntegrationInstance]:
        """Return the instance or ``None`` if it does not exist / is not owned.

        A cross-tenant fetch returns ``None`` rather than raising so
        the REST layer can translate the response into a uniform HTTP
        404 — returning 403 would leak existence of instances owned
        by other tenants.
        """

        self._require_tenant(tenant_id)
        if not instance_id or not instance_id.strip():
            raise ValueError("instance_id must be a non-empty string")

        source = await self._fetch_source(instance_id)
        if source is None:
            return None
        if source.get("tenant_id") != tenant_id:
            logger.info(
                "IntegrationInstanceRepository.get: suppressing "
                "cross-tenant hit for instance=%s (owner=%s, requester=%s)",
                instance_id,
                source.get("tenant_id"),
                tenant_id,
            )
            return None
        return _safe_model_load(source)

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        provider_name: Optional[str] = None,
        category: Optional[IntegrationCategory] = None,
        enabled: Optional[bool] = None,
        status: Optional[IntegrationStatus] = None,
        size: int = DEFAULT_LIST_SIZE,
    ) -> List[IntegrationInstance]:
        """List integration instances for the tenant with optional filters.

        Filters are ANDed together at the ES query layer, then the
        returned documents are re-validated against the caller's
        ``tenant_id`` so a mis-labelled record never crosses the
        repository boundary. Records that fail Pydantic validation
        (because the source schema drifted) are logged and dropped
        rather than raising, so a single corrupt record does not take
        out the whole list endpoint.
        """

        self._require_tenant(tenant_id)
        if size <= 0:
            raise ValueError("size must be a positive integer")

        must: List[Dict[str, Any]] = [{"term": {"tenant_id": tenant_id}}]
        if provider_name:
            must.append({"term": {"provider_name": provider_name}})
        if category is not None:
            must.append({"term": {"category": category}})
        if enabled is not None:
            must.append({"term": {"enabled": enabled}})
        if status is not None:
            must.append({"term": {"status": status}})

        query = {
            "query": {"bool": {"must": must}},
            "size": size,
        }

        resp = await self._es.search_documents(self._index, query, size)
        sources = _extract_sources(resp)

        out: List[IntegrationInstance] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "IntegrationInstanceRepository.list_for_tenant: "
                    "dropping integration_instances doc with mismatched "
                    "tenant_id %s (expected %s)",
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
        instance_id: str,
        patch: Dict[str, Any],
    ) -> Optional[IntegrationInstance]:
        """Apply a partial update and return the refreshed model.

        The tenant guard runs **before** any ES write so an attacker
        cannot use an ``update`` to exfiltrate another tenant's record
        by probing existence. If the record does not exist this method
        returns ``None`` (→ HTTP 404). If it exists but belongs to a
        different tenant, :class:`CrossTenantAccessError` is raised
        (→ HTTP 403 through the middleware).

        Immutable fields (``instance_id``, ``tenant_id``,
        ``provider_name``, ``category``, ``created_at``) are stripped
        from the patch before it is applied. Attempting to mutate them
        is silently ignored — we deliberately do not raise because
        clients frequently re-post the full model on update and
        rejecting the request for a no-op field would be user-hostile.
        """

        self._require_tenant(tenant_id)
        if not instance_id or not instance_id.strip():
            raise ValueError("instance_id must be a non-empty string")
        if not isinstance(patch, dict):
            raise TypeError(
                f"patch must be a dict, got {type(patch).__name__}"
            )

        source = await self._fetch_source(instance_id)
        if source is None:
            return None
        owner = source.get("tenant_id")
        if owner != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                instance_id=instance_id,
                owning_tenant_id=owner,
            )

        clean_patch = {
            k: v for k, v in patch.items() if k not in _UPDATE_IMMUTABLE_FIELDS
        }
        if not clean_patch:
            return _safe_model_load(source)

        # Merge, then re-validate through the Pydantic model so we
        # never persist a payload that would have failed validation on
        # create.
        merged = {**source, **clean_patch}
        merged["updated_at"] = _utcnow_iso()
        validated = IntegrationInstance(**merged)

        # Persist only the delta — ES _update merges into the live doc.
        delta_keys = set(clean_patch.keys()) | {"updated_at"}
        partial = validated.model_dump(mode="json", include=delta_keys)
        await self._es.update_document(self._index, instance_id, partial)

        return validated

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, tenant_id: str, instance_id: str) -> bool:
        """Delete an instance. Returns ``True`` if the row was removed.

        Semantics:
            * Not-found → ``False`` (callers translate to HTTP 404).
            * Cross-tenant → :class:`CrossTenantAccessError`
              (→ HTTP 403).
            * Owned + deleted → ``True`` (→ HTTP 204).
        """

        self._require_tenant(tenant_id)
        if not instance_id or not instance_id.strip():
            raise ValueError("instance_id must be a non-empty string")

        source = await self._fetch_source(instance_id)
        if source is None:
            return False
        owner = source.get("tenant_id")
        if owner != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                instance_id=instance_id,
                owning_tenant_id=owner,
            )

        return bool(await self._es.delete_document(self._index, instance_id))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _require_tenant(tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

    @staticmethod
    def _coerce_to_dict(
        instance: IntegrationInstance | Dict[str, Any],
    ) -> Dict[str, Any]:
        if isinstance(instance, IntegrationInstance):
            return instance.model_dump(mode="python")
        if isinstance(instance, dict):
            # Shallow copy so the caller's dict is never mutated.
            return dict(instance)
        raise TypeError(
            "instance must be an IntegrationInstance or dict, got "
            f"{type(instance).__name__}"
        )

    async def _fetch_source(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """Return the raw ``_source`` or ``None`` if the document is missing.

        Uses a search-by-id rather than a direct ``get_document`` call
        because the ES service's ``get_document`` raises on 404. A
        search returns empty hits cleanly, which matches repository
        semantics.
        """

        query = {
            "query": {"term": {"instance_id": instance_id}},
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(self._index, query, 1)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "IntegrationInstanceRepository._fetch_source: search "
                "failed for instance=%s: %s",
                instance_id,
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
    shape and ``None`` so the helper is robust across the variety of
    mock shapes used by tests.
    """

    if not resp:
        return []
    hits_outer = resp.get("hits") if isinstance(resp, dict) else None
    if not hits_outer:
        return []
    hits = hits_outer.get("hits") or []
    out: List[Dict[str, Any]] = []
    for hit in hits:
        if isinstance(hit, dict) and isinstance(hit.get("_source"), dict):
            out.append(hit["_source"])
    return out


def _safe_model_load(source: Dict[str, Any]) -> Optional[IntegrationInstance]:
    """Build an :class:`IntegrationInstance` from a raw ES source.

    A source document that fails Pydantic validation is logged at
    warning level and dropped so a single corrupt record does not kill
    an entire list response. This matches the pattern used by
    :class:`fuel.depot_models.DepotRepository`.
    """

    try:
        return IntegrationInstance(**source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "IntegrationInstanceRepository: dropping integration_instances "
            "doc that failed model validation (instance_id=%s): %s",
            source.get("instance_id"),
            exc,
        )
        return None


__all__ = [
    "ConnectionResult",
    "CrossTenantAccessError",
    "IntegrationCategory",
    "IntegrationConnector",
    "IntegrationInstance",
    "IntegrationInstanceRepository",
    "IntegrationStatus",
    "SyncOperation",
    "SyncRun",
    "SyncStatus",
]
