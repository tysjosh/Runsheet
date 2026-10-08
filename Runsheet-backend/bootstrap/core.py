"""
Core infrastructure bootstrap module.

Initializes: Settings, Elasticsearch client, Redis client, Telemetry,
DataSeeder, HealthCheckService, DataIngestionService, fleet ConnectionManager.

Requirements: 1.1, 1.2
"""
import asyncio
import logging

from bootstrap.container import ServiceContainer
from persistence.leader_election import run_periodic

logger = logging.getLogger(__name__)

# Module-level reference for the credit override expiry background task
# so shutdown can cancel it.
_credit_override_expiry_task = None

# Module-level reference for the invoice overdue background task
# so shutdown can cancel it.
_invoice_overdue_task = None
_invoice_draft_finalize_task = None

# Module-level reference for the AR aging snapshot background task
# so shutdown can cancel it.
_ar_aging_snapshot_task = None

# Module-level reference for the analytics snapshot background task
# so shutdown can cancel it.
_analytics_snapshot_task = None

# Margin feed background tasks (gap sweep, weekly report).
_margin_gap_sweep_task = None
_margin_weekly_report_task = None

# ── Commerce / Intake flag dependency keys ──────────────────────────
COMMERCE_BACKBONE_FLAG_KEY = "commerce_backbone"
ORDER_INTAKE_PIPELINE_FLAG_KEY = "order_intake_pipeline"

# Overlay states considered "active" (tenant has the feature turned on).
_ACTIVE_OVERLAY_STATES = frozenset({"active_gated", "active_auto"})


class CommerceIntakeMisconfigurationError(Exception):
    """Raised when commerce.backbone_enabled is on but intake.pipeline_enabled is off."""

    pass


class DriverBootstrapMisconfigurationError(Exception):
    """Raised when the driver surface did not wire completely during bootstrap.

    Two conditions are fatal outside production: ``order_service`` missing from
    the container (no driver-initiated status transition can reach
    ``OrderService.apply_status_transition``), and a declared driver index
    absent from Elasticsearch (its ``dynamic: strict`` declaration is then not
    in force, because ES auto-creates the index with ``dynamic: true`` on first
    write).
    """

    pass


async def assert_commerce_requires_intake(container: ServiceContainer) -> None:
    """Assert that every tenant with commerce backbone enabled also has intake pipeline enabled.

    This is a non-production assertion that runs during bootstrap to catch
    mis-configured tenants early. In production the check is skipped so a
    Redis scan cannot block startup.

    The check scans Redis for all overlay keys matching
    ``overlay_ff:commerce_backbone:*`` and, for each tenant found in an
    active state, verifies that ``overlay_ff:order_intake_pipeline:{tenant_id}``
    is also in an active state.

    Raises:
        CommerceIntakeMisconfigurationError: If any tenant has commerce on
            but intake off.
    """
    from config.settings import Environment

    settings = container.settings
    if settings.environment == Environment.PRODUCTION:
        logger.debug(
            "Skipping commerce→intake prerequisite assertion in production"
        )
        return

    if not container.has("ops_feature_flags"):
        logger.debug(
            "Skipping commerce→intake prerequisite assertion: "
            "feature flag service not available"
        )
        return

    feature_flag_service = container.ops_feature_flags
    if feature_flag_service.client is None:
        logger.debug(
            "Skipping commerce→intake prerequisite assertion: "
            "Redis client not connected"
        )
        return

    # Scan for all tenants with the commerce backbone overlay key set.
    prefix = f"overlay_ff:{COMMERCE_BACKBONE_FLAG_KEY}:"
    misconfigured_tenants = []

    try:
        cursor = "0"
        while True:
            cursor, keys = await feature_flag_service.client.scan(
                cursor=cursor, match=f"{prefix}*", count=100
            )
            for key in keys:
                # Extract tenant_id from key: overlay_ff:commerce_backbone:{tenant_id}
                tenant_id = key.removeprefix(prefix)
                if not tenant_id:
                    continue

                # Check if commerce backbone is in an active state
                commerce_state = await feature_flag_service.get_overlay_state(
                    COMMERCE_BACKBONE_FLAG_KEY, tenant_id
                )
                if commerce_state not in _ACTIVE_OVERLAY_STATES:
                    continue

                # Commerce is active — verify intake pipeline is also active
                intake_state = await feature_flag_service.get_overlay_state(
                    ORDER_INTAKE_PIPELINE_FLAG_KEY, tenant_id
                )
                if intake_state not in _ACTIVE_OVERLAY_STATES:
                    misconfigured_tenants.append(
                        (tenant_id, commerce_state, intake_state)
                    )

            if cursor == "0" or cursor == 0:
                break

    except Exception as exc:
        logger.warning(
            "Commerce→intake prerequisite assertion could not complete: %s",
            exc,
        )
        return

    if misconfigured_tenants:
        details = "; ".join(
            f"tenant_id={tid} (commerce={cs}, intake={ist})"
            for tid, cs, ist in misconfigured_tenants
        )
        msg = (
            f"Commerce backbone misconfiguration detected: "
            f"commerce.backbone_enabled is active but "
            f"intake.pipeline_enabled is NOT active for: {details}. "
            f"A tenant MUST have intake.pipeline_enabled active before "
            f"commerce.backbone_enabled can be turned on."
        )
        logger.error(msg)
        raise CommerceIntakeMisconfigurationError(msg)


