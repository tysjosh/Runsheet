"""
REST endpoints for the Integration Layer (Capability 5 / Task 9.3).

Exposes tenant-scoped HTTP routes under ``/api/integrations`` so admin
UIs (:route:`/admin/integrations`, Req 5.6.1) can list, create, update,
enable/disable, and sync the tenant's configured :class:`IntegrationInstance`
records alongside a read-only provider catalog.

Routes mounted on :data:`router`:

* ``GET    /api/integrations``
  — paginated list of the caller's :class:`IntegrationInstance` records
  filtered by ``provider_name``, ``category``, ``enabled``, ``status``.
  Credentials are NEVER returned; only the opaque ``credentials_ref``
  pointer is exposed alongside a coarse ``credentials_status`` of
  ``valid`` / ``missing`` (Req 5.1.8).

* ``POST   /api/integrations``
  — create a new :class:`IntegrationInstance`. When the request body
  carries a ``credentials`` dict, the router forwards it to the
  :class:`services.credentials_vault.TenantCredentialsVault`, persists
  only the returned ``credentials_ref``, and discards the plaintext
  immediately so the secret never returns to the response payload.

* ``PATCH  /api/integrations/{instance_id}``
  — partial update of a single :class:`IntegrationInstance`. Immutable
  fields (``instance_id``, ``tenant_id``, ``provider_name``,
  ``category``, ``created_at``) are stripped before reaching the
  repository; attempts to send a ``credentials`` dict are handled the
  same way as on POST (re-wrapped through the vault, stored as a
  ``credentials_ref``, plaintext never surfaced).

* ``DELETE /api/integrations/{instance_id}``
  — remove a single instance. When an :class:`IntegrationScheduler` is
  wired, the router also unschedules the instance so the next APScheduler
  tick does not attempt to reload a deleted record.

* ``POST   /api/integrations/{instance_id}/enable``
  — flip ``enabled=True`` and (when a scheduler is wired) register a
  cron job through :meth:`IntegrationScheduler.schedule_instance`.

* ``POST   /api/integrations/{instance_id}/disable``
  — flip ``enabled=False`` and unschedule.

* ``POST   /api/integrations/{instance_id}/sync-now``
  — invoke :meth:`IntegrationScheduler.sync_now` to trigger an immediate
  :class:`SyncRun` outside the cron schedule, returning the terminal
  run record.

* ``GET    /api/integrations/{instance_id}/sync-runs``
  — return the most-recent :class:`SyncRun` records for the given
  instance from the ``integration_sync_runs`` ES index. Capped at
  ``limit=10`` by default (Req 5.6.4) with a ceiling of 50.

* ``GET    /api/integrations/providers``
  — return the platform's catalog of available providers registered
  by the per-provider adapters (Tasks 9.4–9.10) via
  :mod:`integrations.provider_catalog`.

Tenant isolation is enforced at two layers:

    1. Every repository call takes the tenant_id from the verified
       :class:`TenantContext` (JWT-only — query-parameter and
       header-supplied tenant ids are ignored by the tenant guard).
    2. Every returned record is re-filtered against the caller's
       tenant_id before it leaves the router, as a defensive safety net
       in case an ES source document has drifted from the strict
       mapping.

Validates: Requirements 5.1.7, 5.1.8, 5.6.2, 5.6.6.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fuel.services.fuel_ops_es_mappings import INTEGRATION_SYNC_RUNS_INDEX
from integrations.connector_base import (
    CrossTenantAccessError,
    IntegrationCategory,
    IntegrationInstance,
    IntegrationInstanceRepository,
    IntegrationStatus,
    SyncRun,
)
from integrations.integration_scheduler import IntegrationScheduler
from integrations.provider_catalog import (
    ProviderCatalogEntry,
    list_providers as list_catalog_providers,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

# Auth policy parity with the other fuel-ops routers. Every handler
# depends on :func:`get_tenant_context`, which rejects unauthenticated
# requests in every environment.
ROUTER_AUTH_POLICY = "jwt_required"


# ---------------------------------------------------------------------------
# Module-level service wiring (same pattern as fuel_ops_endpoints)
# ---------------------------------------------------------------------------

_repository: Optional[IntegrationInstanceRepository] = None
_scheduler: Optional[IntegrationScheduler] = None
_credentials_vault: Any = None
_es_service: Any = None


def configure_integrations_endpoints(
    *,
    repository: IntegrationInstanceRepository,
    scheduler: Optional[IntegrationScheduler] = None,
    credentials_vault: Any = None,
    es_service: Any = None,
) -> None:
    """Wire service dependencies into the integrations REST module.

    Called once during application startup (from the integration
    bootstrap; see :mod:`bootstrap.integrations` once wired). Tests
    inject a fake :class:`IntegrationInstanceRepository`, a recording
    :class:`IntegrationScheduler` stub, and a mock
    :class:`TenantCredentialsVault` so the router can be exercised
    without ES, APScheduler, or AWS KMS.

    Args:
        repository: Tenant-scoped :class:`IntegrationInstanceRepository`
            responsible for CRUD against the ``integration_instances``
            ES index.
        scheduler: Optional :class:`IntegrationScheduler`. When ``None``
            the enable / disable / sync-now endpoints degrade gracefully
            — enable / disable still flip the persisted flag, and
            sync-now returns HTTP 503 ``scheduler_unavailable``. Production
            bootstrap always injects a real scheduler.
        credentials_vault: Optional :class:`TenantCredentialsVault`.
            When ``None`` the create / update endpoints reject payloads
            carrying a ``credentials`` field with HTTP 503
            ``credentials_vault_unavailable`` so secrets never end up
            persisted in plaintext by accident.
        es_service: Optional ElasticsearchService used by the
            ``sync-runs`` endpoint to query the
            ``integration_sync_runs`` index. When ``None`` the endpoint
            returns an empty list (the repository does not own that
            index — see :mod:`integration_scheduler` for the writer).

    Validates: Requirements 5.1.7, 5.1.8.
    """

    global _repository, _scheduler, _credentials_vault, _es_service
    if repository is None:
        raise ValueError("repository must not be None")
    _repository = repository
    _scheduler = scheduler
    _credentials_vault = credentials_vault
    _es_service = es_service


def _get_repository() -> IntegrationInstanceRepository:
    if _repository is None:
        raise RuntimeError(
            "Integrations endpoints not configured. "
            "Call configure_integrations_endpoints() during startup."
        )
    return _repository


def _get_scheduler_or_503() -> IntegrationScheduler:
    if _scheduler is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "scheduler_unavailable",
                "message": (
                    "Integration scheduler is not configured. Finish the "
                    "bootstrap wire-up before calling scheduler-backed "
                    "endpoints."
                ),
            },
        )
    return _scheduler


def _require_credentials_vault_or_400(
    credentials: Optional[Dict[str, Any]],
) -> Any:
    """Return the wired vault when a ``credentials`` payload is supplied.

    When no ``credentials`` payload is supplied the handler does not
    need the vault at all, so this helper returns ``None`` without
    raising. When one IS supplied but the vault is not wired we flip
    to HTTP 503 ``credentials_vault_unavailable`` rather than silently
    persisting the plaintext — Req 5.1.8 forbids any code path that
    could leak credentials through the API surface.
    """

    if not credentials:
        return None
    if _credentials_vault is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "credentials_vault_unavailable",
                "message": (
                    "Credentials vault is not configured. Refusing to "
                    "persist an integration with a credentials payload "
                    "until the vault is wired."
                ),
            },
        )
    return _credentials_vault


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class IntegrationInstanceView(BaseModel):
    """Response shape for a single :class:`IntegrationInstance`.

    Mirrors :class:`integrations.connector_base.IntegrationInstance`
    exactly except for the explicit
    ``credentials_status`` derived field — Req 5.1.8 forbids returning
    credential values, and consumers need SOME indicator that a
    credential exists. The status is derived deterministically from
    ``credentials_ref`` presence and is never a guess about the
    credential's remote validity (the scheduler's ``status`` /
    ``last_error`` fields capture remote validity separately).
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    tenant_id: str
    provider_name: str
    category: IntegrationCategory
    status: IntegrationStatus
    enabled: bool
    credentials_ref: Optional[str] = None
    credentials_status: Literal["valid", "missing"] = "missing"
    schedule_cron: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    last_sync_at: Optional[str] = None
    last_error: Optional[str] = None
    retry_count: int = 0
    updated_at: Optional[str] = None
    created_at: Optional[str] = None

    @classmethod
    def from_model(cls, model: IntegrationInstance) -> "IntegrationInstanceView":
        dumped = model.model_dump(mode="json")
        dumped["credentials_status"] = (
            "valid" if model.credentials_ref else "missing"
        )
        return cls(**dumped)


