"""
Fuel domain bootstrap module.

Initializes: FuelService, fuel Elasticsearch indices, order intake
pipeline repositories, order/webhook endpoint routers, and the
legacy mirror backfill worker (60-second cadence).

Requirements: 1.1, 1.2, 2.4, 2.5
"""
import asyncio
import logging

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)

# Module-level reference so shutdown can cancel the task.
_legacy_mirror_backfill_task = None


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register fuel domain services."""
    from fuel.services.fuel_es_mappings import setup_fuel_indices
    from fuel.services.fuel_service import FuelService
    from fuel.api.endpoints import configure_fuel_api

    es_service = container.es_service

    # Set up fuel indices
    try:
        logger.info("Setting up fuel monitoring indices...")
        setup_fuel_indices(es_service.client, es_service=es_service)
        logger.info("Fuel monitoring indices ready")
    except Exception as e:
        logger.warning("Failed to set up fuel monitoring indices: %s", e)

    # Set up order intake pipeline indices (fuel_orders_current,
    # fuel_order_events, drivers_current, intake_channels,
    # pending_legacy_mirrors)
    try:
        from fuel.services.order_es_mappings import setup_order_intake_indices

        logger.info("Setting up order intake pipeline indices...")
        setup_order_intake_indices(es_service)
        logger.info("Order intake pipeline indices ready")
    except Exception as e:
        logger.warning("Failed to set up order intake pipeline indices: %s", e)

    # Fuel service
    fuel_service = FuelService(es_service)
    container.fuel_service = fuel_service

    # Wire fuel API
    configure_fuel_api(fuel_service=fuel_service)
    logger.info("Fuel API configured")

    # ---------------------------------------------------------------
    # Orders WebSocket manager — Requirement 4.1
    # ---------------------------------------------------------------
    try:
        from fuel.websocket.orders_ws import (
            OrdersWSManager,
            bind_container as bind_orders_ws,
        )

        orders_ws_manager = OrdersWSManager()
        container.orders_ws_manager = orders_ws_manager
        bind_orders_ws(container)
        logger.info("Orders WebSocket manager registered")
    except Exception as e:
        logger.warning("Failed to register Orders WebSocket manager: %s", e)

    # ---------------------------------------------------------------
    # Order intake pipeline feature-flag admin endpoint — wire the
    # FeatureFlagService (from ops bootstrap) and the orders WS manager so
    # POST/GET /api/ops/admin/feature-flags/{tenant}/order-intake-pipeline
    # work and broadcast flag changes. Without this the admin Feature Flags
    # tab 404s. Requirement 9.3.5.
    # ---------------------------------------------------------------
    try:
        from fuel.api.feature_flag_admin_endpoints import (
            configure_feature_flag_admin,
        )

        configure_feature_flag_admin(
            feature_flag_service=(
                container.ops_feature_flags
                if container.has("ops_feature_flags")
                else None
            ),
            orders_ws_manager=(
                container.orders_ws_manager
                if container.has("orders_ws_manager")
                else None
            ),
        )
        logger.info("Feature-flag admin endpoint configured")
    except Exception as e:
        logger.warning("Failed to configure feature-flag admin endpoint: %s", e)

    # ---------------------------------------------------------------
    # Order intake pipeline — repositories + endpoint wiring
    # ---------------------------------------------------------------

    # Create repositories (only need es_service)
    try:
        from fuel.order_repository import FuelOrderRepository
        from fuel.driver_repository import DriverRepository

        order_repository = FuelOrderRepository(es_service)
        container.order_repository = order_repository

        driver_repository = DriverRepository(es_service)
        container.driver_repository = driver_repository

        logger.info("Order and driver repositories registered")
    except Exception as e:
        logger.warning("Failed to create order/driver repositories: %s", e)
        order_repository = None
        driver_repository = None

    # Build the OrderIntakePipeline with available dependencies.
    # Some dependencies (credentials_vault, intake_channel_repo) are
    # registered by later bootstrap modules (agents, integrations).
    # We use container.has() to gracefully degrade — the pipeline will
    # still function for dispatcher/bulk paths that don't require HMAC.
    order_intake_pipeline = None
    try:
        from fuel.services.order_intake_pipeline import OrderIntakePipeline
        from fuel.intake.adapter_base import IntakeAdapterRegistry

        adapter_registry = IntakeAdapterRegistry()

        # Register known adapters. NB: ``register`` takes the adapter
        # positionally and channel_type/schema_version as KEYWORD-only args
        # (note the ``*`` in its signature). Passing them positionally raised
        # TypeError that the broad ``except`` swallowed as a warning — leaving
        # the registry EMPTY, so every dispatcher/CSV/EDI order intake failed
        # with "No adapter registered". (Found in the dispatcher journey test.)
        try:
            from fuel.intake.dispatcher_adapter import DispatcherIntakeAdapter
            adapter_registry.register(
                DispatcherIntakeAdapter(), channel_type="dispatcher", schema_version="1.0"
            )
        except Exception as exc:
            logger.warning("Failed to register dispatcher adapter: %s", exc)

        try:
            from fuel.intake.csv_adapter import CsvIntakeAdapter
            adapter_registry.register(
                CsvIntakeAdapter(), channel_type="csv", schema_version="1.0"
            )
        except Exception as exc:
            logger.warning("Failed to register csv adapter: %s", exc)

        try:
            from fuel.intake.legacy_dinee_adapter import LegacyDineeShipmentAdapter
            adapter_registry.register(
                LegacyDineeShipmentAdapter(), channel_type="legacy", schema_version="1.0"
            )
        except Exception as exc:
            logger.warning("Failed to register legacy dinee adapter: %s", exc)

        try:
            from fuel.intake.api_partner_adapter import ApiPartnerGenericAdapter
            adapter_registry.register(
                ApiPartnerGenericAdapter(), channel_type="api_partner", schema_version="1.0"
            )
        except Exception as exc:
            logger.warning("Failed to register api_partner adapter: %s", exc)

        # Dinee voice intake adapter (channel_type="voice"). Registered here
        # alongside the other channel adapters so voice submissions dispatched
        # through the bridge (Surface A) resolve to a real adapter. The bridge,
        # ledger, and routers are wired in bootstrap/integrations.py once the
        # IntakeChannelRepository + credentials_vault exist.
        try:
            from fuel.intake.voice_intake_adapter import VoiceIntakeAdapter
            adapter_registry.register(
                VoiceIntakeAdapter(), channel_type="voice", schema_version="1.0"
            )
        except Exception as exc:
            logger.warning("Failed to register voice adapter: %s", exc)

        # Gather optional dependencies from the container
        idempotency_service = (
            container.ops_idempotency if container.has("ops_idempotency") else None
        )
        feature_flag_service = (
            container.ops_feature_flags if container.has("ops_feature_flags") else None
        )
        poison_queue_service = (
            container.ops_poison_queue if container.has("ops_poison_queue") else None
        )
        credentials_vault = (
            container.credentials_vault if container.has("credentials_vault") else None
        )
        intake_channel_repo = (
            container.intake_channel_repository
            if container.has("intake_channel_repository")
            else None
        )
        ws_manager = (
            container.orders_ws_manager if container.has("orders_ws_manager") else None
        )

        # Legacy OpsWebSocketManager for dual-broadcast during
        # the deprecation window (Req 4.1.3, 9.3).
        legacy_ws_manager = (
            container.ops_ws_manager if container.has("ops_ws_manager") else None
        )

        # customer_tank_repo — optional, used for tank-ref validation
        customer_tank_repo = (
            container.customer_tank_repository
            if container.has("customer_tank_repository")
            else None
        )

        order_intake_pipeline = OrderIntakePipeline(
            es_service=es_service,
            intake_channel_repo=intake_channel_repo,
            adapter_registry=adapter_registry,
            idempotency_service=idempotency_service,
            feature_flag_service=feature_flag_service,
            poison_queue_service=poison_queue_service,
            ws_manager=ws_manager,
            credentials_vault=credentials_vault,
            customer_tank_repo=customer_tank_repo,
            legacy_ws_manager=legacy_ws_manager,
        )
        container.order_intake_pipeline = order_intake_pipeline
        logger.info("OrderIntakePipeline registered")

        # Register the VoiceReviewHoldHook so voice orders flagged for human
        # review (hold_reason set by the VoiceIntakeAdapter) are promoted from
        # status="placed" to status="on_hold" before persistence (Req 8.1).
        try:
            from fuel.voice.voice_review_hold_hook import VoiceReviewHoldHook
            order_intake_pipeline.register_hook(VoiceReviewHoldHook())
            logger.info("VoiceReviewHoldHook registered")
        except Exception as exc:
            logger.warning("Failed to register VoiceReviewHoldHook: %s", exc)
    except Exception as e:
        logger.warning("Failed to create OrderIntakePipeline: %s", e)

    # Wire order webhook endpoints router
    try:
        from fuel.api.order_webhook_endpoints import configure_order_webhook_endpoints

        if order_intake_pipeline is not None:
            configure_order_webhook_endpoints(
                order_intake_pipeline=order_intake_pipeline,
            )
            logger.info("Order webhook endpoints configured")
        else:
            logger.warning(
                "Order webhook endpoints not configured — "
                "OrderIntakePipeline unavailable"
            )
    except Exception as e:
        logger.warning("Failed to configure order webhook endpoints: %s", e)

    # Wire order REST endpoints router
    try:
        from fuel.api.order_endpoints import configure_order_endpoints
        from fuel.services.driver_counter_service import DriverCounterService

        driver_counter_service = None
        if driver_repository is not None:
            driver_counter_service = DriverCounterService(driver_repo=driver_repository)
            container.driver_counter_service = driver_counter_service

        if order_intake_pipeline is not None and order_repository is not None:
            configure_order_endpoints(
                order_intake_pipeline=order_intake_pipeline,
                order_repository=order_repository,
                driver_repository=driver_repository,
                driver_counter_service=driver_counter_service,
            )
            logger.info("Order REST endpoints configured")

            # Register cross-module reference loaders (customer/asset/driver)
            # on the process-wide RefResolver so ``GET /api/orders/{id}?expand=``
            # resolves links instead of degrading to "unresolved"
            # (cross-module-entity-linkage task 2.1, Req 1.1/5.1/5.4).
            try:
                from services.ref_loaders import register_order_link_loaders
                from services.ref_resolver import get_ref_resolver

                register_order_link_loaders(
                    get_ref_resolver(),
                    es_service=es_service,
                    driver_repository=driver_repository,
                    order_repository=order_repository,
                )
                logger.info("Cross-module reference loaders registered")
            except Exception as e:  # noqa: BLE001 — resolver degrades gracefully
                logger.warning(
                    "Failed to register cross-module reference loaders: %s", e
                )
        else:
            logger.warning(
                "Order REST endpoints not configured — "
                "pipeline or repository unavailable"
            )
    except Exception as e:
        logger.warning("Failed to configure order REST endpoints: %s", e)

    # Wire driver REST endpoints router
    try:
        from fuel.api.driver_endpoints import configure_driver_endpoints

        if driver_repository is not None:
            # Register the shared cross-module reference loaders (customer /
            # asset / driver) on the process-wide resolver so resolver reads —
            # the order ?expand= links (task 2.1) and the driver profile
            # truck → asset link (task 4) — resolve instead of dangling as
            # "unresolved". Registration is idempotent.
            ref_resolver = None
            try:
                from services.ref_resolver import get_ref_resolver
                from services.ref_loaders import register_order_link_loaders

                ref_resolver = get_ref_resolver()
                register_order_link_loaders(
                    ref_resolver,
                    es_service=es_service,
                    driver_repository=driver_repository,
                )
                logger.info("Cross-module reference loaders registered")
            except Exception as exc:
                logger.warning(
                    "Failed to register cross-module reference loaders: %s", exc
                )

            # The App_Access_Service collaborators (driver-mobile-app Req
            # 1.17-1.26). The PostgreSQL unit of work, the SuperTokens admin,
            # and the session revoker all default to their production
            # implementations inside the module; only the audit sink comes from
            # the container.
            configure_driver_endpoints(
                driver_repository=driver_repository,
                ref_resolver=ref_resolver,
                telemetry_service=(
                    container.telemetry_service
                    if container.has("telemetry_service")
                    else None
                ),
            )
            logger.info("Driver REST endpoints configured")
        else:
            logger.warning(
                "Driver REST endpoints not configured — "
                "driver_repository unavailable"
            )
    except Exception as e:
        logger.warning("Failed to configure driver REST endpoints: %s", e)

    # ---------------------------------------------------------------
    # OrderService — driver-mobile-app Requirements 4.1, 4.5, 4.6,
    # 4.8, 4.9.
    #
    # This is the first runtime construction of OrderService: until now
    # the only call sites were in tests, so ``container.has(
    # "order_service")`` was always false and no driver-initiated
    # transition could run the state-machine guard, the delivery-window
    # guard, or the driver counter side-effects.
    #
    # This is the earliest point at which every collaborator exists:
    # order_repository and driver_counter_service are registered above,
    # orders_ws_manager earlier in this module, ops_feature_flags by the
    # ops module which precedes fuel in _BOOT_ORDER.
    #
    # LegacyDualWriter is hoisted here, out of the backfill block below,
    # so one instance serves both OrderService and the
    # LegacyMirrorBackfillWorker rather than two writers racing the same
    # mirror.
    #
    # configure_order_mutation_tools is deliberately NOT called —
    # agent-initiated transitions stay dormant exactly as today.
    # ---------------------------------------------------------------
    try:
        from fuel.services.order_service import OrderService
        from fuel.services.legacy_dual_writer import LegacyDualWriter

        legacy_dual_writer = None
        if container.has("ops_es_service"):
            legacy_dual_writer = LegacyDualWriter(
                ops_es_service=container.ops_es_service,
                es_service=es_service,
            )
            container.legacy_dual_writer = legacy_dual_writer

        if order_repository is not None:
            order_service = OrderService(
                order_repo=order_repository,
                # ws_manager is the one constructor parameter with no
                # default, so it is always passed — None included.
                ws_manager=(
                    container.orders_ws_manager
                    if container.has("orders_ws_manager")
                    else None
                ),
                driver_counter_service=(
                    container.driver_counter_service
                    if container.has("driver_counter_service")
                    else None
                ),
                legacy_dual_writer=legacy_dual_writer,
                feature_flag_service=(
                    container.ops_feature_flags
                    if container.has("ops_feature_flags")
                    else None
                ),
            )
            container.order_service = order_service
            logger.info("OrderService registered")

            # The router is first configured above before OrderService exists.
            # Re-point it at this application-scoped instance so dispatcher
            # transitions publish the same subscribers (notably
            # ``order.dispatched`` push) as driver and internal transitions.
            from fuel.api.order_endpoints import configure_order_endpoints

            configure_order_endpoints(
                order_intake_pipeline=order_intake_pipeline,
                order_repository=order_repository,
                driver_repository=driver_repository,
                driver_counter_service=(
                    container.driver_counter_service
                    if container.has("driver_counter_service")
                    else None
                ),
                order_service=order_service,
            )
            logger.info("Order REST endpoints re-wired to shared OrderService")

            # Registering the service arms the previously dormant
            # ``order.delivered`` subscribers (K-factor calibration at
            # bootstrap/compliance.py and the delivery-completed
            # notification subscriber, both of which run after fuel in
            # _BOOT_ORDER). The delivery-completed subscriber has an
            # outbound customer effect: a real ``delivery_completed``
            # SMS or email on the first driver-marked delivery. No
            # transition can fail as a result — subscriber exceptions are
            # swallowed and logged inside _notify_event_subscribers —
            # but the message is visible to a customer, so the state is
            # surfaced here rather than armed silently. The operator
            # mitigation is to disable the ``delivery_completed`` rule
            # for a pilot tenant in the notification rule surface before
            # that tenant's first driver-marked delivery.
            logger.warning(
                "OrderService registration ARMS the order.delivered "
                "subscribers, including delivery-completed customer "
                "notifications. Disable the 'delivery_completed' "
                "notification rule for any pilot tenant before its first "
                "driver-marked delivery if outbound customer messaging "
                "has not gone live."
            )
            # ---------------------------------------------------------
            # POD_OTP_Service — driver-mobile-app Requirements 5.25,
            # 5.27, 5.31.
            #
            # Registered in the same block as OrderService because the
            # subscription is what makes the service reachable: nothing
            # else calls it. ``order.dispatched`` fires from
            # apply_status_transition, so the subscriber has to exist
            # before the first dispatch of the process.
            #
            # ``notification_service`` is deliberately absent here.
            # ``bootstrap/notifications.py`` runs after ``fuel`` in
            # _BOOT_ORDER, so Notification_Pipeline does not exist yet;
            # ``bootstrap/driver.py`` injects it by setter once it does.
            # Until then the code is generated and persisted but not
            # delivered, which fails closed at POD submission rather
            # than accepting an unverified delivery.
            #
            # This arms no outbound customer messaging on its own:
            # ``otp_required`` defaults to false in every tenant
            # (R5.31), so on_order_dispatched returns before generating
            # anything unless a tenant has opted in.
            # ---------------------------------------------------------
            try:
                from driver.services.pod_otp_service import PODOTPService

                pod_otp_service = PODOTPService(es_service=es_service)
                container.pod_otp_service = pod_otp_service
                order_service.subscribe(
                    "order.dispatched", pod_otp_service.on_order_dispatched
                )
                logger.info(
                    "PODOTPService registered on order.dispatched "
                    "(notification service injected later by "
                    "bootstrap/driver)"
                )
            except Exception as exc:
                logger.warning(
                    "Failed to register PODOTPService on order.dispatched: "
                    "%s",
                    exc,
                )
        else:
            logger.warning(
                "OrderService not registered — order_repository unavailable"
            )
    except Exception as e:
        logger.warning("Failed to register OrderService: %s", e)

    # ---------------------------------------------------------------
    # Commerce invoice subscriber — late binding
    # driver-mobile-app Requirement 4.5.
    #
    # ``core`` precedes ``fuel`` in _BOOT_ORDER, so
    # ``container.has("order_service")`` is still false when
    # bootstrap/core.py reaches its own subscription attempt: that arm
    # only logs its ``else`` warning and the invoice subscriber would
    # stay dormant while the K-factor and delivery-completed subscribers
    # (registered by bootstrap/compliance.py, which follows fuel) go
    # live. Binding here — reading the InvoiceService ``core`` already
    # put on the container — keeps the three order.delivered subscribers
    # in step. The core branch is left untouched, so this is the only
    # registration and there is no double subscription.
    # ---------------------------------------------------------------
    try:
        if container.has("order_service") and container.has(
            "commerce_invoice_service"
        ):
            from commerce.hooks.order_delivered_subscriber import (
                OrderDeliveredInvoiceSubscriber,
            )

            container.order_service.subscribe(
                "order.delivered",
                OrderDeliveredInvoiceSubscriber(
                    invoice_service=container.commerce_invoice_service,
                ),
            )
            logger.info(
                "Commerce invoice generation subscriber registered on "
                "order.delivered (late-bound from bootstrap/fuel)"
            )
        else:
            logger.warning(
                "Commerce invoice generation subscriber not registered — "
                "order_service or commerce_invoice_service unavailable"
            )
    except Exception as e:
        logger.warning(
            "Commerce invoice generation subscriber wiring failed: %s", e
        )

    # ---------------------------------------------------------------
    # Cross-module reference loaders — depot (Req 10.1)
    # ---------------------------------------------------------------
    # Register a ``depot`` loader on the process-wide RefResolver so an
    # asset's ``assigned_depot_id`` and the tenant's ``default_depot_id``
    # resolve to a depot record (round-tripping ``is_default``) instead of
    # degrading to "unresolved" (cross-module-entity-linkage task 9). The
    # DepotRepository is tenant-scoped, so a cross-tenant reference resolves
    # to ``None`` → ``unresolved`` (Req 5.3 / Property 2).
    try:
        from fuel.depot_models import DepotRepository
        from services.ref_loaders import register_depot_loader
        from services.ref_resolver import get_ref_resolver

        depot_repository = DepotRepository(es_service)
        container.depot_repository = depot_repository
        register_depot_loader(
            get_ref_resolver(), depot_repository=depot_repository
        )
        logger.info("Depot reference loader registered")
    except Exception as e:  # noqa: BLE001 — resolver degrades gracefully
        logger.warning("Failed to register depot reference loader: %s", e)

    # ---------------------------------------------------------------
    # Legacy mirror backfill worker (60-second cadence)
    # Validates: Requirements 1.3.2, 9.2
    # ---------------------------------------------------------------
    global _legacy_mirror_backfill_task

    try:
        from fuel.services.legacy_mirror_backfill_worker import (
            LegacyMirrorBackfillWorker,
            run_backfill_cycle,
            WORKER_CADENCE_SECONDS,
        )
        from fuel.services.legacy_dual_writer import LegacyDualWriter

        # Reuse the LegacyDualWriter hoisted into the OrderService block
        # above so one instance serves both the service and this worker;
        # fall back to constructing one only if that registration failed.
        ops_es_service = (
            container.ops_es_service if container.has("ops_es_service") else None
        )
        poison_queue_service_for_worker = (
            container.ops_poison_queue if container.has("ops_poison_queue") else None
        )

        if ops_es_service is not None and poison_queue_service_for_worker is not None:
            if container.has("legacy_dual_writer"):
                legacy_dual_writer = container.legacy_dual_writer
            else:
                legacy_dual_writer = LegacyDualWriter(
                    ops_es_service=ops_es_service,
                    es_service=es_service,
                )
                container.legacy_dual_writer = legacy_dual_writer

            backfill_worker = LegacyMirrorBackfillWorker(
                es_service=es_service,
                legacy_dual_writer=legacy_dual_writer,
                order_repository=order_repository,
                driver_repository=driver_repository,
                poison_queue_service=poison_queue_service_for_worker,
            )
            container.legacy_mirror_backfill_worker = backfill_worker

            async def _periodic_legacy_mirror_backfill() -> None:
                """Background task that drains pending_legacy_mirrors."""
                try:
                    while True:
                        await asyncio.sleep(WORKER_CADENCE_SECONDS)
                        await run_backfill_cycle(backfill_worker)
                except asyncio.CancelledError:
                    logger.info("Legacy mirror backfill task cancelled")

            _legacy_mirror_backfill_task = asyncio.create_task(
                _periodic_legacy_mirror_backfill()
            )
            logger.info(
                "Legacy mirror backfill worker started "
                "(cadence: %ds)",
                WORKER_CADENCE_SECONDS,
            )
        else:
            logger.warning(
                "Legacy mirror backfill worker not started — "
                "ops_es_service or poison_queue unavailable"
            )
    except Exception as e:
        logger.warning("Failed to start legacy mirror backfill worker: %s", e)

    # ---------------------------------------------------------------
    # Driver daily reset cron — Requirement 3.2.4
    # Now registered in bootstrap/scheduling.py where it runs after
    # the fuel module has placed driver_repository on the container.
    # ---------------------------------------------------------------


async def shutdown(app, container: ServiceContainer) -> None:
    """Cancel the legacy mirror backfill background task and shut down WS manager."""
    global _legacy_mirror_backfill_task

    if _legacy_mirror_backfill_task is not None and not _legacy_mirror_backfill_task.done():
        _legacy_mirror_backfill_task.cancel()
        try:
            await _legacy_mirror_backfill_task
        except asyncio.CancelledError:
            pass
        logger.info("Legacy mirror backfill task stopped")

    if container.has("orders_ws_manager"):
        try:
            await container.orders_ws_manager.shutdown()
        except Exception as exc:
            logger.exception("Orders WS manager shutdown failed: %s", exc)
