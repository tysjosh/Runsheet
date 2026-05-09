"""
QuickBooks Online Connector — accounting reference integration (Req 5.2.*).

Implements the QuickBooks Online side of the pluggable integration
framework introduced in Capability 5 / Phase 9 of the fuel-ops
hardening spec:

    * ``connect(credentials)`` — exchanges a client_id / client_secret /
      refresh_token tuple with the QBO OAuth 2.0 server, persists the
      resulting access_token + refresh_token + realm_id into the
      Tenant_Credentials_Vault, and returns a redacted
      :class:`integrations.connector_base.ConnectionResult` pointing at
      the vault reference. The caller (REST handler in Task 9.3) stores
      the ref on the owning :class:`IntegrationInstance`.

    * ``sync_push(payload)`` — creates a single QBO ``Invoice`` from a
      finalized POD when the tenant's ``overlay.qbo_invoice_push``
      feature flag is in an active overlay state. The payload shape is
      the one the POD finalization flow constructs and is documented on
      the method itself. Returns a terminal :class:`SyncRun` the
      Integration_Scheduler can persist verbatim.

    * ``sync_pull(since)`` — fetches QBO Payments and Invoice status
      changes since ``since`` via the standard QBO Query endpoint and
      folds them into the matching :class:`services.reconciliation_service.
      ReconciliationRecord` documents. Invoice events update
      ``invoiced_gallons`` + ``variance_invoiced_vs_delivered_pct``
      (Requirement 4.4.5 SLA); Payment events update ``payment_status``.

    * ``disconnect()`` — revokes the persisted refresh token via the
      QBO revoke endpoint and deletes the credential envelope from the
      Tenant_Credentials_Vault. Idempotent.

Cross-cutting invariants enforced here:

    * **OAuth refresh on 401 (Req 5.2.5).** Every HTTP call routes
      through :meth:`_http_request_with_retry`. A ``401 Unauthorized``
      response triggers exactly one refresh-token exchange; if the
      refresh itself fails, the owning :class:`IntegrationInstance` is
      transitioned to ``status="error"`` with ``last_error=
      "credentials_expired"`` and the original 401 is propagated so the
      scheduler records an error :class:`SyncRun`. On refresh success
      the rotated refresh_token + access_token are written back into
      the Tenant_Credentials_Vault before the call is retried.

    * **500 req/min throttle (Req 5.2.6).** Every HTTP call consults a
      per-tenant sliding-window counter in Redis keyed by
      ``qbo_ratelimit:{tenant_id}:{minute_bucket}``. Attempts above the
      ceiling raise :class:`QuickBooksRateLimitExceeded` which the
      scheduler maps to a non-retryable ``SyncRun.status="error"``.
      Requests never block on a monotonic sleep — the connector is
      async and the scheduler is responsible for cron-based pacing.

    * **Lazy ``intuit-oauth`` import (task 9.4).** The
      :mod:`intuitlib.client` import is deferred to the moment the
      refresh flow actually runs so unit tests (and bootstrap imports)
      never pay the dependency cost. This mirrors how the
      Integration_Scheduler defers ``apscheduler``.

    * **Credentials stay in the vault.** This module never logs,
      returns, or serializes plaintext tokens. Logs use redacted
      ``credentials_ref`` only (Requirement 5.1.8).

    * **Feature-flag gating on push.** ``sync_push`` short-circuits to
      a no-op :class:`SyncRun` (``status="success"``, record_counts
      ``{"invoices_pushed": 0, "skipped_disabled": 1}``) when
      ``overlay.qbo_invoice_push`` is not ``active_gated`` or
      ``active_auto`` for the tenant. Reading the flag never blocks
      the call path — a flag-lookup failure is treated as disabled to
      fail closed.

Validates: Requirements 5.2.1, 5.2.2, 5.2.3, 5.2.4, 5.2.5, 5.2.6.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Tuple
from uuid import uuid4

import httpx

from integrations.connector_base import (
    ConnectionResult,
    IntegrationConnector,
    IntegrationInstance,
    SyncRun,
)
from integrations.provider_catalog import (
    ProviderCatalogEntry,
    register_provider,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Per-tenant QBO rate ceiling per Requirement 5.2.6 (sandbox + prod).
DEFAULT_RATE_LIMIT_PER_MINUTE: int = 500

#: Redis key template used by the minute-bucket counter. The bucket is
#: the ``int(now_unix // 60)`` value so rate-limit state rolls forward
#: without any background sweeper.
RATE_LIMIT_KEY_TEMPLATE: str = "qbo_ratelimit:{tenant_id}:{minute_bucket}"

#: TTL (seconds) on every counter key. 2x the window width so a brief
#: clock skew between the client and Redis never allows a stale count
#: to starve a fresh minute.
RATE_LIMIT_KEY_TTL_SECONDS: int = 120

#: Default QBO OAuth + API endpoints. Production realm; sandbox callers
#: override via constructor to point at ``sandbox-quickbooks.api.intuit.com``.
DEFAULT_QBO_API_BASE_URL: str = "https://quickbooks.api.intuit.com"
DEFAULT_OAUTH_TOKEN_URL: str = (
    "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
)
DEFAULT_OAUTH_REVOKE_URL: str = (
    "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
)

#: Vault key used to persist the OAuth envelope. Scoped by tenant so
#: multiple QBO instances share a single record.
VAULT_CREDENTIAL_KEY: str = "qbo_oauth"

#: Overlay feature flag that gates invoice push on POD finalization
#: (Requirement 5.2.3). Read via FeatureFlagService.get_overlay_state.
QBO_INVOICE_PUSH_FLAG_KEY: str = "overlay.qbo_invoice_push"

#: Overlay states that mean "invoice push is on for this tenant".
#: Mirrors the convention used by PODBOLFinalizer so Storm_Mode's
#: ``shadow`` posture keeps the push idle.
_ACTIVE_OVERLAY_STATES: "frozenset[str]" = frozenset(
    {"active_gated", "active_auto"}
)

#: HTTP timeout on every QBO call. The QBO API is latency-sensitive
#: and default httpx timeouts (5s) are routinely too short for
#: ``/query`` responses with a few pages of results.
DEFAULT_HTTP_TIMEOUT_SECONDS: float = 15.0

#: Canonical ``last_error`` value the scheduler surfaces through the
#: admin UI when the refresh flow fails (Requirement 5.2.5). The
#: string is user-facing; keep it stable so UI copy does not drift.
CREDENTIALS_EXPIRED_REASON: str = "credentials_expired"

#: QBO API minor_version pinned so schema drifts don't silently break
#: the connector. 65 is the current LTS at spec time; bump deliberately.
_QBO_MINOR_VERSION: int = 65


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class QuickBooksRateLimitExceeded(RuntimeError):
    """Raised when the tenant's per-minute QBO request ceiling is exhausted.

    The scheduler catches this as a non-retryable failure for the
    current cron tick — backoff would only burn more budget on the
    next minute. The tenant's next cron firing (or a manual
    ``sync-now`` call after the clock rolls over) proceeds normally.
    """

    def __init__(
        self,
        tenant_id: str,
        minute_bucket: int,
        current: int,
        limit: int,
    ) -> None:
        super().__init__(
            f"QuickBooks Online rate limit exceeded for tenant={tenant_id} "
            f"minute={minute_bucket} current={current} limit={limit}"
        )
        self.tenant_id = tenant_id
        self.minute_bucket = minute_bucket
        self.current = current
        self.limit = limit


class QuickBooksCredentialsExpired(RuntimeError):
    """Raised after a 401 refresh-retry cycle still returns 401.

    The scheduler flips the owning instance to ``status="error"``
    with ``last_error="credentials_expired"`` (Requirement 5.2.5)
    when this propagates out of a sync_pull / sync_push call.
    """

    def __init__(self, tenant_id: str, instance_id: str) -> None:
        super().__init__(
            f"QuickBooks Online credentials expired for tenant={tenant_id} "
            f"instance={instance_id}; re-authorize via the Marketplace UI"
        )
        self.tenant_id = tenant_id
        self.instance_id = instance_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _minute_bucket(now: Optional[float] = None) -> int:
    """Return the wall-clock minute bucket used by the rate-limit counter."""

    return int((now if now is not None else time.time()) // 60)


def _rate_limit_key(tenant_id: str, minute_bucket: int) -> str:
    return RATE_LIMIT_KEY_TEMPLATE.format(
        tenant_id=tenant_id, minute_bucket=minute_bucket
    )


def _iso(ts: datetime) -> str:
    """Return a QBO-compatible ISO-8601 string (UTC, ``Z`` suffix)."""

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Provider-catalog entry (Task 9.10 convenience)
# ---------------------------------------------------------------------------


def build_catalog_entry() -> ProviderCatalogEntry:
    """Return the :class:`ProviderCatalogEntry` for this connector.

    Task 9.10 wires every connector into the shared catalog at bootstrap
    time via :func:`register_catalog_entry`; this helper is also used
    directly by ``/api/integrations/providers`` tests that want to assert
    on the shape without triggering registration side-effects.

    The catalog entry's ``feature_flag_key`` is intentionally left
    unset so :meth:`ProviderCatalogEntry.effective_feature_flag_key`
    surfaces the Marketplace-level default
    ``overlay.integration.quickbooks_online`` (Requirement 5.6.6).
    :data:`QBO_INVOICE_PUSH_FLAG_KEY` remains a separate,
    behaviour-level flag that gates the invoice push path inside
    :meth:`QuickBooksOnlineConnector.sync_push` — it is not the
    Marketplace visibility flag.
    """

    return ProviderCatalogEntry(
        provider_name="quickbooks_online",
        category="accounting",
        description=(
            "Sync finalized deliveries as Invoices and import Payment + "
            "Invoice status changes back into reconciliation from Intuit "
            "QuickBooks Online."
        ),
        required_credential_fields=[
            "client_id",
            "client_secret",
            "refresh_token",
            "realm_id",
        ],
        doc_url="https://developer.intuit.com/app/developer/qbo/docs/get-started",
        auth_mode="oauth2",
    )


def register_catalog_entry() -> ProviderCatalogEntry:
    """Register the connector with the shared provider catalog.

    Task 9.10 wires every connector into the catalog at bootstrap
    time; this helper is kept here (rather than inline at module
    import time) so a test that imports this module does not
    auto-register with the global catalog.
    """

    return register_provider(build_catalog_entry())


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class QuickBooksOnlineConnector(IntegrationConnector):
    """QuickBooks Online adapter (accounting category).

    Args:
        tenant_id: Owning tenant; every Redis / vault / ES write is
            re-scoped on this id.
        instance_id: Owning :class:`IntegrationInstance` id — stamped
            on every :class:`SyncRun` the connector returns.
        credentials_vault: The shared
            :class:`services.credentials_vault.TenantCredentialsVault`.
            Required. The connector NEVER logs or returns the
            plaintext credential envelope; it only references it by
            opaque ``credentials_ref``.
        credentials_ref: Existing vault reference (None means this
            instance has not completed the OAuth handoff yet; only
            ``connect()`` is valid in that state).
        reconciliation_service: Required for ``sync_pull``. Must
            expose :meth:`update_invoice_fields` with the contract
            documented in :mod:`services.reconciliation_service`.
        feature_flag_service: Required for ``sync_push``. Must expose
            ``await get_overlay_state(flag_key, tenant_id) -> str``.
            A missing / erroring flag service yields a silent
            skip (fail-closed).
        redis_client: Optional async Redis client used for the
            500 req/min counter. When ``None`` the throttle is
            disabled — production deployments MUST supply one.
        http_client: Optional injected :class:`httpx.AsyncClient`.
            When ``None`` every call creates a short-lived client
            with :data:`DEFAULT_HTTP_TIMEOUT_SECONDS`. Tests inject a
            mock here.
        es_service: Optional ES service used to look up
            reconciliation records by ``invoice_id`` during
            ``sync_pull``. When ``None`` the connector still succeeds
            but Invoice updates degrade to ``skipped_no_match`` in
            the record_counts.
        reconciliation_index: ES index name for reconciliation
            records. Defaults to the canonical
            :data:`services.reconciliation_service.MVP_RECONCILIATION_INDEX`.
        rate_limit_per_minute: Override the 500 req/min ceiling. The
            default matches the production QBO quota.
        api_base_url: Override the QBO API base. Sandbox callers
            pass ``https://sandbox-quickbooks.api.intuit.com``.
        token_url / revoke_url: OAuth endpoint overrides for testing.
        clock: Zero-arg callable returning ``float`` seconds since the
            epoch. Injected for deterministic rate-limit tests.
    """

    category: ClassVar[str] = "accounting"
    provider_name: ClassVar[str] = "quickbooks_online"

    def __init__(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        credentials_vault: Any,
        credentials_ref: Optional[str] = None,
        reconciliation_service: Optional[Any] = None,
        feature_flag_service: Optional[Any] = None,
        redis_client: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        es_service: Optional[Any] = None,
        reconciliation_index: Optional[str] = None,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        api_base_url: str = DEFAULT_QBO_API_BASE_URL,
        token_url: str = DEFAULT_OAUTH_TOKEN_URL,
        revoke_url: str = DEFAULT_OAUTH_REVOKE_URL,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        clock: Any = time.time,
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ValueError("instance_id must be a non-empty string")
        if credentials_vault is None:
            raise ValueError("credentials_vault is required")
        if rate_limit_per_minute <= 0:
            raise ValueError("rate_limit_per_minute must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        # Resolve the reconciliation index lazily so this module does
        # not force an import of :mod:`services.reconciliation_service`
        # at bootstrap time (which would pull in the ReconciliationService
        # + its ES mappings for every unit test).
        if reconciliation_index is None:
            try:
                from services.reconciliation_service import (
                    MVP_RECONCILIATION_INDEX,
                )

                reconciliation_index = MVP_RECONCILIATION_INDEX
            except Exception:  # pragma: no cover - defensive
                reconciliation_index = "mvp_reconciliation"

        self._tenant_id = tenant_id
        self._instance_id = instance_id
        self._vault = credentials_vault
        self._credentials_ref = credentials_ref
        self._recon = reconciliation_service
        self._feature_flags = feature_flag_service
        self._redis = redis_client
        self._http_client = http_client
        self._es = es_service
        self._reconciliation_index = reconciliation_index
        self._rate_limit = int(rate_limit_per_minute)
        self._api_base = api_base_url.rstrip("/")
        self._token_url = token_url
        self._revoke_url = revoke_url
        self._timeout = float(timeout_seconds)
        self._clock = clock

        # In-memory cached access token. The refresh flow persists the
        # rotated tokens to the vault AND updates this cache so a
        # follow-up call in the same connector lifetime skips the
        # vault round-trip.
        self._cached_tokens: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # IntegrationConnector API
    # ------------------------------------------------------------------

    async def connect(self, credentials: Mapping[str, Any]) -> ConnectionResult:
        """Validate and persist an OAuth credential envelope.

        Expected ``credentials`` shape:

            {
                "client_id": "...",
                "client_secret": "...",
                "refresh_token": "...",   # long-lived; rotated on every refresh
                "realm_id": "...",        # QBO company id, required for every API call
                "access_token": "...",    # optional; will be obtained on first refresh
                "token_expires_at": "...",# optional ISO8601; defaults to now()
            }

        The connector does NOT call QBO during ``connect`` — Intuit's
        OAuth2 grant flow returns the refresh_token client-side; the
        Marketplace UI hands us the tuple here. We validate the shape,
        write to the Tenant_Credentials_Vault, and return a
        :class:`ConnectionResult` with ``status="connected"``. A
        subsequent ``sync_pull`` / ``sync_push`` will exercise the
        refresh flow and prove the credentials are live.
        """

        required = ("client_id", "client_secret", "refresh_token", "realm_id")
        missing = [k for k in required if not credentials.get(k)]
        if missing:
            return ConnectionResult(
                status="error",
                message=f"missing required credential fields: {sorted(missing)}",
            )

        envelope: Dict[str, Any] = {
            "client_id": str(credentials["client_id"]).strip(),
            "client_secret": str(credentials["client_secret"]).strip(),
            "refresh_token": str(credentials["refresh_token"]).strip(),
            "realm_id": str(credentials["realm_id"]).strip(),
            "access_token": (
                str(credentials["access_token"]).strip()
                if credentials.get("access_token")
                else None
            ),
            "token_expires_at": credentials.get("token_expires_at"),
        }

        ref = await self._vault.put(
            tenant_id=self._tenant_id,
            key=VAULT_CREDENTIAL_KEY,
            plaintext=envelope,
            provider_name=self.provider_name,
        )
        self._credentials_ref = ref
        self._cached_tokens = dict(envelope)

        logger.info(
            "QuickBooksOnlineConnector.connect: stored credentials "
            "tenant=%s instance=%s credentials_ref=%s",
            self._tenant_id,
            self._instance_id,
            ref,
        )
        return ConnectionResult(
            status="connected",
            credentials_ref=ref,
            metadata={"realm_id": envelope["realm_id"]},
        )

    async def sync_pull(self, since: datetime) -> SyncRun:
        """Import Payments + Invoice status changes since ``since``.

        Returns a terminal :class:`SyncRun`. On
        :class:`QuickBooksCredentialsExpired` / 401-after-refresh the
        scheduler flips the instance to ``status="error"``; all other
        failures are reported as ``status="error"`` with structured
        error details in the SyncRun.
        """

        run_id = f"qbo_pull_{uuid4()}"
        started_at = _utcnow()
        counts: Dict[str, int] = {
            "invoices_processed": 0,
            "payments_processed": 0,
            "reconciliations_updated": 0,
            "skipped_no_match": 0,
        }

        try:
            # Query Invoices updated since ``since``. QBO's Query API
            # paginates via STARTPOSITION; for the expected low-volume
            # case (invoices for one tenant's deliveries) a single page
            # suffices, but we loop so large batches do not silently
            # truncate.
            invoices = await self._query(
                "Invoice",
                since=since,
                fields=(
                    "Id, DocNumber, TxnDate, TotalAmt, MetaData, "
                    "Balance, CustomerRef, Line"
                ),
            )
            counts["invoices_processed"] = len(invoices)
            for inv in invoices:
                updated = await self._apply_invoice_update(inv)
                if updated:
                    counts["reconciliations_updated"] += 1
                else:
                    counts["skipped_no_match"] += 1

            # Query Payments updated since ``since``.
            payments = await self._query(
                "Payment",
                since=since,
                fields="Id, TxnDate, TotalAmt, Line, MetaData",
            )
            counts["payments_processed"] = len(payments)
            for pay in payments:
                updated = await self._apply_payment_update(pay)
                if updated:
                    counts["reconciliations_updated"] += 1
                else:
                    counts["skipped_no_match"] += 1

        except QuickBooksCredentialsExpired as exc:
            # Surface as an error SyncRun so the scheduler flips the
            # instance to ``status="error"`` with the canonical reason
            # (Requirement 5.2.5).
            return self._error_run(
                run_id=run_id,
                operation="pull",
                started_at=started_at,
                record_counts=counts,
                reason=CREDENTIALS_EXPIRED_REASON,
                exc=exc,
            )
        except QuickBooksRateLimitExceeded as exc:
            return self._error_run(
                run_id=run_id,
                operation="pull",
                started_at=started_at,
                record_counts=counts,
                reason="rate_limit_exceeded",
                exc=exc,
            )
        except Exception as exc:
            return self._error_run(
                run_id=run_id,
                operation="pull",
                started_at=started_at,
                record_counts=counts,
                reason=None,
                exc=exc,
            )

        finished_at = _utcnow()
        status = (
            "partial"
            if counts["skipped_no_match"] > 0
            and counts["reconciliations_updated"] == 0
            else "success"
        )
        return SyncRun(
            run_id=run_id,
            tenant_id=self._tenant_id,
            instance_id=self._instance_id,
            provider_name=self.provider_name,
            operation="pull",
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            record_counts=counts,
            duration_ms=max(
                0, int((finished_at - started_at).total_seconds() * 1000)
            ),
        )

    async def sync_push(self, payload: Mapping[str, Any]) -> SyncRun:
        """Create a QBO Invoice from a finalized delivery or canonical Invoice.

        When ``commerce.qbo_pushes_canonical`` is ON (default when
        commerce backbone is live), the payload is expected to be the
        canonical Invoice shape built by
        :meth:`CommerceExternalSync._build_qbo_push_payload`:

            {
                "invoice_id": "inv_...",
                "customer_id": "cust_...",
                "customer_name": "...",
                "delivery_date": "2025-01-14",
                "product_code": "DIESEL_2",
                "delivered_gallons": 480.0,
                "unit_price_usd": 3.289,
                "total_cents": 157872,
                "subtotal_cents": 150000,
                "tax_cents": 7872,
                "line_items": [...],
                "memo": "Invoice INV-0042",
                "reconciliation_id": "...",
                "invoice_doc_number": "INV-0042",
                "tenant_id": "...",
                "account_id": "acct_...",
                "external_refs": {...},
            }

        When the flag is OFF, the legacy free-form POD payload shape is
        used (rollback path):

            {
                "pod_id": "...",
                "customer_id": "...",          # QBO Customer.Id
                "customer_name": "...",
                "delivery_date": "2025-01-14", # ISO date
                "product_code": "DIESEL_2",
                "delivered_gallons": 480.0,
                "unit_price_usd": 3.289,
                "memo": "optional",
                "reconciliation_id": "...",    # optional back-reference
                "invoice_doc_number": "optional",
            }

        When ``overlay.qbo_invoice_push`` is not in an active overlay
        state for the tenant, the connector short-circuits to a no-op
        :class:`SyncRun` with ``status="success"`` and
        ``skipped_disabled=1`` in the record counts. This mirrors the
        PODBOLFinalizer contract and preserves Requirement 5.2.3's
        "when ``overlay.qbo_invoice_push`` is enabled" gate.
        """

        run_id = f"qbo_push_{uuid4()}"
        started_at = _utcnow()
        counts: Dict[str, int] = {
            "invoices_pushed": 0,
            "skipped_disabled": 0,
            "failed": 0,
        }

        enabled = await self._is_push_enabled()
        if not enabled:
            counts["skipped_disabled"] = 1
            finished_at = _utcnow()
            logger.info(
                "QuickBooksOnlineConnector.sync_push: skipping push — "
                "overlay.qbo_invoice_push disabled for tenant=%s",
                self._tenant_id,
            )
            return SyncRun(
                run_id=run_id,
                tenant_id=self._tenant_id,
                instance_id=self._instance_id,
                provider_name=self.provider_name,
                operation="push",
                started_at=started_at,
                finished_at=finished_at,
                status="success",
                record_counts=counts,
                duration_ms=max(
                    0, int((finished_at - started_at).total_seconds() * 1000)
                ),
            )

        try:
            # Determine whether to use the canonical Invoice path or
            # the legacy free-form POD path based on the
            # commerce.qbo_pushes_canonical feature flag.
            use_canonical = self._should_use_canonical_push()
            if use_canonical:
                body = _build_invoice_body_from_canonical(payload)
            else:
                body = _build_invoice_body(payload)

            created = await self._http_request_with_retry(
                method="POST",
                path=f"/v3/company/{{realm_id}}/invoice",
                json_body=body,
            )
            invoice_node = (created or {}).get("Invoice") or {}
            qbo_invoice_id = invoice_node.get("Id") or ""
            counts["invoices_pushed"] = 1
            finished_at = _utcnow()
            logger.info(
                "QuickBooksOnlineConnector.sync_push: created invoice "
                "tenant=%s %s=%s qbo_invoice_id=%s canonical=%s",
                self._tenant_id,
                "invoice_id" if use_canonical else "pod",
                payload.get("invoice_id") if use_canonical else payload.get("pod_id"),
                qbo_invoice_id,
                use_canonical,
            )
            return SyncRun(
                run_id=run_id,
                tenant_id=self._tenant_id,
                instance_id=self._instance_id,
                provider_name=self.provider_name,
                operation="push",
                started_at=started_at,
                finished_at=finished_at,
                status="success",
                record_counts=counts,
                duration_ms=max(
                    0, int((finished_at - started_at).total_seconds() * 1000)
                ),
            )
        except QuickBooksCredentialsExpired as exc:
            counts["failed"] = 1
            return self._error_run(
                run_id=run_id,
                operation="push",
                started_at=started_at,
                record_counts=counts,
                reason=CREDENTIALS_EXPIRED_REASON,
                exc=exc,
            )
        except QuickBooksRateLimitExceeded as exc:
            counts["failed"] = 1
            return self._error_run(
                run_id=run_id,
                operation="push",
                started_at=started_at,
                record_counts=counts,
                reason="rate_limit_exceeded",
                exc=exc,
            )
        except Exception as exc:
            counts["failed"] = 1
            return self._error_run(
                run_id=run_id,
                operation="push",
                started_at=started_at,
                record_counts=counts,
                reason=None,
                exc=exc,
            )

    async def disconnect(self) -> None:
        """Revoke the refresh token and remove the vault envelope.

        Idempotent: when no credential ref is known, the method is a
        no-op. Revoke failures are logged but never raised — the
        vault delete is the authoritative source of truth for the
        integration's posture on the platform.
        """

        if not self._credentials_ref:
            return
        try:
            tokens = await self._load_tokens()
        except Exception as exc:
            logger.warning(
                "QuickBooksOnlineConnector.disconnect: could not load "
                "tokens for tenant=%s (ref=%s): %s",
                self._tenant_id,
                self._credentials_ref,
                exc,
            )
            tokens = None

        if tokens is not None:
            try:
                await self._revoke_refresh_token(tokens)
            except Exception as exc:
                logger.warning(
                    "QuickBooksOnlineConnector.disconnect: revoke call "
                    "failed for tenant=%s: %s — continuing with vault "
                    "delete",
                    self._tenant_id,
                    exc,
                )

        try:
            await self._vault.delete(self._tenant_id, self._credentials_ref)
        except Exception as exc:
            logger.warning(
                "QuickBooksOnlineConnector.disconnect: vault delete "
                "failed for tenant=%s ref=%s: %s",
                self._tenant_id,
                self._credentials_ref,
                exc,
            )
        self._credentials_ref = None
        self._cached_tokens = None

    # ------------------------------------------------------------------
    # HTTP orchestration
    # ------------------------------------------------------------------

    async def _http_request_with_retry(
        self,
        *,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Issue a QBO API call with refresh-on-401 + rate limit guard.

        ``path`` may contain ``{realm_id}`` which is substituted from
        the current credential envelope so callers don't have to
        reload the realm id on every call.

        Raises:
            QuickBooksRateLimitExceeded: The per-minute ceiling is
                exhausted; the scheduler treats this as a non-retryable
                terminal failure for the current tick.
            QuickBooksCredentialsExpired: A 401 persisted through a
                single refresh-token exchange attempt
                (Requirement 5.2.5).
            httpx.HTTPStatusError: Any other non-2xx response.
        """

        await self._rate_limit_acquire()
        tokens = await self._load_tokens()
        response = await self._issue(
            method=method,
            path=path,
            params=params,
            json_body=json_body,
            tokens=tokens,
        )

        if response.status_code != 401:
            response.raise_for_status()
            return self._parse_json(response)

        # 401 — attempt exactly one refresh + retry (Requirement 5.2.5).
        logger.info(
            "QuickBooksOnlineConnector: 401 on %s %s for tenant=%s — "
            "attempting refresh-token exchange",
            method,
            path,
            self._tenant_id,
        )
        try:
            tokens = await self._refresh_access_token(tokens)
        except Exception as exc:
            logger.warning(
                "QuickBooksOnlineConnector: refresh-token exchange "
                "failed for tenant=%s: %s",
                self._tenant_id,
                exc,
            )
            raise QuickBooksCredentialsExpired(
                self._tenant_id, self._instance_id
            ) from exc

        await self._rate_limit_acquire()
        response = await self._issue(
            method=method,
            path=path,
            params=params,
            json_body=json_body,
            tokens=tokens,
        )
        if response.status_code == 401:
            raise QuickBooksCredentialsExpired(
                self._tenant_id, self._instance_id
            )
        response.raise_for_status()
        return self._parse_json(response)

    async def _issue(
        self,
        *,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]],
        json_body: Optional[Dict[str, Any]],
        tokens: Mapping[str, Any],
    ) -> httpx.Response:
        """Compose headers and issue the request through httpx."""

        realm_id = tokens.get("realm_id")
        if not realm_id:
            raise RuntimeError(
                "QuickBooksOnlineConnector: credential envelope is "
                "missing realm_id"
            )
        resolved_path = path.format(realm_id=realm_id)
        url = f"{self._api_base}{resolved_path}"
        q = dict(params or {})
        q.setdefault("minorversion", _QBO_MINOR_VERSION)

        headers = {
            "Authorization": f"Bearer {tokens.get('access_token', '')}",
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        client, owned = await self._get_http_client()
        try:
            return await client.request(
                method,
                url,
                params=q,
                json=json_body,
                headers=headers,
                timeout=self._timeout,
            )
        finally:
            if owned:
                await client.aclose()

    async def _get_http_client(self) -> Tuple[httpx.AsyncClient, bool]:
        """Return ``(client, owned_here)``.

        When the adapter was constructed with an injected ``http_client``
        we reuse it and leave close to the caller. Otherwise we
        create a short-lived client per call and close it on exit.
        """

        if self._http_client is not None:
            return self._http_client, False
        return httpx.AsyncClient(timeout=self._timeout), True

    @staticmethod
    def _parse_json(response: httpx.Response) -> Dict[str, Any]:
        try:
            return response.json() or {}
        except (ValueError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    async def _load_tokens(self) -> Dict[str, Any]:
        """Fetch the credential envelope (vault or cache) as a dict."""

        if self._cached_tokens is not None:
            return self._cached_tokens
        if not self._credentials_ref:
            raise RuntimeError(
                "QuickBooksOnlineConnector: no credentials_ref — call "
                "connect() first"
            )
        envelope = await self._vault.get(
            self._tenant_id, self._credentials_ref
        )
        if not isinstance(envelope, dict):
            raise RuntimeError(
                "QuickBooksOnlineConnector: vault returned non-dict "
                "credential envelope"
            )
        self._cached_tokens = dict(envelope)
        return self._cached_tokens

    async def _refresh_access_token(
        self, tokens: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Exchange the refresh_token for a new access + refresh pair.

        The ``intuit-oauth`` (``intuitlib``) library is imported lazily
        so unit tests can mock this method without installing the
        dependency. The library provides strict client_id/client_secret
        + refresh_token exchange semantics (refresh-token rotation is
        mandatory on QBO) — we use it as a thin wrapper around the
        canonical POST to ``/tokens/bearer`` so production benefits
        from Intuit's first-party request-signing defaults.

        Returns the rotated envelope. Persists it to the vault before
        returning so the next call on this connector (or a restarted
        process) picks up the rotated refresh_token.

        Raises:
            RuntimeError: The refresh call failed. Callers translate
                this into :class:`QuickBooksCredentialsExpired`.
        """

        client_id = tokens.get("client_id")
        client_secret = tokens.get("client_secret")
        refresh_token = tokens.get("refresh_token")
        realm_id = tokens.get("realm_id")
        if not client_id or not client_secret or not refresh_token:
            raise RuntimeError(
                "QuickBooksOnlineConnector: refresh requires "
                "client_id + client_secret + refresh_token"
            )

        # Lazy import so unit tests don't need the intuit-oauth
        # package installed. When the library is available we let it
        # construct the AuthClient with the correct environment
        # (``production`` vs ``sandbox``); when it is not, we fall
        # back to a direct httpx POST that follows the same wire
        # format Intuit documents publicly.
        new_tokens: Optional[Dict[str, Any]] = None
        try:
            from intuitlib.client import AuthClient  # type: ignore
            from intuitlib.enums import Scopes  # type: ignore  # noqa: F401

            environment = (
                "production"
                if "sandbox" not in self._api_base
                else "sandbox"
            )
            auth = AuthClient(
                client_id=client_id,
                client_secret=client_secret,
                environment=environment,
                redirect_uri="",
            )
            # intuit-oauth exposes a synchronous refresh; wrap in a
            # thread so we don't block the event loop on its network
            # call.
            def _do_refresh() -> Dict[str, Any]:
                auth.refresh(refresh_token=refresh_token)
                return {
                    "access_token": getattr(auth, "access_token", None),
                    "refresh_token": getattr(
                        auth, "refresh_token", refresh_token
                    ),
                    "expires_in": getattr(auth, "expires_in", None),
                }

            new_tokens = await asyncio.to_thread(_do_refresh)
        except ImportError:
            # Library not installed — fall back to a direct HTTP call
            # so the connector still works in environments that haven't
            # yet pip-installed intuit-oauth. The wire format is the
            # OAuth 2.0 Token Exchange request Intuit documents at
            # https://developer.intuit.com/.
            new_tokens = await self._refresh_via_http(
                client_id=str(client_id),
                client_secret=str(client_secret),
                refresh_token=str(refresh_token),
            )
        except Exception as exc:
            # intuit-oauth raises :class:`AuthClientError` on failure;
            # wrap as RuntimeError so the 401 handler maps it to
            # credentials_expired.
            raise RuntimeError(
                f"intuit-oauth refresh failed: {exc}"
            ) from exc

        if not new_tokens or not new_tokens.get("access_token"):
            raise RuntimeError(
                "QuickBooksOnlineConnector: refresh returned empty "
                "access_token"
            )

        rotated: Dict[str, Any] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": new_tokens.get("refresh_token") or refresh_token,
            "realm_id": realm_id,
            "access_token": new_tokens["access_token"],
            "token_expires_at": _iso(_utcnow()),
        }

        # Persist the rotated envelope back into the vault so the next
        # process lifetime picks up the new refresh_token. The vault's
        # ``rotate`` path re-wraps the DEK and persists atomically.
        try:
            await self._vault.rotate(
                self._tenant_id, self._credentials_ref
            )
            # Overwrite plaintext body. Vault's put-over-ref is
            # idempotent; we use the same logical key so the existing
            # ref still resolves.
            new_ref = await self._vault.put(
                tenant_id=self._tenant_id,
                key=VAULT_CREDENTIAL_KEY,
                plaintext=rotated,
                provider_name=self.provider_name,
            )
            # put() mints a fresh ref each time; keep the latest so
            # further calls resolve through the rotated envelope.
            if new_ref:
                # Best-effort: delete the old ref so we don't
                # accumulate orphaned envelopes.
                try:
                    await self._vault.delete(
                        self._tenant_id, self._credentials_ref
                    )
                except Exception:  # pragma: no cover - defensive
                    pass
                self._credentials_ref = new_ref
        except Exception as exc:
            # Persist failure is non-fatal for the current request
            # cycle — the rotated access_token is still usable until
            # it expires. Log and continue.
            logger.warning(
                "QuickBooksOnlineConnector: failed to persist rotated "
                "tokens to vault for tenant=%s: %s",
                self._tenant_id,
                exc,
            )

        self._cached_tokens = rotated
        return rotated

    async def _refresh_via_http(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> Dict[str, Any]:
        """HTTP fallback when ``intuit-oauth`` is not installed."""

        import base64 as _b64

        basic = _b64.b64encode(
            f"{client_id}:{client_secret}".encode("utf-8")
        ).decode("ascii")
        client, owned = await self._get_http_client()
        try:
            response = await client.post(
                self._token_url,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json() or {}
            return {
                "access_token": body.get("access_token"),
                "refresh_token": body.get("refresh_token", refresh_token),
                "expires_in": body.get("expires_in"),
            }
        finally:
            if owned:
                await client.aclose()

    async def _revoke_refresh_token(
        self, tokens: Mapping[str, Any]
    ) -> None:
        """POST to the revoke endpoint so disconnected instances don't leave tokens live."""

        import base64 as _b64

        client_id = tokens.get("client_id")
        client_secret = tokens.get("client_secret")
        refresh_token = tokens.get("refresh_token")
        if not client_id or not client_secret or not refresh_token:
            return
        basic = _b64.b64encode(
            f"{client_id}:{client_secret}".encode("utf-8")
        ).decode("ascii")

        client, owned = await self._get_http_client()
        try:
            await client.post(
                self._revoke_url,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"token": refresh_token},
                timeout=self._timeout,
            )
        finally:
            if owned:
                await client.aclose()

    # ------------------------------------------------------------------
    # Rate limit (Req 5.2.6)
    # ------------------------------------------------------------------

    async def _rate_limit_acquire(self) -> None:
        """Increment the per-tenant minute-bucket counter.

        Raises :class:`QuickBooksRateLimitExceeded` when the bucket is
        already at or above the configured ceiling. Redis failures are
        swallowed — losing budget tracking is preferable to 503'ing a
        sync_run because Redis blipped.
        """

        if self._redis is None:
            return
        bucket = _minute_bucket(self._clock())
        key = _rate_limit_key(self._tenant_id, bucket)
        try:
            incr = getattr(self._redis, "incr", None)
            if incr is None:  # pragma: no cover - alternative client
                raw = await self._redis.get(key)
                current = (int(raw) if raw else 0) + 1
                # Fallback path has no INCR, so we must set the TTL
                # inline with the SET to avoid a counter that never
                # rolls over. Mirrors the primary-path ``expire()`` call
                # below so the minute-bucket semantics are identical.
                await self._redis.set(
                    key, str(current), ex=RATE_LIMIT_KEY_TTL_SECONDS
                )
            else:
                current = await incr(key)
            expire = getattr(self._redis, "expire", None)
            if expire is not None:
                try:
                    await expire(key, RATE_LIMIT_KEY_TTL_SECONDS)
                except Exception:  # pragma: no cover - defensive
                    pass
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "QuickBooksOnlineConnector: rate-limit increment failed "
                "for tenant=%s: %s — allowing call through",
                self._tenant_id,
                exc,
            )
            return

        if int(current) > self._rate_limit:
            raise QuickBooksRateLimitExceeded(
                tenant_id=self._tenant_id,
                minute_bucket=bucket,
                current=int(current),
                limit=self._rate_limit,
            )

    # ------------------------------------------------------------------
    # Feature flags
    # ------------------------------------------------------------------

    async def _is_push_enabled(self) -> bool:
        """Resolve ``overlay.qbo_invoice_push`` for the owning tenant."""

        ff = self._feature_flags
        if ff is None:
            return False
        try:
            state = await ff.get_overlay_state(
                QBO_INVOICE_PUSH_FLAG_KEY, self._tenant_id
            )
        except AttributeError:
            # Legacy FeatureFlagService stubs expose only
            # ``is_enabled``. Honour the simpler API for backwards
            # compat.
            try:
                return bool(await ff.is_enabled(self._tenant_id))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "QuickBooksOnlineConnector: feature flag lookup "
                    "failed tenant=%s: %s",
                    self._tenant_id,
                    exc,
                )
                return False
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "QuickBooksOnlineConnector: overlay state lookup "
                "failed tenant=%s: %s",
                self._tenant_id,
                exc,
            )
            return False
        return state in _ACTIVE_OVERLAY_STATES

    def _should_use_canonical_push(self) -> bool:
        """Determine whether to use the canonical Invoice push path.

        Returns True when ``commerce.qbo_pushes_canonical`` is on,
        meaning the push payload is a canonical Invoice document built
        by CommerceExternalSync._build_qbo_push_payload. Returns False
        to fall back to the legacy free-form POD payload path.

        The flag defaults to True when ``commerce.backbone_enabled`` is
        on. This method reads the flag from config/settings.py via the
        feature_flag_service or falls back to the settings module
        directly. A lookup failure defaults to True (canonical path)
        when commerce backbone is enabled, False otherwise.
        """
        try:
            from config.settings import get_settings
            settings = get_settings()
            # The flag is only meaningful when commerce backbone is on.
            # When backbone is off, always use the legacy path.
            if not getattr(settings, "commerce_backbone_enabled", False):
                return False
            return getattr(settings, "commerce_qbo_pushes_canonical", True)
        except Exception:
            # If settings cannot be loaded (e.g. in tests without full
            # config), default to False (legacy path) for safety.
            return False

    # ------------------------------------------------------------------
    # sync_pull helpers
    # ------------------------------------------------------------------

    async def _query(
        self,
        entity: str,
        *,
        since: datetime,
        fields: str,
        page_size: int = 200,
    ) -> List[Dict[str, Any]]:
        """Page through QBO's Query API for ``entity`` updated since ``since``.

        Returns the combined list of entity dicts.
        """

        # QBO Query syntax: "SELECT ... FROM Entity WHERE MetaData.LastUpdatedTime > '...'"
        since_iso = _iso(since)
        out: List[Dict[str, Any]] = []
        start = 1
        while True:
            query = (
                f"SELECT {fields} FROM {entity} "
                f"WHERE MetaData.LastUpdatedTime > '{since_iso}' "
                f"STARTPOSITION {start} MAXRESULTS {page_size}"
            )
            body = await self._http_request_with_retry(
                method="GET",
                path="/v3/company/{realm_id}/query",
                params={"query": query},
            )
            response = body.get("QueryResponse") or {}
            rows = response.get(entity) or []
            if not rows:
                break
            out.extend(rows)
            if len(rows) < page_size:
                break
            start += page_size
        return out

    async def _apply_invoice_update(
        self, invoice: Mapping[str, Any]
    ) -> bool:
        """Fold one QBO Invoice into the matching ReconciliationRecord.

        Returns True when a record was updated.
        """

        invoice_id = str(invoice.get("Id") or "").strip()
        if not invoice_id:
            return False

        invoiced_gallons = _extract_invoice_gallons(invoice)
        if invoiced_gallons is None:
            logger.debug(
                "QuickBooksOnlineConnector: invoice %s has no "
                "quantity line; skipping variance update",
                invoice_id,
            )
            return False

        payment_status = _extract_payment_status(invoice)

        rec_id = await self._lookup_reconciliation_by_invoice(invoice_id)
        if not rec_id:
            return False
        if self._recon is None:
            logger.warning(
                "QuickBooksOnlineConnector: no reconciliation_service "
                "configured; cannot update invoice=%s",
                invoice_id,
            )
            return False

        try:
            await self._recon.update_invoice_fields(
                tenant_id=self._tenant_id,
                reconciliation_id=rec_id,
                invoice_id=invoice_id,
                invoiced_gallons=float(invoiced_gallons),
                payment_status=payment_status,
            )
            return True
        except (LookupError, PermissionError, ValueError) as exc:
            logger.warning(
                "QuickBooksOnlineConnector: failed to update "
                "reconciliation %s for invoice=%s: %s",
                rec_id,
                invoice_id,
                exc,
            )
            return False

    async def _apply_payment_update(
        self, payment: Mapping[str, Any]
    ) -> bool:
        """Apply a Payment event to any invoice lines it references."""

        updated = False
        for line in payment.get("Line") or []:
            linked = line.get("LinkedTxn") or []
            for link in linked:
                if (link or {}).get("TxnType") != "Invoice":
                    continue
                invoice_id = str(link.get("TxnId") or "").strip()
                if not invoice_id:
                    continue
                rec_id = await self._lookup_reconciliation_by_invoice(
                    invoice_id
                )
                if not rec_id or self._recon is None:
                    continue
                try:
                    # We don't have gallons on a Payment — re-submit
                    # the existing invoiced_gallons by reading the
                    # record first. The ReconciliationService API
                    # requires invoiced_gallons on every call, so we
                    # preserve it by looking it up.
                    invoiced_gallons = await self._lookup_invoiced_gallons(
                        rec_id
                    )
                    if invoiced_gallons is None:
                        continue
                    await self._recon.update_invoice_fields(
                        tenant_id=self._tenant_id,
                        reconciliation_id=rec_id,
                        invoice_id=invoice_id,
                        invoiced_gallons=float(invoiced_gallons),
                        payment_status="paid",
                    )
                    updated = True
                except (LookupError, PermissionError, ValueError) as exc:
                    logger.warning(
                        "QuickBooksOnlineConnector: failed to apply "
                        "payment to reconciliation %s (invoice=%s): %s",
                        rec_id,
                        invoice_id,
                        exc,
                    )
        return updated

    async def _lookup_reconciliation_by_invoice(
        self, invoice_id: str
    ) -> Optional[str]:
        """Return the reconciliation_id whose invoice_id matches ``invoice_id``."""

        if self._es is None:
            return None
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": self._tenant_id}},
                        {"term": {"invoice_id": invoice_id}},
                    ]
                }
            },
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(
                self._reconciliation_index, query, 1
            )
        except Exception as exc:
            logger.warning(
                "QuickBooksOnlineConnector: ES search for invoice=%s "
                "failed: %s",
                invoice_id,
                exc,
            )
            return None
        hits = (
            ((resp or {}).get("hits") or {}).get("hits") or []
        )
        for hit in hits:
            source = (hit or {}).get("_source") if isinstance(hit, dict) else None
            if isinstance(source, dict) and source.get("tenant_id") == self._tenant_id:
                rec_id = source.get("reconciliation_id")
                if isinstance(rec_id, str) and rec_id:
                    return rec_id
        return None

    async def _lookup_invoiced_gallons(
        self, reconciliation_id: str
    ) -> Optional[float]:
        """Return the current ``invoiced_gallons`` on the reconciliation record."""

        if self._es is None:
            return None
        try:
            doc = await self._es.get_document(
                self._reconciliation_index, reconciliation_id
            )
        except Exception:
            return None
        if not isinstance(doc, dict):
            return None
        source = doc.get("_source") if "_source" in doc else doc
        if not isinstance(source, dict):
            return None
        if source.get("tenant_id") != self._tenant_id:
            return None
        value = source.get("invoiced_gallons")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _error_run(
        self,
        *,
        run_id: str,
        operation: str,
        started_at: datetime,
        record_counts: Dict[str, int],
        reason: Optional[str],
        exc: BaseException,
    ) -> SyncRun:
        """Build an error :class:`SyncRun` with structured error details."""

        finished_at = _utcnow()
        message = str(exc) or exc.__class__.__name__
        error_details = (
            f"{reason}: {message}" if reason else message
        )
        return SyncRun(
            run_id=run_id,
            tenant_id=self._tenant_id,
            instance_id=self._instance_id,
            provider_name=self.provider_name,
            operation=operation,  # type: ignore[arg-type]
            started_at=started_at,
            finished_at=finished_at,
            status="error",
            record_counts=record_counts,
            error_details=error_details[:1000],
            duration_ms=max(
                0, int((finished_at - started_at).total_seconds() * 1000)
            ),
        )