class IntegrationInstanceListResponse(BaseModel):
    """Envelope for ``GET /api/integrations``.

    Mirrors the ``{items, total, page, page_size, has_next}`` shape used
    across the other fuel-ops list endpoints so the frontend pagination
    helper consumes it uniformly.
    """

    model_config = ConfigDict(extra="forbid")

    items: List[IntegrationInstanceView]
    total: int
    page: int
    page_size: int
    has_next: bool


class IntegrationInstanceCreateRequest(BaseModel):
    """Body for ``POST /api/integrations``.

    Accepts every writable field on :class:`IntegrationInstance` plus an
    optional ``credentials`` dict that is unwrapped into the
    :class:`TenantCredentialsVault` before the instance document is
    persisted. The ``tenant_id`` field is intentionally absent — the
    router stamps it from the verified JWT context so callers cannot
    spoof ownership.
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional client-supplied identifier. When omitted the "
            "repository mints a uuid4-based id."
        ),
    )
    provider_name: str = Field(..., min_length=1)
    category: IntegrationCategory
    schedule_cron: Optional[str] = None
    enabled: bool = False
    config: Dict[str, Any] = Field(default_factory=dict)
    credentials: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional plaintext credential payload. When supplied the "
            "router forwards it to the TenantCredentialsVault and "
            "persists only the returned credentials_ref — plaintext is "
            "discarded immediately and NEVER appears in the response."
        ),
    )


class IntegrationInstanceUpdateRequest(BaseModel):
    """Body for ``PATCH /api/integrations/{instance_id}``.

    Every field is optional so callers can send just the delta.
    ``credentials`` is handled the same way as on create — re-wrapped
    through the vault, persisted as a ``credentials_ref`` only, never
    surfaced in the response.
    """

    model_config = ConfigDict(extra="forbid")

    schedule_cron: Optional[str] = None
    enabled: Optional[bool] = None
    status: Optional[IntegrationStatus] = None
    config: Optional[Dict[str, Any]] = None
    credentials: Optional[Dict[str, Any]] = None


class SyncRunView(BaseModel):
    """Response shape for a single :class:`SyncRun` in the sync-runs endpoint.

    Intentionally excludes ``error_details`` when it is a stack trace
    exceeding 4KB — the full trace belongs in logs and alerts, not a
    browser payload. Values up to the 4KB cap are surfaced unchanged
    so admins can see structured provider error codes inline.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    tenant_id: str
    instance_id: str
    provider_name: str
    operation: Literal["pull", "push"]
    started_at: str
    finished_at: Optional[str] = None
    status: Literal["running", "success", "partial", "error"]
    record_counts: Dict[str, int] = Field(default_factory=dict)
    error_details: Optional[str] = None
    duration_ms: Optional[int] = None


