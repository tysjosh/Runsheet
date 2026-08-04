"""
Order Intake Pipeline — the single entry point for every intake channel.

Every webhook, dispatcher POST, bulk upload, CSV import, and future partner
integration lands here. The pipeline enforces:

    1. Channel resolution + authentication (HMAC for webhooks, JWT role
       check for dispatcher endpoints).
    2. Per-tenant idempotency (reusing the existing ``IdempotencyService``
       with the tenant-prefixed key shape ``idemp:{tenant_id}:{client_event_id}``).
    3. Schema version whitelist per channel.
    4. Adapter dispatch — one adapter per (channel_type, schema_version).
    5. Strict validation of the adapter's output against the FuelOrder
       schema before ES upsert.
    6. Atomic upsert of fuel_orders_current + fuel_order_events.
    7. /ws/orders broadcast.
    8. Idempotency mark_processed on success.

Step 8 used to be the deprecation dual-write into shipments_current /
riders_current (plus the legacy /ws/ops dual-broadcast and the
shadow-mode divergence comparison). The legacy surface is being dropped,
so that whole step is gone and mark_processed moved up.

Adapter exceptions are caught and routed to the ``ops_poison_queue`` via
the existing :class:`PoisonQueueService` — they never propagate to the
caller.

Constructor deps:
    es_service, intake_channel_repo, adapter_registry, idempotency_service,
    feature_flag_service, poison_queue_service, ws_manager, credentials_vault,
    customer_tank_repo, optional clock.

Validates: Requirements 1.1.6, 2.1, 2.2, 2.3, 9.1.3.
"""
from __future__ import annotations

import json
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from errors.exceptions import (
    channel_disabled,
    invalid_customer_tank_ref,
    missing_client_event_id,
    security_tenant_id_mismatch,
    webhook_signature_invalid,
)
from fuel.intake.adapter_base import (
    AdapterError,
    IntakeAdapterRegistry,
    IntakeContext,
)
from fuel.order_models import FuelOrder
from fuel.services.order_id_generator import mint_event_id, mint_order_id
from ops.webhooks.hmac_util import verify_hmac_sha256_hex
from fuel.services.order_metrics import (
    orders_adapter_errors_total,
    orders_intake_latency_seconds,
    orders_intake_processed_total,
    orders_intake_received_total,
)
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


