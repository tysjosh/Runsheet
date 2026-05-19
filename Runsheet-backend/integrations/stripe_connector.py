"""
Stripe Connector — payment reference integration (Req 5.5.*).

Implements the Stripe side of the pluggable integration framework
introduced in Capability 5 / Phase 9 of the fuel-ops hardening spec:

    * ``connect(credentials)`` — validates a credential envelope
      carrying ``secret_key``, ``publishable_key``, and
      ``webhook_secret``, persists the envelope into the
      Tenant_Credentials_Vault, and returns a redacted
      :class:`integrations.connector_base.ConnectionResult` whose
      ``metadata`` exposes ONLY the ``publishable_key`` — the secret
      key and webhook secret never cross the API boundary
      (Requirement 5.5.1, 5.1.8).

    * ``get_publishable_key()`` — reads the envelope out of the vault
      and returns just the ``publishable_key``. Used by the
      ``GET /api/integrations/stripe/public-config`` endpoint
      (Requirement 5.5.2).

    * ``sync_push(payload)`` — creates a Stripe
      :class:`PaymentIntent` for a finalized POD / reconciliation
      record when the tenant's ``overlay.stripe_autocharge`` feature
      flag is in an active overlay state (Req 5.5.3). Payments whose
      amount (in USD) is at or above the
      tenant-configurable ceiling ``stripe.autocharge_ceiling_usd:
      {tenant_id}`` (default :data:`DEFAULT_AUTOCHARGE_CEILING_USD`)
      are routed through :class:`Agents.confirmation_protocol
      .ConfirmationProtocol` with risk HIGH rather than auto-charged
      (Req 5.5.5).

    * ``sync_pull(since)`` — lists recent :class:`PaymentIntent`s via
      ``stripe.PaymentIntent.list(created={"gte": ...})`` and folds
      each into the matching :class:`services.reconciliation_service
      .ReconciliationRecord` via ``update_payment_status`` when the
      PaymentIntent's metadata carries a ``reconciliation_id``
      (Req 5.5.4).

    * ``verify_webhook_signature(payload_bytes, signature_header)`` —
      wraps ``stripe.Webhook.construct_event`` using the tenant's
      stored ``webhook_secret`` (Req 5.5.4). Raises on invalid
      signature so the endpoint can return HTTP 400.

    * ``handle_webhook_event(event)`` — dispatches on ``event.type``:
      ``payment_intent.succeeded`` → ``payment_status="paid"``;
      ``payment_intent.payment_failed`` → ``payment_status="failed"``;
      ``payment_intent.processing`` → ``payment_status="processing"``.
      Calls :meth:`ReconciliationService.update_payment_status` when
      the PaymentIntent's metadata carries a
      ``reconciliation_id``.

    * ``disconnect()`` — deletes the vault envelope. Idempotent.

Cross-cutting invariants:

    * **Lazy ``stripe`` import.** The :mod:`stripe` SDK is imported
      inside each method rather than at module load so unit tests can
      swap in a stub without the real SDK being installed, mirroring
      the lazy intuit-oauth pattern in
      :mod:`integrations.quickbooks_online`.

    * **Credentials stay in the vault.** Neither the secret key nor the
      webhook secret is ever logged, echoed back on an API surface, or
      persisted outside the Tenant_Credentials_Vault (Requirement
      5.5.1, 5.1.8). The public ``metadata`` on the
      :class:`ConnectionResult` is the ONLY value carrying publishable
      material.

    * **Feature-flag gating on push.** ``sync_push`` short-circuits to
      a no-op :class:`SyncRun` when ``overlay.stripe_autocharge`` is
      not in an active overlay state. Mirrors the QBO pattern so
      Storm_Mode's ``shadow`` posture keeps payments idle.

    * **Ceiling escalation on HIGH risk.** Payments at or above
      ``stripe_autocharge_ceiling_usd`` are routed through
      :class:`ConfirmationProtocol.process_mutation` with
      ``tool_name='stripe_charge_payment_intent'`` rather than being
      charged inline. The resulting :class:`SyncRun` reports
      ``{"escalated_to_confirmation": 1}`` with status ``success``
      (the escalation is a valid outcome, not an error).

Validates: Requirements 5.5.1, 5.5.2, 5.5.3, 5.5.4, 5.5.5, 5.5.7.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List, Mapping, Optional
from uuid import uuid4

from integrations.connector_base import (
    ConnectionResult,
    IntegrationConnector,
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

#: Overlay feature flag that gates the auto-charge push flow
#: (Requirement 5.5.3). Read via FeatureFlagService.get_overlay_state.
STRIPE_AUTOCHARGE_FLAG_KEY: str = "overlay.stripe_autocharge"

#: Overlay states that mean "stripe auto-charge is on for this tenant".
#: Mirrors the convention used by
#: :mod:`integrations.quickbooks_online` so Storm_Mode's ``shadow``
#: posture keeps payments idle.
_ACTIVE_OVERLAY_STATES: "frozenset[str]" = frozenset(
    {"active_gated", "active_auto"}
)

#: Per-tenant auto-charge ceiling (USD). Overridable via Redis key
#: :data:`AUTOCHARGE_CEILING_REDIS_KEY_TEMPLATE`. Payments whose
#: ``amount_usd`` is at or above this value are escalated through the
#: Confirmation_Protocol as HIGH risk rather than charged inline
#: (Requirement 5.5.5).
DEFAULT_AUTOCHARGE_CEILING_USD: float = 5000.0

#: Redis key template for the tenant-configurable auto-charge ceiling.
AUTOCHARGE_CEILING_REDIS_KEY_TEMPLATE: str = "stripe.autocharge_ceiling_usd:{tenant_id}"

#: Vault key used to persist the Stripe credential envelope. Scoped
#: by tenant so multiple Stripe instances share a single record.
VAULT_CREDENTIAL_KEY: str = "stripe_envelope"

#: Tool name reported to :class:`ConfirmationProtocol` when a charge
#: exceeds the ceiling and requires approval. Kept stable so the
#: approval-queue UI and any risk-registry overrides stay wired.
HIGH_RISK_TOOL_NAME: str = "stripe_charge_payment_intent"

#: ``MutationRequest.agent_id`` tag stamped on escalations so the
#: approval queue groups Stripe mutations together.
STRIPE_AGENT_ID: str = "stripe_connector"

#: Stripe webhook event types this connector acts on. The tuple is
#: also surfaced in the catalog description so operators know which
#: events the connector consumes.
SUPPORTED_WEBHOOK_EVENT_TYPES: "tuple[str, ...]" = (
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "payment_intent.processing",
    "payment_intent.canceled",
)

#: Map from Stripe event.type → canonical ReconciliationRecord.payment_status.
_EVENT_TYPE_TO_PAYMENT_STATUS: Dict[str, str] = {
    "payment_intent.succeeded": "paid",
    "payment_intent.payment_failed": "failed",
    "payment_intent.processing": "processing",
    "payment_intent.canceled": "canceled",
}

#: Currency Stripe PaymentIntents are charged in. Fuel-ops hardening
#: scope is US-only; override via config when a tenant operates in
#: another currency.
DEFAULT_CURRENCY: str = "usd"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StripeSignatureVerificationError(RuntimeError):
    """Raised when a webhook payload fails Stripe's signature check.

    The REST handler maps this to HTTP 400 so an unsigned or
    replayed webhook never mutates a reconciliation record.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _autocharge_ceiling_key(tenant_id: str) -> str:
    return AUTOCHARGE_CEILING_REDIS_KEY_TEMPLATE.format(tenant_id=tenant_id)