class SyncRunListResponse(BaseModel):
    """Envelope for ``GET /api/integrations/{instance_id}/sync-runs``."""

    model_config = ConfigDict(extra="forbid")

    items: List[SyncRunView]
    total: int


class ProviderCatalogView(BaseModel):
    """Single-entry response shape for ``GET /api/integrations/providers``.

    Mirrors :class:`ProviderCatalogEntry` verbatim but adds a derived
    ``effective_feature_flag_key`` that the Marketplace UI consults to
    decide whether to surface the provider for the tenant (Req 5.6.6).
    The raw ``feature_flag_key`` is preserved verbatim so callers that
    already depend on the registry's "None-means-default" convention
    keep working unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    provider_name: str
    category: str
    description: str
    required_credential_fields: List[str] = Field(default_factory=list)
    doc_url: Optional[str] = None
    auth_mode: Literal["oauth2", "api_key", "basic", "custom"] = "api_key"
    feature_flag_key: Optional[str] = None
    effective_feature_flag_key: str

    @classmethod
    def from_entry(cls, entry: ProviderCatalogEntry) -> "ProviderCatalogView":
        dumped = entry.model_dump()
        dumped["effective_feature_flag_key"] = entry.effective_feature_flag_key()
        return cls(**dumped)


class ProviderCatalogResponse(BaseModel):
    """Envelope for ``GET /api/integrations/providers``."""

    model_config = ConfigDict(extra="forbid")

    items: List[ProviderCatalogView]
    total: int


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _translate_cross_tenant_error(exc: CrossTenantAccessError) -> HTTPException:
    """Map :class:`CrossTenantAccessError` to HTTP 403 without leaking ownership."""

    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error_code": "cross_tenant_access_denied",
            "message": "Integration instance belongs to a different tenant.",
            "instance_id": exc.instance_id,
        },
    )


def _translate_validation_error(exc: Exception) -> HTTPException:
    """Map Pydantic and value errors to HTTP 422 with structured detail."""

    message = str(exc)
    if isinstance(exc, ValidationError):
        details: Any = []
        for err in exc.errors():
            clean: Dict[str, Any] = {}
            for key, value in err.items():
                if key in ("ctx", "url"):
                    continue
                if isinstance(value, tuple):
                    clean[key] = list(value)
                else:
                    clean[key] = value
            details.append(clean)
    else:
        details = message
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "error_code": "validation_error",
            "message": message,
            "errors": details,
        },
    )


def _not_found(instance_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error_code": "integration_instance_not_found",
            "instance_id": instance_id,
        },
    )


# ---------------------------------------------------------------------------
# GET /api/integrations (Req 5.1.7)
# ---------------------------------------------------------------------------


@router.get("", response_model=IntegrationInstanceListResponse)
async def list_integration_instances(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    provider_name: Optional[str] = Query(
        default=None,
        description="Filter to a single provider_name (e.g. 'quickbooks_online').",
    ),
    category: Optional[IntegrationCategory] = Query(
        default=None,
        description=(
            "Filter by coarse category: accounting, tank_monitor, "
            "gps_eld, payment, tms, terminal_pricing."
        ),
    ),
    enabled: Optional[bool] = Query(
        default=None,
        description="Filter by enabled flag. Omit to include both enabled and disabled.",
    ),
    status_filter: Optional[IntegrationStatus] = Query(
        default=None,
        alias="status",
        description="Filter by rolling health status.",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> IntegrationInstanceListResponse:
    """List integration instances owned by the caller's tenant.

    The list is tenant-scoped through two mechanisms: the ES query
    filters on ``tenant_id`` and every returned source is re-validated
    against the caller's tenant id before surfacing. Credentials never
    appear in the response — only the opaque ``credentials_ref`` and
    a derived ``credentials_status`` flag.

    Validates: Requirement 5.1.7, 5.1.8.
    """

    repo = _get_repository()

    # Repository caps size at DEFAULT_LIST_SIZE; for paging on top of
    # that, we fetch page_size * page entries (up to cap) and slice.
    # This is a reasonable tradeoff for a cardinality that is bounded
    # by the catalog size (≤ dozens of providers × enabled flag).
    try:
        records = await repo.list_for_tenant(
            tenant_id=tenant.tenant_id,
            provider_name=provider_name,
            category=category,
            enabled=enabled,
            status=status_filter,
            size=max(page_size * page, page_size),
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    total = len(records)
    start = (page - 1) * page_size
    end = start + page_size
    page_records = records[start:end]
    has_next = end < total

    items = [IntegrationInstanceView.from_model(r) for r in page_records]
    logger.debug(
        "integrations.list: tenant=%s provider=%s category=%s enabled=%s "
        "status=%s total=%d page=%d",
        tenant.tenant_id,
        provider_name,
        category,
        enabled,
        status_filter,
        total,
        page,
    )
    return IntegrationInstanceListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_next=has_next,
    )


# ---------------------------------------------------------------------------
# POST /api/integrations (Req 5.1.7, 5.1.8)
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=IntegrationInstanceView,
    status_code=status.HTTP_201_CREATED,
)
async def create_integration_instance(
    body: IntegrationInstanceCreateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> IntegrationInstanceView:
    """Create a new :class:`IntegrationInstance` for the caller's tenant.

    When ``credentials`` is supplied the plaintext is forwarded to the
    wired :class:`TenantCredentialsVault` and only the returned opaque
    ``credentials_ref`` is persisted on the instance document. The
    plaintext is discarded immediately and never surfaces in the
    response — Req 5.1.8.

    Validates: Requirements 5.1.7, 5.1.8.
    """

    repo = _get_repository()
    vault = _require_credentials_vault_or_400(body.credentials)

    # Unwrap credentials FIRST so a vault failure aborts before we write
    # a half-configured instance document.
    credentials_ref: Optional[str] = None
    if vault is not None and body.credentials:
        try:
            credentials_ref = await vault.put(
                tenant.tenant_id,
                f"{body.provider_name}_credentials",
                body.credentials,
                provider_name=body.provider_name,
            )
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "credentials_cross_tenant",
                    "message": str(exc),
                },
            )
        except (ValueError, TypeError) as exc:
            raise _translate_validation_error(exc)

    payload: Dict[str, Any] = body.model_dump(
        exclude_none=True, exclude={"credentials"}
    )
    payload["tenant_id"] = tenant.tenant_id
    if credentials_ref is not None:
        payload["credentials_ref"] = credentials_ref

    try:
        instance = await repo.create(tenant.tenant_id, payload)
    except CrossTenantAccessError as exc:
        raise _translate_cross_tenant_error(exc)
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    logger.info(
        "integrations.create: tenant=%s instance=%s provider=%s",
        tenant.tenant_id,
        instance.instance_id,
        instance.provider_name,
    )
    return IntegrationInstanceView.from_model(instance)


# ---------------------------------------------------------------------------
# PATCH /api/integrations/{instance_id} (Req 5.1.7, 5.1.8)
# ---------------------------------------------------------------------------


@router.patch(
    "/{instance_id}",
    response_model=IntegrationInstanceView,
)
async def update_integration_instance(
    instance_id: str,
    body: IntegrationInstanceUpdateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> IntegrationInstanceView:
    """Apply a partial update to an owned :class:`IntegrationInstance`.

    ``credentials`` is handled the same way as on create — re-wrapped
    through the vault, persisted as a ``credentials_ref`` only, never
    surfaced in the response.

    Validates: Requirements 5.1.7, 5.1.8.
    """

    repo = _get_repository()
    vault = _require_credentials_vault_or_400(body.credentials)

    # Load first so we can target the existing credentials_ref when
    # rotating, and so a 404 response is returned before any vault
    # writes happen.
    existing = await repo.get(tenant.tenant_id, instance_id)
    if existing is None:
        raise _not_found(instance_id)

    credentials_ref: Optional[str] = None
    if vault is not None and body.credentials:
        try:
            if existing.credentials_ref:
                await vault.rotate(tenant.tenant_id, existing.credentials_ref)
                credentials_ref = existing.credentials_ref
            else:
                credentials_ref = await vault.put(
                    tenant.tenant_id,
                    f"{existing.provider_name}_credentials",
                    body.credentials,
                    provider_name=existing.provider_name,
                )
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "credentials_cross_tenant",
                    "message": str(exc),
                },
            )
        except (ValueError, TypeError) as exc:
            raise _translate_validation_error(exc)

    patch: Dict[str, Any] = body.model_dump(
        exclude_none=True, exclude={"credentials"}
    )
    if credentials_ref is not None:
        patch["credentials_ref"] = credentials_ref

    if not patch:
        # Empty patch is a no-op — return the existing row so clients
        # get a consistent 200 even when their diff was empty.
        return IntegrationInstanceView.from_model(existing)

    try:
        updated = await repo.update(
            tenant_id=tenant.tenant_id,
            instance_id=instance_id,
            patch=patch,
        )
    except CrossTenantAccessError as exc:
        raise _translate_cross_tenant_error(exc)
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    if updated is None:
        # Record vanished between load and update (e.g. concurrent
        # delete); surface the same 404 as the initial load path.
        raise _not_found(instance_id)

    logger.info(
        "integrations.update: tenant=%s instance=%s keys=%s",
        tenant.tenant_id,
        instance_id,
        sorted(patch.keys()),
    )
    return IntegrationInstanceView.from_model(updated)


# ---------------------------------------------------------------------------
# DELETE /api/integrations/{instance_id} (Req 5.1.7)
# ---------------------------------------------------------------------------


@router.delete(
    "/{instance_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_integration_instance(
    instance_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> None:
    """Delete an owned :class:`IntegrationInstance` and unschedule it.

    When an :class:`IntegrationScheduler` is wired the router calls
    :meth:`IntegrationScheduler.unschedule_instance` so the next
    scheduler tick does not attempt to reload a deleted record. The
    unschedule call is a no-op when the instance was never scheduled
    (disabled at creation time), so it is safe to run unconditionally.

    Validates: Requirement 5.1.7.
    """

    repo = _get_repository()
    try:
        removed = await repo.delete(tenant.tenant_id, instance_id)
    except CrossTenantAccessError as exc:
        raise _translate_cross_tenant_error(exc)

    if not removed:
        raise _not_found(instance_id)

    if _scheduler is not None:
        try:
            await _scheduler.unschedule_instance(instance_id)
        except Exception as exc:  # pragma: no cover - defensive
            # The row is gone; log and swallow so the 204 isn't masked
            # by a scheduler-side error.
            logger.warning(
                "integrations.delete: failed to unschedule instance=%s: %s",
                instance_id,
                exc,
            )


# ---------------------------------------------------------------------------
# POST /api/integrations/{instance_id}/enable (Req 5.1.7)
# ---------------------------------------------------------------------------


@router.post(
    "/{instance_id}/enable",
    response_model=IntegrationInstanceView,
)
async def enable_integration_instance(
    instance_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> IntegrationInstanceView:
    """Flip ``enabled=True`` on an owned instance and schedule it.

    When no :class:`IntegrationScheduler` is wired the persisted flag
    still flips (so a later scheduler start-up picks it up via
    :meth:`IntegrationScheduler.start`), but the router returns the
    updated row immediately without scheduling.

    Validates: Requirement 5.1.7.
    """

    updated = await _flip_enabled(tenant, instance_id, enabled=True)
    if _scheduler is not None:
        try:
            await _scheduler.schedule_instance(updated)
        except RuntimeError:
            # Scheduler is wired but not started. Bubble a 503 so
            # callers can retry once bootstrap completes.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "scheduler_not_started",
                    "message": "Integration scheduler has not started yet.",
                },
            )
    return IntegrationInstanceView.from_model(updated)


# ---------------------------------------------------------------------------
# POST /api/integrations/{instance_id}/disable (Req 5.1.7)
# ---------------------------------------------------------------------------


@router.post(
    "/{instance_id}/disable",
    response_model=IntegrationInstanceView,
)
async def disable_integration_instance(
    instance_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> IntegrationInstanceView:
    """Flip ``enabled=False`` on an owned instance and unschedule it.

    When no :class:`IntegrationScheduler` is wired the persisted flag
    still flips (so a later scheduler start-up honours it), but the
    router returns the updated row immediately without unscheduling.

    Validates: Requirement 5.1.7.
    """

    updated = await _flip_enabled(tenant, instance_id, enabled=False)
    if _scheduler is not None:
        try:
            await _scheduler.unschedule_instance(instance_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "integrations.disable: failed to unschedule instance=%s: %s",
                instance_id,
                exc,
            )
    return IntegrationInstanceView.from_model(updated)


async def _flip_enabled(
    tenant: TenantContext, instance_id: str, *, enabled: bool
) -> IntegrationInstance:
    """Shared helper for enable/disable endpoints."""

    repo = _get_repository()
    try:
        updated = await repo.update(
            tenant_id=tenant.tenant_id,
            instance_id=instance_id,
            patch={"enabled": enabled},
        )
    except CrossTenantAccessError as exc:
        raise _translate_cross_tenant_error(exc)
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    if updated is None:
        raise _not_found(instance_id)
    return updated


# ---------------------------------------------------------------------------
# POST /api/integrations/{instance_id}/sync-now (Req 5.1.7)
# ---------------------------------------------------------------------------


@router.post(
    "/{instance_id}/sync-now",
    response_model=SyncRunView,
)
async def sync_integration_now(
    instance_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> SyncRunView:
    """Trigger an immediate :class:`SyncRun` outside the cron schedule.

    Returns the terminal :class:`SyncRun` so callers can surface the
    result directly. A disabled instance fails with HTTP 400
    ``instance_disabled`` and a missing/cross-tenant instance with HTTP
    404 — both are handled by :meth:`IntegrationScheduler.sync_now`.

    Validates: Requirement 5.1.7.
    """

    scheduler = _get_scheduler_or_503()
    try:
        run = await scheduler.sync_now(tenant.tenant_id, instance_id)
    except LookupError:
        raise _not_found(instance_id)
    except ValueError as exc:
        # sync_now raises ValueError for disabled instances per its
        # contract. Map to HTTP 400.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "instance_disabled",
                "message": str(exc),
                "instance_id": instance_id,
            },
        )

    return _sync_run_to_view(run)


# ---------------------------------------------------------------------------
# GET /api/integrations/{instance_id}/sync-runs (Req 5.6.4)
# ---------------------------------------------------------------------------


@router.get(
    "/{instance_id}/sync-runs",
    response_model=SyncRunListResponse,
)
async def list_sync_runs(
    instance_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    limit: int = Query(
        10,
        ge=1,
        le=50,
        description=(
            "Maximum number of recent Sync_Runs to return. Defaults to "
            "10 per Req 5.6.4; capped at 50 to keep response sizes "
            "bounded for the Marketplace card view."
        ),
    ),
) -> SyncRunListResponse:
    """Return the most-recent :class:`SyncRun` records for the instance.

    Guarded against cross-tenant leakage at two layers: the repository
    load asserts the instance belongs to the caller's tenant (HTTP 404
    otherwise), and the ES query filters on ``tenant_id`` + returned
    rows are re-validated per-document.

    Validates: Requirement 5.6.4.
    """

    repo = _get_repository()
    existing = await repo.get(tenant.tenant_id, instance_id)
    if existing is None:
        raise _not_found(instance_id)

    if _es_service is None:
        # No ES wiring means the scheduler has never had a chance to
        # persist runs. Return an empty list rather than 500 so the
        # UI card renders cleanly on a cold install.
        return SyncRunListResponse(items=[], total=0)

    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant.tenant_id}},
                    {"term": {"instance_id": instance_id}},
                ],
            },
        },
        "sort": [{"started_at": {"order": "desc"}}],
        "size": limit,
    }

    try:
        resp = await _es_service.search_documents(
            INTEGRATION_SYNC_RUNS_INDEX, query, limit
        )
    except Exception as exc:
        logger.warning(
            "integrations.sync_runs: ES query failed tenant=%s instance=%s: %s",
            tenant.tenant_id,
            instance_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "sync_runs_query_failed",
                "message": "Unable to query integration_sync_runs index.",
            },
        )

    sources = _extract_sources(resp)
    items: List[SyncRunView] = []
    for source in sources:
        if source.get("tenant_id") != tenant.tenant_id:
            # Defensive re-check — a mis-labelled row must not leak.
            logger.warning(
                "integrations.sync_runs: dropping cross-tenant sync_run "
                "doc tenant=%s expected=%s",
                source.get("tenant_id"),
                tenant.tenant_id,
            )
            continue
        try:
            view = _source_to_sync_run_view(source)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "integrations.sync_runs: dropping malformed sync_run "
                "doc run_id=%s: %s",
                source.get("run_id"),
                exc,
            )
            continue
        items.append(view)

    return SyncRunListResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# GET /api/integrations/providers (Req 5.6.2, 5.6.6)
# ---------------------------------------------------------------------------


@router.get("/providers", response_model=ProviderCatalogResponse)
async def list_providers(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> ProviderCatalogResponse:
    """Return the platform's catalog of available integration providers.

    The catalog is populated by the per-provider adapter modules
    (:mod:`integrations.quickbooks_online`, :mod:`integrations.veeder_root`,
    :mod:`integrations.geotab`, :mod:`integrations.stripe_connector`,
    …) calling :func:`integrations.provider_catalog.register_provider`
    at import time (Tasks 9.4–9.10). Task 9.3 only requires that the
    endpoint exist and surface the registry; Task 9.10 registers every
    connector in the catalog with its ``required_credential_fields``
    and ``doc_url``.

    The endpoint is tenant-scoped only in the sense that access is
    gated by the tenant guard — the catalog itself is global. The
    Marketplace UI layers its own per-tenant feature-flag check on top
    of the response (Req 5.6.6); returning every registered provider
    here keeps that logic in the UI where the flag lookups already
    live.

    Validates: Requirements 5.6.2, 5.6.6.
    """

    items = list_catalog_providers()
    views = [ProviderCatalogView.from_entry(entry) for entry in items]
    logger.debug(
        "integrations.providers: tenant=%s returned=%d",
        tenant.tenant_id,
        len(views),
    )
    return ProviderCatalogResponse(items=views, total=len(views))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_sources(resp: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` payloads from an ES-shaped response.

    Accepts the canonical ``{"hits": {"hits": [{"_source": ...}]}}``
    shape and ``None`` so the helper is robust across the variety of
    mock shapes used by tests.
    """

    # Handle both dict and ObjectApiResponse
    if not resp or not hasattr(resp, 'get'):
        return []
    hits_outer = resp.get("hits")
    if not hits_outer or not hasattr(hits_outer, 'get'):
        return []
    hits = hits_outer.get("hits") or []
    out: List[Dict[str, Any]] = []
    for hit in hits:
        if hasattr(hit, 'get') and hit.get("_source"):
            out.append(hit["_source"])
    return out


def _source_to_sync_run_view(source: Dict[str, Any]) -> SyncRunView:
    """Build a :class:`SyncRunView` from a raw ES ``_source`` dict.

    Routes through :class:`SyncRun` first so the schema-drift protection
    baked into the connector_base model (negative counts, non-integer
    values, blank required strings) still applies here.
    """

    run = SyncRun(**source)
    return _sync_run_to_view(run)


def _sync_run_to_view(run: SyncRun) -> SyncRunView:
    payload = run.model_dump(mode="json")
    return SyncRunView(**payload)


__all__ = [
    "IntegrationInstanceCreateRequest",
    "IntegrationInstanceListResponse",
    "IntegrationInstanceUpdateRequest",
    "IntegrationInstanceView",
    "ProviderCatalogResponse",
    "ProviderCatalogView",
    "ROUTER_AUTH_POLICY",
    "SyncRunListResponse",
    "SyncRunView",
    "configure_integrations_endpoints",
    "router",
]