async def assert_driver_surface_wired(container: ServiceContainer) -> None:
    """Assert the driver surface finished wiring: ``order_service`` + every driver index.

    Runs from the post-initialization block in ``bootstrap/__init__.py`` after
    every module in ``_BOOT_ORDER`` has run, so it sees the fully-populated
    container. It mirrors :func:`assert_commerce_requires_intake`'s posture —
    *loud outside production, degrade with an ERROR log inside it* — because a
    boot-time check must never be the reason a production deployment fails to
    come up. The ERROR log is the production signal.

    Two invariants, both of which have silently failed in this repository before:

    1. ``container.has("order_service")`` — no ``OrderService(...)`` call site
       existed outside ``tests/``, so every driver-initiated status transition
       and all three ``order.delivered`` subscribers were dormant.
    2. Every index in ``DRIVER_INDEX_MAPPINGS`` exists — ``setup_driver_indices``
       was only ever called by the seeder, so a deployment that skipped it had
       its driver indices auto-created on first write with ``dynamic: true``.

    Neither is evaluated when ``settings`` is absent from the container: core
    never initialized, so the boot never got far enough to wire anything and
    the fail-open contract for module initialization (Requirement 1.5) owns
    that failure.

    Raises:
        DriverBootstrapMisconfigurationError: Outside production, when either
            invariant is violated.

    Validates: Requirements 4.1, 15.12
    """
    from config.settings import Environment

    # ``settings`` is the very first thing ``bootstrap/core.py:initialize``
    # registers. Its absence means core itself never ran, so *nothing* is on
    # the container and a missing ``order_service`` is a symptom of that
    # larger failure rather than a driver misconfiguration. Same reasoning as
    # the unreachable-cluster skip below: a different fault has its own signal
    # (the per-module ERROR log from ``initialize_all``), and this assertion
    # must not convert it into a boot-blocking driver error — that would break
    # the fail-open contract for module initialization.
    if not container.has("settings"):
        logger.warning(
            "Skipping driver surface wiring assertion: core bootstrap did not "
            "complete (settings absent from container), so the driver surface "
            "was never reached"
        )
        return

    problems: list[str] = []

    if not container.has("order_service"):
        problems.append(
            "container.has('order_service') is false — no driver-initiated "
            "status transition can reach OrderService.apply_status_transition, "
            "and the order.delivered subscribers stay dormant"
        )

    # The declared-index presence check is gone with the cluster.
    #
    # It called ``indices.exists`` for each declared driver index and complained
    # about any that were absent, because on Elasticsearch the first write to a
    # missing index auto-created it with ``dynamic: true`` — silently discarding
    # the ``dynamic: strict`` declaration. The document store is one Postgres table
    # created by migration ``0009_es_documents``, so there is no index to be absent
    # and that failure mode cannot occur.
    #
    # Phase 5 scoped the check with ``settings.document_store_is_postgres``. Phase 6
    # deleted that property and left the read here, which raised AttributeError into
    # the caller's ``except`` in ``bootstrap/__init__.py`` — so the WHOLE assertion,
    # including the ``order_service`` check above, silently stopped running in every
    # environment. Found by booting with ENVIRONMENT=staging; the unit tests missed
    # it because they set ``document_store_is_postgres`` on a ``MagicMock``
    # container, supplying an attribute production settings no longer had.
    #
    # What IS lost with strict mappings is worth stating rather than glossing:
    # ``dynamic: strict`` rejected an undeclared field at write time, so a typo'd
    # key was a 400. jsonb accepts anything, so the same typo now stores silently.
    # The compensating controls are the Pydantic models on the way in
    # (``extra="forbid"`` on every driver-surface model) and
    # ``persistence/document_field_policy.py``, which still reads the declared
    # mappings — that is why the mapping modules survive.

    if not problems:
        return

    msg = (
        "Driver surface bootstrap misconfiguration detected: "
        + "; ".join(problems)
        + ". The driver surface is wired by bootstrap/driver.py and "
        "bootstrap/fuel.py."
    )
    logger.error(msg)

    if container.settings.environment == Environment.PRODUCTION:
        # Degrade in production: the ERROR log above names the fault.
        return

    raise DriverBootstrapMisconfigurationError(msg)