# ---------------------------------------------------------------------------
# Payload shaping
# ---------------------------------------------------------------------------


def _build_invoice_body(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Shape a ``sync_push`` payload into a QBO Invoice creation body.

    This is the LEGACY path — reads from the free-form POD payload.
    Used when ``commerce.qbo_pushes_canonical`` is OFF.

    Raises :class:`ValueError` when required fields are missing so the
    scheduler surfaces the error to the operator rather than creating
    a malformed Invoice in QBO.
    """

    for field in ("customer_id", "delivered_gallons", "unit_price_usd"):
        if payload.get(field) in (None, ""):
            raise ValueError(
                f"sync_push payload missing required field {field!r}"
            )

    try:
        gallons = float(payload["delivered_gallons"])
        unit_price = float(payload["unit_price_usd"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"sync_push payload has non-numeric gallons / unit price: {exc}"
        ) from exc

    if gallons <= 0:
        raise ValueError("delivered_gallons must be > 0")
    if unit_price < 0:
        raise ValueError("unit_price_usd must be >= 0")

    line_description = (
        f"{payload.get('product_code', 'FUEL')} delivery "
        f"on {payload.get('delivery_date') or _iso(_utcnow())[:10]} "
        f"(pod_id={payload.get('pod_id', '')})"
    ).strip()

    body: Dict[str, Any] = {
        "CustomerRef": {"value": str(payload["customer_id"])},
        "Line": [
            {
                "Amount": round(gallons * unit_price, 2),
                "DetailType": "SalesItemLineDetail",
                "Description": line_description,
                "SalesItemLineDetail": {
                    "Qty": gallons,
                    "UnitPrice": unit_price,
                },
            }
        ],
    }
    if payload.get("delivery_date"):
        body["TxnDate"] = str(payload["delivery_date"])
    if payload.get("memo"):
        body["CustomerMemo"] = {"value": str(payload["memo"])}
    if payload.get("invoice_doc_number"):
        body["DocNumber"] = str(payload["invoice_doc_number"])
    return body


def _build_invoice_body_from_canonical(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Shape a canonical Invoice payload into a QBO Invoice creation body.

    This is the CANONICAL path — reads from the Invoice document built
    by :meth:`CommerceExternalSync._build_qbo_push_payload`. Used when
    ``commerce.qbo_pushes_canonical`` is ON (default once commerce
    backbone is live).

    The canonical payload carries structured line_items with integer-cent
    pricing, which this function converts to the QBO decimal-dollar
    format. Falls back to the legacy fields (``delivered_gallons``,
    ``unit_price_usd``) when ``line_items`` is empty so the transition
    is graceful.

    Raises :class:`ValueError` when required fields are missing.
    """

    customer_id = payload.get("customer_id")
    if not customer_id:
        raise ValueError(
            "sync_push canonical payload missing required field 'customer_id'"
        )

    line_items = payload.get("line_items") or []

    qbo_lines: list = []
    if line_items:
        # Build QBO line items from the canonical line_items array.
        # Each line item has product_code, quantity_gallons,
        # unit_price_cents, subtotal_cents.
        for item in line_items:
            qty = float(item.get("quantity_gallons", 0))
            unit_price_cents = int(item.get("unit_price_cents", 0))
            unit_price_usd = unit_price_cents / 100.0
            subtotal_cents = int(item.get("subtotal_cents", 0))
            amount_usd = subtotal_cents / 100.0
            product_code = item.get("product_code", "FUEL")

            description = (
                f"{product_code} delivery"
                f" on {payload.get('delivery_date') or _iso(_utcnow())[:10]}"
                f" (invoice={payload.get('invoice_id', '')})"
            ).strip()

            qbo_lines.append({
                "Amount": round(amount_usd, 2),
                "DetailType": "SalesItemLineDetail",
                "Description": description,
                "SalesItemLineDetail": {
                    "Qty": qty,
                    "UnitPrice": round(unit_price_usd, 4),
                },
            })
    else:
        # Fallback: use the top-level delivered_gallons / unit_price_usd
        # fields that CommerceExternalSync._build_qbo_push_payload also
        # populates for backwards compatibility.
        gallons = float(payload.get("delivered_gallons", 0))
        unit_price = float(payload.get("unit_price_usd", 0))
        if gallons <= 0:
            raise ValueError(
                "sync_push canonical payload has no line_items and "
                "delivered_gallons <= 0"
            )

        description = (
            f"{payload.get('product_code', 'FUEL')} delivery"
            f" on {payload.get('delivery_date') or _iso(_utcnow())[:10]}"
            f" (invoice={payload.get('invoice_id', '')})"
        ).strip()

        qbo_lines.append({
            "Amount": round(gallons * unit_price, 2),
            "DetailType": "SalesItemLineDetail",
            "Description": description,
            "SalesItemLineDetail": {
                "Qty": gallons,
                "UnitPrice": unit_price,
            },
        })

    body: Dict[str, Any] = {
        "CustomerRef": {"value": str(customer_id)},
        "Line": qbo_lines,
    }

    if payload.get("delivery_date"):
        body["TxnDate"] = str(payload["delivery_date"])
    if payload.get("memo"):
        body["CustomerMemo"] = {"value": str(payload["memo"])}
    if payload.get("invoice_doc_number"):
        body["DocNumber"] = str(payload["invoice_doc_number"])

    # Attach the canonical invoice_id as a CustomField so QBO-side
    # queries can correlate back to the platform's Invoice record.
    invoice_id = payload.get("invoice_id")
    if invoice_id:
        body["CustomField"] = [
            {
                "DefinitionId": "1",
                "Name": "InvoiceId",
                "Type": "StringType",
                "StringValue": str(invoice_id),
            }
        ]

    return body


def _extract_invoice_gallons(invoice: Mapping[str, Any]) -> Optional[float]:
    """Pull the gallons quantity from a QBO Invoice's line-item payload."""

    for line in invoice.get("Line") or []:
        detail = (
            (line or {}).get("SalesItemLineDetail")
            or (line or {}).get("ItemBasedExpenseLineDetail")
            or {}
        )
        qty = detail.get("Qty")
        if qty in (None, ""):
            continue
        try:
            return float(qty)
        except (TypeError, ValueError):
            continue
    return None


def _extract_payment_status(invoice: Mapping[str, Any]) -> Optional[str]:
    """Derive a coarse payment_status from an Invoice balance + total.

    QBO does not surface a ``payment_status`` enum directly — the
    standard idiom is to compare ``Balance`` to ``TotalAmt``.
    """

    try:
        total = float(invoice.get("TotalAmt") or 0.0)
        balance = float(invoice.get("Balance") or 0.0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    if balance <= 0:
        return "paid"
    if balance < total:
        return "partial"
    return "unpaid"


__all__ = [
    "CREDENTIALS_EXPIRED_REASON",
    "DEFAULT_OAUTH_REVOKE_URL",
    "DEFAULT_OAUTH_TOKEN_URL",
    "DEFAULT_QBO_API_BASE_URL",
    "DEFAULT_RATE_LIMIT_PER_MINUTE",
    "QBO_INVOICE_PUSH_FLAG_KEY",
    "QuickBooksCredentialsExpired",
    "QuickBooksOnlineConnector",
    "QuickBooksRateLimitExceeded",
    "RATE_LIMIT_KEY_TEMPLATE",
    "VAULT_CREDENTIAL_KEY",
    "_build_invoice_body_from_canonical",
    "build_catalog_entry",
    "register_catalog_entry",
]
