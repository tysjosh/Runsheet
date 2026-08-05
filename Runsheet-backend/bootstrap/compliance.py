"""
Fuel Compliance Backbone domain bootstrap module.

Initializes the 11 Elasticsearch indices that back the compliance domain
(tax jurisdictions, exemptions, price-protection contracts, driver
qualification files, vehicle/tanker certifications, meter registry +
audit trail, terminal BOLs, sales pricing rules, IFTA mileage, and
K-factor history) and reserves the ``ServiceContainer`` surface for the
services that subsequent tasks will wire (TaxEngine, VCFCalculator,
PriceProtectionService, HOSChecker, DriverQualificationService,
DyedDieselEnforcer, IFTAReporter, MeterAuditService,
KFactorCalibrationService, TerminalBOLIngestionService,
SalesPricingEngine, AssetCertificationService, DeliveryFilter).

This module is intentionally minimal — per task 1.4 of the
fuel-compliance-backbone spec it only provisions indices. Services are
instantiated in the phase-specific tasks that follow (Phases 2–15) and
will be registered on ``container`` alongside the existing scheduling /
fuel / notifications domains.

Follows the same ``async def initialize(app, container)`` /
``async def shutdown(app, container)`` contract as
``bootstrap/scheduling.py`` and ``bootstrap/notifications.py``.

Validates: Requirements 1.5, 3.1, 3.6, 5.1, 7.1, 8.3, 9.5, 10.1, 11.1, 13.1
"""
import asyncio
import logging

from bootstrap.container import ServiceContainer
from persistence.leader_election import run_periodic

logger = logging.getLogger(__name__)