def _coerce_amount_usd(value: Any) -> float:
    """Best-effort coerce a payload ``amount_usd`` into a non-negative float."""

    if value is None:
        return 0.0
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"sync_push payload amount_usd must be numeric, got {value!r}"
        ) from exc
    if amount < 0:
        raise ValueError("sync_push payload amount_usd must be >= 0")
    return amount


# ---------------------------------------------------------------------------
# Provider-catalog entry (Task 9.10 convenience)
# ---------------------------------------------------------------------------


def build_catalog_entry() -> ProviderCatalogEntry:
    """Return the :class:`ProviderCatalogEntry` for this connector.

    Task 9.10 wires every connector into the shared catalog at
    bootstrap time via :func:`register_catalog_entry`; this helper is
    also used directly by ``/api/integrations/providers`` tests that
    want to assert the shape without triggering registration
    side-effects.

    The catalog entry's ``feature_flag_key`` is intentionally left
    unset so :meth:`ProviderCatalogEntry.effective_feature_flag_key`
    surfaces the Marketplace-level default
    ``overlay.integration.stripe`` (Requirement 5.6.6).
    :data:`STRIPE_AUTOCHARGE_FLAG_KEY` remains a separate,
    behaviour-level flag that gates the auto-charge path inside
    :meth:`StripeConnector.sync_push` — it is not the Marketplace
    visibility flag.
    """

    description = (
        "Capture customer payments via Stripe: auto-create PaymentIntents "
        "on POD finalization (gated by overlay.stripe_autocharge) and "
        "update reconciliation payment_status from signed "
        "payment_intent webhook events. Payments above the tenant's "
        "configured ceiling are routed through the Confirmation_Protocol "
        "for operator approval."
    )
    return ProviderCatalogEntry(
        provider_name="stripe",
        category="payment",
        description=description,
        required_credential_fields=[
            "secret_key",
            "publishable_key",
            "webhook_secret",
        ],
        doc_url="https://stripe.com/docs/api",
        auth_mode="api_key",
    )