def _optional_utc_datetime(value: Any) -> Optional[datetime]:
    """Normalize an optional source timestamp for chronology comparisons."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(
                str(value).strip().replace("Z", "+00:00")
            )
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------


@dataclass
class IntakeResponse:
    """Returned by both ``ingest_webhook`` and ``ingest_dispatcher``.

    Attributes:
        event_id: The idempotency key used for this intake attempt.
        status: One of ``"processed"``, ``"duplicate"``,
                ``"queued_for_review"``.
        order_id: The platform-assigned order ID (set only when
                  ``status == "processed"``).
    """

    event_id: str
    status: str
    order_id: Optional[str] = None


@dataclass
class _CsvImportChannel:
    """Ephemeral authenticated channel used by the tenant CSV importer."""

    channel_id: str
    tenant_id: str
    channel_type: str = "csv"
    supported_schema_versions: List[str] = field(default_factory=lambda: ["1.0"])
    enabled: bool = True


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class OrderIntakePipeline:
    """Single entry point for every intake channel.

    Every webhook, dispatcher POST, bulk upload, CSV import, and future
    partner integration lands here. The pipeline enforces channel
    resolution, authentication, idempotency, schema validation, adapter
    dispatch, model validation, persistence, and broadcast.
    """

    def __init__(
        self,
        *,
        es_service: Any,
        intake_channel_repo: Any,
        adapter_registry: IntakeAdapterRegistry,
        idempotency_service: Any,
        feature_flag_service: Any,
        poison_queue_service: Any,
        ws_manager: Any,
        credentials_vault: Any,
        customer_tank_repo: Any,
        # ``legacy_dual_writer`` was removed with the legacy mirror shim.
        # ``legacy_ws_manager`` is retained only because bootstrap and
        # several callers still pass it; the dual-broadcast that consumed
        # it went out with the mirror.
        legacy_ws_manager: Optional[Any] = None,
        clock: Optional[Callable] = None,
    ) -> None:
        self._es = es_service
        self._intake_channel_repo = intake_channel_repo
        self._adapter_registry = adapter_registry
        self._idempotency_service = idempotency_service
        self._feature_flag_service = feature_flag_service
        self._poison_queue_service = poison_queue_service
        self._ws_manager = ws_manager
        self._credentials_vault = credentials_vault
        self._customer_tank_repo = customer_tank_repo
        self._legacy_ws_manager = legacy_ws_manager
        self._clock = clock or utcnow

        # Registry of IntakeHook instances that run before/after order
        # acceptance. Commerce hooks (pricing, credit-check) register
        # here at startup via register_hook().
        self._hooks: List[Any] = []

    # ------------------------------------------------------------------
    # Late dependency injection
    # ------------------------------------------------------------------

    def set_intake_channel_repo(self, repo: Any) -> None:
        """Inject the intake-channel repository after construction.

        The pipeline is built in ``bootstrap/fuel.py`` (boot order #5), but
        the :class:`IntakeChannelRepository` and ``credentials_vault`` it needs
        for channel resolution are only registered later by the ``agents`` (#10)
        and ``integrations`` (#11) bootstrap modules. Without this setter the
        pipeline would keep ``intake_channel_repo=None`` forever and EVERY
        dispatcher order create / webhook ingest would 500 with
        ``'NoneType' object has no attribute 'get_dispatcher_channel'``.
        ``bootstrap/integrations.py`` calls this once the repo exists.
        """
        self._intake_channel_repo = repo

    def set_credentials_vault(self, vault: Any) -> None:
        """Inject the credentials vault after construction (see
        :meth:`set_intake_channel_repo` for the boot-order rationale)."""
        self._credentials_vault = vault

    def set_customer_tank_repo(self, repo: Any) -> None:
        """Late-inject the customer-tank repository for import reference checks."""

        self._customer_tank_repo = repo

    # ------------------------------------------------------------------
    # Hook registration
    # ------------------------------------------------------------------

    def register_hook(self, hook: Any) -> None:
        """Register an IntakeHook that runs during order intake.

        Hooks are called in sequence during _ingest_common:
        - before_accept: called after adapter transform but before persist.
          May mutate the order draft or raise to reject the order.
        - after_accept: called after the order is persisted. Used for
          side-effects (notifications, event emission).

        Hooks must conform to the IntakeHook protocol:
            async def before_accept(self, order_draft: dict) -> dict
            async def after_accept(self, order: dict) -> None

        Args:
            hook: An object conforming to the IntakeHook protocol.
        """
        self._hooks.append(hook)
        logger.info(
            "OrderIntakePipeline: registered hook %s",
            type(hook).__name__,
        )

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def ingest_webhook(
        self,
        channel_id: str,
        body: bytes,
        signature: str,
        request_id: str,
        *,
        idempotency_key_override: Optional[str] = None,
        schema_version_override: Optional[str] = None,
    ) -> IntakeResponse:
        """Ingest an order from an HMAC-signed webhook.

        Steps:
            (a) Resolve the intake channel by ``channel_id``.
            (b) Resolve the plaintext HMAC secret from the vault via
                ``channel.hmac_secret_ref``.
            (c) Verify the HMAC-SHA256 signature; discard the secret
                immediately after comparison — never log, never store.
            (d) Parse the JSON body and delegate to ``_ingest_common``.

        Args:
            channel_id: The channel identifier from the URL path.
            body: The raw request body bytes.
            signature: The ``X-Runsheet-Signature`` header value.
            request_id: A unique request/trace identifier.
            idempotency_key_override: When provided, this value is used as the
                tenant-scoped idempotency key (``client_event_id``) instead of
                the payload-derived ``event_id``. Additive; used by the Dinee
                voice bridge to map the ``X-Idempotency-Key`` header onto the
                pipeline. When ``None`` (the default), behavior is unchanged.
            schema_version_override: When provided, this value is used for the
                schema-version whitelist check and adapter dispatch instead of
                the payload-derived ``schema_version``. Additive; used by the
                Dinee voice bridge to map the ``X-Schema-Version`` header onto
                the pipeline. When ``None`` (the default), behavior is
                unchanged.

        Returns:
            An :class:`IntakeResponse` with the outcome.
        """
        # (a) Resolve channel
        channel = await self._resolve_channel(channel_id)

        # (b) Resolve plaintext HMAC secret from vault
        credential = await self._credentials_vault.get(
            tenant_id=channel.tenant_id,
            ref=channel.hmac_secret_ref,
        )
        # Extract the plaintext secret — used only for the compare below
        plaintext_secret: str = credential["secret"]

        # (c) Verify HMAC — discard secret after compare
        self._verify_hmac(body, signature, plaintext_secret)
        del plaintext_secret  # noqa: F821 — explicit discard

        # Parse payload
        payload: Dict[str, Any] = json.loads(body)

        # Check for tenant_id mismatch in payload (security event)
        if "tenant_id" in payload and payload["tenant_id"] != channel.tenant_id:
            logger.warning(
                "SECURITY: payload tenant_id=%r does not match channel "
                "tenant_id=%r for channel_id=%s, request_id=%s",
                payload.get("tenant_id"),
                channel.tenant_id,
                channel_id,
                request_id,
            )
            raise security_tenant_id_mismatch(
                details={
                    "payload_tenant_id": payload.get("tenant_id"),
                    "channel_tenant_id": channel.tenant_id,
                    "channel_id": channel_id,
                },
            )

        return await self._ingest_common(
            channel=channel,
            payload=payload,
            request_id=request_id,
            actor_user_id=None,
            client_event_id=(
                idempotency_key_override
                if idempotency_key_override is not None
                else payload.get("event_id")
            ),
            schema_version_override=schema_version_override,
        )

    async def ingest_dispatcher(
        self,
        tenant: Any,
        payload: Dict[str, Any],
        request_id: str,
        client_event_id: Optional[str],
    ) -> IntakeResponse:
        """Ingest an order from the dispatcher keyboard (JWT-authenticated).

        Steps:
            (a) Validate ``client_event_id`` is present — reject with
                HTTP 400 ``missing_client_event_id`` when missing.
            (b) Resolve the tenant's dispatcher channel.
            (c) Delegate to ``_ingest_common``.

        Args:
            tenant: The tenant context from the JWT (must expose
                    ``tenant_id`` and ``user_id`` attributes or keys).
            payload: The order payload from the request body.
            request_id: A unique request/trace identifier.
            client_event_id: The client-supplied idempotency key. MUST
                             be provided for the dispatcher path.

        Returns:
            An :class:`IntakeResponse` with the outcome.
        """
        # (d) Reject missing client_event_id on dispatcher path
        if not client_event_id:
            raise missing_client_event_id(
                details={"path": "dispatcher"},
            )

        # Resolve tenant attributes
        tenant_id = getattr(tenant, "tenant_id", None) or tenant.get("tenant_id")
        user_id = getattr(tenant, "user_id", None) or tenant.get("user_id")

        # (a) Resolve the dispatcher channel for this tenant
        channel = await self._resolve_dispatcher_channel(tenant_id)

        return await self._ingest_common(
            channel=channel,
            payload=payload,
            request_id=request_id,
            actor_user_id=user_id,
            client_event_id=client_event_id,
        )

    async def ingest_csv(
        self,
        *,
        tenant: Any,
        payload: Dict[str, Any],
        request_id: str,
        client_event_id: str,
        import_batch_id: str,
        csv_row_number: int,
    ) -> IntakeResponse:
        """Ingest one authenticated CSV row through the canonical pipeline.

        CSV uploads are already protected by the tenant session, so they do not
        need a persisted HMAC intake channel. The ephemeral channel selects the
        CSV adapter while the same validation, idempotency, event, websocket,
        and canonical repository path used by partner webhooks remains intact.
        """

        if not client_event_id:
            raise missing_client_event_id(details={"path": "csv_import"})

        tenant_id = getattr(tenant, "tenant_id", None) or tenant.get("tenant_id")
        user_id = getattr(tenant, "user_id", None) or tenant.get("user_id")
        csv_payload = dict(payload)
        csv_payload["import_batch_id"] = import_batch_id
        csv_payload["csv_row_number"] = csv_row_number
        channel = _CsvImportChannel(
            channel_id="csv-import",
            tenant_id=tenant_id,
        )
        return await self._ingest_common(
            channel=channel,
            payload=csv_payload,
            request_id=request_id,
            actor_user_id=user_id,
            client_event_id=client_event_id,
        )

    # ------------------------------------------------------------------
    # Core pipeline logic
    # ------------------------------------------------------------------

    async def _ingest_common(
        self,
        channel: Any,
        payload: Dict[str, Any],
        request_id: str,
        actor_user_id: Optional[str],
        client_event_id: Optional[str],
        schema_version_override: Optional[str] = None,
    ) -> IntakeResponse:
        """Shared pipeline logic for all intake paths.

        Steps:
            (0) Check ``overlay.order_intake_pipeline`` feature flag state:
                - ``disabled``: short-circuit (no legacy path remains).
                - every other state: write to the new path.
                NB: ``shadow`` / ``active_gated`` used to additionally
                dual-write and compare against the legacy surface. With
                the legacy mirror retired they behave like ``active_auto``.
            (d) Check tenant-scoped idempotency.
            (e) Validate schema version against channel whitelist.
            (f) Dispatch to the matching adapter.
            (g) Run ``_complete_order_doc`` to stamp platform fields.
            (h) Run ``_complete_event_docs`` to stamp event fields.
            (i) Verify ``customer_tank_id`` ownership.
            (j) Validate via ``FuelOrder.model_validate``.
            (k) Call ``FuelOrderRepository.upsert_with_last_event_timestamp``.
            (l) Append each event via ``append_event``.
            (m) Broadcast through ``OrdersWSManager``.
            (n) Mark processed via idempotency service.

        Step (n) was the ``LegacyDualWriter`` dual-write; it has been
        removed along with the legacy mirror, so mark_processed took
        over the letter.
        """
        tenant_id = channel.tenant_id
        ingest_start = time.monotonic()

        # (0) Check overlay.order_intake_pipeline feature flag state
        overlay_state = await self._get_overlay_state(tenant_id)

        # ``disabled`` → short-circuit. The caller is responsible for deciding
        # what to do with a ``legacy_passthrough`` response.
        #
        # NB: the only caller that ever had a legacy handler to fall back to was
        # the ``POST /webhooks/dinee`` receiver, which has been removed. Every
        # remaining caller treats this as "not processed", so with the flag
        # ``disabled`` an order is simply not ingested rather than being written
        # by an older path.
        if overlay_state == "disabled":
            return IntakeResponse(
                event_id=client_event_id or "",
                status="legacy_passthrough",
            )

        # Generate or use the client-supplied event_id for idempotency
        event_id = client_event_id or mint_event_id()

        # (e) Extract schema version early for metrics. When a caller supplies
        # a schema_version_override (e.g. the Dinee voice bridge mapping the
        # X-Schema-Version header), it takes precedence over the payload value.
        schema_version = (
            schema_version_override
            if schema_version_override is not None
            else payload.get("schema_version", "1.0")
        )
        intake_channel_type = getattr(channel, "channel_type", "unknown")

        # Record intake received metric
        orders_intake_received_total.labels(
            tenant_id=tenant_id,
            intake_channel=intake_channel_type,
            schema_version=schema_version,
        ).inc()

        # (d) Check tenant-scoped idempotency
        if await self._idempotency_service.is_duplicate(
            event_id, tenant_id=tenant_id
        ):
            orders_intake_processed_total.labels(
                tenant_id=tenant_id,
                intake_channel=intake_channel_type,
                status="duplicate",
            ).inc()
            return IntakeResponse(event_id=event_id, status="duplicate")

        # (e) Validate schema version against channel whitelist
        # (f) Dispatch to the matching adapter
        # Both schema validation and adapter dispatch can raise AdapterError
        # which routes to the poison queue rather than propagating.

        # Build the adapter context
        context = IntakeContext(
            tenant_id=tenant_id,
            channel=channel,
            trace_id=request_id,
            request_id=request_id,
            actor_user_id=actor_user_id,
        )

        try:
            self._assert_schema_supported(schema_version, channel)
            adapter = self._adapter_registry.get(
                channel.channel_type, schema_version
            )
            result = adapter.transform(payload, context)
        except AdapterError as exc:
            # Route adapter exceptions to poison queue — never propagate
            await self._poison_queue_service.store_failed_event(
                payload=payload,
                error=str(exc),
                error_type=exc.error_type,
                tenant_id=tenant_id,
                trace_id=request_id,
            )
            # Increment adapter error metric
            orders_adapter_errors_total.labels(
                tenant_id=tenant_id,
                intake_channel=intake_channel_type,
                error_type=exc.error_type,
            ).inc()
            orders_intake_processed_total.labels(
                tenant_id=tenant_id,
                intake_channel=intake_channel_type,
                status="queued_for_review",
            ).inc()
            return IntakeResponse(event_id=event_id, status="queued_for_review")

        # (g) Stamp platform-owned fields on the order doc
        order_doc = self._complete_order_doc(
            result.order_doc, context, event_id
        )

        # ERP files can contain a newer snapshot of an order imported earlier.
        # Reuse the same source-linked order_id, preserve lifecycle state, and
        # refuse an older/equal source version before it can overwrite current
        # dispatcher or driver work.
        csv_upsert_state = await self._prepare_csv_source_upsert(
            order_doc=order_doc,
            tenant_id=tenant_id,
        )
        if csv_upsert_state == "stale":
            await self._idempotency_service.mark_processed(
                event_id, tenant_id=tenant_id
            )
            orders_intake_processed_total.labels(
                tenant_id=tenant_id,
                intake_channel=intake_channel_type,
                status="duplicate",
            ).inc()
            return IntakeResponse(event_id=event_id, status="duplicate")
        if csv_upsert_state == "updated":
            for event_doc in result.event_docs:
                event_doc["event_type"] = "order_source_updated"

        # (h) Stamp platform-owned fields on each event doc
        event_docs = self._complete_event_docs(
            result.event_docs, order_doc, context
        )

        # (i) Verify customer_tank_id ownership (when present)
        if order_doc.get("customer_tank_id"):
            tank_exists = await self._customer_tank_repo.get(
                tenant_id, order_doc["customer_tank_id"]
            )
            if not tank_exists:
                raise invalid_customer_tank_ref(
                    details={
                        "customer_tank_id": order_doc["customer_tank_id"],
                        "tenant_id": tenant_id,
                    },
                )

        # (i2) Run registered IntakeHook.before_accept hooks.
        # Commerce hooks (PricingHook, CreditCheckHook) run here.
        # Each hook may mutate the order_doc (e.g. attach pricing fields)
        # or raise to reject the order.
        for hook in self._hooks:
            try:
                order_doc = await hook.before_accept(order_doc)
            except Exception as hook_exc:
                # Re-raise hook exceptions — they signal order rejection
                # (e.g. PricingError.no_rule_matched).
                raise hook_exc

        # (j) Validate via FuelOrder.model_validate BEFORE writing
        FuelOrder.model_validate(order_doc)

        # (k) Upsert the order document
        from fuel.order_repository import FuelOrderRepository

        order_repo = FuelOrderRepository(self._es)
        await order_repo.upsert_with_last_event_timestamp(
            tenant_id, order_doc
        )

        # (l) Append each event
        for ev in event_docs:
            await order_repo.append_event(tenant_id, ev)

        # (m) Broadcast through OrdersWSManager
        await self._broadcast(order_doc, event_docs)

        # (m2) used to dual-broadcast shipment_update / rider_update to
        # legacy /ws/ops subscribers. Removed with the legacy mirror.

        # (m3) Run registered IntakeHook.after_accept hooks.
        # Side-effects only — failures are logged but do not block intake.
        for hook in self._hooks:
            try:
                await hook.after_accept(order_doc)
            except Exception as hook_exc:
                logger.warning(
                    "OrderIntakePipeline: after_accept hook %s failed for "
                    "order=%s: %s",
                    type(hook).__name__,
                    order_doc.get("order_id"),
                    hook_exc,
                )

        # The former step (n) mirrored the order into the legacy surface
        # through LegacyDualWriter, and (n2) ran the shadow-mode
        # divergence comparison against the legacy adapter output. Both
        # depended on the legacy surface and were removed with it.

        # (n) Mark processed in idempotency store
        await self._idempotency_service.mark_processed(
            event_id, tenant_id=tenant_id
        )

        # Record latency and processed metric
        ingest_elapsed = time.monotonic() - ingest_start
        event_type = (
            event_docs[0].get("event_type", "order_placed")
            if event_docs
            else "order_placed"
        )
        orders_intake_latency_seconds.labels(
            tenant_id=tenant_id,
            intake_channel=intake_channel_type,
            event_type=event_type,
        ).observe(ingest_elapsed)
        orders_intake_processed_total.labels(
            tenant_id=tenant_id,
            intake_channel=intake_channel_type,
            status="processed",
        ).inc()

        return IntakeResponse(
            event_id=event_id,
            status="processed",
            order_id=order_doc["order_id"],
        )

    # ------------------------------------------------------------------
    # Platform-assigned field stamping
    # ------------------------------------------------------------------

    def _complete_order_doc(
        self,
        adapter_output: Dict[str, Any],
        context: IntakeContext,
        event_id: str,
    ) -> Dict[str, Any]:
        """Stamp the platform-owned fields on the adapter's output.

        Adapters MAY NOT set ``order_id``, ``tenant_id``, ``status``,
        ``created_at``, ``updated_at``, ``last_event_timestamp``,
        ``trace_id``. This method enforces that contract by overwriting
        any adapter-set values for those fields — the adapter's
        responsibility is the business shape, not the lifecycle metadata.
        """
        now = self._clock()
        order_doc = dict(adapter_output)  # shallow copy
        # Overwrite platform-owned fields unconditionally
        intake_metadata = order_doc.get("intake_metadata") or {}
        source_system = str(intake_metadata.get("source_system") or "").strip()
        source_record_id = str(
            intake_metadata.get("source_record_id") or ""
        ).strip()
        if (
            getattr(context.channel, "channel_type", None) == "csv"
            and source_system
            and source_record_id
        ):
            digest = hashlib.sha256(
                (
                    f"{context.tenant_id}|{source_system}|{source_record_id}"
                ).encode()
            ).hexdigest()[:32]
            order_doc["order_id"] = f"ord_import_{digest}"
        else:
            order_doc["order_id"] = mint_order_id()
        order_doc["tenant_id"] = context.tenant_id
        order_doc["status"] = "placed"
        order_doc["created_at"] = now.isoformat()
        order_doc["updated_at"] = now.isoformat()
        order_doc["last_event_timestamp"] = now.isoformat()
        order_doc["trace_id"] = context.trace_id
        return order_doc

    async def _prepare_csv_source_upsert(
        self,
        *,
        order_doc: Dict[str, Any],
        tenant_id: str,
    ) -> str:
        """Return ``new``, ``updated``, or ``stale`` for a CSV source record."""

        if order_doc.get("intake_channel") != "csv":
            return "new"
        metadata = order_doc.get("intake_metadata") or {}
        if not metadata.get("source_system") or not metadata.get(
            "source_record_id"
        ):
            return "new"

        from fuel.order_repository import FuelOrderRepository

        existing = await FuelOrderRepository(self._es).get(
            tenant_id, order_doc["order_id"]
        )
        if existing is None:
            return "new"

        incoming_updated_at = _optional_utc_datetime(
            metadata.get("source_updated_at")
        )
        existing_updated_at = existing.intake_metadata.source_updated_at
        if existing_updated_at is not None:
            if existing_updated_at.tzinfo is None:
                existing_updated_at = existing_updated_at.replace(
                    tzinfo=timezone.utc
                )
            else:
                existing_updated_at = existing_updated_at.astimezone(timezone.utc)
        if (
            incoming_updated_at is not None
            and existing_updated_at is not None
            and incoming_updated_at <= existing_updated_at
        ):
            return "stale"

        # Source snapshots may change commercial fields, but must never reset
        # execution state that dispatchers and drivers own.
        existing_doc = existing.model_dump(mode="json")
        for field_name in (
            "status",
            "assigned_driver_id",
            "assigned_asset_id",
            "assigned_run_id",
            "hold_reason",
            "pod_otp",
            "pod_otp_generated_at",
            "refusal_reason_code",
            "legacy_origin_snapshot",
            "created_at",
        ):
            order_doc[field_name] = existing_doc.get(field_name)
        return "updated"

    def _complete_event_docs(
        self,
        adapter_events: List[Dict[str, Any]],
        order_doc: Dict[str, Any],
        context: IntakeContext,
    ) -> List[Dict[str, Any]]:
        """Stamp platform-owned fields on each event document."""
        completed: List[Dict[str, Any]] = []
        now = order_doc["last_event_timestamp"]
        for ev in adapter_events:
            ev = dict(ev)  # shallow copy
            ev["event_id"] = mint_event_id()
            ev["order_id"] = order_doc["order_id"]
            ev["tenant_id"] = context.tenant_id
            ev["event_timestamp"] = now
            ev["ingested_at"] = now
            ev["trace_id"] = context.trace_id
            ev["source_schema_version"] = order_doc.get(
                "source_schema_version", "1.0"
            )
            completed.append(ev)
        return completed

    # ------------------------------------------------------------------
    # Feature flag state resolution
    # ------------------------------------------------------------------

    #: The overlay flag key used for the order intake pipeline rollout.
    OVERLAY_FLAG_KEY = "order_intake_pipeline"

    async def _get_overlay_state(self, tenant_id: str) -> str:
        """Return the overlay state for the order intake pipeline.

        Delegates to ``FeatureFlagService.get_overlay_state`` with the
        canonical flag key. Returns ``"disabled"`` when the service is
        unavailable (fail-closed).

        Valid states: ``disabled``, ``shadow``, ``active_gated``, ``active_auto``.
        """
        try:
            return await self._feature_flag_service.get_overlay_state(
                self.OVERLAY_FLAG_KEY, tenant_id
            )
        except Exception as exc:
            logger.warning(
                "OrderIntakePipeline: failed to read overlay state for "
                "tenant=%s: %s — defaulting to disabled",
                tenant_id,
                exc,
            )
            return "disabled"

    # ------------------------------------------------------------------
    # Channel resolution
    # ------------------------------------------------------------------

    async def _resolve_channel(self, channel_id: str) -> Any:
        """Resolve an intake channel by ID and validate it is enabled.

        Raises:
            resource_not_found: If the channel does not exist.
            channel_disabled: If the channel is disabled.
        """
        # The intake_channel_repo.get requires tenant_id for scoping,
        # but for webhook resolution we need to look up by channel_id
        # across all tenants (the channel_id is globally unique within
        # the platform). We use a direct search here.
        channel = await self._intake_channel_repo.get_by_channel_id(channel_id)
        if channel is None:
            from errors.exceptions import resource_not_found

            raise resource_not_found(
                message=f"Intake channel {channel_id!r} not found",
                details={"channel_id": channel_id},
            )
        if not channel.enabled:
            raise channel_disabled(
                details={"channel_id": channel_id},
            )
        return channel

    async def _resolve_dispatcher_channel(self, tenant_id: str) -> Any:
        """Resolve the tenant's single dispatcher channel.

        The dispatcher channel is an implicit, always-present intake surface
        (every tenant's operators can key in orders), so we provision it on
        first use rather than 404-ing when an admin never registered one. The
        repository's ``ensure_dispatcher_channel`` looks it up and creates a
        stable default only when missing (idempotent).
        """
        ensure = getattr(self._intake_channel_repo, "ensure_dispatcher_channel", None)
        if ensure is not None:
            channel = await ensure(tenant_id)
        else:
            # Backwards-compatible fallback for repos/mocks without the ensure
            # helper: look up directly.
            channel = await self._intake_channel_repo.get_dispatcher_channel(
                tenant_id
            )
        if channel is None:
            from errors.exceptions import resource_not_found

            raise resource_not_found(
                message="Dispatcher channel not configured for this tenant",
                details={"tenant_id": tenant_id},
            )
        if not channel.enabled:
            raise channel_disabled(
                details={"channel_id": channel.channel_id},
            )
        return channel

    # ------------------------------------------------------------------
    # HMAC verification
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_hmac(body: bytes, signature: str, secret: str) -> None:
        """Verify the HMAC-SHA256 signature of the request body.

        Delegates to the shared :func:`verify_hmac_sha256_hex` helper so there
        is a single HMAC verification implementation across the codebase.

        The secret is used only for this comparison and MUST be discarded
        immediately after — never logged, never stored.

        Raises:
            webhook_signature_invalid: If the signature does not match.
        """
        if not verify_hmac_sha256_hex(secret, body, signature):
            raise webhook_signature_invalid(
                details={"reason": "HMAC-SHA256 mismatch"},
            )

    # ------------------------------------------------------------------
    # Schema version validation
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_schema_supported(schema_version: str, channel: Any) -> None:
        """Validate the payload's schema_version against the channel whitelist.

        Raises:
            AdapterError: With ``error_type="unknown_schema_version"`` when
                the version is not in the channel's supported list.
        """
        supported = getattr(channel, "supported_schema_versions", [])
        if schema_version not in supported:
            raise AdapterError(
                error_type="unknown_schema_version",
                message=(
                    f"Schema version {schema_version!r} is not supported "
                    f"by channel {channel.channel_id!r}. "
                    f"Supported: {supported}"
                ),
            )

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    async def _broadcast(
        self,
        order_doc: Dict[str, Any],
        event_docs: List[Dict[str, Any]],
    ) -> None:
        """Broadcast the order placement through the WebSocket manager."""
        try:
            await self._ws_manager.broadcast(
                event_type="order_placed",
                data=order_doc,
                tenant_id=order_doc["tenant_id"],
            )
        except Exception as exc:
            # Broadcast failures MUST NOT block the main path
            logger.warning(
                "OrderIntakePipeline: WebSocket broadcast failed for "
                "order=%s: %s",
                order_doc.get("order_id"),
                exc,
            )

    # ------------------------------------------------------------------
    # Legacy mirror surface — removed
    # ------------------------------------------------------------------
    # Six methods lived below this line and all six went out with the
    # legacy dual-write shim:
    #
    #   _run_shadow_divergence_check      compared new vs legacy adapter
    #                                     output on a sampled basis
    #   _dual_broadcast_legacy_if_enabled pushed shipment_update /
    #                                     rider_update to legacy /ws/ops
    #   _project_order_to_shipment_broadcast
    #   _project_order_to_rider_broadcast the two legacy projections that
    #                                     fed that broadcast
    #   _dual_write_legacy_if_enabled     mirrored into shipments_current
    #   _enqueue_pending_legacy_mirror    retry queue for mirror failures
    #
    # Nothing reads the legacy surface any more, so keeping inert copies
    # would only invite them back.


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "IntakeResponse",
    "OrderIntakePipeline",
]