# Module-level references so ``shutdown`` can cancel cron tasks.
_price_protection_expiry_task = None
_rack_price_refresh_task = None
_driver_expiry_cron_agent = None
_asset_cert_expiry_cron_agent = None
_meter_calibration_cron_agent = None


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register fuel-compliance domain services.

    Currently only provisions the 11 compliance indices. Service
    instantiation (TaxEngine, VCFCalculator, PriceProtectionService,
    etc.) is performed by the phase-specific tasks in the
    fuel-compliance-backbone spec. Each service, once added, is
    expected to follow the pattern:

        svc = SomeService(es_service, ...)
        container.some_service = svc

    so that endpoint modules can resolve the dependency through the
    explicit ``ServiceContainer`` (see ``bootstrap/container.py``).
    """
    from compliance.services.compliance_es_mappings import (
        setup_compliance_indices,
    )

    es_service = container.es_service

    # Set up compliance indices. Mirrors the pattern used by
    # ``bootstrap/scheduling.py`` / ``bootstrap/notifications.py``:
    # failures are logged but do not abort the bootstrap chain so
    # remaining modules still initialize (fail-open, per
    # ``bootstrap/__init__.py``).
    try:
        logger.info("Setting up compliance indices...")
        setup_compliance_indices(es_service)
        logger.info("Compliance indices ready")
    except Exception as exc:
        logger.warning("Failed to set up compliance indices: %s", exc)

    # ── Tax Engine REST endpoints (Task 3.9) ───────────────────────
    # Wire the application-scoped ES service into
    # ``compliance.api.tax_endpoints`` so the POST /tax/compute,
    # GET/POST /tax-jurisdictions, and GET/POST /exemptions handlers
    # can construct a per-request, tenant-scoped TaxEngine (design
    # §"Bootstrap Wiring"). Router inclusion is still performed by
    # ``main.py`` so every top-level REST surface stays discoverable
    # in one place.
    try:
        from compliance.api.tax_endpoints import configure_tax_api

        configure_tax_api(es_service=es_service)
        logger.info("Compliance tax API configured")
    except Exception as exc:
        logger.warning("Compliance tax API wiring failed: %s", exc)

    # ── Price Protection REST endpoints (Task 4.7) ─────────────────
    # Wire the application-scoped ES service into
    # ``commerce.api.price_protection_endpoints`` so the CRUD and
    # variance handlers under
    # ``/api/commerce/price-protection-contracts`` can construct a
    # per-request, tenant-scoped :class:`PriceProtectionService`.
    # Router inclusion is still performed by ``main.py`` so every
    # top-level REST surface stays discoverable in one place.
    try:
        from commerce.api.price_protection_endpoints import (
            configure_price_protection_api,
        )

        configure_price_protection_api(es_service=es_service)
        logger.info("Commerce price-protection API configured")
    except Exception as exc:
        logger.warning(
            "Commerce price-protection API wiring failed: %s", exc
        )

    # ── InvoiceService TaxEngine wiring (Task 3.10) ────────────────
    # Inject a per-tenant TaxEngine factory into the already-wired
    # commerce InvoiceService so ``generate_from_order`` appends the
    # computed TaxBreakdown to the invoice before finalization. The
    # factory is tenant-scoped (TaxEngine binds a single tenant per
    # instance) so each invoice-generation call rebuilds the engine
    # with the correct tenant context. InvoiceService preserves the
    # legacy ``tax_cents`` fallback when the factory is absent or
    # when the caller does not supply a destination_fips, so callers
    # that have not adopted the compliance backbone continue to work
    # (commerce-backbone tests still pass).
    try:
        from compliance.services.tax_engine import TaxEngine

        def _tax_engine_factory(tenant_id: str):
            return TaxEngine(es_service=es_service, tenant_id=tenant_id)

        if container.has("commerce_invoice_service"):
            inv_svc = container.commerce_invoice_service
            # Attribute assignment mirrors the pattern used by the
            # commerce external-sync wiring (bootstrap/core.py).
            inv_svc._tax_engine_factory = _tax_engine_factory
            logger.info(
                "InvoiceService wired with TaxEngine factory (task 3.10)"
            )
        else:
            logger.warning(
                "InvoiceService not present in container — TaxEngine "
                "factory not wired (task 3.10). commerce bootstrap may "
                "have failed earlier; InvoiceService will still honor "
                "caller-supplied tax_cents."
            )
    except Exception as exc:
        logger.warning(
            "InvoiceService TaxEngine wiring failed (task 3.10): %s", exc
        )

    # ── Price-protection expiry cron (Task 4.5 / Req 3.6) ─────────
    # Daily background task that iterates over every tenant with
    # price_protection_contracts rows and transitions
    # ``active → exhausted`` (zero remaining_gallons) or
    # ``active → expired`` (past end_date). Follows the
    # ``asyncio.create_task`` pattern used by
    # ``bootstrap/core.py`` (invoice_overdue_job, credit_override_expiry)
    # and ``bootstrap/scheduling.py`` (driver_daily_reset).
    global _price_protection_expiry_task
    try:
        from commerce.services.price_protection_expiry_job import (
            run_price_protection_expiry_cycle,
            PRICE_PROTECTION_EXPIRY_INTERVAL_SECONDS,
        )

        async def _price_protection_expiry_cycle() -> None:
            """One pass transitioning terminal contracts."""
            transitioned = await run_price_protection_expiry_cycle(
                es_service=es_service,
            )
            if transitioned:
                logger.info(
                    "Price protection expiry job: %d "
                    "contract(s) transitioned",
                    transitioned,
                )

        _price_protection_expiry_task = asyncio.create_task(
            run_periodic(
                "compliance.price-protection-expiry",
                PRICE_PROTECTION_EXPIRY_INTERVAL_SECONDS,
                _price_protection_expiry_cycle,
            )
        )
        logger.info(
            "Price protection expiry job started (interval: %ds)",
            PRICE_PROTECTION_EXPIRY_INTERVAL_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "Price protection expiry job wiring failed (task 4.5): %s", exc
        )

    # ── Rack price refresh cron (Task 5.7 / Req 11.6) ────────────
    # Daily background task that refreshes OPIS rack prices with
    # 90-day retention. The actual OPIS API integration is out of
    # scope for this spec — this wires the placeholder so the cron
    # infrastructure is ready when the connector lands.
    global _rack_price_refresh_task
    try:
        from commerce.services.rack_price_refresh_job import (
            refresh_rack_prices,
            RACK_PRICE_REFRESH_INTERVAL_SECONDS,
        )

        async def _rack_price_refresh_cycle() -> None:
            """One pass refreshing OPIS rack prices."""
            refreshed = await refresh_rack_prices()
            if refreshed:
                logger.info(
                    "Rack price refresh job: %d price(s) "
                    "refreshed",
                    refreshed,
                )

        _rack_price_refresh_task = asyncio.create_task(
            run_periodic(
                "compliance.rack-price-refresh",
                RACK_PRICE_REFRESH_INTERVAL_SECONDS,
                _rack_price_refresh_cycle,
            )
        )
        logger.info(
            "Rack price refresh job started (interval: %ds)",
            RACK_PRICE_REFRESH_INTERVAL_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "Rack price refresh job wiring failed (task 5.7): %s", exc
        )

    # ── Pricing endpoints (Task 5.10) ─────────────────────────────
    # Wire the application-scoped ES service into
    # ``commerce.api.pricing_endpoints`` so the CRUD and resolve
    # handlers can construct a per-request, tenant-scoped
    # SalesPricingEngine.
    try:
        from commerce.api.pricing_endpoints import configure_pricing_api

        configure_pricing_api(es_service=es_service)
        logger.info("Commerce pricing API configured")
    except Exception as exc:
        logger.warning(
            "Commerce pricing API wiring failed (task 5.10): %s", exc
        )

    # ── SalesPricingEngine → InvoiceService wiring (Task 5.11) ────
    # Inject a per-tenant SalesPricingEngine factory into the
    # InvoiceService so generate_from_order resolves sell prices
    # before tax computation. Backwards compatible — if the factory
    # is absent, existing line item prices are used as-is.
    try:
        from commerce.services.sales_pricing_engine import SalesPricingEngine

        def _sales_pricing_engine_factory(tenant_id: str):
            return SalesPricingEngine(
                es_service=es_service, tenant_id=tenant_id
            )

        if container.has("commerce_invoice_service"):
            inv_svc = container.commerce_invoice_service
            inv_svc._sales_pricing_engine_factory = (
                _sales_pricing_engine_factory
            )
            logger.info(
                "InvoiceService wired with SalesPricingEngine factory "
                "(task 5.11)"
            )
        else:
            logger.warning(
                "InvoiceService not present in container — "
                "SalesPricingEngine factory not wired (task 5.11)."
            )
    except Exception as exc:
        logger.warning(
            "InvoiceService SalesPricingEngine wiring failed "
            "(task 5.11): %s",
            exc,
        )

    # ── DriverQualificationService REST endpoints (Task 6.9 / Req 5.1–5.8) ──
    # Instantiate the DriverQualificationService and wire it into
    # ``compliance.api.driver_endpoints`` so the CRUD and dashboard
    # handlers can delegate to the service. Router inclusion is
    # performed by ``main.py``.
    try:
        from compliance.services.driver_qualification_service import (
            DriverQualificationService,
        )
        from compliance.api.driver_endpoints import configure_driver_api

        driver_service = DriverQualificationService(es_service=es_service)
        container.driver_qualification_service = driver_service

        # Configure the REST API endpoints
        configure_driver_api(driver_service=driver_service)
        logger.info("Driver Qualification API configured")

        # Cross-module-entity-linkage task 4: inject the qualification service
        # into the ops Drivers endpoints so the correlated profile read
        # (GET /api/ops/drivers/{driver_id}/profile) can surface qualification
        # status by driver_id. Fuel bootstrap runs before compliance, so this
        # is a deferred injection.
        try:
            from fuel.api.driver_endpoints import (
                set_driver_qualification_service,
            )

            set_driver_qualification_service(driver_service)
            logger.info(
                "Driver profile correlation wired with qualification service"
            )
        except Exception as exc:
            logger.warning(
                "Failed to wire qualification service into ops driver "
                "endpoints (task 4): %s",
                exc,
            )
    except Exception as exc:
        logger.warning(
            "Driver Qualification API wiring failed (task 6.9): %s", exc
        )

    # ── Driver Qualification Expiry Cron (Task 6.10 / Req 5.2–5.4, 5.8) ──
    # Daily autonomous agent that runs check_expiry_alerts(),
    # auto_suspend_expired_drivers(), and check_drug_test_overdue()
    # for all tenants. Registered with the AgentScheduler so it
    # benefits from restart policies, health reporting, and SLO
    # tracking alongside the other autonomous agents.
    global _driver_expiry_cron_agent
    try:
        from Agents.autonomous.driver_expiry_cron_agent import (
            DriverExpiryCronAgent,
        )
        from bootstrap.agent_scheduler import RestartPolicy

        # Resolve dependencies — activity_log_service, ws_manager,
        # and confirmation_protocol are required by AutonomousAgentBase.
        # Use container references (same pattern as bootstrap/agents.py).
        activity_log_service = (
            container.get("activity_log_service")
            if container.has("activity_log_service")
            else None
        )
        ws_manager = (
            container.get("ws_manager")
            if container.has("ws_manager")
            else None
        )
        confirmation_protocol = (
            container.get("confirmation_protocol")
            if container.has("confirmation_protocol")
            else None
        )

        _driver_expiry_cron_agent = DriverExpiryCronAgent(
            es_service=es_service,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
        )

        # Register with AgentScheduler (ON_FAILURE restart policy —
        # if the daily cycle crashes, the scheduler will restart it
        # so the next day's run is not missed).
        if container.has("agent_scheduler"):
            scheduler = container.agent_scheduler
            scheduler.register(_driver_expiry_cron_agent, RestartPolicy.ON_FAILURE)
            await scheduler.start_all()
            logger.info(
                "DriverExpiryCronAgent registered with AgentScheduler "
                "(daily, ON_FAILURE restart)"
            )
        else:
            # Fallback: start the agent directly if the scheduler is
            # not yet available (e.g., compliance bootstrap runs before
            # agents bootstrap). The agent manages its own asyncio task.
            await _driver_expiry_cron_agent.start()
            logger.info(
                "DriverExpiryCronAgent started directly (AgentScheduler "
                "not available)"
            )
    except Exception as exc:
        logger.warning(
            "DriverExpiryCronAgent wiring failed (task 6.10): %s", exc
        )

    # ---------------------------------------------------------------
    # Service instantiation placeholders
    # ---------------------------------------------------------------
    # Subsequent tasks in the fuel-compliance-backbone spec add the
    # following services to this bootstrap module. They are listed
    # here as a contract for the domain — DO NOT instantiate them in
    # task 1.4; each has its own task that owns the wiring.
    #
    #   Phase 2  — VCFCalculator                      (task 2.x)
    #   Phase 3  — TaxEngine                          (task 3.x)
    #   Phase 4  — PriceProtectionService             (task 4.x)
    #   Phase 5  — SalesPricingEngine                 (task 5.x)
    #   Phase 6  — DriverQualificationService         (task 6.x)
    #   Phase 7  — HOSChecker                         (task 7.x)
    #   Phase 8  — AssetCertificationService          (task 8.x)
    #   Phase 9  — DyedDieselEnforcer                 (task 9.x)
    #   Phase 10 — MeterAuditService                  (task 10.x)
    #   Phase 11 — TerminalBOLIngestionService        (task 11.x)
    #   Phase 12 — IFTAReporter                       (task 12.x)
    #   Phase 13 — KFactorCalibrationService          (task 13.x)
    #   Phase 15 — DeliveryFilter                     (task 15.x)
    # ---------------------------------------------------------------

    # ── HOSChecker → Route_Planning_Agent wiring (Task 7.8 / Req 4.1–4.7) ──
    # Inject the HOSChecker into the Route_Planning_Agent so it checks
    # driver HOS compliance AFTER the DriverQualificationService check
    # and BEFORE building a route. Follows the same optional-service
    # injection pattern (setter + graceful degradation).
    try:
        from compliance.services.hos_checker import HOSChecker

        # Resolve dependencies for HOSChecker
        redis_client = (
            container.get("redis_client")
            if container.has("redis_client")
            else None
        )
        geotab_connector = (
            container.get("geotab_connector")
            if container.has("geotab_connector")
            else None
        )

        if redis_client and geotab_connector:
            hos_checker = HOSChecker(
                es_service=es_service,
                redis_client=redis_client,
                geotab_connector=geotab_connector,
            )
            container.hos_checker = hos_checker

            # Inject into Route_Planning_Agent if available
            if container.has("route_planning_agent"):
                route_agent = container.route_planning_agent
                route_agent.set_hos_checker(hos_checker)
                logger.info(
                    "HOSChecker wired into Route_Planning_Agent (task 7.8)"
                )
            else:
                logger.warning(
                    "Route_Planning_Agent not present in container — "
                    "HOSChecker not injected (task 7.8). Agent bootstrap "
                    "may run later; HOSChecker will be available on "
                    "container.hos_checker for deferred wiring."
                )
        else:
            logger.warning(
                "HOSChecker dependencies not available (redis_client=%s, "
                "geotab_connector=%s) — HOS checking disabled (task 7.8)",
                "present" if redis_client else "missing",
                "present" if geotab_connector else "missing",
            )
    except Exception as exc:
        logger.warning(
            "HOSChecker wiring failed (task 7.8): %s", exc
        )

    # ── AssetCertificationService → Route_Planning_Agent wiring (Task 8.10 / Req 13.5) ──
    # Inject the AssetCertificationService into the Route_Planning_Agent
    # so it checks asset (truck) DOT cargo tank certification validity
    # AFTER the HOS check and BEFORE building a route. Follows the same
    # optional-service injection pattern (setter + graceful degradation).
    try:
        from compliance.services.asset_certification_service import (
            AssetCertificationService,
        )
        from compliance.api.asset_certification_endpoints import (
            configure_asset_certification_api,
        )

        asset_cert_service = AssetCertificationService(es_service=es_service)
        container.asset_certification_service = asset_cert_service

        # Configure the REST API endpoints. Pass the shared RefResolver so the
        # create path validates the certification's asset subject_ref in-tenant
        # (cross-module-entity-linkage task 10, Req 11.1).
        from services.ref_resolver import get_ref_resolver as _get_ref_resolver

        configure_asset_certification_api(
            asset_certification_service=asset_cert_service,
            ref_resolver=_get_ref_resolver(),
        )
        logger.info("Asset Certification API configured")

        # Inject into Route_Planning_Agent if available
        if container.has("route_planning_agent"):
            route_agent = container.route_planning_agent
            route_agent.set_asset_certification_service(asset_cert_service)
            logger.info(
                "AssetCertificationService wired into Route_Planning_Agent "
                "(task 8.10)"
            )
        else:
            logger.warning(
                "Route_Planning_Agent not present in container — "
                "AssetCertificationService not injected (task 8.10). "
                "Agent bootstrap may run later; "
                "AssetCertificationService will be available on "
                "container.asset_certification_service for deferred wiring."
            )
    except Exception as exc:
        logger.warning(
            "AssetCertificationService wiring failed (task 8.10): %s", exc
        )

    # ── MeterAuditService REST API wiring ──
    # The meter-audit REST surface (register meters, list, audit-trail)
    # is configured here so the MeterAuditPage in the Compliance hub can
    # reach it. Router inclusion is performed by ``main.py``.
    try:
        from compliance.services.meter_audit_service import MeterAuditService
        from compliance.api.meter_endpoints import configure_meter_api

        meter_service = MeterAuditService(es_service=es_service)
        container.meter_audit_service = meter_service
        configure_meter_api(meter_service=meter_service)
        logger.info("Meter Audit API configured")
    except Exception as exc:
        logger.warning("MeterAuditService wiring failed: %s", exc)

    # ── Asset Compliance Status API (cross-module-entity-linkage task 10.2) ──
    # Aggregates an asset's certification + meter status into a single
    # chip-friendly ``overall_status`` consumable by the Fleet assignment
    # surface (GET /api/fleet/assets/{asset_id}/compliance) so an operator does
    # not dispatch a non-compliant asset (Req 11.2). Depends on both the asset
    # certification and meter services wired above; wired here once both exist.
    try:
        from compliance.services.asset_compliance_status_service import (
            AssetComplianceStatusService,
        )
        from compliance.api.asset_compliance_endpoints import (
            configure_asset_compliance_api,
        )

        if container.has("asset_certification_service") and container.has(
            "meter_audit_service"
        ):
            asset_compliance_service = AssetComplianceStatusService(
                certification_service=container.asset_certification_service,
                meter_audit_service=container.meter_audit_service,
            )
            container.asset_compliance_status_service = asset_compliance_service
            configure_asset_compliance_api(
                asset_compliance_status_service=asset_compliance_service
            )
            logger.info("Asset Compliance Status API configured (task 10.2)")
        else:
            logger.warning(
                "Asset Compliance Status API not wired (task 10.2): "
                "certification or meter service missing from container."
            )
    except Exception as exc:
        logger.warning(
            "Asset Compliance Status API wiring failed (task 10.2): %s", exc
        )

    # ── Asset Certification Expiry Cron (Task 8.11 / Req 13.2–13.4) ──
    # Daily autonomous agent that runs check_expiry_alerts() for all
    # tenants with asset certifications. Handles both alert generation
    # and status transitions (valid → expiring_soon → expired).
    # Registered with the AgentScheduler so it benefits from restart
    # policies, health reporting, and SLO tracking.
    global _asset_cert_expiry_cron_agent
    try:
        from Agents.autonomous.asset_cert_expiry_cron_agent import (
            AssetCertExpiryCronAgent,
        )
        from bootstrap.agent_scheduler import RestartPolicy

        # Resolve dependencies — activity_log_service, ws_manager,
        # and confirmation_protocol are required by AutonomousAgentBase.
        # Use container references (same pattern as driver expiry cron).
        activity_log_service = (
            container.get("activity_log_service")
            if container.has("activity_log_service")
            else None
        )
        ws_manager = (
            container.get("ws_manager")
            if container.has("ws_manager")
            else None
        )
        confirmation_protocol = (
            container.get("confirmation_protocol")
            if container.has("confirmation_protocol")
            else None
        )

        _asset_cert_expiry_cron_agent = AssetCertExpiryCronAgent(
            es_service=es_service,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
        )

        # Register with AgentScheduler (ON_FAILURE restart policy —
        # if the daily cycle crashes, the scheduler will restart it
        # so the next day's run is not missed).
        if container.has("agent_scheduler"):
            scheduler = container.agent_scheduler
            scheduler.register(_asset_cert_expiry_cron_agent, RestartPolicy.ON_FAILURE)
            await scheduler.start_all()
            logger.info(
                "AssetCertExpiryCronAgent registered with AgentScheduler "
                "(daily, ON_FAILURE restart)"
            )
        else:
            # Fallback: start the agent directly if the scheduler is
            # not yet available (e.g., compliance bootstrap runs before
            # agents bootstrap). The agent manages its own asyncio task.
            await _asset_cert_expiry_cron_agent.start()
            logger.info(
                "AssetCertExpiryCronAgent started directly (AgentScheduler "
                "not available)"
            )
    except Exception as exc:
        logger.warning(
            "AssetCertExpiryCronAgent wiring failed (task 8.11): %s", exc
        )

    # ── Meter Calibration Cron (Task 10.10 / Req 8.4) ──────────────
    # Daily autonomous agent that runs check_calibration_alerts() for
    # all tenants with meters in the system. Generates alerts for meters
    # whose calibration_expiry_date is within 30 days. Registered with
    # the AgentScheduler so it benefits from restart policies, health
    # reporting, and SLO tracking.
    global _meter_calibration_cron_agent
    try:
        from Agents.autonomous.meter_calibration_cron_agent import (
            MeterCalibrationCronAgent,
        )
        from bootstrap.agent_scheduler import RestartPolicy

        # Resolve dependencies — activity_log_service, ws_manager,
        # and confirmation_protocol are required by AutonomousAgentBase.
        # Use container references (same pattern as asset cert expiry cron).
        activity_log_service = (
            container.get("activity_log_service")
            if container.has("activity_log_service")
            else None
        )
        ws_manager = (
            container.get("ws_manager")
            if container.has("ws_manager")
            else None
        )
        confirmation_protocol = (
            container.get("confirmation_protocol")
            if container.has("confirmation_protocol")
            else None
        )

        _meter_calibration_cron_agent = MeterCalibrationCronAgent(
            es_service=es_service,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
        )

        # Register with AgentScheduler (ON_FAILURE restart policy —
        # if the daily cycle crashes, the scheduler will restart it
        # so the next day's run is not missed).
        if container.has("agent_scheduler"):
            scheduler = container.agent_scheduler
            scheduler.register(_meter_calibration_cron_agent, RestartPolicy.ON_FAILURE)
            await scheduler.start_all()
            logger.info(
                "MeterCalibrationCronAgent registered with AgentScheduler "
                "(daily, ON_FAILURE restart)"
            )
        else:
            # Fallback: start the agent directly if the scheduler is
            # not yet available (e.g., compliance bootstrap runs before
            # agents bootstrap). The agent manages its own asyncio task.
            await _meter_calibration_cron_agent.start()
            logger.info(
                "MeterCalibrationCronAgent started directly (AgentScheduler "
                "not available)"
            )
    except Exception as exc:
        logger.warning(
            "MeterCalibrationCronAgent wiring failed (task 10.10): %s", exc
        )

    # ── DyedDieselEnforcer → OrderIntakePipeline wiring (Task 9.7 / Req 6.1) ──
    # Register the DyedDieselIntakeHook on the OrderIntakePipeline so
    # that dyed-diesel orders are validated against IRS 637M certificates
    # before acceptance. Follows the same hook registration pattern as
    # PricingHook and CreditCheckHook (bootstrap/core.py).
    try:
        from compliance.services.dyed_diesel_enforcer import DyedDieselEnforcer
        from compliance.hooks.dyed_diesel_intake_hook import DyedDieselIntakeHook

        # Resolve signal_bus for sales team notifications (Req 6.2)
        signal_bus = (
            container.get("signal_bus")
            if container.has("signal_bus")
            else None
        )

        dyed_diesel_enforcer = DyedDieselEnforcer(
            es_service=es_service,
            signal_bus=signal_bus,
        )
        container.dyed_diesel_enforcer = dyed_diesel_enforcer

        # Register the intake hook on the pipeline if available
        if container.has("order_intake_pipeline"):
            dyed_diesel_hook = DyedDieselIntakeHook(
                dyed_diesel_enforcer=dyed_diesel_enforcer,
            )
            pipeline = container.order_intake_pipeline
            pipeline.register_hook(dyed_diesel_hook)
            container.dyed_diesel_intake_hook = dyed_diesel_hook
            logger.info(
                "DyedDieselEnforcer wired into OrderIntakePipeline "
                "(task 9.7)"
            )
        else:
            logger.warning(
                "OrderIntakePipeline not present in container — "
                "DyedDieselIntakeHook not registered (task 9.7). "
                "DyedDieselEnforcer is available on "
                "container.dyed_diesel_enforcer for deferred wiring."
            )

        # Task 9.8 / Req 6.3, 6.4: Wire the DyedDieselEnforcer into
        # the CompartmentLoadingAgent so dyed-diesel assignments are
        # validated against compartment dyed-compatible flags before
        # the loading plan is committed.
        if container.has("compartment_loading_agent"):
            compartment_agent = container.compartment_loading_agent
            compartment_agent.set_dyed_diesel_enforcer(dyed_diesel_enforcer)
            logger.info(
                "DyedDieselEnforcer wired into CompartmentLoadingAgent "
                "(task 9.8)"
            )
        else:
            logger.warning(
                "CompartmentLoadingAgent not present in container — "
                "DyedDieselEnforcer not injected (task 9.8). "
                "DyedDieselEnforcer is available on "
                "container.dyed_diesel_enforcer for deferred wiring."
            )

        # Task 9.9 / Req 6.5, 6.7: Wire the DyedDieselEnforcer into
        # the InvoiceService so generate_from_order() performs a
        # post-generation check confirming dyed-diesel invoices exclude
        # road-use excise tax and logs the sale for IRS audit.
        if container.has("commerce_invoice_service"):
            inv_svc = container.commerce_invoice_service
            inv_svc.set_dyed_diesel_enforcer(dyed_diesel_enforcer)
            logger.info(
                "DyedDieselEnforcer wired into InvoiceService "
                "(task 9.9)"
            )
        else:
            logger.warning(
                "InvoiceService not present in container — "
                "DyedDieselEnforcer not injected (task 9.9). "
                "DyedDieselEnforcer is available on "
                "container.dyed_diesel_enforcer for deferred wiring."
            )
    except Exception as exc:
        logger.warning(
            "DyedDieselEnforcer intake hook wiring failed (task 9.7): %s",
            exc,
        )

    # ── IFTA Reporter → GeotabConnector wiring (Task 12.3 / Req 7.1) ──
    # Inject the IFTAReporter and StateBoundaryDetector into the
    # GeotabConnector so that sync_pull automatically detects state
    # boundary crossings from GPS telemetry and records trip segments.
    # This hook is optional — if the GeotabConnector is not available
    # (e.g., no Geotab integration configured), the IFTA reporter
    # still functions for manual mileage adjustments.
    try:
        from compliance.services.ifta_reporter import IFTAReporter
        from compliance.services.state_boundary_detector import (
            StateBoundaryDetector,
        )

        state_boundary_detector = StateBoundaryDetector()
        ifta_reporter = IFTAReporter(
            es_service=es_service,
            state_boundary_detector=state_boundary_detector,
        )
        container.ifta_reporter = ifta_reporter
        container.state_boundary_detector = state_boundary_detector

        # Wire into GeotabConnector if available
        geotab_connector = (
            container.get("geotab_connector")
            if container.has("geotab_connector")
            else None
        )
        if geotab_connector is not None:
            geotab_connector.set_ifta_reporter(
                ifta_reporter, state_boundary_detector
            )
            logger.info(
                "IFTAReporter wired into GeotabConnector (task 12.3)"
            )
        else:
            logger.warning(
                "GeotabConnector not present in container — "
                "IFTAReporter hook not injected (task 12.3). "
                "IFTAReporter is available on container.ifta_reporter "
                "for deferred wiring when a Geotab integration is "
                "configured."
            )
    except Exception as exc:
        logger.warning(
            "IFTAReporter → GeotabConnector wiring failed (task 12.3): %s",
            exc,
        )

    # ── IFTA Reporter REST endpoints (Task 12.10 / Req 7.4–7.7) ──
    # Wire the IFTAReporter into ``compliance.api.ifta_endpoints`` so
    # the GET /report, GET /fleet-mpg, POST /adjustments,
    # GET /adjustments, and GET /completeness handlers can delegate
    # to the service. Router inclusion is performed by ``main.py``.
    try:
        from compliance.api.ifta_endpoints import configure_ifta_api

        ifta_reporter_for_api = (
            container.get("ifta_reporter")
            if container.has("ifta_reporter")
            else None
        )
        if ifta_reporter_for_api is not None:
            configure_ifta_api(ifta_reporter=ifta_reporter_for_api)
            logger.info("IFTA Reporter API configured (task 12.10)")
        else:
            logger.warning(
                "IFTAReporter not present in container — "
                "IFTA API not configured (task 12.10). "
                "The IFTA endpoints will return a runtime error until "
                "the IFTAReporter is wired."
            )
    except Exception as exc:
        logger.warning(
            "IFTA Reporter API wiring failed (task 12.10): %s", exc
        )

    # ── Terminal BOL Ingestion REST endpoints (Task 11.11) ────────
    # Wire the TerminalBOLIngestionService and ES service into
    # ``compliance.api.terminal_bol_endpoints`` so the POST (EDI),
    # POST /upload (manual), POST /{bol_id}/confirm, POST /{bol_id}/link,
    # and GET (list) handlers can delegate to the service.
    # Router inclusion is still performed by ``main.py`` so every
    # top-level REST surface stays discoverable in one place.
    try:
        from compliance.api.terminal_bol_endpoints import (
            configure_terminal_bol_api,
        )
        from compliance.services.terminal_bol_ingestion_service import (
            TerminalBOLIngestionService,
        )
        from compliance.services.terminal_bol_edi_parser import (
            EDIParserRegistry,
        )

        # Resolve optional dependencies for the ingestion service
        vcf_calculator = (
            container.get("vcf_calculator")
            if container.has("vcf_calculator")
            else None
        )
        driver_qualification_service = (
            container.get("driver_qualification_service")
            if container.has("driver_qualification_service")
            else None
        )
        file_storage_service = (
            container.get("file_storage_service")
            if container.has("file_storage_service")
            else None
        )

        edi_parser_registry = EDIParserRegistry()
        bol_ingestion_service = TerminalBOLIngestionService(
            es_service=es_service,
            edi_parser_registry=edi_parser_registry,
            vcf_calculator=vcf_calculator,
            driver_qualification_service=driver_qualification_service,
            file_storage_service=file_storage_service,
        )
        container.terminal_bol_ingestion_service = bol_ingestion_service

        configure_terminal_bol_api(
            bol_service=bol_ingestion_service,
            es_service=es_service,
        )
        logger.info("Terminal BOL ingestion API configured (task 11.11)")
    except Exception as exc:
        logger.warning(
            "Terminal BOL ingestion API wiring failed (task 11.11): %s", exc
        )

    # ── KFactor Calibration → order.delivered subscriber (Task 13.8 / Req 9.1) ──
    # Subscribe the KFactorDeliverySubscriber to the OrderService's
    # order.delivered event so that K-factor variance is computed
    # automatically when a delivery is completed for an auto-fill or
    # keep-full customer. Follows the same subscriber pattern as the
    # commerce invoice generation subscriber (bootstrap/core.py).
    # The handler is fault-tolerant: errors are logged but never block
    # the delivery pipeline.
    try:
        from compliance.hooks.kfactor_delivery_subscriber import (
            KFactorDeliverySubscriber,
        )
        from compliance.services.kfactor_calibration_service import (
            KFactorCalibrationService,
        )

        # Resolve optional dependencies for the KFactor service
        weather_provider = (
            container.get("weather_provider")
            if container.has("weather_provider")
            else None
        )
        # Fallback: when no external weather adapter (NOAA/OpenWeather) is
        # configured, read accumulated HDD from the persisted
        # ``weather_observations`` index so K-factor variance still computes
        # instead of silently disabling the feature.
        if weather_provider is None:
            from compliance.services.es_hdd_provider import EsHddProvider

            weather_provider = EsHddProvider(es_service)
            logger.info(
                "KFactorCalibrationService: no external weather adapter "
                "configured — using EsHddProvider (weather_observations) for "
                "accumulated HDD"
            )
        signal_bus = (
            container.get("signal_bus")
            if container.has("signal_bus")
            else None
        )
        notification_service = (
            container.get("notification_service")
            if container.has("notification_service")
            else None
        )

        kfactor_service = KFactorCalibrationService(
            es_service=es_service,
            weather_provider=weather_provider,
            signal_bus=signal_bus,
            notification_service=notification_service,
        )
        container.kfactor_calibration_service = kfactor_service

        # Create the subscriber handler
        kfactor_subscriber = KFactorDeliverySubscriber(
            kfactor_service=kfactor_service,
            notification_service=notification_service,
        )

        # Register on the OrderService's public subscription helper
        if container.has("order_service"):
            order_service = container.order_service
            order_service.subscribe("order.delivered", kfactor_subscriber)
            logger.info(
                "KFactorDeliverySubscriber registered on order.delivered "
                "event (task 13.8)"
            )
        else:
            logger.warning(
                "KFactorDeliverySubscriber not registered — "
                "order_service not available in container (task 13.8). "
                "KFactorCalibrationService is available on "
                "container.kfactor_calibration_service for deferred wiring."
            )
    except Exception as exc:
        logger.warning(
            "KFactorDeliverySubscriber wiring failed (task 13.8): %s", exc
        )

    # ── K-Factor Calibration REST endpoints (Task 13.9 / Req 9.1–9.6) ──
    # Wire the KFactorCalibrationService into
    # ``compliance.api.kfactor_endpoints`` so the GET /dashboard,
    # POST /{tank_id}/approve, GET /{tank_id}/variance, and
    # GET /{tank_id}/suggest handlers can delegate to the service.
    # Router inclusion is performed by ``main.py``.
    try:
        from compliance.api.kfactor_endpoints import configure_kfactor_api

        kfactor_service_for_api = (
            container.get("kfactor_calibration_service")
            if container.has("kfactor_calibration_service")
            else None
        )
        if kfactor_service_for_api is not None:
            configure_kfactor_api(kfactor_service=kfactor_service_for_api)
            logger.info("K-Factor Calibration API configured (task 13.9)")
        else:
            logger.warning(
                "KFactorCalibrationService not present in container — "
                "K-Factor API not configured (task 13.9). "
                "The K-Factor endpoints will return a runtime error until "
                "the KFactorCalibrationService is wired."
            )
    except Exception as exc:
        logger.warning(
            "K-Factor Calibration API wiring failed (task 13.9): %s", exc
        )

    # ── InvoiceService NotificationService wiring (Task 14.5 / Req 12.6) ──
    # Inject the NotificationService into the InvoiceService so that
    # mark_overdue() fires a ``past_due_invoice`` notification to the
    # account billing contact when an invoice transitions to overdue.
    # Non-blocking: notification failures are logged but never break
    # the overdue transition pipeline.
    try:
        if container.has("commerce_invoice_service") and container.has(
            "notification_service"
        ):
            inv_svc = container.commerce_invoice_service
            notif_svc = container.notification_service
            inv_svc.set_notification_service(notif_svc)
            logger.info(
                "NotificationService wired into InvoiceService for "
                "past_due_invoice notifications (task 14.5)"
            )
        else:
            missing = []
            if not container.has("commerce_invoice_service"):
                missing.append("commerce_invoice_service")
            if not container.has("notification_service"):
                missing.append("notification_service")
            logger.warning(
                "past_due_invoice notification wiring skipped — missing "
                "container services: %s (task 14.5)",
                ", ".join(missing),
            )
    except Exception as exc:
        logger.warning(
            "past_due_invoice notification wiring failed (task 14.5): %s",
            exc,
        )

    # ── DeliveryCompletedSubscriber → order.delivered (Task 14.6 / Req 12.7) ──
    # Subscribe the DeliveryCompletedSubscriber to the OrderService's
    # order.delivered event so that a ``delivery_completed`` notification
    # is sent to the customer's delivery contact when POD is confirmed.
    # The handler is fault-tolerant: notification failures are logged but
    # never block the delivery pipeline.
    try:
        from compliance.hooks.delivery_completed_subscriber import (
            DeliveryCompletedSubscriber,
        )

        notif_svc_for_delivery = (
            container.get("notification_service")
            if container.has("notification_service")
            else None
        )

        if notif_svc_for_delivery is not None:
            delivery_completed_subscriber = DeliveryCompletedSubscriber(
                notification_service=notif_svc_for_delivery,
            )

            if container.has("order_service"):
                order_service = container.order_service
                order_service.subscribe(
                    "order.delivered", delivery_completed_subscriber
                )
                logger.info(
                    "DeliveryCompletedSubscriber registered on "
                    "order.delivered event (task 14.6)"
                )
            else:
                logger.warning(
                    "DeliveryCompletedSubscriber not registered — "
                    "order_service not available in container (task 14.6)"
                )
        else:
            logger.warning(
                "DeliveryCompletedSubscriber not registered — "
                "notification_service not available in container (task 14.6)"
            )
    except Exception as exc:
        logger.warning(
            "DeliveryCompletedSubscriber wiring failed (task 14.6): %s", exc
        )

    # ── BOLSignedSubscriber → PODBOLFinalizer (Task 14.7 / Req 12.8) ──
    # Register the BOLSignedSubscriber on the PODBOLFinalizer so that
    # an ``e_bol_delivery`` notification is sent to the customer's
    # designated BOL recipient email when a signed BOL PDF is generated.
    # The handler is fault-tolerant: notification failures are logged but
    # never block the BOL generation pipeline.
    try:
        from compliance.hooks.bol_signed_subscriber import (
            BOLSignedSubscriber,
        )

        notif_svc_for_bol = (
            container.get("notification_service")
            if container.has("notification_service")
            else None
        )

        if notif_svc_for_bol is not None:
            bol_signed_subscriber = BOLSignedSubscriber(
                notification_service=notif_svc_for_bol,
            )

            if container.has("pod_bol_finalizer"):
                pod_bol_finalizer = container.pod_bol_finalizer
                pod_bol_finalizer.add_bol_subscriber(bol_signed_subscriber)
                logger.info(
                    "BOLSignedSubscriber registered on "
                    "PODBOLFinalizer (task 14.7)"
                )
            else:
                logger.warning(
                    "BOLSignedSubscriber not registered — "
                    "pod_bol_finalizer not available in container (task 14.7). "
                    "The subscriber will be available for deferred wiring "
                    "when the PODBOLFinalizer is initialized."
                )
        else:
            logger.warning(
                "BOLSignedSubscriber not registered — "
                "notification_service not available in container (task 14.7)"
            )
    except Exception as exc:
        logger.warning(
            "BOLSignedSubscriber wiring failed (task 14.7): %s", exc
        )

    # ── DeliveryFilter → Route_Planning_Agent wiring (Task 15.5 / Req 14.5) ──
    # Inject the DeliveryFilter into the Route_Planning_Agent so it
    # partitions delivery candidates by customer call type (will_call,
    # auto_fill, keep_full) at the top of evaluate() before the
    # optimization solver runs. Follows the same optional-service
    # injection pattern (setter + graceful degradation).
    try:
        from compliance.services.delivery_filter import DeliveryFilter

        delivery_filter = DeliveryFilter()
        container.delivery_filter = delivery_filter

        # Inject into Route_Planning_Agent if available
        if container.has("route_planning_agent"):
            route_agent = container.route_planning_agent
            route_agent.set_delivery_filter(delivery_filter)
            logger.info(
                "DeliveryFilter wired into Route_Planning_Agent (task 15.5)"
            )
        else:
            logger.warning(
                "Route_Planning_Agent not present in container — "
                "DeliveryFilter not injected (task 15.5). Agent bootstrap "
                "may run later; DeliveryFilter will be available on "
                "container.delivery_filter for deferred wiring."
            )
    except Exception as exc:
        logger.warning(
            "DeliveryFilter wiring failed (task 15.5): %s", exc
        )

    logger.info("Compliance domain initialized")


async def shutdown(app, container: ServiceContainer) -> None:
    """Shut down fuel-compliance domain services.

    Cancels the price-protection expiry cron (Task 4.5) registered
    by ``initialize``. As subsequent tasks add additional cron loops
    (driver/asset expiry checks, meter calibration alerts, IFTA
    aggregation) they are expected to follow the same pattern:

        1. Capture the ``asyncio.create_task(...)`` handle in a
           module-level variable in ``initialize`` (same pattern as
           ``bootstrap/scheduling.py``).
        2. Cancel the handle here and await it under a
           ``try/except asyncio.CancelledError`` block.
    """
    global _price_protection_expiry_task
    global _rack_price_refresh_task
    global _driver_expiry_cron_agent
    global _asset_cert_expiry_cron_agent
    global _meter_calibration_cron_agent

    if (
        _price_protection_expiry_task is not None
        and not _price_protection_expiry_task.done()
    ):
        _price_protection_expiry_task.cancel()
        try:
            await _price_protection_expiry_task
        except asyncio.CancelledError:
            pass
        logger.info("Price protection expiry task stopped")

    if (
        _rack_price_refresh_task is not None
        and not _rack_price_refresh_task.done()
    ):
        _rack_price_refresh_task.cancel()
        try:
            await _rack_price_refresh_task
        except asyncio.CancelledError:
            pass
        logger.info("Rack price refresh task stopped")

    # Stop the DriverExpiryCronAgent (Task 6.10). If it was registered
    # with the AgentScheduler, the scheduler handles its lifecycle;
    # this is a fallback for the direct-start path.
    if _driver_expiry_cron_agent is not None:
        if not container.has("agent_scheduler"):
            await _driver_expiry_cron_agent.stop()
            logger.info("DriverExpiryCronAgent stopped (direct)")
        _driver_expiry_cron_agent = None

    # Stop the AssetCertExpiryCronAgent (Task 8.11). If it was registered
    # with the AgentScheduler, the scheduler handles its lifecycle;
    # this is a fallback for the direct-start path.
    if _asset_cert_expiry_cron_agent is not None:
        if not container.has("agent_scheduler"):
            await _asset_cert_expiry_cron_agent.stop()
            logger.info("AssetCertExpiryCronAgent stopped (direct)")
        _asset_cert_expiry_cron_agent = None

    # Stop the MeterCalibrationCronAgent (Task 10.10). If it was registered
    # with the AgentScheduler, the scheduler handles its lifecycle;
    # this is a fallback for the direct-start path.
    if _meter_calibration_cron_agent is not None:
        if not container.has("agent_scheduler"):
            await _meter_calibration_cron_agent.stop()
            logger.info("MeterCalibrationCronAgent stopped (direct)")
        _meter_calibration_cron_agent = None

    logger.info("Compliance domain shut down")