def register_catalog_entry() -> ProviderCatalogEntry:
    """Register the connector with the shared provider catalog.

    Kept as a helper (rather than inline at module import time) so a
    test that imports this module does not auto-register with the
    global catalog.
    """

    return register_provider(build_catalog_entry())


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class StripeConnector(IntegrationConnector):
    """Stripe adapter (payment category).

    Args:
        tenant_id: Owning tenant; every Redis / vault / ES write is
            re-scoped on this id.
        instance_id: Owning :class:`IntegrationInstance` id — stamped
            on every :class:`SyncRun` the connector returns.
        credentials_vault: The shared
            :class:`services.credentials_vault.TenantCredentialsVault`.
            Required. The connector NEVER logs or returns the
            plaintext envelope; it only references it by opaque
            ``credentials_ref``.
        credentials_ref: Existing vault reference (``None`` means this
            instance has not completed ``connect()`` yet; only
            ``connect()`` and ``disconnect()`` are valid in that state).
        reconciliation_service: Required for ``sync_pull`` and
            ``handle_webhook_event``. Must expose
            :meth:`update_payment_status` per
            :mod:`services.reconciliation_service`.
        feature_flag_service: Required for ``sync_push``. Must expose
            ``await get_overlay_state(flag_key, tenant_id) -> str``.
            A missing / erroring flag service yields a silent skip
            (fail-closed).
        confirmation_protocol: Required for ``sync_push`` escalations.
            Must expose ``await process_mutation(MutationRequest) ->
            MutationResult`` (see :mod:`Agents.confirmation_protocol`).
        redis_client: Optional async Redis client used to read the
            per-tenant auto-charge ceiling. When ``None`` the platform
            default :data:`DEFAULT_AUTOCHARGE_CEILING_USD` is used.
        es_service: Optional ES service used to look up reconciliation
            records by ``payment_intent_id`` during ``sync_pull``.
        http_client: Reserved for future use (signature parity with
            the QBO / Veeder-Root connectors) — the Stripe SDK
            manages its own HTTP client, so this is not wired yet.
        stripe_module: Optional injected Stripe SDK module. When
            supplied tests can hand in an in-memory stub with
            ``PaymentIntent`` and ``Webhook`` attributes; production
            leaves this as ``None`` and the connector imports
            ``stripe`` lazily on first use.
        clock: Zero-arg callable returning ``float`` seconds since the
            epoch. Injected for deterministic tests.
    """

    category: ClassVar[str] = "payment"
    provider_name: ClassVar[str] = "stripe"

    def __init__(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        credentials_vault: Any,
        credentials_ref: Optional[str] = None,
        reconciliation_service: Optional[Any] = None,
        feature_flag_service: Optional[Any] = None,
        confirmation_protocol: Optional[Any] = None,
        redis_client: Optional[Any] = None,
        es_service: Optional[Any] = None,
        http_client: Optional[Any] = None,
        stripe_module: Optional[Any] = None,
        default_autocharge_ceiling_usd: float = DEFAULT_AUTOCHARGE_CEILING_USD,
        clock: Any = time.time,
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ValueError("instance_id must be a non-empty string")
        if credentials_vault is None:
            raise ValueError("credentials_vault is required")
        if default_autocharge_ceiling_usd < 0:
            raise ValueError("default_autocharge_ceiling_usd must be >= 0")

        self._tenant_id = tenant_id
        self._instance_id = instance_id
        self._vault = credentials_vault
        self._credentials_ref = credentials_ref
        self._recon = reconciliation_service
        self._feature_flags = feature_flag_service
        self._confirmation = confirmation_protocol
        self._redis = redis_client
        self._es = es_service
        self._http_client = http_client
        self._stripe_module = stripe_module
        self._default_ceiling = float(default_autocharge_ceiling_usd)
        self._clock = clock

        # In-memory cache of the credential envelope so a single
        # webhook verify / sync_pull does not round-trip to the vault
        # for every call.
        self._cached_envelope: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # IntegrationConnector API
    # ------------------------------------------------------------------

    async def connect(self, credentials: Mapping[str, Any]) -> ConnectionResult:
        """Validate and persist a Stripe credential envelope.

        Expected ``credentials`` shape::

            {
                "secret_key":      "sk_live_...",   # required
                "publishable_key": "pk_live_...",   # required
                "webhook_secret":  "whsec_...",     # required
            }

        The connector does NOT call Stripe during ``connect`` — the
        secret key is validated lazily on first ``sync_pull`` /
        ``sync_push``. The envelope is written to the
        Tenant_Credentials_Vault and the returned
        :class:`ConnectionResult` exposes ONLY the ``publishable_key``
        as non-secret metadata (Requirement 5.5.1, 5.1.8).
        """

        required = ("secret_key", "publishable_key", "webhook_secret")
        missing = [k for k in required if not credentials.get(k)]
        if missing:
            return ConnectionResult(
                status="error",
                message=f"missing required credential fields: {sorted(missing)}",
            )

        envelope: Dict[str, Any] = {
            "secret_key": str(credentials["secret_key"]).strip(),
            "publishable_key": str(credentials["publishable_key"]).strip(),
            "webhook_secret": str(credentials["webhook_secret"]).strip(),
        }

        ref = await self._vault.put(
            tenant_id=self._tenant_id,
            key=VAULT_CREDENTIAL_KEY,
            plaintext=envelope,
            provider_name=self.provider_name,
        )
        self._credentials_ref = ref
        self._cached_envelope = dict(envelope)

        logger.info(
            "StripeConnector.connect: stored credentials tenant=%s "
            "instance=%s credentials_ref=%s",
            self._tenant_id,
            self._instance_id,
            ref,
        )
        return ConnectionResult(
            status="connected",
            credentials_ref=ref,
            metadata={"publishable_key": envelope["publishable_key"]},
        )

    async def sync_pull(self, since: datetime) -> SyncRun:
        """Import recent PaymentIntents and update matching reconciliations.

        Queries Stripe for every :class:`PaymentIntent` created after
        ``since`` via ``stripe.PaymentIntent.list(created={"gte": ...})``.
        For each result whose ``metadata.reconciliation_id`` resolves
        to a known reconciliation record, the service's
        :meth:`update_payment_status` is called with the canonical
        status mapping.
        """

        run_id = f"stripe_pull_{uuid4()}"
        started_at = _utcnow()
        counts: Dict[str, int] = {
            "payment_intents_processed": 0,
            "reconciliations_updated": 0,
            "skipped_no_metadata": 0,
            "skipped_no_match": 0,
        }

        try:
            stripe_sdk = await self._get_stripe_module()
            envelope = await self._load_envelope()
            stripe_sdk.api_key = envelope["secret_key"]

            since_ts = int(since.timestamp())

            def _list() -> Any:
                # Wrap the synchronous Stripe SDK call so we don't
                # block the event loop on its network I/O.
                return stripe_sdk.PaymentIntent.list(
                    created={"gte": since_ts}, limit=100
                )

            listing = await asyncio.to_thread(_list)
            items = self._extract_list_items(listing)

            for intent in items:
                counts["payment_intents_processed"] += 1
                payment_status = _EVENT_TYPE_TO_PAYMENT_STATUS.get(
                    f"payment_intent.{intent.get('status', '').replace('requires_', '')}",
                    None,
                )
                # Map raw Stripe PaymentIntent.status to our payment_status
                # when the event-based mapping doesn't produce a match.
                if payment_status is None:
                    payment_status = self._map_intent_status(
                        str(intent.get("status") or "")
                    )
                if payment_status is None:
                    # Unknown transient state — skip rather than write
                    # an arbitrary value.
                    continue

                metadata = intent.get("metadata") or {}
                reconciliation_id = None
                if isinstance(metadata, dict):
                    reconciliation_id = metadata.get("reconciliation_id")
                if not reconciliation_id:
                    counts["skipped_no_metadata"] += 1
                    continue

                if self._recon is None:
                    counts["skipped_no_match"] += 1
                    continue

                try:
                    await self._recon.update_payment_status(
                        tenant_id=self._tenant_id,
                        reconciliation_id=reconciliation_id,
                        payment_status=payment_status,
                        payment_intent_id=intent.get("id"),
                    )
                    counts["reconciliations_updated"] += 1
                except (LookupError, PermissionError, ValueError) as exc:
                    logger.warning(
                        "StripeConnector.sync_pull: could not update "
                        "reconciliation=%s for payment_intent=%s: %s",
                        reconciliation_id,
                        intent.get("id"),
                        exc,
                    )
                    counts["skipped_no_match"] += 1

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
        """Create a Stripe :class:`PaymentIntent` for a finalized POD.

        Expected ``payload`` shape::

            {
                "pod_id":            "...",        # optional, audit only
                "reconciliation_id": "...",        # required — metadata.reconciliation_id
                "customer_id":       "cus_...",    # optional Stripe customer
                "amount_usd":        123.45,       # required; USD
                "currency":          "usd",        # optional; defaults to usd
                "description":       "optional",
                "metadata":          {...},        # optional extra metadata
                "payment_method_id": "pm_...",     # optional
            }

        Returns a terminal :class:`SyncRun`. When
        ``overlay.stripe_autocharge`` is not in an active overlay
        state the call short-circuits with ``skipped_disabled=1``.
        When the payment amount meets or exceeds the tenant's
        auto-charge ceiling the call routes through the
        Confirmation_Protocol with risk HIGH and reports
        ``escalated_to_confirmation=1`` rather than calling Stripe
        (Requirement 5.5.5).
        """

        run_id = f"stripe_push_{uuid4()}"
        started_at = _utcnow()
        counts: Dict[str, int] = {
            "payment_intents_created": 0,
            "escalated_to_confirmation": 0,
            "skipped_disabled": 0,
            "failed": 0,
        }

        enabled = await self._is_autocharge_enabled()
        if not enabled:
            counts["skipped_disabled"] = 1
            logger.info(
                "StripeConnector.sync_push: skipping push — "
                "overlay.stripe_autocharge disabled for tenant=%s",
                self._tenant_id,
            )
            return self._terminal_run(
                run_id=run_id,
                operation="push",
                started_at=started_at,
                record_counts=counts,
                status="success",
            )

        try:
            amount_usd = _coerce_amount_usd(payload.get("amount_usd"))
            ceiling = await self._resolve_ceiling()
            if amount_usd >= ceiling:
                escalated = await self._escalate_through_confirmation(
                    payload=payload,
                    amount_usd=amount_usd,
                    ceiling=ceiling,
                )
                counts["escalated_to_confirmation"] = 1 if escalated else 0
                if not escalated:
                    # No confirmation_protocol wired; log and fall
                    # through as a success (the push was NOT executed
                    # — we deliberately refuse to auto-charge above
                    # the ceiling).
                    counts["skipped_disabled"] = 1
                return self._terminal_run(
                    run_id=run_id,
                    operation="push",
                    started_at=started_at,
                    record_counts=counts,
                    status="success",
                )

            stripe_sdk = await self._get_stripe_module()
            envelope = await self._load_envelope()
            stripe_sdk.api_key = envelope["secret_key"]

            body = _build_payment_intent_body(payload, amount_usd)

            def _create() -> Any:
                return stripe_sdk.PaymentIntent.create(**body)

            intent = await asyncio.to_thread(_create)
            intent_dict = _as_dict(intent)
            counts["payment_intents_created"] = 1

            logger.info(
                "StripeConnector.sync_push: created payment_intent "
                "tenant=%s pod=%s payment_intent_id=%s amount_usd=%.2f",
                self._tenant_id,
                payload.get("pod_id"),
                intent_dict.get("id"),
                amount_usd,
            )

            run = self._terminal_run(
                run_id=run_id,
                operation="push",
                started_at=started_at,
                record_counts=counts,
                status="success",
            )
            # Record the payment_intent id on the record_counts via a
            # dedicated key so the scheduler can surface it in logs;
            # SyncRun.record_counts is a Dict[str, int] so we stop
            # short of persisting the full id here.
            return run

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

    async def list_payments(
        self,
        *,
        limit: int = 10,
        starting_after: Optional[str] = None,
        created_gte: Optional[datetime] = None,
        created_lte: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Return a paginated, redacted page of recent PaymentIntents.

        Thin read-only wrapper around ``stripe.PaymentIntent.list`` used
        by the ``GET /api/integrations/stripe/payments`` endpoint
        (Requirement 5.5.6). The connector never returns raw Stripe
        objects on this path: every item is reduced to an operator-safe
        subset via :func:`_redact_payment_intent` so card numbers,
        payment method data, receipt emails, ``client_secret`` and
        anything else PII-adjacent stay inside the Stripe boundary
        (Requirement 5.1.8).

        Args:
            limit: Page size. Values above 100 are clamped to 100 to
                match Stripe's own upper bound and keep response sizes
                bounded. Non-positive values coerce to 1.
            starting_after: Stripe PaymentIntent id — the cursor for
                "give me items created before this one". Forwarded
                verbatim when provided.
            created_gte: Optional inclusive lower bound on
                ``PaymentIntent.created`` (a UTC epoch in Stripe's
                wire format).
            created_lte: Optional inclusive upper bound on
                ``PaymentIntent.created``.

        Returns:
            ``{"items": [redacted, ...], "has_more": bool,
            "next_starting_after": Optional[str]}``.
            ``next_starting_after`` is the id of the final item in the
            page when ``has_more`` is true, otherwise ``None`` so the
            client can stop paging.

        Validates: Requirement 5.5.6, 5.1.8.
        """

        try:
            bounded_limit = int(limit)
        except (TypeError, ValueError):
            bounded_limit = 10
        if bounded_limit < 1:
            bounded_limit = 1
        if bounded_limit > 100:
            bounded_limit = 100

        # Check if we should use mock data from Elasticsearch (demo mode)
        use_mock_data = self._tenant_id == "demo-tenant"
        
        if use_mock_data:
            # Read from Elasticsearch mock data instead of calling Stripe API
            items = await self._list_payments_from_es(
                limit=bounded_limit,
                starting_after=starting_after,
                created_gte=created_gte,
                created_lte=created_lte,
            )
            
            # Determine if there are more results
            has_more = len(items) == bounded_limit
            next_starting_after: Optional[str] = None
            if has_more and items:
                last_id = items[-1].get("id")
                if isinstance(last_id, str) and last_id:
                    next_starting_after = last_id
            
            return {
                "items": items,
                "has_more": has_more,
                "next_starting_after": next_starting_after,
            }
        
        # Production path: call real Stripe API
        stripe_sdk = await self._get_stripe_module()
        envelope = await self._load_envelope()
        stripe_sdk.api_key = envelope["secret_key"]

        kwargs: Dict[str, Any] = {"limit": bounded_limit}
        if starting_after:
            kwargs["starting_after"] = str(starting_after)
        created_filter: Dict[str, int] = {}
        if created_gte is not None:
            created_filter["gte"] = int(created_gte.timestamp())
        if created_lte is not None:
            created_filter["lte"] = int(created_lte.timestamp())
        if created_filter:
            kwargs["created"] = created_filter

        def _list() -> Any:
            return stripe_sdk.PaymentIntent.list(**kwargs)

        listing = await asyncio.to_thread(_list)
        raw_items = self._extract_list_items(listing)
        items = [_redact_payment_intent(item) for item in raw_items]

        has_more = False
        if isinstance(listing, dict):
            has_more = bool(listing.get("has_more", False))
        else:
            has_more = bool(getattr(listing, "has_more", False))

        next_starting_after: Optional[str] = None
        if has_more and items:
            last_id = items[-1].get("id")
            if isinstance(last_id, str) and last_id:
                next_starting_after = last_id

        return {
            "items": items,
            "has_more": has_more,
            "next_starting_after": next_starting_after,
        }

    async def _list_payments_from_es(
        self,
        limit: int,
        starting_after: Optional[str] = None,
        created_gte: Optional[datetime] = None,
        created_lte: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Read mock payment data from Elasticsearch for demo mode.
        
        Args:
            limit: Maximum number of payments to return
            starting_after: Payment ID to start after (for pagination)
            created_gte: Lower bound on created timestamp
            created_lte: Upper bound on created timestamp
            
        Returns:
            List of redacted payment intent dictionaries
        """
        from datetime import datetime, timezone
        
        # Build Elasticsearch query
        must_clauses = [
            {"term": {"tenant_id": self._tenant_id}}
        ]
        
        # Add date range filter if provided
        if created_gte or created_lte:
            range_filter = {}
            if created_gte:
                range_filter["gte"] = created_gte.isoformat()
            if created_lte:
                range_filter["lte"] = created_lte.isoformat()
            must_clauses.append({"range": {"created": range_filter}})
        
        # Build search query
        query = {
            "query": {
                "bool": {
                    "must": must_clauses
                }
            },
            "sort": [
                {"created": {"order": "desc"}}
            ]
        }
        
        # Add search_after for pagination if provided
        if starting_after:
            # Find the document with starting_after ID to get its sort values
            try:
                doc = await self._es.get_document(
                    "stripe_payment_intents",
                    starting_after
                )
                if doc and "created" in doc:
                    created_ts = doc.get("created")
                    if created_ts:
                        query["search_after"] = [created_ts]
            except Exception as e:
                logger.warning(
                    "Failed to find starting_after document %s: %s",
                    starting_after,
                    e
                )
        
        # Execute search
        try:
            result = await self._es.search_documents(
                "stripe_payment_intents",
                query,
                size=limit
            )
            
            hits = result.get("hits", {}).get("hits", [])
            items = []
            
            for hit in hits:
                source = hit.get("_source", {})
                # Convert our ES format to Stripe-like format for redaction
                payment_intent = {
                    "id": source.get("payment_id"),
                    "amount": source.get("amount"),
                    "currency": source.get("currency"),
                    "status": source.get("status"),
                    "customer": source.get("customer_id"),
                    "description": source.get("description"),
                    "payment_method": source.get("payment_method"),
                    "payment_method_details": source.get("payment_method_details"),
                    "metadata": source.get("metadata", {}),
                    "created": source.get("created"),
                }
                
                # Add customer email and name if available
                if source.get("customer_email"):
                    payment_intent["customer_email"] = source.get("customer_email")
                if source.get("customer_name"):
                    payment_intent["customer_name"] = source.get("customer_name")
                
                # Redact the payment intent
                redacted = _redact_payment_intent(payment_intent)
                items.append(redacted)
            
            return items
            
        except Exception as e:
            logger.error(
                "Failed to query mock payments from ES for tenant=%s: %s",
                self._tenant_id,
                e
            )
            return []

    async def disconnect(self) -> None:
        """Delete the vault envelope. Idempotent."""

        if not self._credentials_ref:
            return
        try:
            await self._vault.delete(self._tenant_id, self._credentials_ref)
        except Exception as exc:
            logger.warning(
                "StripeConnector.disconnect: vault delete failed for "
                "tenant=%s ref=%s: %s",
                self._tenant_id,
                self._credentials_ref,
                exc,
            )
        self._credentials_ref = None
        self._cached_envelope = None

    # ------------------------------------------------------------------
    # Public non-ABC API
    # ------------------------------------------------------------------

    async def get_publishable_key(self) -> str:
        """Return the stored publishable_key.

        Used by the public-config endpoint
        (``GET /api/integrations/stripe/public-config``). The secret
        key and webhook secret are never returned here — only the
        publishable key, which is meant for client-side use anyway
        (Requirement 5.5.2).
        """

        envelope = await self._load_envelope()
        publishable_key = envelope.get("publishable_key")
        if not isinstance(publishable_key, str) or not publishable_key:
            raise RuntimeError(
                "StripeConnector: credential envelope is missing publishable_key"
            )
        return publishable_key

    async def verify_webhook_signature(
        self,
        payload_bytes: bytes,
        signature_header: str,
    ) -> Dict[str, Any]:
        """Verify a Stripe webhook signature and return the parsed event dict.

        Wraps :func:`stripe.Webhook.construct_event`. Raises
        :class:`StripeSignatureVerificationError` on any signature
        / parse failure so the REST handler can uniformly return
        HTTP 400.
        """

        if not isinstance(payload_bytes, (bytes, bytearray)):
            raise StripeSignatureVerificationError(
                "payload_bytes must be bytes"
            )
        if not isinstance(signature_header, str) or not signature_header:
            raise StripeSignatureVerificationError(
                "signature_header must be a non-empty string"
            )

        envelope = await self._load_envelope()
        webhook_secret = envelope.get("webhook_secret")
        if not isinstance(webhook_secret, str) or not webhook_secret:
            raise StripeSignatureVerificationError(
                "tenant does not have a webhook_secret configured"
            )

        stripe_sdk = await self._get_stripe_module()
        webhook_api = getattr(stripe_sdk, "Webhook", None)
        if webhook_api is None or not hasattr(webhook_api, "construct_event"):
            raise StripeSignatureVerificationError(
                "stripe SDK is missing Webhook.construct_event"
            )
        # ``stripe.Webhook.construct_event`` raises
        # :class:`stripe.error.SignatureVerificationError` — we catch
        # broadly so a stub that raises ``ValueError`` or a custom
        # exception is still mapped to our canonical error class.
        try:
            event = await asyncio.to_thread(
                webhook_api.construct_event,
                payload_bytes,
                signature_header,
                webhook_secret,
            )
        except Exception as exc:
            raise StripeSignatureVerificationError(
                f"signature verification failed: {exc}"
            ) from exc

        return _as_dict(event)

    async def handle_webhook_event(
        self, event: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Dispatch on ``event.type`` and update ReconciliationService.

        Supported event types map to a canonical ``payment_status``:

            * ``payment_intent.succeeded``       → ``paid``
            * ``payment_intent.payment_failed``  → ``failed``
            * ``payment_intent.processing``      → ``processing``
            * ``payment_intent.canceled``        → ``canceled``

        Events without a ``metadata.reconciliation_id`` on their data
        object are logged and ignored — the Stripe dashboard captures
        the authoritative record regardless. Other event types are
        surfaced in the returned summary as ``ignored`` so the REST
        handler can return 200 without mutating anything.
        """

        event_type = str(event.get("type") or "")
        summary: Dict[str, Any] = {
            "event_type": event_type,
            "handled": False,
            "reason": None,
            "payment_intent_id": None,
            "reconciliation_id": None,
            "payment_status": None,
        }

        if event_type not in _EVENT_TYPE_TO_PAYMENT_STATUS:
            summary["reason"] = "ignored_event_type"
            return summary

        data = event.get("data") or {}
        intent = data.get("object") if isinstance(data, dict) else None
        if not isinstance(intent, dict):
            summary["reason"] = "missing_data_object"
            return summary

        metadata = intent.get("metadata") or {}
        reconciliation_id = None
        if isinstance(metadata, dict):
            reconciliation_id = metadata.get("reconciliation_id")

        payment_status = _EVENT_TYPE_TO_PAYMENT_STATUS[event_type]
        summary["payment_intent_id"] = intent.get("id")
        summary["reconciliation_id"] = reconciliation_id
        summary["payment_status"] = payment_status

        if not reconciliation_id:
            summary["reason"] = "missing_reconciliation_id"
            return summary
        if self._recon is None:
            summary["reason"] = "reconciliation_service_not_wired"
            return summary

        try:
            await self._recon.update_payment_status(
                tenant_id=self._tenant_id,
                reconciliation_id=reconciliation_id,
                payment_status=payment_status,
                payment_intent_id=intent.get("id"),
            )
        except (LookupError, PermissionError, ValueError) as exc:
            summary["reason"] = f"update_failed: {exc}"
            logger.warning(
                "StripeConnector.handle_webhook_event: update failed "
                "tenant=%s reconciliation=%s event=%s: %s",
                self._tenant_id,
                reconciliation_id,
                event_type,
                exc,
            )
            return summary

        summary["handled"] = True
        return summary

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _load_envelope(self) -> Dict[str, Any]:
        """Fetch the credential envelope (vault or cache) as a dict."""

        if self._cached_envelope is not None:
            return dict(self._cached_envelope)
        if not self._credentials_ref:
            raise RuntimeError(
                "StripeConnector: no credentials_ref — call connect() first"
            )
        envelope = await self._vault.get(
            self._tenant_id, self._credentials_ref
        )
        if not isinstance(envelope, dict):
            raise RuntimeError(
                "StripeConnector: vault returned non-dict envelope"
            )
        self._cached_envelope = dict(envelope)
        return dict(envelope)

    async def _get_stripe_module(self) -> Any:
        """Return the ``stripe`` SDK, importing lazily when necessary."""

        if self._stripe_module is not None:
            return self._stripe_module
        try:
            import stripe as stripe_sdk  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "StripeConnector: the 'stripe' package is not installed — "
                "pip install stripe>=10.0.0 to enable this connector"
            ) from exc
        self._stripe_module = stripe_sdk
        return stripe_sdk

    async def _is_autocharge_enabled(self) -> bool:
        """Resolve ``overlay.stripe_autocharge`` for the owning tenant."""

        ff = self._feature_flags
        if ff is None:
            return False
        try:
            state = await ff.get_overlay_state(
                STRIPE_AUTOCHARGE_FLAG_KEY, self._tenant_id
            )
        except AttributeError:
            try:
                return bool(await ff.is_enabled(self._tenant_id))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "StripeConnector: feature flag lookup failed "
                    "tenant=%s: %s",
                    self._tenant_id,
                    exc,
                )
                return False
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "StripeConnector: overlay state lookup failed "
                "tenant=%s: %s",
                self._tenant_id,
                exc,
            )
            return False
        return state in _ACTIVE_OVERLAY_STATES

    async def _resolve_ceiling(self) -> float:
        """Return the per-tenant auto-charge ceiling (USD)."""

        if self._redis is None:
            return self._default_ceiling
        try:
            raw = await self._redis.get(
                _autocharge_ceiling_key(self._tenant_id)
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "StripeConnector: Redis ceiling lookup failed "
                "tenant=%s: %s",
                self._tenant_id,
                exc,
            )
            return self._default_ceiling
        if raw is None:
            return self._default_ceiling
        try:
            text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            value = float(text)
        except (TypeError, ValueError, UnicodeDecodeError):
            logger.warning(
                "StripeConnector: malformed autocharge_ceiling_usd "
                "tenant=%s value=%r — using default",
                self._tenant_id,
                raw,
            )
            return self._default_ceiling
        if value < 0:
            logger.warning(
                "StripeConnector: negative autocharge_ceiling_usd "
                "tenant=%s value=%s — using default",
                self._tenant_id,
                value,
            )
            return self._default_ceiling
        return value

    async def _escalate_through_confirmation(
        self,
        *,
        payload: Mapping[str, Any],
        amount_usd: float,
        ceiling: float,
    ) -> bool:
        """Route a high-value charge through the Confirmation_Protocol.

        Returns ``True`` when the protocol accepted the mutation
        (queued or executed), ``False`` when no protocol is wired.
        """

        if self._confirmation is None:
            logger.warning(
                "StripeConnector: payment %.2f >= ceiling %.2f but no "
                "confirmation_protocol wired — skipping auto-charge "
                "tenant=%s",
                amount_usd,
                ceiling,
                self._tenant_id,
            )
            return False

        # Lazy import so this module does not pull Agents.* at load
        # time (keeps unit-test bootstrap fast).
        from Agents.confirmation_protocol import MutationRequest

        parameters = {
            "pod_id": payload.get("pod_id"),
            "reconciliation_id": payload.get("reconciliation_id"),
            "customer_id": payload.get("customer_id"),
            "amount_usd": amount_usd,
            "currency": payload.get("currency") or DEFAULT_CURRENCY,
            "description": payload.get("description"),
            "ceiling_usd": ceiling,
        }
        request = MutationRequest(
            tool_name=HIGH_RISK_TOOL_NAME,
            parameters={k: v for k, v in parameters.items() if v is not None},
            tenant_id=self._tenant_id,
            agent_id=STRIPE_AGENT_ID,
        )
        try:
            await self._confirmation.process_mutation(request)
        except Exception as exc:
            logger.warning(
                "StripeConnector: confirmation_protocol rejected high-risk "
                "charge tenant=%s: %s",
                self._tenant_id,
                exc,
            )
            return False
        logger.info(
            "StripeConnector: escalated payment amount=%.2f ceiling=%.2f "
            "tenant=%s to Confirmation_Protocol as HIGH risk",
            amount_usd,
            ceiling,
            self._tenant_id,
        )
        return True

    @staticmethod
    def _extract_list_items(listing: Any) -> List[Dict[str, Any]]:
        """Extract a list of PaymentIntent dicts from a Stripe list response.

        Stripe's SDK returns a ``ListObject`` whose ``data`` attribute
        (or ``auto_paging_iter()``) carries the items; a stub may hand
        in a plain list or dict with a ``data`` key. We accept any of
        the common shapes so tests don't have to replicate Stripe's
        class hierarchy exactly.
        """

        if listing is None:
            return []
        if isinstance(listing, list):
            return [_as_dict(item) for item in listing]
        if isinstance(listing, dict):
            data = listing.get("data")
            if isinstance(data, list):
                return [_as_dict(item) for item in data]
            return []
        data = getattr(listing, "data", None)
        if isinstance(data, list):
            return [_as_dict(item) for item in data]
        return []

    @staticmethod
    def _map_intent_status(status: str) -> Optional[str]:
        """Map a raw Stripe PaymentIntent.status to our payment_status."""

        status = (status or "").lower()
        if status == "succeeded":
            return "paid"
        if status in ("processing", "requires_capture"):
            return "processing"
        if status == "canceled":
            return "canceled"
        if status in ("requires_payment_method", "requires_confirmation", "requires_action"):
            return "failed"
        return None

    def _terminal_run(
        self,
        *,
        run_id: str,
        operation: str,
        started_at: datetime,
        record_counts: Dict[str, int],
        status: str,
    ) -> SyncRun:
        finished_at = _utcnow()
        return SyncRun(
            run_id=run_id,
            tenant_id=self._tenant_id,
            instance_id=self._instance_id,
            provider_name=self.provider_name,
            operation=operation,  # type: ignore[arg-type]
            started_at=started_at,
            finished_at=finished_at,
            status=status,  # type: ignore[arg-type]
            record_counts=record_counts,
            duration_ms=max(
                0, int((finished_at - started_at).total_seconds() * 1000)
            ),
        )

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
        finished_at = _utcnow()
        message = str(exc) or exc.__class__.__name__
        error_details = f"{reason}: {message}" if reason else message
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
# Pure helpers (exposed for unit tests)
# ---------------------------------------------------------------------------


def _as_dict(obj: Any) -> Dict[str, Any]:
    """Coerce a Stripe SDK object (or a test stub) into a plain dict.

    Stripe's ``ListObject`` / ``StripeObject`` classes subclass ``dict``
    in recent SDK releases, so ``dict(obj)`` typically works. Older
    releases (and test stubs) may expose a ``to_dict`` method. We
    honour both without reaching for the SDK's private internals.
    """

    if isinstance(obj, dict):
        return dict(obj)
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
            if isinstance(result, dict):
                return dict(result)
        except Exception:
            pass
    try:
        return dict(obj)
    except (TypeError, ValueError):
        return {}


def _build_payment_intent_body(
    payload: Mapping[str, Any], amount_usd: float
) -> Dict[str, Any]:
    """Shape a ``sync_push`` payload into a PaymentIntent.create body.

    Stripe expects ``amount`` in the smallest currency unit (cents for
    USD) so we round to the nearest cent here. ``metadata.reconciliation_id``
    is set unconditionally when present on the payload so the webhook
    round-trip can identify the downstream record.
    """

    if amount_usd <= 0:
        raise ValueError("amount_usd must be > 0 for a PaymentIntent")

    currency = str(payload.get("currency") or DEFAULT_CURRENCY).strip().lower()
    amount_cents = int(round(amount_usd * 100))

    body: Dict[str, Any] = {
        "amount": amount_cents,
        "currency": currency,
    }
    if payload.get("customer_id"):
        body["customer"] = str(payload["customer_id"])
    if payload.get("description"):
        body["description"] = str(payload["description"])
    if payload.get("payment_method_id"):
        body["payment_method"] = str(payload["payment_method_id"])
        body["confirm"] = True
    if payload.get("off_session") is not None:
        body["off_session"] = bool(payload["off_session"])

    metadata: Dict[str, str] = {}
    if payload.get("reconciliation_id"):
        metadata["reconciliation_id"] = str(payload["reconciliation_id"])
    if payload.get("pod_id"):
        metadata["pod_id"] = str(payload["pod_id"])
    # Caller-provided extras win over defaults.
    extra = payload.get("metadata") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is None:
                continue
            metadata[str(k)] = str(v)
    if metadata:
        body["metadata"] = metadata
    return body


def _redact_payment_intent(intent: Any) -> Dict[str, Any]:
    """Reduce a Stripe PaymentIntent to the operator-safe subset.

    The ``GET /api/integrations/stripe/payments`` endpoint
    (Requirement 5.5.6) surfaces enough fields for an operator to
    reconcile a payment against a Reconciliation_Record — no more.
    Anything PII-adjacent (card numbers, ``payment_method_data``,
    ``charges.*.billing_details``, ``receipt_email``,
    ``client_secret``, ``last_payment_error``, etc.) is dropped at
    this boundary so the frontend never sees it (Requirement 5.1.8).

    ``metadata.reconciliation_id`` is preserved as the single metadata
    field operators need to join payments to their reconciliation
    records; all other metadata keys are discarded to avoid leaking
    anything a rogue integration wrote into the PaymentIntent.
    """

    raw = _as_dict(intent)

    allowed_fields: "tuple[str, ...]" = (
        "id",
        "status",
        "amount",
        "currency",
        "created",
        "customer",
        "description",
    )
    safe: Dict[str, Any] = {}
    for field_name in allowed_fields:
        if field_name in raw:
            safe[field_name] = raw[field_name]

    reconciliation_id: Optional[str] = None
    metadata = raw.get("metadata")
    if isinstance(metadata, dict):
        candidate = metadata.get("reconciliation_id")
        if isinstance(candidate, str) and candidate:
            reconciliation_id = candidate
    safe["metadata"] = (
        {"reconciliation_id": reconciliation_id}
        if reconciliation_id is not None
        else {}
    )
    return safe


__all__ = [
    "AUTOCHARGE_CEILING_REDIS_KEY_TEMPLATE",
    "DEFAULT_AUTOCHARGE_CEILING_USD",
    "DEFAULT_CURRENCY",
    "HIGH_RISK_TOOL_NAME",
    "STRIPE_AGENT_ID",
    "STRIPE_AUTOCHARGE_FLAG_KEY",
    "SUPPORTED_WEBHOOK_EVENT_TYPES",
    "StripeConnector",
    "StripeSignatureVerificationError",
    "VAULT_CREDENTIAL_KEY",
    "_build_payment_intent_body",
    "_redact_payment_intent",
    "build_catalog_entry",
    "register_catalog_entry",
]