def wire_margin_feed(container: ServiceContainer, elasticsearch_service) -> object:
    """Construct the margin feed services and attach the hooks (margin-feed FR3).

    Builds MarginRepository, MarginCostEntryService and MarginService, sets
    the invoice margin hook and, when the OrderService already exists,
    registers MarginOrderSubscriber (otherwise bootstrap/fuel late-binds it,
    the same reason as the invoice subscriber). Every hook and job checks
    ``commerce_margin_feed_enabled`` per event / cycle, so a flag flip needs
    no restart. The signal bus is resolved lazily: bootstrap/agents creates
    it after core.
    """
    from commerce.services.margin_cost_entry_service import MarginCostEntryService
    from commerce.services.margin_repository import MarginRepository
    from commerce.services.margin_service import MarginService
    from fuel.terminal_models import TerminalRepository

    terminals = TerminalRepository(elasticsearch_service)
    margin_repository = MarginRepository()
    invoice_service = (
        container.commerce_invoice_service
        if container.has("commerce_invoice_service")
        else None
    )
    margin_service = MarginService(
        margin_repository,
        es_service=elasticsearch_service,
        terminals=terminals,
        invoice_service=invoice_service,
        signal_bus_provider=lambda: (
            container.signal_bus if container.has("signal_bus") else None
        ),
    )
    container.margin_repository = margin_repository
    container.margin_cost_entry_service = MarginCostEntryService(
        margin_repository,
        terminals=terminals,
        es_service=elasticsearch_service,
    )
    container.margin_service = margin_service
    if invoice_service is not None:
        invoice_service.set_margin_hook(margin_service.hook)
    if container.has("order_service"):
        from commerce.hooks.margin_order_subscriber import (
            register_margin_order_subscribers,
        )

        container.margin_order_subscribers = register_margin_order_subscribers(
            container.order_service, margin_service.hook
        )
    return margin_service


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register core infrastructure services."""
    from config.settings import get_settings
    from services.elasticsearch_service import elasticsearch_service
    from services.data_seeder import data_seeder
    from telemetry.service import initialize_telemetry
    from health.service import HealthCheckService
    from ingestion.service import DataIngestionService
    from websocket.connection_manager import ConnectionManager, bind_container as bind_fleet
    from errors.handlers import register_exception_handlers

    # Settings
    settings = get_settings()
    container.settings = settings

    # ── Sweep leader election ─────────────────────────────────────────
    # Started first, before anything schedules a periodic job, because every
    # background job in this process consults it. Without it the API had to run
    # as exactly one task — two processes meant two AR-aging snapshots for the
    # same day and two overdue sweeps racing the same invoices — which made
    # every deploy a downtime window. See persistence/leader_election.py.
    try:
        from persistence.leader_election import SweepLeader, set_sweep_leader

        sweep_leader = SweepLeader()
        set_sweep_leader(sweep_leader)
        container.sweep_leader = sweep_leader
        await sweep_leader.start()
    except Exception as exc:
        # Fail closed on the *scheduling* side rather than the request side: if
        # election cannot start, leave no leader registered so periodic jobs run
        # (single-process assumption) and say so loudly, rather than silently
        # running no background work at all.
        logger.error(
            "Sweep leader election failed to start (%s) — periodic jobs will run "
            "in every process. Do NOT run more than one replica until this is "
            "resolved.",
            exc,
            exc_info=True,
        )

    # Telemetry
    telemetry_service = initialize_telemetry(settings)
    container.telemetry_service = telemetry_service

    # Elasticsearch (module-level singleton)
    container.es_service = elasticsearch_service

    # FileStorageService (S3). Built here, before any domain boots, so
    # compliance (terminal BOL raw documents) and agents share one instance.
    # It used to be built in the agents bootstrap, which runs after
    # compliance, so terminal BOLs never had storage (finding C9).
    try:
        from services.file_storage_service import build_file_storage_service_from_env

        file_storage_service = build_file_storage_service_from_env()
        if file_storage_service is not None:
            container.file_storage_service = file_storage_service
            logger.info("FileStorageService registered")
        else:
            logger.info(
                "FileStorageService not registered — FUEL_OPS_S3_BUCKET / "
                "FUEL_OPS_S3_REGION not configured"
            )
    except Exception as exc:
        logger.warning("FileStorageService wiring failed: %s", exc)

    # Seed baseline data (development / demo only).
    #
    # Historically this ran unconditionally at every boot, which meant a
    # production deployment cold-booted with synthetic trucks, drivers,
    # and support tickets written into the live Elasticsearch cluster.
    # It is now gated behind ``settings.seed_demo_data`` (default False)
    # so production boots are inert and the seeded docs (tenant-stamped
    # with ``tenant_id="demo"`` in the seeder layer) cannot bleed into
    # any real tenant's reads.
    if getattr(settings, "seed_demo_data", False):
        try:
            logger.info("Seeding Elasticsearch with baseline morning data (seed_demo_data=true)...")
            await data_seeder.seed_baseline_data(operational_time="09:00")
            logger.info("Baseline data seeding completed.")
        except Exception as e:
            logger.error("Failed to seed Elasticsearch data: %s", e)
    else:
        logger.info(
            "Skipping baseline data seeding: seed_demo_data flag is False. "
            "Set SEED_DEMO_DATA=true in .env to enable for local dev."
        )

    # Health check service
    health_check_service = HealthCheckService(
        es_service=elasticsearch_service,
        session_store=None,
        check_timeout=5.0,
    )
    container.health_check_service = health_check_service

    # Data ingestion service
    data_ingestion_service = DataIngestionService(
        es_service=elasticsearch_service,
        telemetry=telemetry_service,
    )
    container.data_ingestion_service = data_ingestion_service

    # Fleet WebSocket manager
    fleet_ws_manager = ConnectionManager()
    container.fleet_ws_manager = fleet_ws_manager
    bind_fleet(container)

    # Wire ingestion → fleet WS for live broadcasts
    data_ingestion_service.set_connection_manager(fleet_ws_manager)

    # Register structured exception handlers
    register_exception_handlers(app)

    # ── Commerce Backbone ES indices (Task 1.5) ────────────────────────
    # Provision the commerce ES indices only when the master feature flag
    # ``commerce_backbone_enabled`` is on.  This mirrors the fuel-ops
    # pattern in bootstrap/agents.py but gates on the flag so tenants
    # without commerce enabled never pay the index-creation cost.
    if getattr(settings, "commerce_backbone_enabled", False):

        # ── Commerce Customer API wiring (Task 3.3) ────────────────────
        # Wire the CustomerService into the customer_endpoints module so
        # the router handlers can access it without circular imports.
        try:
            from commerce.services.customer_service import CustomerService
            from commerce.api.customer_endpoints import configure_customer_api

            customer_service = CustomerService(es_service=elasticsearch_service)
            configure_customer_api(customer_service=customer_service)
            container.commerce_customer_service = customer_service
            logger.info("Commerce customer API configured")
        except Exception as exc:
            logger.warning("Commerce customer API wiring failed: %s", exc)

        # ── Customer portal principal check (OI-06, design §2.3) ──────
        # Only when CustomerService exists; without it the portal guard
        # fails closed (503 PORTAL_UNAVAILABLE).
        if container.has("commerce_customer_service"):
            try:
                from portal.api.me_endpoints import configure_portal_me
                from portal.services.principal import (
                    PortalPrincipalChecker,
                    configure_portal_principal,
                )

                configure_portal_principal(
                    PortalPrincipalChecker(
                        customer_service=container.commerce_customer_service,
                        cache_seconds=settings.portal_principal_cache_seconds,
                    )
                )
                configure_portal_me(
                    customer_service=container.commerce_customer_service
                )
                logger.info("Customer portal principal checker configured")

                # Portal-user provisioning (design §1.7). The unit of work,
                # the SuperTokens seams and the link minter default to their
                # production implementations; only the audit sink comes from
                # the container.
                from portal.services.portal_access_service import (
                    PortalAccessService,
                    configure_portal_access,
                )

                configure_portal_access(
                    PortalAccessService(
                        customer_service=container.commerce_customer_service,
                        telemetry_service=(
                            container.telemetry_service
                            if container.has("telemetry_service")
                            else None
                        ),
                    )
                )
                logger.info("Customer portal access service configured")
            except Exception as exc:
                logger.warning("Customer portal principal wiring failed: %s", exc)

        # ── Commerce Account API wiring (Task 4.3) ─────────────────────
        # Wire the AccountService and CreditService into the
        # account_endpoints module so the router handlers can access them.
        try:
            from commerce.services.account_service import AccountService
            from commerce.services.credit_service import CreditService
            from commerce.api.account_endpoints import configure_account_api

            account_service = AccountService(es_service=elasticsearch_service)
            credit_service = CreditService(es_service=elasticsearch_service)
            configure_account_api(
                account_service=account_service,
                credit_service=credit_service,
            )
            container.commerce_account_service = account_service
            container.commerce_credit_service = credit_service
            logger.info("Commerce account API configured")
        except Exception as exc:
            logger.warning("Commerce account API wiring failed: %s", exc)

        # ── Commerce Price Book API wiring (Task 5.3) ──────────────────
        # Wire the PriceBookService and PricingEngine into the
        # price_book_endpoints module so the router handlers can access them.
        try:
            from commerce.services.price_book_service import PriceBookService
            from commerce.services.pricing_engine import PricingEngine
            from commerce.api.price_book_endpoints import (
                configure_price_book_api,
                configure_account_service_for_resolve,
            )

            # Try to get the canonicalize function
            canonicalize_fn = None
            try:
                from fuel.services.fuel_product_catalog import canonicalize
                canonicalize_fn = canonicalize
            except ImportError:
                logger.debug("fuel_product_catalog.canonicalize not available")

            # Try to get the Redis client
            redis_client = None
            if container.has("redis_client"):
                redis_client = container.redis_client

            price_book_service = PriceBookService(
                es_service=elasticsearch_service,
                redis_client=redis_client,
                canonicalize_fn=canonicalize_fn,
            )
            pricing_engine = PricingEngine(
                es_service=elasticsearch_service,
                redis_client=redis_client,
                canonicalize_fn=canonicalize_fn,
            )
            configure_price_book_api(
                price_book_service=price_book_service,
                pricing_engine=pricing_engine,
            )

            # Wire account service for the resolve endpoint
            if container.has("commerce_account_service"):
                configure_account_service_for_resolve(
                    container.commerce_account_service
                )

            container.commerce_price_book_service = price_book_service
            container.commerce_pricing_engine = pricing_engine
            logger.info("Commerce price book API configured")
        except Exception as exc:
            logger.warning("Commerce price book API wiring failed: %s", exc)

        # ── Commerce Intake Hooks Registration (Task 6.2) ──────────────
        # Register PricingHook and CreditCheckHook on the
        # OrderIntakePipeline so pricing and credit-check run on every
        # order intake when commerce.backbone_enabled is on.
        # The hooks themselves re-evaluate their sub-flags per-request,
        # so a flag flip takes effect without a restart.
        try:
            from commerce.hooks.intake_hooks import PricingHook, CreditCheckHook

            # Use the already-wired PricingEngine and CreditService
            _pricing_engine = (
                container.commerce_pricing_engine
                if container.has("commerce_pricing_engine")
                else None
            )
            _credit_service = (
                container.commerce_credit_service
                if container.has("commerce_credit_service")
                else None
            )

            # Only register hooks if the pipeline and dependencies are available
            if (
                container.has("order_intake_pipeline")
                and _pricing_engine is not None
                and _credit_service is not None
            ):
                pricing_hook = PricingHook(pricing_engine=_pricing_engine)
                credit_check_hook = CreditCheckHook(credit_service=_credit_service)

                pipeline = container.order_intake_pipeline
                pipeline.register_hook(pricing_hook)
                pipeline.register_hook(credit_check_hook)

                container.commerce_pricing_hook = pricing_hook
                container.commerce_credit_check_hook = credit_check_hook
                logger.info(
                    "Commerce intake hooks registered (PricingHook, CreditCheckHook)"
                )
            else:
                missing = []
                if not container.has("order_intake_pipeline"):
                    missing.append("order_intake_pipeline")
                if _pricing_engine is None:
                    missing.append("commerce_pricing_engine")
                if _credit_service is None:
                    missing.append("commerce_credit_service")
                logger.warning(
                    "Commerce intake hooks not registered — missing dependencies: %s",
                    ", ".join(missing),
                )
        except Exception as exc:
            logger.warning("Commerce intake hooks registration failed: %s", exc)

        # ── Commerce Invoice Generation Subscriber (Task 7.3) ─────────
        # Subscribe InvoiceService.generate_from_order to the
        # OrderService's order.delivered event via the public
        # subscription helper. The subscriber checks
        # commerce.invoicing_enabled per-event so a flag flip takes
        # effect without a restart.
        try:
            from commerce.hooks.order_delivered_subscriber import (
                OrderDeliveredInvoiceSubscriber,
            )
            from commerce.services.invoice_service import InvoiceService

            # Use the already-wired invoice service if available, otherwise
            # create a fresh instance.
            _invoice_service = None
            if container.has("commerce_invoice_service"):
                _invoice_service = container.commerce_invoice_service
            else:
                # Create an InvoiceService instance for the subscriber
                _idempotency_svc = (
                    container.idempotency_service
                    if container.has("idempotency_service")
                    else None
                )
                _invoice_service = InvoiceService(
                    es_service=elasticsearch_service,
                    idempotency_service=_idempotency_svc,
                )
                container.commerce_invoice_service = _invoice_service

            # Create the subscriber handler
            _invoice_subscriber = OrderDeliveredInvoiceSubscriber(
                invoice_service=_invoice_service,
            )

            # Register on the OrderService's public subscription helper
            if container.has("order_service"):
                order_service = container.order_service
                order_service.subscribe("order.delivered", _invoice_subscriber)
                logger.info(
                    "Commerce invoice generation subscriber registered "
                    "on order.delivered event"
                )
            else:
                logger.warning(
                    "Commerce invoice generation subscriber not registered "
                    "— order_service not available in container"
                )
        except Exception as exc:
            logger.warning(
                "Commerce invoice generation subscriber wiring failed: %s", exc
            )

        # ── Commerce Invoice API wiring (Task 7.5) ────────────────────
        # Wire the InvoiceService into the invoice_endpoints module so
        # the router handlers can access it without circular imports.
        try:
            from commerce.api.invoice_endpoints import configure_invoice_api

            # Use the already-wired invoice service from the container
            _inv_svc_for_api = None
            if container.has("commerce_invoice_service"):
                _inv_svc_for_api = container.commerce_invoice_service
            else:
                from commerce.services.invoice_service import InvoiceService as _InvSvc

                _idempotency_svc_api = (
                    container.idempotency_service
                    if container.has("idempotency_service")
                    else None
                )
                _inv_svc_for_api = _InvSvc(
                    es_service=elasticsearch_service,
                    idempotency_service=_idempotency_svc_api,
                )
                container.commerce_invoice_service = _inv_svc_for_api

            configure_invoice_api(invoice_service=_inv_svc_for_api)
            logger.info("Commerce invoice API configured")

            from portal.api.me_endpoints import configure_portal_me

            configure_portal_me(invoice_service=_inv_svc_for_api)

            # Customer portal invoices (OI-06, design §5).
            from portal.services.portal_invoice_service import wire_portal_invoices

            wire_portal_invoices(
                invoice_service=_inv_svc_for_api,
                account_service=(
                    container.commerce_account_service
                    if container.has("commerce_account_service")
                    else None
                ),
                customer_service=(
                    container.commerce_customer_service
                    if container.has("commerce_customer_service")
                    else None
                ),
            )
        except Exception as exc:
            logger.warning("Commerce invoice API wiring failed: %s", exc)

        # ── Commerce Payment API wiring (Task 8.4) ────────────────────
        # Wire the PaymentService into the payment_endpoints module so
        # the router handlers can access it without circular imports.
        try:
            from commerce.api.payment_endpoints import configure_payment_api
            from commerce.services.payment_service import PaymentService as _PaySvc

            _idempotency_svc_pay = (
                container.idempotency_service
                if container.has("idempotency_service")
                else None
            )
            _inv_svc_pay = (
                container.commerce_invoice_service
                if container.has("commerce_invoice_service")
                else None
            )
            _pay_svc = _PaySvc(
                es_service=elasticsearch_service,
                idempotency_service=_idempotency_svc_pay,
                invoice_service=_inv_svc_pay,
            )
            container.commerce_payment_service = _pay_svc

            configure_payment_api(payment_service=_pay_svc)
            logger.info("Commerce payment API configured")
        except Exception as exc:
            logger.warning("Commerce payment API wiring failed: %s", exc)

        # ── Commerce billing reference loaders (cross-module-entity-linkage
        #    task 12, Req 12.3) ──────────────────────────────────────────
        # Register invoice / account / payment loaders on the process-wide
        # RefResolver so a payment's invoice_id/account_id resolve to
        # navigable references (and a canonical payment_id resolves to a
        # summary). Idempotent + degrades to "unresolved" when ES is absent.
        try:
            from services.ref_loaders import register_billing_link_loaders
            from services.ref_resolver import get_ref_resolver

            register_billing_link_loaders(
                get_ref_resolver(),
                es_service=elasticsearch_service,
            )
            logger.info("Commerce billing reference loaders registered")
        except Exception as exc:
            logger.warning(
                "Commerce billing reference loader registration failed: %s", exc
            )

        # ── Commerce AR Aging API wiring (Task 10.3) ──────────────────
        # Wire the ARAgingService into the ar_aging_endpoints module so
        # the router handlers can access it without circular imports.
        try:
            from commerce.api.ar_aging_endpoints import configure_ar_aging_api
            from commerce.services.ar_aging_service import ARAgingService as _ARAgingSvc

            _ar_aging_svc_for_api = (
                container.commerce_ar_aging_service
                if container.has("commerce_ar_aging_service")
                else _ARAgingSvc(es_service=elasticsearch_service)
            )
            container.commerce_ar_aging_service = _ar_aging_svc_for_api

            configure_ar_aging_api(
                ar_aging_service=_ar_aging_svc_for_api,
            )
            logger.info("Commerce AR aging API configured")
        except Exception as exc:
            logger.warning("Commerce AR aging API wiring failed: %s", exc)

        # ── Commerce External Sync wiring (Task 9.2) ──────────────────
        # Wire CommerceExternalSync into the InvoiceService so that
        # finalize_draft fires on_invoice_finalized as a non-blocking
        # post-commit callback. This enqueues a QBO push sync_run via
        # the existing Integration_Scheduler path without blocking the
        # HTTP response (Design §7).
        try:
            from commerce.services.commerce_external_sync import CommerceExternalSync

            # Resolve QBO connector (may be None if not configured)
            _qbo_connector = None
            if container.has("qbo_connector"):
                _qbo_connector = container.qbo_connector

            # Resolve Stripe connector (may be None if not configured)
            _stripe_connector = None
            if container.has("stripe_connector"):
                _stripe_connector = container.stripe_connector

            # Use the already-wired invoice and payment services
            _ext_inv_svc = (
                container.commerce_invoice_service
                if container.has("commerce_invoice_service")
                else None
            )
            _ext_pay_svc = (
                container.commerce_payment_service
                if container.has("commerce_payment_service")
                else None
            )

            _external_sync = CommerceExternalSync(
                qbo_connector=_qbo_connector,
                stripe_connector=_stripe_connector,
                invoice_service=_ext_inv_svc,
                payment_service=_ext_pay_svc,
            )
            container.commerce_external_sync = _external_sync

            # Inject the external_sync dependency into the InvoiceService
            # so finalize_draft can fire the post-commit callback.
            if _ext_inv_svc is not None:
                _ext_inv_svc._external_sync = _external_sync

            logger.info("Commerce external sync wired into InvoiceService")
        except Exception as exc:
            logger.warning("Commerce external sync wiring failed: %s", exc)

        # ── Margin feed (margin-feed FR3) ──────────────────────────────
        # MarginService, the invoice margin hook and the order-event
        # subscriber, and the admin margin API (margin_endpoints).
        try:
            from commerce.api.margin_endpoints import configure_margin_api
            _margin_svc_for_api = wire_margin_feed(container, elasticsearch_service)
            configure_margin_api(
                margin_service=_margin_svc_for_api,
                cost_entry_service=container.margin_cost_entry_service,
            )
            logger.info("Margin feed services and API wired")
        except Exception as exc:
            logger.warning("Margin feed wiring failed: %s", exc)

        # ── Commerce Sync Pull Subscribers (Task 9.3) ─────────────────
        # Register on_qbo_payment_observed and on_stripe_charge_observed
        # as subscribers on the respective connector sync_pull output
        # streams. Each event is handed to PaymentService.ingest via the
        # CommerceExternalSync adapter. The bridge patches the connectors'
        # sync_pull methods to notify the commerce layer after each
        # successful pull completes.
        try:
            from commerce.services.sync_pull_bridge import register_pull_subscribers

            _bridge_external_sync = (
                container.commerce_external_sync
                if container.has("commerce_external_sync")
                else None
            )
            _bridge_qbo = (
                container.qbo_connector
                if container.has("qbo_connector")
                else None
            )
            _bridge_stripe = (
                container.stripe_connector
                if container.has("stripe_connector")
                else None
            )

            if _bridge_external_sync is not None:
                subscribers = register_pull_subscribers(
                    external_sync=_bridge_external_sync,
                    qbo_connector=_bridge_qbo,
                    stripe_connector=_bridge_stripe,
                    es_service=elasticsearch_service,
                )
                container.commerce_sync_pull_bridge = subscribers

                registered = []
                if subscribers.get("qbo_subscriber"):
                    registered.append("QBO")
                if subscribers.get("stripe_subscriber"):
                    registered.append("Stripe")

                if registered:
                    logger.info(
                        "Commerce sync pull subscribers registered: %s",
                        ", ".join(registered),
                    )
                else:
                    logger.debug(
                        "Commerce sync pull subscribers: no connectors "
                        "available to subscribe to"
                    )
            else:
                logger.warning(
                    "Commerce sync pull subscribers not registered — "
                    "commerce_external_sync not available"
                )
        except Exception as exc:
            logger.warning(
                "Commerce sync pull subscriber registration failed: %s", exc
            )

        # ── Commerce Credit Override Expiry Job (Task 4.4) ─────────────
        # Periodic background task that scans for accounts with expired
        # credit overrides and transitions them back to ok/hold.
        # Runs every 10 minutes following the same asyncio.create_task
        # pattern used by scheduling/fuel periodic jobs.
        try:
            global _credit_override_expiry_task
            from commerce.services.credit_override_expiry_job import (
                run_credit_override_expiry_cycle,
                CREDIT_OVERRIDE_EXPIRY_INTERVAL_SECONDS,
            )
            from commerce.services.credit_service import CreditService as _CreditServiceCls

            # Use the already-wired credit_service if available, otherwise
            # create a fresh instance for the background job.
            _expiry_credit_service = (
                container.commerce_credit_service
                if container.has("commerce_credit_service")
                else _CreditServiceCls(es_service=elasticsearch_service)
            )

            async def _credit_override_expiry_cycle() -> None:
                """One pass expiring stale credit overrides."""
                expired = await run_credit_override_expiry_cycle(
                    es_service=elasticsearch_service,
                    credit_service=_expiry_credit_service,
                )
                if expired:
                    logger.info(
                        "Credit override expiry job: %d override(s) expired",
                        expired,
                    )

            _credit_override_expiry_task = asyncio.create_task(
                run_periodic(
                    "commerce.credit-override-expiry",
                    CREDIT_OVERRIDE_EXPIRY_INTERVAL_SECONDS,
                    _credit_override_expiry_cycle,
                )
            )
            logger.info(
                "Credit override expiry job started (interval: %ds)",
                CREDIT_OVERRIDE_EXPIRY_INTERVAL_SECONDS,
            )
        except Exception as exc:
            logger.warning("Credit override expiry job wiring failed: %s", exc)

        # ── Margin gap sweep + weekly report jobs (margin-feed) ────────
        # Leader-elected via run_periodic. Each cycle returns before any
        # query when commerce_margin_feed_enabled or persistence is off.
        try:
            global _margin_gap_sweep_task, _margin_weekly_report_task
            from commerce.services.margin_jobs import (
                MARGIN_GAP_SWEEP_INTERVAL_SECONDS,
                MARGIN_WEEKLY_REPORT_INTERVAL_SECONDS,
                run_margin_gap_sweep_cycle,
                run_margin_weekly_report_cycle,
            )

            if container.has("margin_service"):
                _margin_service = container.margin_service

                async def _margin_gap_sweep_cycle() -> None:
                    """One margin gap-sweep pass."""
                    counts = await run_margin_gap_sweep_cycle(
                        _margin_service, es_service=elasticsearch_service
                    )
                    if counts.get("written"):
                        logger.info(
                            "Margin gap sweep: %d record(s) written",
                            counts["written"],
                        )

                async def _margin_weekly_report_cycle() -> None:
                    """One margin weekly-report pass."""
                    written = await run_margin_weekly_report_cycle(_margin_service)
                    if written:
                        logger.info(
                            "Margin weekly report job: %d report(s) written", written
                        )

                _margin_gap_sweep_task = asyncio.create_task(
                    run_periodic(
                        "commerce.margin-gap-sweep",
                        MARGIN_GAP_SWEEP_INTERVAL_SECONDS,
                        _margin_gap_sweep_cycle,
                    )
                )
                _margin_weekly_report_task = asyncio.create_task(
                    run_periodic(
                        "commerce.margin-weekly-report",
                        MARGIN_WEEKLY_REPORT_INTERVAL_SECONDS,
                        _margin_weekly_report_cycle,
                    )
                )
                logger.info(
                    "Margin jobs started (gap sweep %ds, weekly report %ds)",
                    MARGIN_GAP_SWEEP_INTERVAL_SECONDS,
                    MARGIN_WEEKLY_REPORT_INTERVAL_SECONDS,
                )
        except Exception as exc:
            logger.warning("Margin job wiring failed: %s", exc)

        # ── Invoice overdue scheduled job ────────────────────────────────
        # Scans invoices_current for open/partial invoices past their
        # due_date and transitions them to overdue. Runs every hour
        # following the same asyncio.create_task pattern.
        try:
            global _invoice_overdue_task
            from commerce.services.invoice_overdue_job import (
                run_invoice_overdue_cycle,
                INVOICE_OVERDUE_INTERVAL_SECONDS,
            )
            from commerce.services.invoice_service import InvoiceService as _InvoiceServiceCls

            # Use the already-wired invoice_service if available, otherwise
            # create a fresh instance for the background job.
            _overdue_invoice_service = (
                container.commerce_invoice_service
                if container.has("commerce_invoice_service")
                else _InvoiceServiceCls(es_service=elasticsearch_service)
            )

            async def _invoice_overdue_cycle() -> None:
                """One pass transitioning past-due invoices to overdue."""
                transitioned = await run_invoice_overdue_cycle(
                    es_service=elasticsearch_service,
                    invoice_service=_overdue_invoice_service,
                )
                if transitioned:
                    logger.info(
                        "Invoice overdue job: %d invoice(s) transitioned",
                        transitioned,
                    )

            _invoice_overdue_task = asyncio.create_task(
                run_periodic(
                    "commerce.invoice-overdue",
                    INVOICE_OVERDUE_INTERVAL_SECONDS,
                    _invoice_overdue_cycle,
                )
            )
            logger.info(
                "Invoice overdue job started (interval: %ds)",
                INVOICE_OVERDUE_INTERVAL_SECONDS,
            )

            # ── Invoice draft-grace finalize job (Req 5.2) ───────────────
            # Transitions draft invoices to open once their grace window
            # elapses. Without this the delivery→ERP loop stays open: an
            # invoice generated from a delivered order waits for a human to
            # call POST /invoices/{id}/finalize, and nothing enqueues the QBO
            # push. Shares the overdue job's service instance and pattern.
            global _invoice_draft_finalize_task
            from commerce.services.invoice_draft_finalize_job import (
                run_invoice_draft_finalize_cycle,
                INVOICE_DRAFT_FINALIZE_INTERVAL_SECONDS,
            )

            _draft_redis = (
                container.redis_client
                if container.has("redis_client")
                else None
            )

            async def _invoice_draft_finalize_cycle() -> None:
                """One pass finalizing drafts past their grace."""
                finalized = await run_invoice_draft_finalize_cycle(
                    es_service=elasticsearch_service,
                    invoice_service=_overdue_invoice_service,
                    redis_client=_draft_redis,
                )
                if finalized:
                    logger.info(
                        "Invoice draft-finalize job: %d "
                        "invoice(s) finalized",
                        finalized,
                    )

            _invoice_draft_finalize_task = asyncio.create_task(
                run_periodic(
                    "commerce.invoice-draft-finalize",
                    INVOICE_DRAFT_FINALIZE_INTERVAL_SECONDS,
                    _invoice_draft_finalize_cycle,
                )
            )
            logger.info(
                "Invoice draft-finalize job started (interval: %ds)",
                INVOICE_DRAFT_FINALIZE_INTERVAL_SECONDS,
            )
        except Exception as exc:
            logger.warning("Invoice overdue job wiring failed: %s", exc)

        # ── AR Aging Snapshot Job (Task 10.2) ─────────────────────────────
        # Daily background task that writes AR aging snapshots for every
        # tenant with commerce.backbone_enabled on. Discovers tenants via
        # accounts_current and calls write_daily_snapshot for each.
        try:
            global _ar_aging_snapshot_task
            from commerce.services.ar_aging_snapshot_job import (
                run_ar_aging_snapshot_cycle,
                AR_AGING_SNAPSHOT_INTERVAL_SECONDS,
            )
            from commerce.services.ar_aging_service import ARAgingService as _ARAgingServiceCls

            # Use the already-wired ar_aging_service if available, otherwise
            # create a fresh instance for the background job.
            _snapshot_ar_aging_service = (
                container.commerce_ar_aging_service
                if container.has("commerce_ar_aging_service")
                else _ARAgingServiceCls(es_service=elasticsearch_service)
            )
            container.commerce_ar_aging_service = _snapshot_ar_aging_service

            async def _ar_aging_snapshot_cycle() -> None:
                """One pass writing daily AR aging snapshots."""
                written = await run_ar_aging_snapshot_cycle(
                    es_service=elasticsearch_service,
                    ar_aging_service=_snapshot_ar_aging_service,
                )
                if written:
                    logger.info(
                        "AR aging snapshot job: %d snapshot(s) written",
                        written,
                    )

            _ar_aging_snapshot_task = asyncio.create_task(
                run_periodic(
                    "commerce.ar-aging-snapshot",
                    AR_AGING_SNAPSHOT_INTERVAL_SECONDS,
                    _ar_aging_snapshot_cycle,
                )
            )
            logger.info(
                "AR aging snapshot job started (interval: %ds)",
                AR_AGING_SNAPSHOT_INTERVAL_SECONDS,
            )
        except Exception as exc:
            logger.warning("AR aging snapshot job wiring failed: %s", exc)

        # ── Commerce AR Aging API wiring (Task 10.3) ──────────────────
        # Wire the ARAgingService into the ar_aging_endpoints module so
        # the router handlers can access it without circular imports.
        try:
            from commerce.api.ar_aging_endpoints import configure_ar_aging_api
            from commerce.services.ar_aging_service import ARAgingService as _ARAgingSvcCls

            # Use the already-wired ar_aging_service if available, otherwise
            # create a fresh instance for the API.
            _api_ar_aging_service = (
                container.commerce_ar_aging_service
                if container.has("commerce_ar_aging_service")
                else _ARAgingSvcCls(es_service=elasticsearch_service)
            )
            container.commerce_ar_aging_service = _api_ar_aging_service

            configure_ar_aging_api(ar_aging_service=_api_ar_aging_service)
            logger.info("Commerce AR aging API configured")
        except Exception as exc:
            logger.warning("Commerce AR aging API wiring failed: %s", exc)
    else:
        logger.debug(
            "Skipping commerce ES index provisioning: "
            "commerce_backbone_enabled is off"
        )

    # ── Analytics snapshot job ─────────────────────────────────────────
    # Daily background task that computes real daily_performance /
    # route_performance / delay_cause_analysis / regional_performance
    # documents in analytics_events from actual delivered/failed orders,
    # replacing the seed-only demo data. Not gated behind
    # commerce_backbone_enabled — this scores fuel_orders_current
    # directly and has no commerce/invoicing dependency.
    try:
        global _analytics_snapshot_task
        from commerce.services.analytics_snapshot_job import (
            run_analytics_snapshot_cycle,
            ANALYTICS_SNAPSHOT_INTERVAL_SECONDS,
        )

        async def _analytics_snapshot_cycle() -> None:
            """One pass computing daily analytics snapshots."""
            snapshotted = await run_analytics_snapshot_cycle(
                es_service=elasticsearch_service,
            )
            if snapshotted:
                logger.info(
                    "Analytics snapshot job: %d tenant(s) snapshotted",
                    snapshotted,
                )

        _analytics_snapshot_task = asyncio.create_task(
            run_periodic(
                "core.analytics-snapshot",
                ANALYTICS_SNAPSHOT_INTERVAL_SECONDS,
                _analytics_snapshot_cycle,
            )
        )
        logger.info(
            "Analytics snapshot job started (interval: %ds)",
            ANALYTICS_SNAPSHOT_INTERVAL_SECONDS,
        )
    except Exception as exc:
        logger.warning("Analytics snapshot job wiring failed: %s", exc)

    logger.info("Core infrastructure initialized")


async def shutdown(app, container: ServiceContainer) -> None:
    """Cleanup core resources."""
    global _credit_override_expiry_task
    global _invoice_overdue_task
    global _ar_aging_snapshot_task
    global _analytics_snapshot_task
    global _margin_gap_sweep_task
    global _margin_weekly_report_task

    # Stand down as sweep leader first, so the replacement task can pick up
    # leadership as soon as this one's lock connection closes rather than
    # waiting for the OS to reap it.
    try:
        from persistence.leader_election import set_sweep_leader

        if container.has("sweep_leader"):
            await container.sweep_leader.stop()
        set_sweep_leader(None)
    except Exception as exc:
        logger.warning("Sweep leader shutdown failed: %s", exc)

    # Cancel the credit override expiry background task if running.
    if _credit_override_expiry_task is not None and not _credit_override_expiry_task.done():
        _credit_override_expiry_task.cancel()
        try:
            await _credit_override_expiry_task
        except asyncio.CancelledError:
            pass
        logger.info("Credit override expiry task stopped")

    # Cancel the invoice overdue background task if running.
    if _invoice_overdue_task is not None and not _invoice_overdue_task.done():
        _invoice_overdue_task.cancel()
        try:
            await _invoice_overdue_task
        except asyncio.CancelledError:
            pass
        logger.info("Invoice overdue task stopped")

    # Cancel the AR aging snapshot background task if running.
    if _ar_aging_snapshot_task is not None and not _ar_aging_snapshot_task.done():
        _ar_aging_snapshot_task.cancel()
        try:
            await _ar_aging_snapshot_task
        except asyncio.CancelledError:
            pass
        logger.info("AR aging snapshot task stopped")

    # Cancel the analytics snapshot background task if running.
    if _analytics_snapshot_task is not None and not _analytics_snapshot_task.done():
        _analytics_snapshot_task.cancel()
        try:
            await _analytics_snapshot_task
        except asyncio.CancelledError:
            pass
        logger.info("Analytics snapshot task stopped")

    # Margin feed: let in-flight margin tasks finish (bounded), then stop
    # the margin jobs.
    if container.has("margin_service"):
        try:
            await container.margin_service.drain(timeout=10)
        except Exception as exc:
            logger.warning("Margin service drain failed: %s", exc)
    for _task in (_margin_gap_sweep_task, _margin_weekly_report_task):
        if _task is not None and not _task.done():
            _task.cancel()
            try:
                await _task
            except asyncio.CancelledError:
                pass
    _margin_gap_sweep_task = None
    _margin_weekly_report_task = None

    # Redis client cleanup is handled by modules that own the connection.
    logger.info("Core infrastructure shut down")
