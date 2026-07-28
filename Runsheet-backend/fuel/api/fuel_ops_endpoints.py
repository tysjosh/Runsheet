"""
REST API endpoints for the Fuel Ops hardening capability.

Exposes tenant-scoped endpoints for the US fuel-marketer domain model
introduced in Capabilities 1 and 6 of the fuel-ops-hardening spec:

* ``GET /api/fuel/products`` — the default fuel product catalog filtered by
  the requesting tenant's configured Region (US or NG). The Region is read
  from the JWT-derived :class:`TenantContext` populated by
  :func:`ops.middleware.tenant_guard.get_tenant_context`.

* ``GET /api/fuel/destinations`` — the unified Delivery_Destination list that
  merges legacy ``fuel_stations`` and new ``customer_tanks`` records into a
  single view via :class:`services.delivery_destination_service.DeliveryDestinationService`.
  Optional filters: ``destination_type`` (``retail_station`` | ``customer_tank``),
  ``fuel_product`` (canonical product_code or legacy alias), ``zip_code``.

* ``GET /api/fuel/mvp/customer-tanks`` — list Customer_Tanks for the tenant
  (Req 1.6.2) with optional ``status``, ``customer_id``, ``customer_type``,
  ``fuel_type`` and ``zip_code`` filters.

* ``GET /api/fuel/mvp/customer-tanks/{customer_tank_id}`` — fetch a single
  tank, HTTP 404 on missing / cross-tenant.

* ``POST /api/fuel/mvp/customer-tanks`` — create a Customer_Tank (Req 1.6.3).
  The body is validated through :class:`fuel.customer_tank_models.CustomerTank`
  which canonicalizes legacy aliases (LPG → PROPANE, etc.).

* ``PATCH /api/fuel/mvp/customer-tanks/{customer_tank_id}`` — apply a partial
  update (Req 1.6.3). Immutable fields (``customer_tank_id``, ``tenant_id``,
  ``created_at``) are silently stripped; cross-tenant writes raise 403.

* ``GET /api/fuel/mvp/depots`` — paginated list of Depots for the tenant
  (Req 2.2.2) with optional ``status`` and ``fuel_type`` filters. Latency
  and WGS84 coordinate bounds are enforced by the :class:`Depot` Pydantic
  model.

* ``POST /api/fuel/mvp/depots`` — create a Depot (Req 2.2.2).
  ``location_lat`` and ``location_lon`` are bounded by FastAPI's request
  validation to ``[-90, 90]`` and ``[-180, 180]`` respectively;
  ``fuel_types_supported`` entries are canonicalized to US product codes.

* ``PATCH /api/fuel/mvp/depots/{depot_id}`` — partial update (Req 2.2.2)
  with the same coordinate bounds and product-canonicalization.

* ``DELETE /api/fuel/mvp/depots/{depot_id}`` — delete an owned Depot
  (Req 2.2.2). Missing → 404, cross-tenant → 403, success → 204.

* ``GET /api/fuel/mvp/forecasts`` — paginated tank forecasts, extended with
  ``customer_tank_id``, ``customer_id``, ``customer_type``, ``fuel_type``
  filters on top of the existing ``station_id`` / ``fuel_grade`` / ``run_id``
  filters (Req 1.1.4, 1.6.1). The row shape includes the Capability 1
  annotations (model_name, customer_type_multiplier, weather_fallback,
  scheduled_deliveries) as persisted by the Tank_Forecasting_Agent.

* ``GET /api/fuel/mvp/replans/{event_id}/diff`` — fetch the structured
  Replan_Diff persisted by the Exception_Replanning_Agent for a given
  replan event (Req 2.5.3, Task 4.10). The diff carries ``added_stops``,
  ``removed_stops``, ``reordered_stops``, ``reassigned_stops``,
  ``quantity_changes``, and ``eta_shifts`` nested arrays alongside the
  triggering ``replan_type`` and ``status``. Cross-tenant or missing
  events surface as HTTP 404; events that were escalated without a
  feasible patch (and therefore carry no structured diff) surface the
  distinct ``replan_diff_not_available`` 404.

* ``GET /api/fuel/mvp/compartments/{compartment_id}/load-eligibility`` —
  return the compatibility decision and governing rule for a proposed
  fuel product against the compartment's persisted state (Req 7.2.5,
  Task 6.7). Surfaces the same decision the Compartment_Loading_Agent
  applies before each compartment assignment so dispatchers can
  preview eligibility without triggering a plan. Cross-tenant or
  missing compartments surface as HTTP 404 (existence is never
  leaked); unknown product codes surface as HTTP 422 with a stable
  ``unknown_product_code`` error code.

* ``GET /api/fuel/rack-prices`` — paginated list of the latest
  persisted rack prices for the requesting tenant (Req 8.2.6, Task 7.5).
  Supports ``terminal_id``, ``product_code`` (canonical or legacy
  alias), and ``branded_flag`` filters. Rows are read from the
  ``rack_prices`` ES index populated by the Rack_Price_Provider sync
  in Task 7.4 and re-validated through :class:`RackPrice` so a caller
  cannot force an upstream provider call through the read surface.

* ``GET /api/fuel/mvp/reconciliation`` — paginated list of
  :class:`ReconciliationRecord` documents persisted to the
  ``mvp_reconciliation`` ES index by
  :class:`services.reconciliation_service.ReconciliationService`
  (Req 4.4.4, Task 8.8). Supports ``order_id``, ``plan_id``, ``pod_id``,
  and ``min_variance_pct`` filters — the last one is an OR-across the
  three variance percentages (load-vs-order, delivered-vs-loaded,
  invoiced-vs-delivered). Tenant-scoped via the ES ``term`` filter and a
  defensive re-check on every returned source document. ``invoiced_gallons``
  and ``variance_invoiced_vs_delivered_pct`` surface after the
  QuickBooks Online Connector (Phase 9) calls
  :meth:`ReconciliationService.update_invoice_fields` on the record
  (integration contract documented in Req 4.4.5).

* ``GET /api/fuel/storm-mode/status`` — return the current Storm_Mode
  state for the requesting tenant alongside the triggering alerts,
  any active manual override, and the activation window (Req 9.1.6,
  9.4.3, Task 10.4). The endpoint is backed by the already-wired
  :class:`fuel.services.storm_mode_evaluator.StormModeEvaluator`: it
  reads the persisted state via :meth:`StormModeEvaluator.get_state`
  so the REST call never runs a full evaluation tick, then hydrates
  the triggering :class:`WeatherAlert`\\ s from the ``weather_alerts``
  ES index and the active :class:`StormModeOverride` (if any) from
  ``storm_mode_overrides``. When an ``activate`` / ``deactivate`` /
  ``snooze`` override is in effect, ``override_active`` is ``true``
  and the response's top-level ``state`` reflects the override; the
  computed (alert-derived) state is always preserved in
  ``computed_state`` so the dispatcher UI can distinguish an
  override-forced posture from the automatic one. The endpoint is
  strictly tenant-scoped: the ES filter pins ``tenant_id`` and a
  defensive re-check drops any row whose ``tenant_id`` does not
  match the caller.

* ``POST /api/fuel/storm-mode/override`` — persist a dispatcher or
  admin Storm_Mode override (Req 9.4.2, 9.4.4, Task 10.5). Accepts
  ``action`` (one of activate/deactivate/snooze/clear), ``reason``,
  and optional ``expires_at``. The router stamps ``tenant_id`` from
  the JWT context, derives ``actor_id`` from the verified session
  (``tenant.user_id``) so audit attribution cannot be spoofed by a
  client-supplied value (Req 5.5), and mints ``override_id``
  (``smo_<uuid4>``) so callers cannot spoof ownership or reuse an
  existing id. Role-restricted to dispatcher or admin per Req 9.4.4 —
  other callers receive HTTP 403 ``INSUFFICIENT_ROLE`` via the shared
  exact-match Role_Authorizer. The persisted
  record is visible to the :class:`StormModeEvaluator` on its next
  5-minute tick, and the status endpoint reads overrides out of band
  so the dispatcher banner reflects the submission immediately.

* ``POST /api/fuel/storm-mode/road-restrictions`` — persist a
  tenant-uploaded GeoJSON polygon / multi-polygon representing a
  road closure or impassable area while Storm_Mode is active
  (Req 9.3.3, 9.3.5, Task 10.8). The router stamps ``tenant_id``
  from the JWT context and mints ``restriction_id``
  (``srr_<uuid4>``) so callers cannot spoof ownership. The
  polygon is validated for WGS84 coordinate bounds, ring closure,
  and geometry type (``Polygon`` / ``MultiPolygon`` only); severity
  is constrained to the NOAA bucket enum. Role-restricted to
  dispatcher or admin per Req 9.4.4 — the same role gate the
  override endpoint applies.

* ``GET /api/fuel/storm-mode/road-restrictions`` — return active
  restriction polygons for the tenant so the dispatcher UI's map
  layer can render them with severity-coded colours (Req 9.3.5,
  Task 10.8). By default only currently-applicable restrictions
  are returned (``effective_from <= now`` and, when set,
  ``effective_to >= now``); pass ``include_expired=true`` for
  historical review. Supports an optional ``severity`` filter so
  the UI can re-render a single severity layer without a client-
  side filter pass. Tenant-scoped via the ES ``term`` filter and a
  defensive per-row re-check; malformed rows are dropped rather
  than failing the request.

All endpoints are scoped by ``tenant_id`` exclusively from the signed JWT —
query-parameter or header tenant_ids are ignored. Wiring follows the same
``configure_X`` pattern as :mod:`Agents.support.mvp_endpoints`: bootstrap
calls :func:`configure_fuel_ops_endpoints` once with an ES-service-like
object; the routers are then registered in ``main.py``.

Validates: Requirements 1.1.4, 1.6.1, 1.6.2, 1.6.3, 2.2.2, 2.5.3, 3.1.4, 3.2.4,
4.4.4, 4.4.5, 6.1.3, 6.2.4, 7.2.5, 8.2.6, 8.4.2, 8.4.4, 9.1.6, 9.3.3, 9.3.4,
9.3.5, 9.4.2, 9.4.3, 9.4.4.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Mapping, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from errors.exceptions import (
    depot_not_found,
    driver_not_found,
    supplier_contract_not_found,
    terminal_not_found,
    validation_error,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from auth.authorization import require_role
from Agents.confirmation_protocol import ConfirmationProtocol, MutationRequest
from Agents.support.mvp_es_mappings import (
    MVP_LOAD_PLANS_INDEX,
    MVP_REPLAN_EVENTS_INDEX,
    MVP_ROUTES_INDEX,
    TRUCK_COMPARTMENTS_INDEX,
)
from Agents.support.replan_diff_models import (
    ReplanDiff as FlatReplanDiff,
    compute_replan_diff,
)
from Agents.support.route_solver import (
    INSERT_REASON_CAPACITY,
    INSERT_REASON_OFF_DUTY,
    INSERT_REASON_SLA,
    InfeasibleInsertion,
    insert_emergency_stop,
)
from fuel.combinable_group_models import (
    CombinableGroup,
    CombinableGroupRepository,
)
from fuel.compartment_state_models import (
    CleaningEvent,
    CleaningEventPersistenceError,
    CleaningEventService,
    CleaningMethod,
    CompartmentNotFoundError,
    CompartmentStateConflictError,
    CompartmentStateRepository,
    CrossTenantCompartmentAccessError,
)
from fuel.customer_tank_models import (
    CrossTenantAccessError,
    CustomerTank,
    CustomerTankRepository,
    CustomerTankStatus,
    CustomerType,
    FuelType,
)
from fuel.depot_models import (
    CrossTenantAccessError as DepotCrossTenantAccessError,
    Depot,
    DepotRepository,
    DepotStatus,
)
from fuel.services.compatibility_matrix import (
    CompatibilityDecision,
    check_compatibility,
    load_tenant_compatibility_rules,
)
from fuel.services.delivery_destination_service import (
    DeliveryDestination,
    DeliveryDestinationFilters,
    DeliveryDestinationService,
)
from fuel.services.fuel_planning_ws_manager import FuelPlanningWSManager
from fuel.services.fuel_product_catalog import (
    FuelProduct,
    UnknownFuelProductError,
    canonicalize,
    get_products_for_region,
)
from fuel.services.prioritization_helpers import (
    PriorityCluster,
    SafeToDelayBucket,
    compute_priority_clusters,
)
from fuel.terminal_models import (
    ActiveStatus as TerminalActiveStatus,
    CrossTenantAccessError as TerminalCrossTenantAccessError,
    OperatingHours,
    SourcingRecommendation,
    SourcingRecommendationRepository,
    SupplierContract,
    SupplierContractRepository,
    Terminal,
    TerminalRepository,
    TerminalWaitReport,
    TerminalWaitReportRepository,
    WaitReportSource,
)
from fuel.services.contract_lift_service import (
    ContractLiftService,
    ContractLiftSummary,
    month_bucket,
)
from fuel.services.fuel_ops_es_mappings import (
    MVP_RECONCILIATION_INDEX,
    RACK_PRICES_INDEX,
    STORM_MODE_OVERRIDES_INDEX,
    STORM_ROAD_RESTRICTIONS_INDEX,
    WEATHER_ALERTS_INDEX,
)
from fuel.services.sourcing_recommender import (
    InvalidBrandedPreferenceError,
    SourcingRecommender,
)
from fuel.services.storm_mode_evaluator import (
    ACTIVE as STORM_MODE_ACTIVE,
    DEFAULT_ACTIVATION_SEVERITY,
    DEFAULT_ACTIVATION_WINDOW_HOURS,
    INACTIVE as STORM_MODE_INACTIVE,
    StormModeEvaluator,
)
from fuel.storm_mode_models import (
    StormModeOverride,
    StormModeOverrideAction,
    StormRoadRestriction,
    WeatherAlert,
    WeatherAlertSeverity,
    WeatherAlertSource,
    WeatherAlertStatus,
)
from integrations.rack_price_provider_base import RackPrice

from Agents.support.mvp_es_mappings import MVP_REPLAN_EVENTS_INDEX
from Agents.support.replan_diff_models import ReplanDiff as StructuredReplanDiff

from driver.services.driver_es_mappings import PROOF_OF_DELIVERY_INDEX
from schemas.common import paginated_response_dict
from services.pod_hash_chain import (
    ZERO_HASH,
    canonicalize_pod,
    compute_pod_hash,
)
from services.reconciliation_service import (
    ReconciliationRecord,
    ReconciliationService,
)
from services.ref_resolver import get_ref_resolver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service references, wired via configure_fuel_ops_endpoints()
# ---------------------------------------------------------------------------

_es_service: Any = None
_destination_service: Optional[DeliveryDestinationService] = None
_customer_tank_repository: Optional[CustomerTankRepository] = None
_depot_repository: Optional[DepotRepository] = None
_terminal_repository: Optional[TerminalRepository] = None
_supplier_contract_repository: Optional[SupplierContractRepository] = None
_contract_lift_service: Optional[ContractLiftService] = None
_terminal_wait_report_repository: Optional[TerminalWaitReportRepository] = None
#: Optional async Redis client used by the Task 7.7 wait-summary endpoint to
#: cache the rolling 2-hour average at ``terminal_wait:{tenant_id}:{terminal_id}``
#: so the Sourcing_Recommender (Task 7.9) can read it without re-scanning the
#: ``terminal_wait_reports`` index on every sourcing request. When unset, the
#: wait-summary endpoint falls back to a direct ES aggregation on each call.
_redis_client: Any = None
_cleaning_event_service: Optional[CleaningEventService] = None
_compartment_state_repository: Optional[CompartmentStateRepository] = None
_file_storage_service: Any = None
#: Shared cross-module :class:`RefResolver` used to validate canonical
#: references (e.g. a Cleaning_Event's ``driver_id``) at write time. Defaults
#: to the process-wide resolver; tests may inject one pre-loaded with fakes.
#: cross-module-entity-linkage Req 8.2 / 5.3.
_ref_resolver: Any = None
_confirmation_protocol: Optional[ConfirmationProtocol] = None
_fuel_planning_ws_manager: Optional[FuelPlanningWSManager] = None
_combinable_group_repository: Optional[CombinableGroupRepository] = None
#: Optional :class:`SourcingRecommender` singleton used by the Task 7.10
#: sourcing endpoint. Bootstrap constructs the recommender once with all
#: its dependencies (terminals, contracts, rack-price provider, wait
#: resolver, rack-price sync service) and passes it through
#: :func:`configure_fuel_ops_endpoints`. When unset the endpoint returns
#: HTTP 503 ``sourcing_recommender_unavailable`` so tests that exercise
#: only the persistence path fail loudly rather than silently skipping
#: the recommender call.
_sourcing_recommender: Optional[SourcingRecommender] = None
#: Optional :class:`SourcingRecommendationRepository` used by the Task
#: 7.10 sourcing endpoint to persist every recommendation to the
#: ``sourcing_recommendations`` index for audit. When unset, one is
#: constructed lazily from ``es_service`` at wire-up time.
_sourcing_recommendation_repository: Optional[SourcingRecommendationRepository] = None
#: Optional :class:`fuel.services.storm_mode_evaluator.StormModeEvaluator`
#: singleton used by the Task 10.4 ``GET /api/fuel/storm-mode/status``
#: endpoint. Bootstrap constructs the evaluator once (with the shared
#: ES service, Redis client, and SignalBus) and passes it through
#: :func:`configure_fuel_ops_endpoints`. When unset the status endpoint
#: returns HTTP 503 ``storm_mode_evaluator_unavailable`` so tests that
#: do not wire the evaluator fail loudly rather than silently rendering
#: stale state.
_storm_mode_evaluator: Optional[StormModeEvaluator] = None
#: Optional tenant-config Redis-handle used by the load-eligibility endpoint
#: (Task 6.7) to fetch ``compatibility_matrix_config:{tenant_id}`` overrides.
#: When unset the endpoint evaluates against the default seed table so a
#: Redis outage never blocks eligibility checks — same graceful-degradation
#: contract as the Compartment_Loading_Agent (Task 6.5).
_tenant_config: Any = None

#: Router mounted under ``/api/fuel`` — the MVP-specific endpoints live under
#: ``/api/fuel/mvp`` on :mod:`Agents.support.mvp_endpoints`, so this prefix
#: intentionally differs to keep the two surfaces separate.
router = APIRouter(prefix="/api/fuel", tags=["fuel-ops"])

#: Secondary router dedicated to the ``/api/fuel/mvp`` surface. Task 3.6
#: of the fuel-ops-hardening spec (customer-tank CRUD + extended forecasts)
#: mandates this prefix so clients can reuse the existing ``/api/fuel/mvp``
#: base URL they already use for :mod:`Agents.support.mvp_endpoints`.
mvp_router = APIRouter(prefix="/api/fuel/mvp", tags=["fuel-ops-mvp"])

# Auth policy: JWT_REQUIRED for all fuel-ops endpoints. The tenant guard
# dependency rejects requests without a valid JWT tenant_id claim.
ROUTER_AUTH_POLICY = "jwt_required"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class FuelProductItem(BaseModel):
    """Response shape for a single entry in ``GET /api/fuel/products``.

    Mirrors Requirement 6.1.3's mandated fields (product_code, display_name,
    density_lbs_per_gallon, tax_class, aliases) and includes ``category``
    for downstream contamination/reporting logic and ``region_availability``
    so admin UIs can show where each product is offered.
    """

    model_config = ConfigDict(extra="forbid")

    product_code: str
    display_name: str
    category: str
    density_lbs_per_gallon: float
    tax_class: str
    aliases: List[str] = Field(default_factory=list)
    region_availability: List[str] = Field(default_factory=list)

    @classmethod
    def from_catalog(cls, product: FuelProduct) -> "FuelProductItem":
        return cls(
            product_code=product.product_code,
            display_name=product.display_name,
            category=product.category,
            density_lbs_per_gallon=product.density_lbs_per_gallon,
            tax_class=product.tax_class,
            aliases=list(product.aliases),
            region_availability=list(product.region_availability),
        )


class FuelProductsResponse(BaseModel):
    """Envelope for ``GET /api/fuel/products``.

    ``region`` echoes the tenant's configured Region so the caller can display
    context without parsing tokens. ``total`` is the number of entries
    returned (equal to ``len(items)``) for parity with other list endpoints.
    """

    model_config = ConfigDict(extra="forbid")

    region: str
    items: List[FuelProductItem]
    total: int


class DeliveryDestinationsResponse(BaseModel):
    """Envelope for ``GET /api/fuel/destinations``.

    Mirrors the ``{items, total}`` shape used across other list endpoints so
    front-end pagination helpers can consume it uniformly.
    """

    model_config = ConfigDict(extra="forbid")

    items: List[DeliveryDestination]
    total: int


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_fuel_ops_endpoints(
    *,
    es_service: Any,
    destination_service: Optional[DeliveryDestinationService] = None,
    customer_tank_repository: Optional[CustomerTankRepository] = None,
    depot_repository: Optional[DepotRepository] = None,
    terminal_repository: Optional[TerminalRepository] = None,
    supplier_contract_repository: Optional[SupplierContractRepository] = None,
    contract_lift_service: Optional[ContractLiftService] = None,
    terminal_wait_report_repository: Optional[TerminalWaitReportRepository] = None,
    compartment_state_repository: Optional[CompartmentStateRepository] = None,
    cleaning_event_service: Optional[CleaningEventService] = None,
    file_storage_service: Any = None,
    ref_resolver: Any = None,
    combinable_group_repository: Optional[CombinableGroupRepository] = None,
    confirmation_protocol: Optional[ConfirmationProtocol] = None,
    fuel_planning_ws_manager: Optional[FuelPlanningWSManager] = None,
    sourcing_recommender: Optional[SourcingRecommender] = None,
    sourcing_recommendation_repository: Optional[SourcingRecommendationRepository] = None,
    storm_mode_evaluator: Optional[StormModeEvaluator] = None,
    tenant_config: Any = None,
    redis_client: Any = None,
) -> None:
    """Wire service dependencies into the fuel-ops endpoints module.

    Called once during application startup (from
    :mod:`Runsheet-backend.bootstrap.agents`). Pass ``destination_service``,
    ``customer_tank_repository``, ``depot_repository``, or
    ``terminal_repository`` explicitly in tests to inject mocks; in
    production bootstrap we construct them from ``es_service`` so callers
    only need to supply the Elasticsearch handle already available at that
    wiring point.

    Args:
        es_service: The shared Elasticsearch service (or anything exposing an
            async ``search_documents(index, query, size)``,
            ``index_document(index, doc_id, doc)``,
            ``update_document(index, doc_id, partial)``,
            ``delete_document(index, doc_id)``  method).
        destination_service: Optional pre-built DeliveryDestinationService.
            When omitted, one is constructed from ``es_service``.
        customer_tank_repository: Optional pre-built repository for the
            ``customer_tanks`` index. When omitted, one is constructed from
            ``es_service``. Introduced by Task 3.6 (Req 1.6.2 / 1.6.3).
        depot_repository: Optional pre-built repository for the ``depots``
            index. When omitted, one is constructed from ``es_service``.
            Introduced by Task 4.3 (Req 2.2.2).
        terminal_repository: Optional pre-built repository for the
            ``terminals`` index. When omitted, one is constructed from
            ``es_service``. Introduced by Task 7.2 (Req 8.1.2 / 8.1.4).
        compartment_state_repository: Optional pre-built repository for the
            ``truck_compartments`` lifecycle state. When omitted, one is
            constructed from ``es_service``. Introduced by Task 6.3
            (Req 7.1.4) so the cleaning-events endpoint can look up the
            compartment's ``truck_id`` before persisting a Cleaning_Event.
        cleaning_event_service: Optional pre-built
            :class:`CleaningEventService`. When omitted, one is constructed
            from ``es_service`` + ``compartment_state_repository`` +
            ``file_storage_service``. Introduced by Task 6.3 (Req 7.1.4).
        file_storage_service: Optional :class:`FileStorageService` used to
            validate ``evidence_refs`` belong to the requesting tenant
            before a Cleaning_Event is persisted (Req 7.1.4). When
            omitted the endpoint still accepts ``evidence_refs`` but does
            not validate them — production bootstrap always injects a
            real service so cross-tenant refs are rejected upstream.
        combinable_group_repository: Optional pre-built repository for the
            ``mvp_combinable_groups`` index. When omitted, one is
            constructed from ``es_service``. Introduced by Task 5.6
            (Req 3.2.4) so the ``/api/fuel/mvp/combinable-groups`` list
            endpoint can share the application-scoped ES service.
        confirmation_protocol: Optional :class:`ConfirmationProtocol` used
            by the Task 4.9 emergency-stop endpoint to route the patched
            route through the platform's risk classification + autonomy
            matrix (Req 2.4.5). When omitted the endpoint returns HTTP
            503 ``confirmation_protocol_unavailable`` so tests and early
            bootstrap states fail loudly rather than silently skipping
            the approval step.
        fuel_planning_ws_manager: Optional :class:`FuelPlanningWSManager`
            used by the Task 4.9 emergency-stop endpoint to broadcast
            the Req 2.4.6 ``emergency_stop_inserted`` WebSocket event.
            When omitted the endpoint proceeds without broadcasting so
            HTTP-only tests can exercise the persistence path.
        tenant_config: Optional Redis-like handle (``async get(key) -> raw``)
            used by the Task 6.7 load-eligibility endpoint to fetch
            ``compatibility_matrix_config:{tenant_id}`` overrides from
            Redis. When omitted the endpoint evaluates against the default
            seed table — same graceful-degradation contract as the
            Compartment_Loading_Agent so a Redis outage never blocks
            eligibility checks (Req 7.2.5).
        sourcing_recommender: Optional pre-built
            :class:`fuel.services.sourcing_recommender.SourcingRecommender`
            used by the Task 7.10 sourcing endpoint. Bootstrap
            constructs the recommender once with its full dependency
            set (terminals, contracts, rack-price provider, wait
            resolver, rack-price sync service); tests can pass ``None``
            and the sourcing endpoint returns HTTP 503 until the
            recommender is wired.
        sourcing_recommendation_repository: Optional pre-built
            :class:`fuel.terminal_models.SourcingRecommendationRepository`
            used by the Task 7.10 sourcing endpoint to persist every
            recommendation to the ``sourcing_recommendations`` index
            for audit. When omitted, one is constructed from
            ``es_service``.
        storm_mode_evaluator: Optional pre-built
            :class:`fuel.services.storm_mode_evaluator.StormModeEvaluator`
            used by the Task 10.4
            ``GET /api/fuel/storm-mode/status`` endpoint. Bootstrap
            constructs the evaluator once with its SignalBus + Redis +
            severity loader; tests can pass ``None`` and the status
            endpoint returns HTTP 503 ``storm_mode_evaluator_unavailable``
            until it is wired. The endpoint only calls
            :meth:`StormModeEvaluator.get_state` so no write-path
            dependencies are required at this seam.
        supplier_contract_repository: Optional pre-built repository for the
            ``supplier_contracts`` index. When omitted, one is constructed
            from ``es_service``. Introduced by Task 7.6 (Req 8.3.2 / 8.3.4)
            so the Supplier_Contract CRUD endpoints can share the
            application-scoped ES service.
        contract_lift_service: Optional pre-built
            :class:`ContractLiftService`. When omitted, one is constructed
            from ``redis_client``. Introduced by Task 7.6 (Req 8.3.4) so
            the Loading_Plan commit path and the Supplier_Contract admin
            endpoint share a single counter implementation.
        terminal_wait_report_repository: Optional pre-built repository for
            the ``terminal_wait_reports`` index. When omitted, one is
            constructed from ``es_service``. Introduced by Task 7.7
            (Req 8.4.2 / 8.4.4) so the wait-report submission and
            wait-summary endpoints can share the application-scoped ES
            service.
        redis_client: Optional async Redis client used to construct the
            default :class:`ContractLiftService` when
            ``contract_lift_service`` is not supplied directly. When both
            are ``None`` the counter degrades to a no-op (reads return
            zero) rather than failing the Loading_Plan commit.

            The same Redis client is also used by the Task 7.7 wait-
            summary endpoint to store the rolling 2-hour average at
            ``terminal_wait:{tenant_id}:{terminal_id}`` so the
            Sourcing_Recommender (Task 7.9) can read it in O(1) without
            re-scanning the ``terminal_wait_reports`` index. When omitted,
            the endpoint falls back to a direct ES aggregation over the
            trailing 2 hours on every request.
    """

    global _es_service, _destination_service, _customer_tank_repository
    global _depot_repository, _terminal_repository
    global _supplier_contract_repository, _contract_lift_service
    global _terminal_wait_report_repository, _redis_client
    global _cleaning_event_service, _compartment_state_repository
    global _file_storage_service, _combinable_group_repository
    global _confirmation_protocol, _fuel_planning_ws_manager, _tenant_config
    global _sourcing_recommender, _sourcing_recommendation_repository
    global _storm_mode_evaluator
    global _ref_resolver
    _es_service = es_service
    _destination_service = destination_service or DeliveryDestinationService(es_service)
    _customer_tank_repository = (
        customer_tank_repository or CustomerTankRepository(es_service)
    )
    _depot_repository = depot_repository or DepotRepository(es_service)
    _terminal_repository = terminal_repository or TerminalRepository(es_service)
    _supplier_contract_repository = (
        supplier_contract_repository or SupplierContractRepository(es_service)
    )
    _contract_lift_service = contract_lift_service or ContractLiftService(
        redis_client=redis_client
    )
    _terminal_wait_report_repository = (
        terminal_wait_report_repository
        or TerminalWaitReportRepository(es_service)
    )
    _redis_client = redis_client
    _file_storage_service = file_storage_service
    _compartment_state_repository = (
        compartment_state_repository or CompartmentStateRepository(es_service)
    )
    _cleaning_event_service = cleaning_event_service or CleaningEventService(
        es_service=es_service,
        state_repository=_compartment_state_repository,
        file_storage=file_storage_service,
    )
    # Shared resolver used to validate a Cleaning_Event's optional canonical
    # ``driver_id`` at write time (cross-module-entity-linkage Req 8.2). When
    # omitted the process-wide resolver is used; validation is skipped when no
    # ``driver`` loader is registered so partially-wired environments stay
    # additive/backward-compatible.
    _ref_resolver = ref_resolver
    _combinable_group_repository = (
        combinable_group_repository or CombinableGroupRepository(es_service)
    )
    _confirmation_protocol = confirmation_protocol
    _fuel_planning_ws_manager = fuel_planning_ws_manager
    _tenant_config = tenant_config
    # Sourcing_Recommender wiring (Task 7.10). Bootstrap constructs the
    # recommender with its full dependency set; tests can pass ``None``
    # and the sourcing endpoint will return HTTP 503 until the
    # recommender is wired. The recommendation repository defaults to a
    # fresh instance so the endpoint can persist audit records without
    # needing a separate ES service reference.
    _sourcing_recommender = sourcing_recommender
    _sourcing_recommendation_repository = (
        sourcing_recommendation_repository
        or SourcingRecommendationRepository(es_service)
    )
    # Storm_Mode_Evaluator wiring (Task 10.4). Bootstrap constructs the
    # evaluator with its SignalBus + Redis + severity loader once; tests
    # can pass ``None`` and the status endpoint will return HTTP 503
    # ``storm_mode_evaluator_unavailable`` until it is wired. The
    # evaluator is read-only here (the endpoint calls
    # :meth:`StormModeEvaluator.get_state`) so no additional construction
    # happens when the caller passes ``None``.
    _storm_mode_evaluator = storm_mode_evaluator


def _get_destination_service() -> DeliveryDestinationService:
    if _destination_service is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _destination_service


def _get_customer_tank_repository() -> CustomerTankRepository:
    if _customer_tank_repository is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _customer_tank_repository


def _get_depot_repository() -> DepotRepository:
    if _depot_repository is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _depot_repository


def _get_terminal_repository() -> TerminalRepository:
    if _terminal_repository is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _terminal_repository


def _get_supplier_contract_repository() -> SupplierContractRepository:
    if _supplier_contract_repository is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _supplier_contract_repository


def _get_contract_lift_service() -> ContractLiftService:
    if _contract_lift_service is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _contract_lift_service


def _get_terminal_wait_report_repository() -> TerminalWaitReportRepository:
    if _terminal_wait_report_repository is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _terminal_wait_report_repository


def _get_redis_client() -> Any:
    """Return the module-wired Redis client, or ``None`` when not configured.

    Task 7.7 writes the rolling 2-hour wait average to
    ``terminal_wait:{tenant_id}:{terminal_id}`` so the Sourcing_Recommender
    can read it in O(1). A missing client degrades the wait-summary
    endpoint to an ES-only aggregation (still correct, just slower), so
    we return ``None`` rather than raising.
    """

    return _redis_client


def _get_ref_resolver():
    """Return the shared :class:`RefResolver` used for write-time validation.

    Defaults to the process-wide resolver when one was not injected via
    :func:`configure_fuel_ops_endpoints` (cross-module-entity-linkage Req 8.2).
    """
    return _ref_resolver if _ref_resolver is not None else get_ref_resolver()


def _get_cleaning_event_service() -> CleaningEventService:
    if _cleaning_event_service is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _cleaning_event_service


def _get_compartment_state_repository() -> CompartmentStateRepository:
    if _compartment_state_repository is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _compartment_state_repository


def _get_combinable_group_repository() -> CombinableGroupRepository:
    if _combinable_group_repository is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _combinable_group_repository


def _get_sourcing_recommender() -> SourcingRecommender:
    """Return the module-wired :class:`SourcingRecommender` singleton.

    Task 7.10 requires the sourcing endpoint to invoke the already-wired
    recommender. When the recommender has not been configured (e.g.
    early bootstrap or a test that only exercises persistence), the
    endpoint surfaces HTTP 503 ``sourcing_recommender_unavailable`` so
    callers fail loudly rather than silently skipping the ranking step.
    """

    if _sourcing_recommender is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "sourcing_recommender_unavailable",
                "message": (
                    "Sourcing recommender is not configured. Finish the "
                    "bootstrap wire-up (see bootstrap/agents.py) before "
                    "calling /api/fuel/sourcing/recommendations."
                ),
            },
        )
    return _sourcing_recommender


def _get_sourcing_recommendation_repository() -> SourcingRecommendationRepository:
    if _sourcing_recommendation_repository is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _sourcing_recommendation_repository


def _get_es() -> Any:
    if _es_service is None:
        raise RuntimeError(
            "Fuel-ops endpoints not configured. "
            "Call configure_fuel_ops_endpoints() during startup."
        )
    return _es_service


# ---------------------------------------------------------------------------
# GET /api/fuel/products (Req 6.1.3)
# ---------------------------------------------------------------------------


@router.get("/products", response_model=FuelProductsResponse)
async def list_fuel_products(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> FuelProductsResponse:
    """Return the default fuel product catalog filtered by the tenant's Region.

    The Region comes from :class:`TenantContext`, which the tenant guard
    middleware populates from the tenant settings service (or defaults to
    ``"US"`` for tenants without an explicit record). When the Region has no
    catalog entries (e.g., a misconfigured or unrecognized Region), the
    response carries an empty ``items`` list rather than erroring — the
    admin UI surfaces this as a "no products configured" setup task.

    Validates: Requirement 6.1.3.
    """

    region = tenant.region or "US"
    products = get_products_for_region(region)
    items = [FuelProductItem.from_catalog(p) for p in products]
    logger.debug(
        "fuel_ops.products: tenant=%s region=%s returned=%d",
        tenant.tenant_id,
        region,
        len(items),
    )
    return FuelProductsResponse(region=region, items=items, total=len(items))


# ---------------------------------------------------------------------------
# GET /api/fuel/destinations (Req 6.2.4)
# ---------------------------------------------------------------------------


DestinationTypeLiteral = Literal["retail_station", "customer_tank"]


@router.get("/destinations", response_model=DeliveryDestinationsResponse)
async def list_delivery_destinations(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    destination_type: Optional[DestinationTypeLiteral] = Query(
        default=None,
        description=(
            "Restrict to retail_station or customer_tank destinations only. "
            "Omit to return both."
        ),
    ),
    fuel_product: Optional[str] = Query(
        default=None,
        description=(
            "Filter by fuel product. Accepts the canonical product_code "
            "(e.g. DIESEL_2) or a legacy alias (e.g. AGO); aliases are "
            "resolved via the fuel product catalog."
        ),
    ),
    zip_code: Optional[str] = Query(
        default=None,
        description="Exact-match filter on the destination's zip_code.",
    ),
) -> DeliveryDestinationsResponse:
    """Return the unified Delivery_Destination list for the tenant.

    Results merge ``fuel_stations`` and ``customer_tanks`` via
    :class:`DeliveryDestinationService`, which also handles volume
    normalization (legacy liters → US gallons) and product alias
    canonicalization so the filter matches regardless of how the source
    record stored its grade.

    Validates: Requirement 6.2.4.
    """

    service = _get_destination_service()

    # Only send filters that were actually provided — ``DeliveryDestinationFilters``
    # treats empty strings as "not set" but dropping them here keeps the shape
    # explicit and avoids a Pydantic validation error for empty literals.
    raw_filters: dict[str, Any] = {}
    if destination_type:
        raw_filters["destination_type"] = destination_type
    if fuel_product and fuel_product.strip():
        raw_filters["fuel_product"] = fuel_product.strip()
    if zip_code and zip_code.strip():
        raw_filters["zip_code"] = zip_code.strip()

    try:
        filters = DeliveryDestinationFilters(**raw_filters)
    except ValueError as exc:
        # Pydantic raises ValueError/ValidationError for unknown literals.
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        destinations = await service.list(
            tenant_id=tenant.tenant_id,
            filters=filters,
        )
    except ValueError as exc:
        # Empty tenant_id would raise; guarded upstream but re-raised as 400.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "fuel_ops.destinations: unexpected error for tenant=%s",
            tenant.tenant_id,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    logger.debug(
        "fuel_ops.destinations: tenant=%s type=%s product=%s zip=%s returned=%d",
        tenant.tenant_id,
        destination_type,
        fuel_product,
        zip_code,
        len(destinations),
    )
    return DeliveryDestinationsResponse(
        items=destinations,
        total=len(destinations),
    )


# ---------------------------------------------------------------------------
# GET /api/fuel/rack-prices (Req 8.2.6, Task 7.5)
# ---------------------------------------------------------------------------


class RackPriceListResponse(BaseModel):
    """Envelope for ``GET /api/fuel/rack-prices`` (Req 8.2.6).

    Mirrors the ``{items, total, page, page_size, has_next}`` shape used
    across the other fuel-ops list endpoints so the frontend pagination
    helper consumes it uniformly. Items are returned as
    :class:`integrations.rack_price_provider_base.RackPrice` models —
    the same Pydantic shape the Rack_Price_Provider adapters persist to
    the ``rack_prices`` ES index (Task 7.4). The endpoint reads from the
    index directly rather than re-invoking the provider so a caller
    cannot force a fresh upstream fetch; the freshest price visible to
    a tenant is whatever the provider / sync job last wrote.
    """

    model_config = ConfigDict(extra="forbid")

    items: List[RackPrice]
    total: int
    page: int
    page_size: int
    has_next: bool


@router.get("/rack-prices", response_model=RackPriceListResponse)
async def list_rack_prices(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    terminal_id: Optional[str] = Query(
        default=None,
        description=(
            "Filter to a single terminal_id. Must match an owning "
            "Terminal or the caller sees an empty list — tenant "
            "isolation is enforced on the underlying ``rack_prices`` "
            "ES index."
        ),
    ),
    product_code: Optional[str] = Query(
        default=None,
        description=(
            "Filter by canonical product_code (e.g. ``DIESEL_2``, "
            "``PROPANE``) or a legacy alias (``AGO``, ``LPG``, "
            "``PMS``, ``ATK``); aliases are resolved through the fuel "
            "product catalog before the ES query is issued. Unknown "
            "codes short-circuit to an empty result set."
        ),
    ),
    branded_flag: Optional[bool] = Query(
        default=None,
        description=(
            "``true`` to restrict to branded prices, ``false`` to "
            "restrict to unbranded. Omit to include both."
        ),
    ),
    page: int = Query(1, ge=1, description="Page number, 1-indexed."),
    size: int = Query(
        50,
        ge=1,
        le=500,
        description="Page size (1–500). Defaults to 50.",
    ),
) -> RackPriceListResponse:
    """Return the latest rack prices for the tenant, filtered.

    The endpoint queries the ``rack_prices`` ES index directly rather
    than invoking a provider so a caller cannot force an upstream fetch
    or inflate a tenant's provider budget via the read surface. The
    rows returned are whatever the Rack_Price_Provider adapters
    (Task 7.4) last persisted for this tenant. Tenant isolation is
    enforced at two points:

        1. The ES query filters on ``tenant_id`` via a ``term`` clause.
        2. Every returned ``_source`` is re-validated against the
           caller's ``tenant_id`` before it is surfaced — a mis-labelled
           document never crosses the endpoint boundary.

    Results are ordered by ``effective_at`` (descending) then
    ``retrieved_at`` (descending) so clients asking for "the latest
    price" always see the freshest row first. The ``total`` field
    reflects the ES hit count when available so the UI can render
    correct pagination totals even when a single page is requested.

    Validates: Requirement 8.2.6.
    """

    es = _get_es()

    must_clauses: List[Dict[str, Any]] = [
        {"term": {"tenant_id": tenant.tenant_id}}
    ]

    if terminal_id and terminal_id.strip():
        must_clauses.append({"term": {"terminal_id": terminal_id.strip()}})

    if product_code and product_code.strip():
        try:
            canonical_product = canonicalize(product_code.strip())
        except UnknownFuelProductError:
            # Unknown product → empty result set. Surface ``total=0``
            # rather than a 400 because the spec treats the filter as a
            # best-effort match and the UI already renders empty lists.
            logger.debug(
                "fuel_ops.rack_prices: unknown product_code %r → empty list",
                product_code,
            )
            return RackPriceListResponse(
                items=[], total=0, page=page, page_size=size, has_next=False
            )
        must_clauses.append({"term": {"product_code": canonical_product}})

    if branded_flag is not None:
        must_clauses.append({"term": {"branded_flag": bool(branded_flag)}})

    query: Dict[str, Any] = {
        "query": {"bool": {"must": must_clauses}},
        "sort": [
            {"effective_at": {"order": "desc"}},
            {"retrieved_at": {"order": "desc"}},
        ],
        "from": (page - 1) * size,
        "size": size,
    }

    try:
        resp = await es.search_documents(RACK_PRICES_INDEX, query, size)
    except Exception as exc:
        logger.error(
            "fuel_ops.rack_prices: ES query failed for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    # Handle both dict and ObjectApiResponse
    hits_outer = resp.get("hits", {}) if hasattr(resp, 'get') else {}
    hits = hits_outer.get("hits", []) or []
    total_block = hits_outer.get("total", {}) if hasattr(hits_outer, 'get') else {}
    if hasattr(total_block, 'get'):
        total_count = int(total_block.get("value", 0) or 0)
    else:
        try:
            total_count = int(total_block or 0)
        except (TypeError, ValueError):
            total_count = 0

    items: List[RackPrice] = []
    for hit in hits:
        source = hit.get("_source") if hasattr(hit, 'get') else None
        if not isinstance(source, dict):
            continue
        # Defense-in-depth: drop any row whose tenant_id does not match
        # the caller. The ES ``term`` clause should already exclude them
        # but a mis-labelled document must never leak across the
        # endpoint boundary.
        if source.get("tenant_id") != tenant.tenant_id:
            logger.warning(
                "fuel_ops.rack_prices: dropping row with mismatched "
                "tenant_id %s (expected %s)",
                source.get("tenant_id"),
                tenant.tenant_id,
            )
            continue
        try:
            # The persisted ``rack_prices`` document carries ES-only
            # bookkeeping fields (``created_at`` / ``updated_at``) that the
            # strict ``RackPrice`` model (``extra="forbid"``) does not
            # define. Drop them before validation so a freshly-synced or
            # seeded row is not silently discarded on read.
            model_fields = RackPrice.model_fields.keys()
            cleaned = {k: v for k, v in source.items() if k in model_fields}
            items.append(RackPrice(**cleaned))
        except ValidationError as exc:
            # Corrupt row → drop it with a warning so a single bad
            # document does not take out the entire list response.
            logger.warning(
                "fuel_ops.rack_prices: dropping row that failed model "
                "validation (rack_price_id=%s): %s",
                source.get("rack_price_id"),
                exc,
            )

    has_next = total_count > page * size

    logger.debug(
        "fuel_ops.rack_prices: tenant=%s terminal=%s product=%s branded=%s "
        "page=%d size=%d returned=%d total=%d",
        tenant.tenant_id,
        terminal_id,
        product_code,
        branded_flag,
        page,
        size,
        len(items),
        total_count,
    )
    return RackPriceListResponse(
        items=items,
        total=total_count,
        page=page,
        page_size=size,
        has_next=has_next,
    )


# ---------------------------------------------------------------------------
# Customer_Tank request / response models (Req 1.6.2, 1.6.3)
# ---------------------------------------------------------------------------


class CustomerTankCreateRequest(BaseModel):
    """Body for ``POST /api/fuel/mvp/customer-tanks`` (Req 1.6.3).

    Mirrors :class:`fuel.customer_tank_models.CustomerTank` but omits
    repository-managed fields (``tenant_id``, ``created_at``,
    ``updated_at``) and makes ``customer_tank_id`` optional so the
    repository can mint one when it is not supplied by the caller.
    """

    model_config = ConfigDict(extra="forbid")

    customer_tank_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional client-supplied identifier. When omitted the "
            "repository mints a uuid4-based id."
        ),
    )
    customer_id: str = Field(..., min_length=1)
    customer_type: CustomerType
    fuel_type: FuelType
    fuel_product_code: str = Field(..., min_length=1)
    capacity_gallons: float = Field(..., gt=0)
    current_level_gallons: float = Field(..., ge=0)
    last_reading_at: Optional[Any] = None
    location_lat: float = Field(..., ge=-90.0, le=90.0)
    location_lon: float = Field(..., ge=-180.0, le=180.0)
    zip_code: str = Field(..., min_length=1)
    k_factor: Optional[float] = Field(default=None, ge=0)
    use_case: Optional[str] = None
    status: CustomerTankStatus = "active"
    last_refill_order_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Optional reference to the fuel order whose delivery most "
            "recently refilled this tank (cross-module-entity-linkage "
            "Req 7.2)."
        ),
    )


class CustomerTankUpdateRequest(BaseModel):
    """Body for ``PATCH /api/fuel/mvp/customer-tanks/{id}`` (Req 1.6.3).

    Every field is optional so callers can send just the delta. The
    repository silently ignores attempts to mutate immutable fields
    (``customer_tank_id``, ``tenant_id``, ``created_at``) so the router
    does not need to strip them itself.
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: Optional[str] = Field(default=None, min_length=1)
    customer_type: Optional[CustomerType] = None
    fuel_type: Optional[FuelType] = None
    fuel_product_code: Optional[str] = Field(default=None, min_length=1)
    capacity_gallons: Optional[float] = Field(default=None, gt=0)
    current_level_gallons: Optional[float] = Field(default=None, ge=0)
    last_reading_at: Optional[Any] = None
    location_lat: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    location_lon: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    zip_code: Optional[str] = Field(default=None, min_length=1)
    k_factor: Optional[float] = Field(default=None, ge=0)
    use_case: Optional[str] = None
    status: Optional[CustomerTankStatus] = None
    last_refill_order_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Associate the fulfilling delivery order with this tank "
            "(cross-module-entity-linkage Req 7.2)."
        ),
    )


class CustomerTankListResponse(BaseModel):
    """Envelope for ``GET /api/fuel/mvp/customer-tanks``."""

    model_config = ConfigDict(extra="forbid")

    items: List[CustomerTank]
    total: int
    page: int
    page_size: int
    has_next: bool


#: Reference types ``GET /api/fuel/mvp/customer-tanks/{id}?expand=...`` resolves.
_VALID_CUSTOMER_TANK_EXPAND = ("customer", "last_refill_order")


def _parse_customer_tank_expand(expand: Optional[str]) -> set[str]:
    """Parse a comma-separated ``expand`` query into a set of known tokens.

    Unknown tokens are ignored so the param stays additive/forward-compatible
    (cross-module-entity-linkage Req 6.3).
    """
    if not expand:
        return set()
    requested = {tok.strip() for tok in expand.split(",") if tok.strip()}
    return requested & set(_VALID_CUSTOMER_TANK_EXPAND)


class CustomerTankDetailResponse(CustomerTank):
    """``CustomerTank`` plus a resolved cross-module ``links`` object.

    Returned by ``GET /api/fuel/mvp/customer-tanks/{id}`` only when ``expand``
    is supplied; each link is either a resolved summary (``{status, id,
    summary}``) or an explicit ``{status: "unresolved", id}`` / ``{status:
    "empty", id}`` marker so the UI can render an "unlinked" affordance rather
    than a silently-dropped field (cross-module-entity-linkage Req 5.4 /
    Property 4 / 7.3).
    """

    model_config = ConfigDict(extra="forbid")

    links: Dict[str, Any]


async def _build_customer_tank_links(
    tenant_id: str, tank: CustomerTank, expand: set[str]
) -> Dict[str, Any]:
    """Resolve the requested customer-tank references into a ``links`` object.

    All resolution is tenant-scoped via the loaders; references never cross
    tenants (Req 5.3). A reference is returned resolved or explicitly
    unresolved — never omitted (Req 5.4). ``last_refill_order`` resolves
    against the ``order`` entity type since the refilling delivery is a fuel
    order (Req 7.2).
    """
    refs: Dict[str, tuple[str, Optional[str]]] = {}
    if "customer" in expand:
        refs["customer"] = ("customer", tank.customer_id)
    if "last_refill_order" in expand:
        refs["last_refill_order"] = ("order", tank.last_refill_order_id)

    resolver = _get_ref_resolver()
    resolved = await resolver.resolve_many(tenant_id, refs)
    return {key: ref.to_dict() for key, ref in resolved.items()}


async def _validate_customer_ref(tenant_id: str, customer_id: Optional[str]) -> None:
    """Reject a customer-tank write whose ``customer_id`` does not resolve.

    Enforces that the tank's ``customer_id`` references an existing commerce
    customer in the same tenant at write time (cross-module-entity-linkage
    Req 7.1). Validation is delegated to the shared ``RefResolver`` and is only
    enforced when a ``customer`` loader is registered, so a partially-wired
    environment (e.g. a focused unit test that injects no resolver) stays
    additive/backward-compatible rather than rejecting every write. Raises
    ``validation_error`` (HTTP 400, ``details.reason = customer_not_found``)
    when the reference is non-existent or cross-tenant.
    """
    if not customer_id:
        return
    resolver = _get_ref_resolver()
    try:
        registered = "customer" in resolver.registered_types()
    except Exception:  # noqa: BLE001 - defensive; never block a write on this
        registered = False
    if not registered:
        return
    await resolver.validate_ref(tenant_id, "customer", customer_id, required=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _translate_cross_tenant_error(exc: CrossTenantAccessError) -> HTTPException:
    """Map :class:`CrossTenantAccessError` to an HTTP 403 without leaking
    the owning tenant's identity back to the caller."""

    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error_code": "cross_tenant_access_denied",
            "message": "Customer tank belongs to a different tenant.",
            "customer_tank_id": exc.customer_tank_id,
        },
    )


def _translate_validation_error(exc: Exception) -> HTTPException:
    """Map Pydantic validation or catalog errors to a 422 with structured detail.

    Sanitizes Pydantic's ``ValidationError.errors()`` output by dropping
    the ``ctx`` and ``url`` fields and converting any residual non-JSON
    primitives (e.g. nested :class:`Exception` instances inside ``ctx``,
    :class:`datetime` values inside ``input``) to strings. This is
    important because FastAPI's default JSON encoder raises
    :class:`TypeError` when it encounters a non-serializable object,
    which would otherwise mask the 422 as a 500 to the caller.
    """

    def _to_jsonable(value: Any) -> Any:
        """Best-effort conversion of Pydantic error values to JSON-safe types."""
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple)):
            return [_to_jsonable(item) for item in value]
        if isinstance(value, dict):
            return {str(k): _to_jsonable(v) for k, v in value.items()}
        # datetime, Exception, and other opaque objects fall through here —
        # str() produces a safe, readable representation so the 422 payload
        # always renders.
        return str(value)

    message = str(exc)
    details: Any
    if isinstance(exc, ValidationError):
        details = []
        for err in exc.errors():
            clean = {}
            for key, value in err.items():
                if key in ("ctx", "url"):
                    # ``ctx`` nests a raw Exception which isn't JSON-safe;
                    # ``url`` points to Pydantic's own docs and doesn't
                    # help callers.
                    continue
                if isinstance(value, tuple):
                    clean[key] = [_to_jsonable(item) for item in value]
                else:
                    clean[key] = _to_jsonable(value)
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


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/customer-tanks (Req 1.6.2)
# ---------------------------------------------------------------------------


@mvp_router.get("/customer-tanks", response_model=CustomerTankListResponse)
async def list_customer_tanks(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    status_filter: Optional[CustomerTankStatus] = Query(
        default=None,
        alias="status",
        description="Restrict to a single status (active | inactive | maintenance).",
    ),
    customer_id: Optional[str] = Query(
        default=None, description="Filter by owning customer_id."
    ),
    customer_type: Optional[CustomerType] = Query(
        default=None, description="Filter by customer_type."
    ),
    fuel_type: Optional[FuelType] = Query(
        default=None, description="Filter by fuel_type family."
    ),
    zip_code: Optional[str] = Query(
        default=None, description="Exact-match ZIP filter."
    ),
    page: int = Query(1, ge=1, description="Page number, 1-indexed."),
    size: int = Query(20, ge=1, le=500, description="Page size (1–500)."),
) -> CustomerTankListResponse:
    """Return the paginated list of Customer_Tanks for the tenant.

    Enforces tenant isolation through
    :class:`CustomerTankRepository.list_for_tenant`, which re-validates
    every returned document against the caller's tenant_id.

    Validates: Requirement 1.6.2.
    """

    repo = _get_customer_tank_repository()

    # Fetch a slightly larger window so we can paginate deterministically
    # without an additional round-trip. ``size`` is capped at 500 via the
    # Query validator so ``page * size`` stays bounded.
    try:
        window = await repo.list_for_tenant(
            tenant_id=tenant.tenant_id,
            status=status_filter,
            customer_id=customer_id,
            customer_type=customer_type,
            fuel_type=fuel_type,
            zip_code=zip_code,
            size=page * size + 1,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    total = len(window)
    start = (page - 1) * size
    end = start + size
    page_items = window[start:end]

    # ``has_next`` reports whether the unbounded result set would contain
    # another page — we cannot know the global total without a count query
    # so we conservatively say "yes" when the window extends past ``end``.
    has_next = len(window) > end

    logger.debug(
        "fuel_ops.customer_tanks.list: tenant=%s page=%d size=%d total_window=%d returned=%d",
        tenant.tenant_id,
        page,
        size,
        total,
        len(page_items),
    )
    return CustomerTankListResponse(
        items=page_items,
        total=total,
        page=page,
        page_size=size,
        has_next=has_next,
    )


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/customer-tanks/{customer_tank_id} (Req 1.6.2)
# ---------------------------------------------------------------------------


@mvp_router.get(
    "/customer-tanks/{customer_tank_id}",
    response_model=None,
)
async def get_customer_tank(
    customer_tank_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    expand: Optional[str] = Query(
        default=None,
        description=(
            "Comma-separated cross-module references to resolve into a `links` "
            "object: any of customer,last_refill_order. Omit for the unchanged, "
            "additive-only customer-tank contract."
        ),
    ),
) -> CustomerTank | CustomerTankDetailResponse:
    """Fetch a single Customer_Tank by id.

    Returns HTTP 404 for both "not found" and "owned by another tenant"
    so existence is never leaked across tenants (the repository already
    degrades cross-tenant reads to ``None``).

    When ``expand`` is supplied (cross-module-entity-linkage Req 7.2, 7.3,
    5.4), the response additionally carries a ``links`` object resolving the
    requested references (customer / last_refill_order) via the shared
    ``RefResolver``. Each link is either a resolved summary or an explicit
    ``unresolved``/``empty`` marker, never silently dropped. Reads without
    ``expand`` return the pre-existing :class:`CustomerTank` contract unchanged
    (Req 6.3). All resolution is tenant-scoped; references never cross tenants
    (Req 5.3).

    Validates: Requirements 1.6.2, 7.2, 7.3, 5.3, 5.4.
    """

    repo = _get_customer_tank_repository()
    try:
        tank = await repo.get(tenant.tenant_id, customer_tank_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if tank is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "customer_tank_not_found",
                "customer_tank_id": customer_tank_id,
            },
        )

    requested = _parse_customer_tank_expand(expand)
    if not requested:
        # Backward-compatible path: unchanged customer-tank contract (Req 6.3).
        return tank

    links = await _build_customer_tank_links(tenant.tenant_id, tank, requested)
    base = tank.model_dump()
    return CustomerTankDetailResponse(**base, links=links)


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/customer-tanks (Req 1.6.3)
# ---------------------------------------------------------------------------


@mvp_router.post(
    "/customer-tanks",
    response_model=CustomerTank,
    status_code=status.HTTP_201_CREATED,
)
async def create_customer_tank(
    body: CustomerTankCreateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> CustomerTank:
    """Create a new Customer_Tank scoped to the requesting tenant.

    The router stamps ``tenant_id`` from the verified JWT context so the
    caller cannot spoof ownership. Fuel-product canonicalization happens
    inside :class:`CustomerTank` via its field validator, which surfaces
    :class:`UnknownFuelProductError` as a 422.

    Validates: Requirement 1.6.3.
    """

    repo = _get_customer_tank_repository()

    payload: Dict[str, Any] = body.model_dump(exclude_none=True)
    payload["tenant_id"] = tenant.tenant_id

    # Write-time reference validation: the tank's customer_id must resolve to an
    # existing commerce customer in this tenant (Req 7.1). Rejected with a
    # structured 400 (``customer_not_found``) before the tank is persisted.
    await _validate_customer_ref(tenant.tenant_id, payload.get("customer_id"))

    try:
        tank = await repo.create(tenant.tenant_id, payload)
    except CrossTenantAccessError as exc:
        # Shouldn't happen since we stamped tenant_id ourselves, but the
        # repository is defensive and so are we.
        raise _translate_cross_tenant_error(exc)
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "unknown_product_code",
                "message": "Unknown fuel product code.",
                "fuel_product_code": exc.code_or_alias,
            },
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    logger.info(
        "fuel_ops.customer_tanks.create: tenant=%s tank=%s",
        tenant.tenant_id,
        tank.customer_tank_id,
    )
    return tank


# ---------------------------------------------------------------------------
# PATCH /api/fuel/mvp/customer-tanks/{customer_tank_id} (Req 1.6.3)
# ---------------------------------------------------------------------------


@mvp_router.patch(
    "/customer-tanks/{customer_tank_id}",
    response_model=CustomerTank,
)
async def update_customer_tank(
    customer_tank_id: str,
    body: CustomerTankUpdateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> CustomerTank:
    """Apply a partial update to an owned Customer_Tank.

    Returns 404 when the tank does not exist (or belongs to another
    tenant — the repository raises :class:`CrossTenantAccessError` in
    that case, which we map to 403 separately so owners can distinguish
    "missing" from "forbidden"). Returns 422 when the merged record
    would fail Pydantic validation (e.g. level > capacity).

    Validates: Requirement 1.6.3.
    """

    repo = _get_customer_tank_repository()

    patch = body.model_dump(exclude_none=True)
    if not patch:
        # An empty patch is a no-op — we still need to load-or-404 so
        # clients get a consistent response even when they send {} by
        # accident.
        existing = await repo.get(tenant.tenant_id, customer_tank_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "customer_tank_not_found",
                    "customer_tank_id": customer_tank_id,
                },
            )
        return existing

    # If the patch reassigns the tank's customer, validate the new reference
    # resolves to an existing same-tenant commerce customer (Req 7.1).
    if "customer_id" in patch:
        await _validate_customer_ref(tenant.tenant_id, patch.get("customer_id"))

    try:
        updated = await repo.update(
            tenant_id=tenant.tenant_id,
            customer_tank_id=customer_tank_id,
            patch=patch,
        )
    except CrossTenantAccessError as exc:
        raise _translate_cross_tenant_error(exc)
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "unknown_product_code",
                "message": "Unknown fuel product code.",
                "fuel_product_code": exc.code_or_alias,
            },
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "customer_tank_not_found",
                "customer_tank_id": customer_tank_id,
            },
        )
    logger.info(
        "fuel_ops.customer_tanks.update: tenant=%s tank=%s fields=%s",
        tenant.tenant_id,
        customer_tank_id,
        sorted(patch.keys()),
    )
    return updated


# ---------------------------------------------------------------------------
# Depot request / response models (Req 2.2.2)
# ---------------------------------------------------------------------------


class DepotCreateRequest(BaseModel):
    """Body for ``POST /api/fuel/mvp/depots`` (Req 2.2.2).

    Mirrors :class:`fuel.depot_models.Depot` but omits repository-managed
    fields (``tenant_id``, ``created_at``, ``updated_at``) and makes
    ``depot_id`` optional so the repository can mint one (``depot_<uuid4>``)
    when it isn't supplied by the caller. Latitude / longitude bounds are
    enforced at the Pydantic layer so invalid coordinates surface as a
    clean 422 from FastAPI's request validation before they can reach the
    repository or Elasticsearch.
    """

    model_config = ConfigDict(extra="forbid")

    depot_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional client-supplied identifier. When omitted the "
            "repository mints a uuid4-based id (``depot_<uuid4>``)."
        ),
    )
    name: str = Field(..., min_length=1)
    location_lat: float = Field(..., ge=-90.0, le=90.0)
    location_lon: float = Field(..., ge=-180.0, le=180.0)
    address: str = Field(..., min_length=1)
    timezone: str = Field(..., min_length=1)
    fuel_types_supported: List[str] = Field(default_factory=list)
    status: DepotStatus = "active"
    is_default: bool = Field(
        default=False,
        description="Mark this depot as the tenant's default depot.",
    )


class DepotUpdateRequest(BaseModel):
    """Body for ``PATCH /api/fuel/mvp/depots/{depot_id}`` (Req 2.2.2).

    Every field is optional so callers can send just the delta. The
    repository refuses to overwrite immutable fields (``depot_id``,
    ``tenant_id``, ``created_at``); those are not even exposed here so
    malicious or accidental payloads are rejected by the ``extra="forbid"``
    pydantic policy before reaching the repository.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1)
    location_lat: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    location_lon: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    address: Optional[str] = Field(default=None, min_length=1)
    timezone: Optional[str] = Field(default=None, min_length=1)
    fuel_types_supported: Optional[List[str]] = None
    status: Optional[DepotStatus] = None
    is_default: Optional[bool] = Field(
        default=None,
        description="Set True to make this depot the tenant's default depot.",
    )


class DepotListResponse(BaseModel):
    """Envelope for ``GET /api/fuel/mvp/depots``."""

    model_config = ConfigDict(extra="forbid")

    items: List[Depot]
    total: int
    page: int
    page_size: int
    has_next: bool


class DepotAssetSummary(BaseModel):
    """A single asset assigned to a depot (Req 10.2).

    Returned by ``GET /api/fuel/mvp/depots/{depot_id}?expand=assets``. ``name``
    falls back across the ``asset_name`` / ``name`` document fields so both the
    fleet-asset and legacy-truck document vintages render a label.
    """

    model_config = ConfigDict(extra="ignore")

    asset_id: str
    name: Optional[str] = None
    asset_type: Optional[str] = None
    status: Optional[str] = None


class DepotReadResponse(BaseModel):
    """Envelope for ``GET /api/fuel/mvp/depots/{depot_id}`` (Req 10.2, 10.3).

    ``depot`` round-trips the full :class:`Depot` record — including the
    ``is_default`` flag — so the UI never has to infer the tenant-default depot
    from a loosely-typed shape. ``assigned_assets`` is populated only when the
    caller passes ``?expand=assets`` and lists the assets whose
    ``assigned_depot_id`` points at this depot.
    """

    model_config = ConfigDict(extra="forbid")

    depot: Depot
    assigned_assets: Optional[List[DepotAssetSummary]] = Field(
        default=None,
        description=(
            "Present only when ?expand=assets is requested; the tenant's "
            "assets whose assigned_depot_id references this depot."
        ),
    )


def _translate_depot_cross_tenant_error(
    exc: DepotCrossTenantAccessError,
) -> HTTPException:
    """Map :class:`fuel.depot_models.CrossTenantAccessError` to HTTP 403.

    We deliberately do not echo the owning tenant back to the caller — the
    ``depot_id`` is the only identifier needed to reconcile the response
    against the request, and surfacing the owning tenant would leak cross-
    tenant metadata.
    """

    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error_code": "cross_tenant_access_denied",
            "message": "Depot belongs to a different tenant.",
            "depot_id": exc.depot_id,
        },
    )


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/depots (Req 2.2.2)
# ---------------------------------------------------------------------------


@mvp_router.get("/depots", response_model=DepotListResponse)
async def list_depots(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    status_filter: Optional[DepotStatus] = Query(
        default=None,
        alias="status",
        description="Restrict to a single status (active | inactive).",
    ),
    fuel_type: Optional[str] = Query(
        default=None,
        description=(
            "Filter by supported fuel product. Accepts the canonical "
            "product_code (e.g. DIESEL_2) or a legacy alias (e.g. AGO); "
            "aliases are resolved via the fuel product catalog before the "
            "ES query is issued."
        ),
    ),
    page: int = Query(1, ge=1, description="Page number, 1-indexed."),
    size: int = Query(20, ge=1, le=500, description="Page size (1–500)."),
) -> DepotListResponse:
    """Return the paginated list of Depots for the tenant.

    Enforces tenant isolation through
    :class:`DepotRepository.list_for_tenant`, which filters the ES query
    on ``tenant_id`` *and* re-validates every returned document against
    the caller's tenant_id before it crosses the repository boundary.

    Validates: Requirement 2.2.2.
    """

    repo = _get_depot_repository()

    # Fetch a slightly larger window so we can paginate deterministically
    # without an extra round-trip — ``size`` is capped at 500 so
    # ``page * size`` stays bounded.
    try:
        window = await repo.list_for_tenant(
            tenant_id=tenant.tenant_id,
            status=status_filter,
            fuel_type=fuel_type,
            size=page * size + 1,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    total = len(window)
    start = (page - 1) * size
    end = start + size
    page_items = window[start:end]

    has_next = len(window) > end

    logger.debug(
        "fuel_ops.depots.list: tenant=%s page=%d size=%d total_window=%d returned=%d",
        tenant.tenant_id,
        page,
        size,
        total,
        len(page_items),
    )
    return DepotListResponse(
        items=page_items,
        total=total,
        page=page,
        page_size=size,
        has_next=has_next,
    )


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/depots/{depot_id} (Req 10.2, 10.3)
# ---------------------------------------------------------------------------


async def _enumerate_depot_assets(
    tenant_id: str,
    depot_id: str,
    *,
    size: int = 500,
) -> List[DepotAssetSummary]:
    """Return the tenant's assets whose ``assigned_depot_id`` is ``depot_id``.

    Queries the ``assets`` alias (→ ``trucks`` index) tenant-scoped two ways for
    defense-in-depth: the ES query filters on ``tenant_id`` *and* every returned
    source is re-validated against the caller's ``tenant_id`` before it is
    summarised, so a mis-labelled document can never leak across tenants
    (Req 5.3 / Property 2). A backing-store failure degrades to an empty list
    rather than failing the depot read.

    Validates: Requirement 10.2.
    """

    es = _get_es()
    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {"assigned_depot_id": depot_id}},
                ]
            }
        },
        "size": size,
    }
    try:
        resp = await es.search_documents("assets", query, size)
    except Exception as exc:  # noqa: BLE001 — never 500 the depot read
        logger.warning(
            "fuel_ops.depots.get: asset enumeration failed for depot=%s "
            "tenant=%s: %s",
            depot_id,
            tenant_id,
            exc,
        )
        return []

    hits = (resp.get("hits") or {}).get("hits") or [] if resp else []
    out: List[DepotAssetSummary] = []
    for hit in hits:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            continue
        if source.get("tenant_id") != tenant_id:
            continue
        asset_id = source.get("asset_id") or source.get("truck_id")
        if not asset_id:
            continue
        out.append(
            DepotAssetSummary(
                asset_id=str(asset_id),
                name=source.get("asset_name") or source.get("name"),
                asset_type=source.get("asset_type"),
                status=source.get("status"),
            )
        )
    return out


@mvp_router.get("/depots/{depot_id}", response_model=DepotReadResponse)
async def get_depot(
    depot_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    expand: Optional[str] = Query(
        default=None,
        description=(
            "Comma-separated expansions. Pass 'assets' to enumerate the "
            "assets assigned to this depot (Req 10.2)."
        ),
    ),
) -> DepotReadResponse:
    """Fetch a single Depot owned by the tenant, round-tripping ``is_default``.

    The depot record is returned in full — including the ``is_default`` flag —
    so the UI renders the tenant-default affordance from the canonical field
    rather than inferring it (Req 10.3). Passing ``?expand=assets`` additionally
    lists the assets whose ``assigned_depot_id`` references this depot
    (Req 10.2).

    Returns 404 (``depot_not_found``) when the depot does not exist or belongs
    to another tenant — a cross-tenant fetch is suppressed to a 404 so depot
    existence does not leak across tenants. Both error paths go through the
    structured ``ErrorResponse`` envelope.

    Validates: Requirements 10.2, 10.3.
    """

    repo = _get_depot_repository()

    try:
        depot = await repo.get(tenant.tenant_id, depot_id)
    except ValueError as exc:
        raise validation_error(str(exc))

    if depot is None:
        raise depot_not_found(
            f"Depot {depot_id} not found.",
            details={"depot_id": depot_id},
        )

    expansions = {
        part.strip() for part in (expand or "").split(",") if part.strip()
    }
    assigned_assets: Optional[List[DepotAssetSummary]] = None
    if "assets" in expansions:
        assigned_assets = await _enumerate_depot_assets(
            tenant.tenant_id, depot_id
        )

    logger.debug(
        "fuel_ops.depots.get: tenant=%s depot=%s expand=%s assets=%s",
        tenant.tenant_id,
        depot_id,
        sorted(expansions),
        None if assigned_assets is None else len(assigned_assets),
    )
    return DepotReadResponse(depot=depot, assigned_assets=assigned_assets)


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/depots (Req 2.2.2)
# ---------------------------------------------------------------------------


@mvp_router.post(
    "/depots",
    response_model=Depot,
    status_code=status.HTTP_201_CREATED,
)
async def create_depot(
    body: DepotCreateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Depot:
    """Create a new Depot scoped to the requesting tenant.

    The router stamps ``tenant_id`` from the verified JWT context so the
    caller cannot spoof ownership. ``fuel_types_supported`` entries are
    canonicalized inside :class:`Depot` via its field validator, which
    surfaces :class:`UnknownFuelProductError` — we map that to a 400 with
    a structured ``unknown_product_code`` payload so clients can
    distinguish "bad product" from generic validation errors.

    Coordinate bounds are enforced twice: once by the
    :class:`DepotCreateRequest` (surfaces as 422 from FastAPI), and again
    by the :class:`Depot` Pydantic model inside the repository, which
    would re-raise as a 422 should a future code path bypass the request
    schema.

    Validates: Requirement 2.2.2.
    """

    repo = _get_depot_repository()

    payload: Dict[str, Any] = body.model_dump(exclude_none=True)
    payload["tenant_id"] = tenant.tenant_id

    try:
        depot = await repo.create(tenant.tenant_id, payload)
    except DepotCrossTenantAccessError as exc:
        # Shouldn't happen since we stamped tenant_id ourselves, but the
        # repository is defensive and so are we.
        raise _translate_depot_cross_tenant_error(exc)
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "unknown_product_code",
                "message": "Unknown fuel product code.",
                "fuel_product_code": exc.code_or_alias,
            },
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    logger.info(
        "fuel_ops.depots.create: tenant=%s depot=%s",
        tenant.tenant_id,
        depot.depot_id,
    )
    return depot


# ---------------------------------------------------------------------------
# PATCH /api/fuel/mvp/depots/{depot_id} (Req 2.2.2)
# ---------------------------------------------------------------------------


@mvp_router.patch(
    "/depots/{depot_id}",
    response_model=Depot,
)
async def update_depot(
    depot_id: str,
    body: DepotUpdateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Depot:
    """Apply a partial update to an owned Depot.

    Returns 404 when the depot does not exist. Returns 403 when it
    belongs to another tenant (:class:`CrossTenantAccessError` from the
    repository). Returns 422 when the merged record would fail Pydantic
    validation (e.g. invalid IANA timezone or coordinates outside
    range). Returns 400 when ``fuel_types_supported`` contains an
    unknown product code.

    Validates: Requirement 2.2.2.
    """

    repo = _get_depot_repository()

    patch = body.model_dump(exclude_none=True)
    if not patch:
        # An empty patch is a no-op — load-or-404 so clients get a
        # consistent response when they accidentally send {}.
        existing = await repo.get(tenant.tenant_id, depot_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "depot_not_found",
                    "depot_id": depot_id,
                },
            )
        return existing

    try:
        updated = await repo.update(
            tenant_id=tenant.tenant_id,
            depot_id=depot_id,
            patch=patch,
        )
    except DepotCrossTenantAccessError as exc:
        raise _translate_depot_cross_tenant_error(exc)
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "unknown_product_code",
                "message": "Unknown fuel product code.",
                "fuel_product_code": exc.code_or_alias,
            },
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "depot_not_found",
                "depot_id": depot_id,
            },
        )
    logger.info(
        "fuel_ops.depots.update: tenant=%s depot=%s fields=%s",
        tenant.tenant_id,
        depot_id,
        sorted(patch.keys()),
    )
    return updated


# ---------------------------------------------------------------------------
# DELETE /api/fuel/mvp/depots/{depot_id} (Req 2.2.2)
# ---------------------------------------------------------------------------


@mvp_router.delete(
    "/depots/{depot_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_depot(
    depot_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> None:
    """Delete a Depot owned by the tenant.

    * Owned + deleted → HTTP 204 (no body).
    * Not-found → HTTP 404 with structured ``depot_not_found`` detail.
    * Cross-tenant → HTTP 403 with structured
      ``cross_tenant_access_denied`` detail.

    Validates: Requirement 2.2.2.
    """

    repo = _get_depot_repository()

    try:
        deleted = await repo.delete(tenant.tenant_id, depot_id)
    except DepotCrossTenantAccessError as exc:
        raise _translate_depot_cross_tenant_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "depot_not_found",
                "depot_id": depot_id,
            },
        )
    logger.info(
        "fuel_ops.depots.delete: tenant=%s depot=%s",
        tenant.tenant_id,
        depot_id,
    )
    return None


# ---------------------------------------------------------------------------
# Truck compartments list response models (Req 7.1.1, 7.1.2, 7.1.3)
# ---------------------------------------------------------------------------


class TruckCompartmentStateItem(BaseModel):
    """One row returned by ``GET /api/fuel/mvp/trucks/{truck_id}/compartments``.

    Bundles the static compartment configuration (``capacity_liters``,
    ``allowed_grades``, ``position_index``) with the lifecycle state
    triple (``state``, ``last_loaded_product``, ``last_loaded_at``,
    ``last_cleaned_at``) so the truck-detail UI can render a single
    row per compartment with both the capability (what it can load)
    and the posture (what it's currently holding, and whether it
    needs cleaning before the next load).

    Legacy pre-Task-6.1 documents that lack the lifecycle fields fall
    back to ``state="clean"`` with null timestamps — same
    degrade-gracefully contract the Compartment_Loading_Agent uses
    when reading older documents.
    """

    model_config = ConfigDict(extra="forbid")

    compartment_id: str = Field(
        ..., description="Tenant-scoped, truck-qualified compartment id."
    )
    truck_id: str = Field(..., description="Parent truck identifier.")
    capacity_liters: float = Field(
        ..., ge=0, description="Compartment capacity in liters."
    )
    allowed_grades: List[str] = Field(
        default_factory=list,
        description=(
            "Canonical fuel product_codes the compartment may load "
            "(post-canonicalization; legacy NG aliases are normalized "
            "on write per Req 6.1.4)."
        ),
    )
    position_index: int = Field(
        default=0,
        description="Ordinal position along the tanker axle for loading order.",
    )
    state: str = Field(
        ...,
        description=(
            "Lifecycle flag: clean | loaded | needs_cleaning "
            "(Req 7.1.1)."
        ),
    )
    last_loaded_product: Optional[str] = Field(
        default=None,
        description=(
            "Canonical product_code of the most recent load, or null "
            "when the compartment is empty / freshly cleaned."
        ),
    )
    last_loaded_at: Optional[str] = Field(
        default=None,
        description="ISO-8601 UTC timestamp of the most recent Loading_Plan commit.",
    )
    last_cleaned_at: Optional[str] = Field(
        default=None,
        description="ISO-8601 UTC timestamp of the most recent Cleaning_Event write.",
    )


class TruckCompartmentListResponse(BaseModel):
    """Envelope for ``GET /api/fuel/mvp/trucks/{truck_id}/compartments``.

    ``items`` is sorted by ``position_index`` so the UI renders
    compartments in physical loading order without a client-side sort.
    ``total`` matches ``len(items)`` and is surfaced for parity with
    other list endpoints (there is no pagination — a single tanker
    has at most a dozen compartments).
    """

    model_config = ConfigDict(extra="forbid")

    truck_id: str
    items: List[TruckCompartmentStateItem]
    total: int


class CompartmentTruckSummary(BaseModel):
    """One truck that has at least one compartment configured.

    Surfaced by ``GET /api/fuel/mvp/compartment-trucks`` so the
    Truck Compartments UI can present a pick-list instead of forcing the
    dispatcher to memorise tanker IDs. ``compartment_count`` lets the UI
    show how many compartments each tanker carries without a second
    round-trip per truck.
    """

    model_config = ConfigDict(extra="forbid")

    truck_id: str
    compartment_count: int


class CompartmentTrucksResponse(BaseModel):
    """Envelope for ``GET /api/fuel/mvp/compartment-trucks``.

    ``items`` is sorted by ``truck_id`` so the dropdown renders in a
    stable order. ``total`` matches ``len(items)``.
    """

    model_config = ConfigDict(extra="forbid")

    items: List[CompartmentTruckSummary]
    total: int


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/trucks/{truck_id}/compartments (Req 7.1.1, 7.1.2, 7.1.3)
# ---------------------------------------------------------------------------


@mvp_router.get(
    "/compartment-trucks",
    response_model=CompartmentTrucksResponse,
)
async def list_compartment_trucks(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> CompartmentTrucksResponse:
    """List trucks that have at least one compartment configured.

    Powers the Truck Compartments tab's truck picker so a dispatcher can
    select a tanker from a dropdown instead of having to know its id
    up-front. Uses a tenant-scoped ``terms`` aggregation on ``truck_id``
    over the ``truck_compartments`` index; the per-bucket ``doc_count``
    becomes ``compartment_count``. An empty result returns
    ``items: []`` (never 404) so the UI renders a clean empty state.

    Error modes:

        * 500 ``compartment_trucks_lookup_failed`` — ES query raised.
    """

    es = _get_es()

    query = {
        "query": {"bool": {"must": [{"term": {"tenant_id": tenant.tenant_id}}]}},
        "size": 0,
        "aggs": {
            "trucks": {
                "terms": {"field": "truck_id", "size": 1000},
            },
        },
    }

    try:
        resp = await es.search_documents(TRUCK_COMPARTMENTS_INDEX, query, 0)
    except Exception as exc:
        logger.exception(
            "fuel_ops.compartment_trucks.list: lookup failed for tenant=%s",
            tenant.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "compartment_trucks_lookup_failed",
                "message": str(exc),
            },
        )

    aggs = resp.get("aggregations", {}) if hasattr(resp, "get") else {}
    buckets = (aggs.get("trucks", {}) or {}).get("buckets", []) or []

    items: List[CompartmentTruckSummary] = []
    for bucket in buckets:
        truck_id = bucket.get("key")
        if not truck_id:
            continue
        items.append(
            CompartmentTruckSummary(
                truck_id=str(truck_id),
                compartment_count=int(bucket.get("doc_count") or 0),
            )
        )

    items.sort(key=lambda t: t.truck_id)

    logger.debug(
        "fuel_ops.compartment_trucks.list: tenant=%s count=%d",
        tenant.tenant_id,
        len(items),
    )
    return CompartmentTrucksResponse(items=items, total=len(items))


@mvp_router.get(
    "/trucks/{truck_id}/compartments",
    response_model=TruckCompartmentListResponse,
)
async def list_truck_compartments(
    truck_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> TruckCompartmentListResponse:
    """Return the compartment roster + lifecycle state for a truck.

    Serves the truck-detail page's compartment badges and cleaning-event
    form (Task 11.9 / Req 7.1.4). Reads from ``truck_compartments`` with
    a tenant-scoped ``bool.filter`` and a defensive per-row tenant
    re-check so cross-tenant rows never leak. Empty-result (unknown
    truck or no compartments configured) returns ``items: []`` rather
    than 404 so the UI can render the "configure compartments" empty
    state without special-casing the error.

    Error modes:

        * 400 ``invalid_truck_id`` — path parameter is blank.
        * 500 ``truck_compartments_lookup_failed`` — ES query raised.
    """

    if not truck_id or not truck_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_truck_id",
                "message": "truck_id must be a non-empty string.",
            },
        )

    es = _get_es()
    truck_id_clean = truck_id.strip()

    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant.tenant_id}},
                    {"term": {"truck_id": truck_id_clean}},
                ],
            },
        },
        "size": 200,
    }

    try:
        resp = await es.search_documents(
            TRUCK_COMPARTMENTS_INDEX, query, 200
        )
    except Exception as exc:
        logger.exception(
            "fuel_ops.truck_compartments.list: lookup failed "
            "for truck=%s tenant=%s",
            truck_id_clean,
            tenant.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "truck_compartments_lookup_failed",
                "message": str(exc),
            },
        )

    items: List[TruckCompartmentStateItem] = []
    for hit in resp.get("hits", {}).get("hits", []):
        source = hit.get("_source") or {}
        # Defensive per-row tenant re-check. The ES filter already
        # pins tenant_id but a mis-indexed doc must not slip through.
        if source.get("tenant_id") != tenant.tenant_id:
            continue
        compartment_id = source.get("compartment_id")
        if not compartment_id:
            continue

        def _as_iso(value: Any) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, str):
                return value
            if hasattr(value, "isoformat"):
                return value.isoformat()
            return str(value)

        try:
            item = TruckCompartmentStateItem(
                compartment_id=compartment_id,
                truck_id=source.get("truck_id") or truck_id_clean,
                capacity_liters=float(source.get("capacity_liters") or 0.0),
                allowed_grades=list(source.get("allowed_grades") or []),
                position_index=int(source.get("position_index") or 0),
                state=source.get("state") or "clean",
                last_loaded_product=source.get("last_loaded_product"),
                last_loaded_at=_as_iso(source.get("last_loaded_at")),
                last_cleaned_at=_as_iso(source.get("last_cleaned_at")),
            )
        except (ValidationError, ValueError, TypeError) as exc:
            logger.warning(
                "fuel_ops.truck_compartments.list: dropping malformed row "
                "compartment=%s truck=%s tenant=%s err=%s",
                compartment_id,
                truck_id_clean,
                tenant.tenant_id,
                exc,
            )
            continue
        items.append(item)

    items.sort(key=lambda c: (c.position_index, c.compartment_id))

    logger.debug(
        "fuel_ops.truck_compartments.list: tenant=%s truck=%s count=%d",
        tenant.tenant_id,
        truck_id_clean,
        len(items),
    )
    return TruckCompartmentListResponse(
        truck_id=truck_id_clean,
        items=items,
        total=len(items),
    )


# ---------------------------------------------------------------------------
# Cleaning Event request / response models (Req 7.1.4)
# ---------------------------------------------------------------------------


class CleaningEventCreateRequest(BaseModel):
    """Body for ``POST /api/fuel/mvp/compartments/{id}/cleaning-events``.

    Mirrors the fields mandated by Requirement 7.1.4: ``method`` (one of
    flush | purge | sanitize), ``actor_id``, ``notes`` (optional
    free-text), and ``evidence_refs`` (optional :class:`FileStorageService`
    refs). The router stamps ``tenant_id``, derives ``truck_id`` from the
    stored compartment state, and mints the event id so the service layer
    never has to hand those back to the caller.

    Immutable / server-computed fields (``cleaning_event_id``,
    ``tenant_id``, ``truck_id``, ``cleaned_at``, ``created_at``,
    ``updated_at``) are not exposed here; the ``extra="forbid"`` policy
    rejects any such payload keys at validation time.
    """

    model_config = ConfigDict(extra="forbid")

    method: CleaningMethod = Field(
        ...,
        description="Cleaning regime: flush | purge | sanitize (Req 7.1.4).",
    )
    actor_id: str = Field(
        ...,
        min_length=1,
        description=(
            "User / service principal that recorded the cleaning. "
            "DEPRECATED as a canonical reference — prefer ``driver_id`` "
            "below. Retained as a free-text alias for backward "
            "compatibility (cross-module-entity-linkage Req 8.2)."
        ),
    )
    driver_id: Optional[str] = Field(
        default=None,
        description=(
            "Canonical, resolvable driver reference for the actor that "
            "performed the cleaning (cross-module-entity-linkage Req 8.2). "
            "Supersedes the free-text ``actor_id`` alias. Optional/nullable; "
            "when supplied it is validated against the Drivers module in the "
            "requesting tenant and a non-existent driver is rejected with "
            "HTTP 400 ``driver_not_found``."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description="Free-text operator notes. Blank / whitespace is normalized to null.",
    )
    evidence_refs: List[str] = Field(
        default_factory=list,
        description=(
            "Optional File_Storage_Service file_refs (photos, "
            "certification PDFs). Each ref is validated against the "
            "requesting tenant before the event is persisted."
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/compartments/{compartment_id}/cleaning-events (Req 7.1.4)
# ---------------------------------------------------------------------------


@mvp_router.post(
    "/compartments/{compartment_id}/cleaning-events",
    response_model=CleaningEvent,
    status_code=status.HTTP_201_CREATED,
)
async def record_cleaning_event(
    compartment_id: str,
    body: CleaningEventCreateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> CleaningEvent:
    """Persist a Cleaning_Event for the compartment and reset its state.

    Validates: Requirement 7.1.4.

    Flow:

        1. Look up the compartment's current state for the requesting
           tenant. ``None`` is returned for both "missing" and
           "cross-tenant" cases — the compartment state repository
           deliberately degrades cross-tenant reads to ``None`` so the
           API returns a uniform HTTP 404 and never leaks existence
           across tenants.
        2. Validate every ``evidence_refs`` entry belongs to the tenant
           via :meth:`FileStorageService.validate_ref`; cross-tenant
           refs surface as HTTP 403 with a structured error.
        3. Delegate to :meth:`CleaningEventService.record`, which
           persists the event to ``compartment_cleaning_events`` and
           resets the compartment state to ``clean`` via
           :meth:`CompartmentStateRepository.mark_cleaned`.

    Error modes:

        * 404 ``compartment_not_found`` — compartment missing or owned
          by a different tenant.
        * 403 ``cross_tenant_file_ref`` — one of ``evidence_refs`` did
          not pass the tenant-prefix check.
        * 403 ``cross_tenant_access_denied`` — the compartment exists
          but belongs to another tenant (only surfaces when the state
          reset step's own tenant guard trips, which shouldn't happen
          after the pre-flight check but is handled defensively).
        * 422 ``validation_error`` — method outside
          ``{flush, purge, sanitize}`` or other body validation failure.
        * 409 ``compartment_state_conflict`` — optimistic concurrency
          control repeatedly lost the race on the state reset.
        * 500 ``cleaning_event_persistence_error`` — the event was
          persisted but the subsequent state reset failed; callers
          should retry the state reset using the returned event id.
    """

    compartment_state_repo = _get_compartment_state_repository()
    cleaning_service = _get_cleaning_event_service()
    file_storage = _file_storage_service

    if not compartment_id or not compartment_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_compartment_id",
                "message": "compartment_id must be a non-empty string.",
            },
        )

    # Pre-flight: compartment must exist and belong to the tenant so we
    # can derive truck_id and short-circuit before any side-effects.
    try:
        state = await compartment_state_repo.get(
            tenant.tenant_id, compartment_id.strip()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "compartment_not_found",
                "compartment_id": compartment_id,
            },
        )

    # Validate evidence_refs against the tenant via FileStorageService
    # (Req 7.1.4). Performing this check before handing to the service
    # keeps the 403 response shape consistent with the driver POD
    # endpoint and avoids coupling the service to the HTTP surface.
    if body.evidence_refs and file_storage is not None:
        for idx, ref in enumerate(body.evidence_refs):
            try:
                file_storage.validate_ref(
                    tenant_id=tenant.tenant_id,
                    file_ref=ref,
                    actor=body.actor_id,
                )
            except PermissionError as exc:
                logger.warning(
                    "Cross-tenant cleaning-event file_ref denied: "
                    "tenant=%s compartment=%s idx=%d ref=%s err=%s",
                    tenant.tenant_id,
                    compartment_id,
                    idx,
                    ref,
                    exc,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error_code": "cross_tenant_file_ref",
                        "message": "Evidence ref belongs to a different tenant.",
                        "field": f"evidence_refs[{idx}]",
                    },
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error_code": "invalid_file_ref",
                        "message": str(exc),
                        "field": f"evidence_refs[{idx}]",
                    },
                )

    # Validate the optional canonical driver reference (Req 8.2). When a
    # ``driver_id`` is supplied we assert it resolves to an existing driver in
    # this tenant via the shared RefResolver; a non-existent / cross-tenant id
    # is rejected with HTTP 400 ``driver_not_found``. Validation is skipped when
    # no ``driver`` loader is registered so partially-wired environments remain
    # additive/backward-compatible (the field simply persists unvalidated).
    if body.driver_id and body.driver_id.strip():
        resolver = _get_ref_resolver()
        if resolver is not None and "driver" in resolver.registered_types():
            driver_ref = await resolver.resolve(
                tenant.tenant_id, "driver", body.driver_id.strip()
            )
            if not driver_ref.is_resolved:
                raise driver_not_found(
                    "Referenced driver does not exist in this tenant.",
                    details={
                        "field": "driver_id",
                        "driver_id": body.driver_id,
                    },
                )

    try:
        event = await cleaning_service.record(
            tenant_id=tenant.tenant_id,
            compartment_id=compartment_id.strip(),
            truck_id=state.truck_id,
            method=body.method,
            actor_id=body.actor_id,
            notes=body.notes,
            evidence_refs=body.evidence_refs,
            driver_id=body.driver_id,
        )
    except CompartmentNotFoundError:
        # Another caller deleted the compartment between our pre-flight
        # lookup and the service call. Translate to 404 so the client
        # observes the same mode as the pre-flight case.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "compartment_not_found",
                "compartment_id": compartment_id,
            },
        )
    except CrossTenantCompartmentAccessError as exc:
        logger.warning(
            "Cross-tenant cleaning-event write denied: tenant=%s compartment=%s",
            tenant.tenant_id,
            compartment_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "cross_tenant_access_denied",
                "message": "Compartment belongs to a different tenant.",
                "compartment_id": exc.compartment_doc_id,
            },
        )
    except PermissionError as exc:
        # The service layer re-validates evidence_refs when a file
        # storage dependency was injected; a cross-tenant ref raises
        # PermissionError which we map to 403 consistently with the
        # pre-flight branch above.
        logger.warning(
            "Cross-tenant cleaning-event file_ref denied at service "
            "layer: tenant=%s compartment=%s err=%s",
            tenant.tenant_id,
            compartment_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "cross_tenant_file_ref",
                "message": "Evidence ref belongs to a different tenant.",
            },
        )
    except CompartmentStateConflictError as exc:
        logger.warning(
            "Cleaning-event state-reset OCC conflict: tenant=%s compartment=%s attempts=%d",
            tenant.tenant_id,
            compartment_id,
            exc.attempts,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "compartment_state_conflict",
                "message": "Concurrent modification detected; retry the request.",
                "compartment_id": compartment_id,
            },
        )
    except CleaningEventPersistenceError as exc:
        logger.error(
            "Cleaning-event persisted but state reset failed: "
            "tenant=%s compartment=%s event=%s",
            tenant.tenant_id,
            compartment_id,
            exc.cleaning_event_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "cleaning_event_persistence_error",
                "message": (
                    "Cleaning event persisted but the compartment state "
                    "reset failed; retry the reset using the returned "
                    "cleaning_event_id."
                ),
                "cleaning_event_id": exc.cleaning_event_id,
                "compartment_id": compartment_id,
            },
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    logger.info(
        "fuel_ops.cleaning_events.create: tenant=%s compartment=%s event=%s method=%s",
        tenant.tenant_id,
        compartment_id,
        event.cleaning_event_id,
        event.method,
    )
    return event


# ---------------------------------------------------------------------------
# Load eligibility response models (Req 7.2.5)
# ---------------------------------------------------------------------------


class LoadEligibilityCompartmentState(BaseModel):
    """Narrow view of the compartment state fields surfaced to the caller.

    Only the state fields that drive the compatibility decision are
    echoed back so the client can display the "why" alongside the
    decision without a second round-trip to the compartment endpoint.
    Sensitive / operational fields (``truck_id`` aside) are excluded to
    keep the response tightly scoped to the eligibility question.
    """

    model_config = ConfigDict(extra="forbid")

    compartment_id: str = Field(
        ..., description="Compartment identifier (path parameter echo)."
    )
    truck_id: str = Field(..., description="Parent truck identifier.")
    state: str = Field(
        ...,
        description=(
            "Lifecycle flag: clean | loaded | needs_cleaning. Informs "
            "whether a ``requires_cleaning`` rule will downgrade to "
            "``allowed`` (Req 7.2.4)."
        ),
    )
    last_loaded_product: Optional[str] = Field(
        default=None,
        description=(
            "Canonical catalog product_code of the most recent load, "
            "or null when the compartment is empty."
        ),
    )
    last_loaded_at: Optional[str] = Field(
        default=None,
        description="ISO-8601 UTC timestamp of the most recent Loading_Plan commit.",
    )
    last_cleaned_at: Optional[str] = Field(
        default=None,
        description="ISO-8601 UTC timestamp of the most recent Cleaning_Event write.",
    )


class LoadEligibilityResponse(BaseModel):
    """Envelope for ``GET /api/fuel/mvp/compartments/{id}/load-eligibility``.

    The response bundles the compatibility decision, the governing rule
    that drove it, and the compartment state inputs so operators see both
    *what* the answer is and *why* — critical for explaining a
    ``blocked`` or ``requires_cleaning`` outcome on a dispatcher screen.

    Fields:

    * ``decision`` — one of ``allowed`` | ``blocked`` | ``requires_cleaning``
      as defined by the Compatibility_Matrix engine.
    * ``reason`` — stable machine-readable reason code. ``None`` on
      ``allowed``; ``cross_contamination_blocked`` on ``blocked``;
      ``cleaning_required`` on ``requires_cleaning``.
    * ``governing_rule`` — the rule value in the matrix that drove the
      decision: ``allowed`` | ``blocked`` | ``requires_cleaning``. When
      the decision is ``allowed`` but the governing rule is
      ``requires_cleaning``, the compartment was freshly cleaned and
      the rule downgraded (Req 7.2.4).
    * ``previous_product`` — canonical product_code of the last load, or
      null when the compartment is empty.
    * ``proposed_product`` — canonical form of the caller-supplied
      ``product_code`` query parameter.
    * ``compartment_state`` — narrow view of the state fields used in
      the evaluation so callers can audit the inputs.
    """

    model_config = ConfigDict(extra="forbid")

    compartment_id: str = Field(
        ..., description="Compartment identifier echoed from the path."
    )
    proposed_product: str = Field(
        ...,
        description=(
            "Canonical product_code of the proposed load. Legacy aliases "
            "supplied in the query (e.g. AGO, PMS) are canonicalized before "
            "the decision is evaluated."
        ),
    )
    previous_product: Optional[str] = Field(
        default=None,
        description=(
            "Canonical product_code of the most recent load, or null "
            "when the compartment is empty / freshly cleaned."
        ),
    )
    decision: str = Field(
        ...,
        description=(
            "Compatibility decision: allowed | blocked | requires_cleaning."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        description=(
            "Stable reason code. ``cross_contamination_blocked`` on "
            "blocked, ``cleaning_required`` on requires_cleaning, null "
            "on allowed."
        ),
    )
    governing_rule: str = Field(
        ...,
        description=(
            "Rule value in the Compatibility_Matrix that drove the "
            "decision: allowed | blocked | requires_cleaning."
        ),
    )
    compartment_state: LoadEligibilityCompartmentState


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/compartments/{compartment_id}/load-eligibility (Req 7.2.5)
# ---------------------------------------------------------------------------


@mvp_router.get(
    "/compartments/{compartment_id}/load-eligibility",
    response_model=LoadEligibilityResponse,
)
async def check_compartment_load_eligibility(
    compartment_id: str,
    request: Request,
    product_code: str = Query(
        ...,
        min_length=1,
        description=(
            "Canonical product_code of the proposed load, or a legacy "
            "alias (e.g. AGO → DIESEL_2, PMS → GASOLINE_REG). The value "
            "is canonicalized before the compatibility rule is evaluated."
        ),
    ),
    tenant: TenantContext = Depends(get_tenant_context),
) -> LoadEligibilityResponse:
    """Return the compatibility decision and governing rule for a proposed load.

    Surfaces the same decision the Compartment_Loading_Agent applies
    before each compartment assignment (Task 6.5) so dispatchers can
    preview eligibility from the UI without triggering a plan. The
    decision is computed by merging tenant overrides from Redis
    (``compatibility_matrix_config:{tenant_id}``) on top of the seed
    rule table and calling :func:`check_compatibility` with the
    compartment's persisted state.

    Validates: Requirement 7.2.5.

    Error modes:

        * 400 ``invalid_compartment_id`` — path parameter is blank.
        * 400 ``invalid_product_code`` — query parameter is blank.
        * 404 ``compartment_not_found`` — compartment missing or owned
          by a different tenant (existence is never leaked).
        * 422 ``validation_error`` — ``product_code`` cannot be
          canonicalized against the fuel product catalog (either an
          unknown code or a non-string value).
    """

    if not compartment_id or not compartment_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_compartment_id",
                "message": "compartment_id must be a non-empty string.",
            },
        )

    if not product_code or not product_code.strip():
        # FastAPI's ``min_length=1`` catches the empty-string case at
        # validation time, but a whitespace-only value slips through so
        # we re-assert here for a consistent error shape.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_product_code",
                "message": "product_code must be a non-empty string.",
            },
        )

    compartment_state_repo = _get_compartment_state_repository()

    # Load the compartment's state for the requesting tenant. Cross-tenant
    # hits are downgraded to ``None`` by the repository so the endpoint
    # returns a uniform 404 — existence is never leaked.
    try:
        state = await compartment_state_repo.get(
            tenant.tenant_id, compartment_id.strip()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "compartment_not_found",
                "compartment_id": compartment_id,
            },
        )

    # Merge tenant overrides on top of the default matrix. The helper
    # degrades to the default table on Redis outage / missing key /
    # malformed payload so a config-store failure never blocks an
    # eligibility check (same graceful-degradation contract as the
    # Compartment_Loading_Agent, Task 6.5).
    compatibility_rules = await load_tenant_compatibility_rules(
        tenant.tenant_id, _tenant_config
    )

    try:
        decision: CompatibilityDecision = check_compatibility(
            previous_product=state.last_loaded_product,
            next_product=product_code.strip(),
            compartment_state=state,
            rules=compatibility_rules,
        )
    except UnknownFuelProductError as exc:
        # Unknown product code: surface a 422 so clients can show the
        # caller which product_code was rejected rather than a generic
        # 400.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "unknown_product_code",
                "message": str(exc),
                "product_code": product_code,
            },
        )
    except (TypeError, ValueError) as exc:
        raise _translate_validation_error(exc)

    # Canonicalize the proposed product for the response so the client
    # sees the same code the matrix was consulted with; this mirrors the
    # canonicalization the decision itself performed and keeps response
    # shapes stable regardless of whether the caller passed a legacy
    # alias or the canonical code.
    try:
        proposed_canonical = canonicalize(product_code)
    except UnknownFuelProductError as exc:  # pragma: no cover - defensive
        # Practically unreachable: check_compatibility already canonicalized
        # the same code without raising. Guarded so a future engine change
        # doesn't hide the catalog error.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "unknown_product_code",
                "message": str(exc),
                "product_code": product_code,
            },
        )

    state_view = LoadEligibilityCompartmentState(
        compartment_id=state.compartment_id,
        truck_id=state.truck_id,
        state=state.state,
        last_loaded_product=state.last_loaded_product,
        last_loaded_at=(
            state.last_loaded_at.isoformat() if state.last_loaded_at else None
        ),
        last_cleaned_at=(
            state.last_cleaned_at.isoformat() if state.last_cleaned_at else None
        ),
    )

    response = LoadEligibilityResponse(
        compartment_id=compartment_id,
        proposed_product=proposed_canonical,
        previous_product=state.last_loaded_product,
        decision=decision["decision"],
        reason=decision["reason"],
        governing_rule=decision["governing_rule"],
        compartment_state=state_view,
    )
    logger.debug(
        "fuel_ops.load_eligibility: tenant=%s compartment=%s "
        "previous=%s proposed=%s decision=%s governing_rule=%s",
        tenant.tenant_id,
        compartment_id,
        state.last_loaded_product,
        proposed_canonical,
        response.decision,
        response.governing_rule,
    )
    return response


# ---------------------------------------------------------------------------
# Priority-cluster response models (Req 3.4.3)
# ---------------------------------------------------------------------------


class PriorityClusterCentroid(BaseModel):
    """Geographic centroid of a priority cluster.

    Kept as a dedicated model rather than a nested ``Dict[str, float]`` so
    the OpenAPI schema carries explicit ``lat`` / ``lon`` fields with
    their WGS84 bounds documented.
    """

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)


class PriorityClusterItem(BaseModel):
    """One row returned by ``GET /api/fuel/mvp/priority-clusters``.

    Mirrors the fields mandated by Req 3.4.3: cluster_id, centroid,
    member_count, highest_priority_bucket, fuel_grades. Noise points
    (per Req 3.4.4) are deliberately excluded from the response — the
    endpoint exposes a consolidated dispatcher view, and noise rows
    are not actionable clusters.
    """

    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    centroid: PriorityClusterCentroid
    member_count: int = Field(..., ge=2)
    highest_priority_bucket: Optional[Literal["critical", "high", "medium", "low"]]
    fuel_grades: List[str] = Field(default_factory=list)


class PriorityClustersResponse(BaseModel):
    """Envelope for ``GET /api/fuel/mvp/priority-clusters``.

    ``run_id`` echoes the priority run the clusters came from so the
    caller can correlate with ``/api/fuel/mvp/priorities`` without a
    second request. ``eps_miles`` and ``min_samples`` surface the
    DBSCAN parameters actually used — useful for debugging and for the
    UI's "why are these clustered together?" explainer.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: Optional[str] = None
    eps_miles: float
    min_samples: int
    items: List[PriorityClusterItem]
    total: int


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/priority-clusters (Req 3.4.1, 3.4.2, 3.4.3, 3.4.4)
# ---------------------------------------------------------------------------

#: Default DBSCAN radius (miles). Matches the tenant-configurable
#: ``cluster_eps_miles`` default of 3.0 mandated by Req 3.4.1.
DEFAULT_CLUSTER_EPS_MILES: float = 3.0

#: Default DBSCAN min_samples. Matches the tenant-configurable
#: ``cluster_min_samples`` default of 2 mandated by Req 3.4.1.
DEFAULT_CLUSTER_MIN_SAMPLES: int = 2

#: Upper bound on how many priorities we pull for cluster computation. The
#: MVP priorities endpoint caps its page size at 100 for normal reads, but
#: cluster computation needs the full latest priority list, so we load up
#: to this many entries in one ES query. A ceiling keeps the DBSCAN run
#: bounded in the face of an anomalous tenant with tens of thousands of
#: open stops.
_PRIORITY_CLUSTER_FETCH_CEILING: int = 500


@mvp_router.get(
    "/priority-clusters",
    response_model=PriorityClustersResponse,
)
async def list_priority_clusters(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    eps_miles: float = Query(
        default=DEFAULT_CLUSTER_EPS_MILES,
        gt=0,
        le=50.0,
        description=(
            "DBSCAN proximity threshold in statute miles. Defaults to "
            "3.0 per Req 3.4.1. Upper-bounded at 50 miles to keep the "
            "ball-tree radius-search cheap."
        ),
    ),
    min_samples: int = Query(
        default=DEFAULT_CLUSTER_MIN_SAMPLES,
        ge=1,
        le=25,
        description=(
            "Minimum members a dense cluster requires. Defaults to 2 "
            "per Req 3.4.1. Points with fewer than this many neighbours "
            "within ``eps_miles`` are treated as noise (Req 3.4.4) and "
            "excluded from the response."
        ),
    ),
) -> PriorityClustersResponse:
    """Return the DBSCAN cluster view over the latest priority list.

    Flow:
        1. Load the tenant's most recent ``mvp_delivery_priorities``
           document. When none exists, return an empty list — a fresh
           tenant without any priorities yet should not 404 the UI.
        2. For each priority entry, look up the destination's lat/lon
           via :class:`DeliveryDestinationService`. Entries whose
           destination cannot be located (missing geo, cross-tenant,
           or deleted) are skipped with a warning rather than failing
           the whole request.
        3. Run :func:`compute_priority_clusters` with the caller-provided
           ``eps_miles`` / ``min_samples`` (defaults 3.0 mi / 2). The
           helper runs sklearn's DBSCAN with the haversine metric so
           distance comparisons are great-circle accurate.
        4. Project the helper's cluster aggregates into
           :class:`PriorityClusterItem` rows — one per dense cluster.
           Noise entries are omitted per Req 3.4.3's "one row per
           cluster" wording.

    Validates: Requirements 3.4.1, 3.4.2, 3.4.3, 3.4.4.
    """

    destination_service = _get_destination_service()
    es = _get_es()

    latest = await _load_latest_priority_list(es, tenant.tenant_id)
    if latest is None:
        return PriorityClustersResponse(
            run_id=None,
            eps_miles=eps_miles,
            min_samples=min_samples,
            items=[],
            total=0,
        )

    run_id = _str_or_none(latest.get("run_id"))
    entries = latest.get("priorities") or []

    # Map station_id / customer_tank_id → (lat, lon) by loading the
    # tenant's destination list once. The list is capped at ~1000 by
    # DeliveryDestinationService so a single query covers any realistic
    # priority run.
    destinations = await destination_service.list(tenant_id=tenant.tenant_id)
    location_by_id: Dict[str, PriorityClusterCentroid] = {}
    for destination in destinations:
        if destination.location is None:
            continue
        location_by_id[destination.destination_id] = PriorityClusterCentroid(
            lat=destination.location.lat,
            lon=destination.location.lon,
        )

    cluster_inputs: List[Dict[str, Any]] = []
    for entry in entries:
        # Priority entries use ``station_id`` historically; the Task 5.5
        # extension will add ``customer_tank_id``, so we resolve against
        # whichever ID is present.
        destination_id = (
            _str_or_none(entry.get("customer_tank_id"))
            or _str_or_none(entry.get("station_id"))
            or _str_or_none(entry.get("destination_id"))
        )
        if destination_id is None:
            continue
        location = location_by_id.get(destination_id)
        if location is None:
            logger.debug(
                "fuel_ops.priority_clusters: skipping entry without "
                "resolvable location tenant=%s destination=%s",
                tenant.tenant_id,
                destination_id,
            )
            continue
        cluster_inputs.append(
            {
                "destination_id": destination_id,
                "lat": location.lat,
                "lon": location.lon,
                "priority_bucket": _str_or_none(entry.get("priority_bucket")),
                "fuel_grade": _str_or_none(entry.get("fuel_grade")),
            }
        )

    try:
        _, clusters = compute_priority_clusters(
            cluster_inputs,
            eps_miles=eps_miles,
            min_samples=min_samples,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    items = [_cluster_to_item(cluster) for cluster in clusters]

    logger.debug(
        "fuel_ops.priority_clusters: tenant=%s run=%s priorities=%d "
        "dense_clusters=%d eps=%.2fmi min_samples=%d",
        tenant.tenant_id,
        run_id,
        len(cluster_inputs),
        len(items),
        eps_miles,
        min_samples,
    )
    return PriorityClustersResponse(
        run_id=run_id,
        eps_miles=eps_miles,
        min_samples=min_samples,
        items=items,
        total=len(items),
    )


# ---------------------------------------------------------------------------
# Priority-cluster helpers
# ---------------------------------------------------------------------------


async def _load_latest_priority_list(
    es: Any, tenant_id: str
) -> Optional[Dict[str, Any]]:
    """Return the most recent priority-list document for the tenant, or None.

    Errors are logged and swallowed so a transient ES hiccup does not
    bubble up as a 5xx — the endpoint simply reports "no clusters yet".
    """

    query = {
        "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
        "sort": [{"timestamp": {"order": "desc"}}],
        "size": 1,
    }
    try:
        resp = await es.search_documents(
            "mvp_delivery_priorities", query, _PRIORITY_CLUSTER_FETCH_CEILING
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "fuel_ops.priority_clusters: failed to load priorities for "
            "tenant=%s: %s",
            tenant_id,
            exc,
        )
        return None
    hits = resp.get("hits", {}).get("hits", []) if resp else []
    if not hits:
        return None
    source = hits[0].get("_source") or {}
    if source.get("tenant_id") != tenant_id:
        # Defensive: the ``term`` clause already filters by tenant, but
        # re-verify here so a mis-seeded document never crosses tenants.
        logger.warning(
            "fuel_ops.priority_clusters: dropped priority list with "
            "mismatched tenant_id=%s (expected %s)",
            source.get("tenant_id"),
            tenant_id,
        )
        return None
    return source


def _cluster_to_item(cluster: PriorityCluster) -> PriorityClusterItem:
    """Project a :class:`PriorityCluster` dataclass into the API item."""

    return PriorityClusterItem(
        cluster_id=cluster.cluster_id,
        centroid=PriorityClusterCentroid(
            lat=cluster.centroid["lat"],
            lon=cluster.centroid["lon"],
        ),
        member_count=cluster.member_count,
        highest_priority_bucket=cluster.highest_priority_bucket,
        fuel_grades=list(cluster.fuel_grades),
    )


def _str_or_none(value: Any) -> Optional[str]:
    """Return a stripped string or None for empty/whitespace inputs."""

    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    stripped = value.strip()
    return stripped or None


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/replans/{event_id}/diff (Req 2.5.3, Task 4.10)
# ---------------------------------------------------------------------------


class ReplanDiffResponse(BaseModel):
    """Envelope for ``GET /api/fuel/mvp/replans/{event_id}/diff`` (Req 2.5.3).

    The envelope carries the :class:`StructuredReplanDiff` payload along
    with the surrounding ReplanEvent metadata (``event_id``,
    ``replan_type``, ``status``) so the dispatcher UI can render both the
    diff and its triggering context in one request. ``diff`` matches the
    Pydantic schema defined in
    :mod:`Agents.support.replan_diff_models` verbatim so the JSON
    round-trip property (Req 2.5.5) holds.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    replan_type: str
    status: str
    diff: StructuredReplanDiff


@mvp_router.get(
    "/replans/{event_id}/diff",
    response_model=ReplanDiffResponse,
)
async def get_replan_diff(
    event_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> ReplanDiffResponse:
    """Return the structured Replan_Diff for a given replan event.

    Loads the ReplanEvent document from ``mvp_replan_events`` by event_id,
    enforces tenant isolation (cross-tenant reads are masked as 404 so
    existence is never leaked), and returns the persisted
    :class:`StructuredReplanDiff` embedded under ``replan_diff``.

    Returns HTTP 404 when the event is missing, owned by another tenant,
    or exists but has no structured diff attached (escalated replans
    skip the diff computation because the handler returned no feasible
    patch). Returns HTTP 502 when the ES query itself fails.

    Validates: Requirement 2.5.3.
    """

    es = _get_es()

    # Use a search query scoped by tenant_id so cross-tenant hits are
    # filtered out in ES itself — this keeps tenant isolation independent
    # of any caller-side check and matches the pattern used by the rest
    # of the fuel-ops endpoints (which also drive ES via search rather
    # than get-by-id so mocked test doubles don't need get_document).
    try:
        resp = await es.search_documents(
            MVP_REPLAN_EVENTS_INDEX,
            {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"event_id": event_id}},
                            {"term": {"tenant_id": tenant.tenant_id}},
                        ]
                    }
                },
                "size": 1,
            },
            1,
        )
    except Exception as exc:
        logger.exception(
            "fuel_ops.replans.diff: ES search failed for event=%s",
            event_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error_code": "replan_events_unavailable",
                "message": "Replan events store is unavailable.",
            },
        ) from exc

    hits = (resp or {}).get("hits", {}).get("hits", [])
    if not hits:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "replan_event_not_found",
                "event_id": event_id,
            },
        )
    source = hits[0].get("_source") or {}

    # Defensive tenant re-check — the ES query already filtered by
    # tenant_id, but a corrupt or misconfigured mapping could still leak
    # a cross-tenant row; masking as 404 here keeps existence opaque.
    if source.get("tenant_id") != tenant.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "replan_event_not_found",
                "event_id": event_id,
            },
        )

    diff_payload = source.get("replan_diff")
    if not isinstance(diff_payload, dict) or not diff_payload:
        # Escalated replans, or events indexed before Task 4.10 shipped,
        # won't carry a structured diff. Surface a distinct 404 so the FE
        # can differentiate "event missing" from "no diff for this event".
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "replan_diff_not_available",
                "event_id": event_id,
                "message": (
                    "This replan event has no structured diff. It was "
                    "either escalated without a feasible replan or "
                    "predates the structured diff rollout."
                ),
            },
        )

    try:
        structured = StructuredReplanDiff.model_validate(diff_payload)
    except ValidationError as exc:
        # A stored diff that no longer parses is a data-integrity issue;
        # don't crash the request — return 500 with a structured error so
        # the operator can debug without leaking the raw doc to the FE.
        logger.error(
            "fuel_ops.replans.diff: stored replan_diff failed validation "
            "for event=%s: %s",
            event_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "replan_diff_corrupt",
                "event_id": event_id,
            },
        ) from exc

    return ReplanDiffResponse(
        event_id=source.get("event_id", event_id),
        replan_type=source.get("replan_type", ""),
        status=source.get("status", ""),
        diff=structured,
    )


# ---------------------------------------------------------------------------
# Priorities response models (Req 3.1.4)
# ---------------------------------------------------------------------------


class PriorityListResponse(BaseModel):
    """Envelope for ``GET /api/fuel/mvp/priorities`` (Req 3.1.4).

    The persisted ``mvp_delivery_priorities`` document shape has grown
    along with Capability 3: each list entry now carries the new
    ``safe_to_delay_days`` / ``safe_to_delay_bucket`` (Req 3.1.3),
    ``business_impact_score`` / ``business_impact_reasons``
    (Req 3.3.3 / 3.3.4), and ``cluster_id`` / ``cluster_size``
    (Req 3.4.2) columns alongside the original ``priority_score`` /
    ``priority_bucket`` fields.

    The envelope itself intentionally mirrors the dual-field shape
    returned by :func:`schemas.common.paginated_response_dict` —
    ``items`` + ``total`` + ``page`` + ``page_size`` + ``has_next`` plus
    the deprecated ``data`` / ``pagination`` aliases — so existing
    frontend clients (see ``runsheet/src/services/fuelApi.ts``) keep
    working during the migration from
    :mod:`Agents.support.mvp_endpoints`.
    """

    model_config = ConfigDict(extra="allow")

    items: List[Dict[str, Any]]
    total: int
    page: int
    page_size: int
    has_next: bool


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/priorities (Req 3.1.4)
# ---------------------------------------------------------------------------


@mvp_router.get("/priorities")
async def list_priorities(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    safe_to_delay_bucket: Optional[SafeToDelayBucket] = Query(
        default=None,
        description=(
            "Restrict to priority lists that contain at least one entry "
            "in the supplied safe-to-delay bucket "
            "(none | short | medium | long). Req 3.1.4."
        ),
    ),
    run_id: Optional[str] = Query(
        default=None,
        description="Filter to a single prioritization run_id.",
    ),
    page: int = Query(1, ge=1, description="Page number, 1-indexed."),
    size: int = Query(20, ge=1, le=100, description="Page size (1–100)."),
) -> Dict[str, Any]:
    """Return the latest delivery priority rankings for the requesting tenant.

    Responds with the same dual-field envelope as the legacy endpoint
    (``items`` / ``total`` / ``page`` / ``page_size`` / ``has_next`` +
    deprecated ``data`` / ``pagination``) so the ``fuelApi.ts`` client
    keeps working.

    Each returned priority-list document carries a nested ``priorities``
    array whose entries include the Capability 3 extensions:

        * ``safe_to_delay_days`` / ``safe_to_delay_bucket`` — Req 3.1.3
        * ``business_impact_score`` / ``business_impact_reasons`` — Req 3.3.3 / 3.3.4
        * ``cluster_id`` / ``cluster_size`` — Req 3.4.2

    The ``safe_to_delay_bucket`` query parameter filters at the
    Elasticsearch layer via a ``nested`` query on the ``priorities``
    sub-document so lists without any entry in the selected bucket are
    omitted from the response entirely. The fields themselves are
    emitted verbatim from the persisted document — the agent is the
    source of truth for their values.

    Validates: Requirement 3.1.4.
    """

    es = _get_es()

    must_clauses: List[Dict[str, Any]] = [
        {"term": {"tenant_id": tenant.tenant_id}},
    ]
    if run_id and run_id.strip():
        must_clauses.append({"term": {"run_id": run_id.strip()}})
    if safe_to_delay_bucket:
        # The ``priorities`` field is mapped as ``nested`` (see
        # :data:`Agents.support.mvp_es_mappings.MVP_DELIVERY_PRIORITIES_MAPPING`),
        # so a ``nested`` query is required to match a sub-document's
        # ``safe_to_delay_bucket`` without accidentally collapsing fields
        # across siblings.
        must_clauses.append(
            {
                "nested": {
                    "path": "priorities",
                    "query": {
                        "term": {
                            "priorities.safe_to_delay_bucket": safe_to_delay_bucket,
                        },
                    },
                },
            }
        )

    query = {
        "query": {"bool": {"must": must_clauses}},
        "sort": [{"timestamp": {"order": "desc"}}],
        "from": (page - 1) * size,
        "size": size,
    }

    try:
        resp = await es.search_documents(
            "mvp_delivery_priorities", query, size
        )
    except Exception as exc:
        logger.error(
            "fuel_ops.priorities: ES query failed for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    hits = resp.get("hits", {}).get("hits", []) if resp else []
    total_block = resp.get("hits", {}).get("total", {}) if resp else {}
    total_count = (
        total_block.get("value", 0)
        if isinstance(total_block, dict)
        else int(total_block or 0)
    )

    items = [hit.get("_source") or {} for hit in hits]

    from schemas.common import paginated_response_dict

    logger.debug(
        "fuel_ops.priorities: tenant=%s run=%s bucket=%s page=%d size=%d "
        "returned=%d total=%d",
        tenant.tenant_id,
        run_id,
        safe_to_delay_bucket,
        page,
        size,
        len(items),
        total_count,
    )
    return paginated_response_dict(
        items=items,
        total=total_count,
        page=page,
        page_size=size,
    )


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/combinable-groups (Req 3.2.4)
# ---------------------------------------------------------------------------


class CombinableGroupListResponse(BaseModel):
    """Envelope for ``GET /api/fuel/mvp/combinable-groups`` (Req 3.2.4).

    Uses the same pagination shape as the other fuel-ops list endpoints
    so the existing frontend pagination helpers consume it uniformly.
    """

    model_config = ConfigDict(extra="forbid")

    items: List[CombinableGroup]
    total: int
    page: int
    page_size: int
    has_next: bool


@mvp_router.get(
    "/combinable-groups",
    response_model=CombinableGroupListResponse,
)
async def list_combinable_groups(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    run_id: Optional[str] = Query(
        default=None,
        description=(
            "Filter to a single prioritization run_id. Combinable groups "
            "are emitted per run so this is the common slice."
        ),
    ),
    fuel_grade: Optional[str] = Query(
        default=None,
        description=(
            "Filter to groups whose ``fuel_grades`` array contains the "
            "supplied product. Accepts either the canonical US "
            "product_code (DIESEL_2, PROPANE, ...) or a legacy alias "
            "(AGO, LPG, ...); the repository canonicalizes before the "
            "ES term-match. Unknown codes short-circuit to an empty "
            "result set."
        ),
    ),
    min_members: Optional[int] = Query(
        default=None,
        ge=2,
        le=500,
        description=(
            "Drop groups with fewer than this many members. Groups of "
            "one are already excluded by :func:`compute_combinable_groups`, "
            "so the practical minimum is 2."
        ),
    ),
    page: int = Query(1, ge=1, description="Page number, 1-indexed."),
    size: int = Query(20, ge=1, le=100, description="Page size (1–100)."),
) -> CombinableGroupListResponse:
    """Return the paginated list of Combinable_Groups for the tenant.

    Enforces tenant isolation through
    :class:`CombinableGroupRepository.list_for_tenant`, which filters
    the Elasticsearch query on ``tenant_id`` *and* re-validates every
    returned document's ``tenant_id`` against the caller before the row
    crosses the repository boundary. The optional ``run_id``,
    ``fuel_grade``, and ``min_members`` filters match the spec-mandated
    REST surface (Req 3.2.4).

    The endpoint fetches a single page-sized window on top of the
    ``page * size`` skip so pagination is deterministic without a
    separate ``count`` round-trip — ``has_next`` is reported
    conservatively when the fetched window extends past the end of the
    current page.

    Validates: Requirement 3.2.4.
    """

    repo = _get_combinable_group_repository()

    # Fetch enough rows to cover the requested page plus a single extra
    # row to compute ``has_next`` without an additional count query. The
    # ``size`` param is capped at 100 via the Query validator so
    # ``page * size`` stays bounded.
    try:
        window = await repo.list_for_tenant(
            tenant_id=tenant.tenant_id,
            run_id=run_id.strip() if run_id and run_id.strip() else None,
            fuel_grade=(
                fuel_grade.strip() if fuel_grade and fuel_grade.strip() else None
            ),
            min_members=min_members,
            size=page * size + 1,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    total_window = len(window)
    start = (page - 1) * size
    end = start + size
    page_items = window[start:end]
    has_next = total_window > end

    logger.debug(
        "fuel_ops.combinable_groups: tenant=%s run=%s fuel_grade=%s "
        "min_members=%s page=%d size=%d returned=%d window=%d",
        tenant.tenant_id,
        run_id,
        fuel_grade,
        min_members,
        page,
        size,
        len(page_items),
        total_window,
    )
    return CombinableGroupListResponse(
        items=page_items,
        total=total_window,
        page=page,
        page_size=size,
        has_next=has_next,
    )


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/forecasts filter extensions (Req 1.1.4, 1.6.1)
# ---------------------------------------------------------------------------
#
# The extended ``GET /api/fuel/mvp/forecasts`` endpoint required by
# Requirements 1.1.4 and 1.6.1 lives on the existing MVP router in
# :mod:`Agents.support.mvp_endpoints`. That router has served the endpoint
# since the fuel-distribution MVP shipped and was extended in place with
# the new ``customer_tank_id``, ``customer_id``, ``customer_type``, and
# ``fuel_type`` query parameters; keeping the single route definition
# avoids the FastAPI registration conflict that would result from mounting
# two routers at the same ``/api/fuel/mvp/forecasts`` path.
#
# Future consolidation (tracked by fuel-ops hardening Task 5.6 / 12.3)
# will migrate the forecasts endpoint into this module alongside the rest
# of the Capability-1+ surface. Until then, consumers should continue to
# rely on the existing endpoint, which honours every filter documented
# above.


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/reconciliation (Req 4.4.4, Task 8.8)
# ---------------------------------------------------------------------------


class ReconciliationListResponse(BaseModel):
    """Envelope for ``GET /api/fuel/mvp/reconciliation`` (Req 4.4.4).

    Mirrors the dual-field pagination shape used by the other fuel-ops
    list endpoints (``items`` + ``total`` + ``page`` + ``page_size`` +
    ``has_next`` plus the legacy ``data`` / ``pagination`` / ``request_id``
    aliases produced by :func:`schemas.common.paginated_response_dict`)
    so front-end pagination helpers consume it uniformly. ``items``
    surface full :class:`services.reconciliation_service.ReconciliationRecord`
    documents re-validated out of the ``mvp_reconciliation`` ES index —
    internal ES wrapping (``_id`` / ``_source``) is never leaked.
    """

    model_config = ConfigDict(extra="allow")

    items: List[ReconciliationRecord]
    total: int
    page: int
    page_size: int
    has_next: bool


@mvp_router.get("/reconciliation", response_model=ReconciliationListResponse)
async def list_reconciliation_records(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    order_id: Optional[str] = Query(
        default=None,
        description="Filter to a single order_id.",
    ),
    plan_id: Optional[str] = Query(
        default=None,
        description="Filter to a single loading plan_id.",
    ),
    pod_id: Optional[str] = Query(
        default=None,
        description="Filter to a single POD pod_id.",
    ),
    min_variance_pct: Optional[float] = Query(
        default=None,
        ge=0.0,
        description=(
            "Return only records where the absolute value of ANY of "
            "``variance_load_vs_order_pct``, "
            "``variance_delivered_vs_loaded_pct``, or "
            "``variance_invoiced_vs_delivered_pct`` meets or exceeds "
            "this percentage. Must be non-negative."
        ),
    ),
    page: int = Query(1, ge=1, description="Page number, 1-indexed."),
    size: int = Query(
        50,
        ge=1,
        le=500,
        description="Page size (1–500). Defaults to 50.",
    ),
) -> Dict[str, Any]:
    """Return paginated :class:`ReconciliationRecord` rows for the tenant.

    The endpoint reads from the ``mvp_reconciliation`` ES index populated
    by :class:`services.reconciliation_service.ReconciliationService`
    (Task 8.7) and filtered further by the QuickBooks Online Connector
    (Task 8.8 / Phase 9) as invoice events arrive.

    Query filters:

        * ``order_id`` / ``plan_id`` / ``pod_id`` — exact-match term
          filters applied directly to the ES query, so narrowing is
          cheap and O(log n).
        * ``min_variance_pct`` — records are included when *any* of the
          three variance percentages meets or exceeds this value. We
          apply the filter post-hoc in Python (rather than as an ES
          ``range`` query) so the OR across three fields, plus the
          ``None`` handling on ``variance_invoiced_vs_delivered_pct``
          before the QBO invoice lands, stays straightforward. The
          endpoint caps ``size`` at 500 to keep the post-hoc scan bounded.

    Tenant isolation is enforced twice:

        1. The ES query filters on ``tenant_id`` via a ``term`` clause.
        2. Every returned ``_source`` is re-validated against the
           caller's ``tenant_id`` before it is surfaced — a mis-labelled
           document never crosses the endpoint boundary.

    Results are ordered by ``generated_at`` descending so clients see
    the freshest reconciliation first. Invalid rows (failing
    :class:`ReconciliationRecord` model validation) are dropped with a
    warning rather than surfaced as a 500 so a single malformed
    document does not break the entire list.

    Validates: Requirements 4.4.4, 4.4.5.
    """

    es = _get_es()

    must_clauses: List[Dict[str, Any]] = [
        {"term": {"tenant_id": tenant.tenant_id}}
    ]
    if order_id and order_id.strip():
        must_clauses.append({"term": {"order_id": order_id.strip()}})
    if plan_id and plan_id.strip():
        must_clauses.append({"term": {"plan_id": plan_id.strip()}})
    if pod_id and pod_id.strip():
        must_clauses.append({"term": {"pod_id": pod_id.strip()}})

    # When ``min_variance_pct`` is supplied we need to scan enough rows
    # to find ``page * size`` matches after the post-hoc filter. A
    # bounded window (10× the requested page) gives the UI a usable
    # result without an unbounded ES scan. Without the filter we fetch
    # the requested page directly.
    es_size = size if min_variance_pct is None else min(size * 10, 2000)
    es_from = (page - 1) * size if min_variance_pct is None else 0

    query: Dict[str, Any] = {
        "query": {"bool": {"must": must_clauses}},
        "sort": [{"generated_at": {"order": "desc"}}],
        "from": es_from,
        "size": es_size,
    }

    try:
        resp = await es.search_documents(
            MVP_RECONCILIATION_INDEX, query, es_size
        )
    except Exception as exc:
        logger.error(
            "fuel_ops.reconciliation: ES query failed for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    # Handle both dict and ObjectApiResponse
    hits_outer = resp.get("hits", {}) if hasattr(resp, 'get') else {}
    hits = hits_outer.get("hits", []) or []
    total_block = (
        hits_outer.get("total", {}) if hasattr(hits_outer, 'get') else {}
    )
    if hasattr(total_block, 'get'):
        es_total = int(total_block.get("value", 0) or 0)
    else:
        try:
            es_total = int(total_block or 0)
        except (TypeError, ValueError):
            es_total = 0

    validated_rows: List[ReconciliationRecord] = []
    for hit in hits:
        source = hit.get("_source") if hasattr(hit, 'get') else None
        if not source:
            continue
        # Defense-in-depth: drop any row whose tenant_id does not match
        # the caller. The ES ``term`` clause should already exclude
        # them but a mis-labelled document must never leak.
        if source.get("tenant_id") != tenant.tenant_id:
            logger.warning(
                "fuel_ops.reconciliation: dropping row with mismatched "
                "tenant_id %s (expected %s)",
                source.get("tenant_id"),
                tenant.tenant_id,
            )
            continue
        # Strip persistence-only fields that are not part of the model.
        # ``_id``/``_source`` wrapping is already consumed above; here
        # we drop the ``mvp_reconciliation`` mapping's ``created_at`` /
        # ``updated_at`` / ``payment_status`` surrogates so the model's
        # ``extra="forbid"`` does not trip. They are not part of the
        # ReconciliationRecord contract.
        doc = {k: v for k, v in source.items() if k not in (
            "created_at",
            "updated_at",
            "payment_status",
        )}
        try:
            validated_rows.append(ReconciliationRecord(**doc))
        except ValidationError as exc:
            logger.warning(
                "fuel_ops.reconciliation: dropping row that failed "
                "model validation (reconciliation_id=%s): %s",
                source.get("reconciliation_id"),
                exc,
            )

    # Apply the min_variance_pct filter after model validation so we can
    # reason about the three variance percentages as floats in a single
    # place. We treat ``None`` (no QBO invoice yet) as "does not
    # contribute to the match" so partial records aren't excluded on
    # the strength of a missing field alone — only the present
    # variances need to cross the threshold.
    if min_variance_pct is not None:
        threshold = abs(float(min_variance_pct))

        def _matches(record: ReconciliationRecord) -> bool:
            candidates = (
                record.variance_load_vs_order_pct,
                record.variance_delivered_vs_loaded_pct,
                record.variance_invoiced_vs_delivered_pct,
            )
            return any(
                v is not None and abs(float(v)) >= threshold
                for v in candidates
            )

        filtered = [r for r in validated_rows if _matches(r)]
        # Apply pagination over the filtered set. ``has_next`` is
        # conservative when the ES window exceeded ``es_size`` — we
        # cannot know whether additional matches exist beyond the scan
        # window so we report ``True`` when the post-scan count fills
        # the page and the ES total is larger than what we scanned.
        total_filtered = len(filtered)
        start = (page - 1) * size
        end = start + size
        page_rows = filtered[start:end]
        has_next_filtered = total_filtered > end or (
            es_total > len(hits) and len(page_rows) == size
        )
        logger.debug(
            "fuel_ops.reconciliation: tenant=%s order=%s plan=%s pod=%s "
            "min_variance=%s page=%d size=%d es_window=%d filtered=%d "
            "returned=%d",
            tenant.tenant_id,
            order_id,
            plan_id,
            pod_id,
            min_variance_pct,
            page,
            size,
            len(validated_rows),
            total_filtered,
            len(page_rows),
        )
        response = paginated_response_dict(
            items=[r.model_dump(mode="json") for r in page_rows],
            total=total_filtered,
            page=page,
            page_size=size,
            request_id=getattr(request.state, "request_id", "unknown"),
        )
        # ``has_next`` from the paginator is based on total_pages only —
        # override it with the scan-aware value so clients observe the
        # correct truthy value on the last scanned page.
        response["has_next"] = has_next_filtered
        return response

    # No post-hoc filter — the ES page is authoritative.
    logger.debug(
        "fuel_ops.reconciliation: tenant=%s order=%s plan=%s pod=%s "
        "page=%d size=%d returned=%d total=%d",
        tenant.tenant_id,
        order_id,
        plan_id,
        pod_id,
        page,
        size,
        len(validated_rows),
        es_total,
    )
    has_next = es_total > page * size
    response = paginated_response_dict(
        items=[r.model_dump(mode="json") for r in validated_rows],
        total=es_total,
        page=page,
        page_size=size,
        request_id=getattr(request.state, "request_id", "unknown"),
    )
    # Override has_next from the paginator (which uses total / page_size
    # to derive total_pages) so we can report ``True`` when there are
    # additional ES rows beyond the current page even when total_pages
    # math would disagree due to integer division rounding.
    response["has_next"] = has_next
    return response


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/routes/{route_id}/emergency-stop (Req 2.4.1, 2.4.3,
#                                                       2.4.5, 2.4.6)
# ---------------------------------------------------------------------------


#: Tool name used when the emergency-stop insertion should be classified
#: MEDIUM risk by :class:`ConfirmationProtocol`. The risk registry
#: (:mod:`Agents.risk_registry`) maps this to ``RiskLevel.MEDIUM``.
EMERGENCY_STOP_TOOL_MEDIUM = "emergency_stop_insertion"

#: Tool name used when the emergency-stop insertion should be classified
#: HIGH risk by :class:`ConfirmationProtocol`. The risk registry maps
#: this to ``RiskLevel.HIGH``. Selected per Req 2.4.5 when the insertion
#: shifts 3+ existing stops or risks an SLA breach.
EMERGENCY_STOP_TOOL_HIGH = "emergency_stop_insertion_high_risk"

#: Threshold (inclusive) at which Req 2.4.5 mandates HIGH risk. Three or
#: more existing stops shifting elevates the approval path from MEDIUM
#: (auto-approve under ``auto-medium`` autonomy) to HIGH (always queued).
EMERGENCY_STOP_HIGH_RISK_SHIFT_THRESHOLD = 3


class EmergencyStopRequest(BaseModel):
    """Body for ``POST /api/fuel/mvp/routes/{route_id}/emergency-stop``
    (Req 2.4.1).

    Exactly one of ``station_id`` / ``customer_tank_id`` is required.
    ``fuel_grade`` is canonicalized via the fuel product catalog so
    legacy aliases (``AGO`` → ``DIESEL_2``, ``PMS`` → ``GASOLINE_REG``,
    etc.) resolve to the same canonical key the persisted route stores.
    ``SLA_by`` is optional — when omitted the solver only checks
    existing stops' SLA windows, not the emergency's own.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    station_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Retail station id the emergency should deliver to. Mutually "
            "exclusive with ``customer_tank_id``."
        ),
    )
    customer_tank_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Customer_Tank id the emergency should deliver to. Mutually "
            "exclusive with ``station_id``."
        ),
    )
    fuel_grade: str = Field(
        ...,
        min_length=1,
        description=(
            "Canonical fuel product_code (e.g. ``DIESEL_2``) or a legacy "
            "alias (``AGO``/``PMS``/``ATK``/``LPG``). The endpoint "
            "canonicalizes the value before capacity checks."
        ),
    )
    requested_gallons: float = Field(
        ...,
        gt=0,
        description="Gallons to drop at the emergency stop (must be > 0).",
    )
    priority_reason: str = Field(
        ...,
        min_length=1,
        description=(
            "Operator-supplied narrative explaining why this insertion is "
            "urgent. Persisted on the approval record for audit."
        ),
    )
    SLA_by: Optional[str] = Field(
        default=None,
        alias="SLA_by",
        description=(
            "Optional ISO-8601 timestamp the emergency stop must be "
            "served by. Reserved for downstream enrichment — the solver "
            "treats ``sla_by_hours`` on the stop itself as the "
            "authoritative deadline."
        ),
    )


class EmergencyStopResponse(BaseModel):
    """Response envelope for the emergency-stop endpoint.

    Returns the computed insertion detail (``insert_index``,
    ``added_distance_km``, ``stops_shifted_count``), the new Replan_Diff
    persisted to ``mvp_replan_events``, and the ConfirmationProtocol
    outcome (risk level, confirmation method, optional approval id).
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    route_id: str
    tenant_id: str
    insert_index: int
    stops_shifted_count: int
    added_distance_km: float
    risk_level: Literal["medium", "high"]
    confirmation_method: str
    approval_id: Optional[str] = None
    sla_at_risk: bool
    diff: FlatReplanDiff


def _get_confirmation_protocol() -> ConfirmationProtocol:
    if _confirmation_protocol is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "confirmation_protocol_unavailable",
                "message": (
                    "ConfirmationProtocol is not wired into the fuel-ops "
                    "endpoints module; emergency-stop insertions cannot be "
                    "routed."
                ),
            },
        )
    return _confirmation_protocol


async def _load_route(tenant_id: str, route_id: str) -> Dict[str, Any]:
    """Load a route document from ``mvp_routes`` with strict tenant scoping.

    Returns the raw ``_source`` dict. Raises HTTP 404 when the route is
    not found and HTTP 403 when a route exists under a different tenant
    id — same pattern the existing cross-tenant protections use.
    """

    es = _get_es()
    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {"route_id": route_id}},
                ]
            }
        },
        "size": 1,
    }
    try:
        resp = await es.search_documents(MVP_ROUTES_INDEX, query, 1)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "fuel_ops.emergency_stop: ES search failed for route=%s tenant=%s",
            route_id,
            tenant_id,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "route_not_found",
                "route_id": route_id,
            },
        )
    source = hits[0].get("_source", {}) or {}
    # Double-check tenant isolation: even though the ES filter should
    # guarantee this, we revalidate because downstream callers depend
    # on the invariant.
    if source.get("tenant_id") and source["tenant_id"] != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "cross_tenant_access_denied",
                "message": "Route belongs to a different tenant.",
                "route_id": route_id,
            },
        )
    return source


async def _resolve_destination_coordinates(
    tenant_id: str,
    *,
    station_id: Optional[str],
    customer_tank_id: Optional[str],
) -> Dict[str, float]:
    """Return the emergency stop's ``{lat, lon}`` dict.

    Prefers the Customer_Tank repository when ``customer_tank_id`` is
    provided so the endpoint honours the new Capability 1 destination
    surface (Req 1.1.1). Falls back to the ``fuel_stations`` index when
    only ``station_id`` is provided. Raises HTTP 400 when neither or
    both are provided, HTTP 404 when the target cannot be resolved.
    """

    if bool(station_id) == bool(customer_tank_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "destination_required",
                "message": (
                    "Provide exactly one of station_id or customer_tank_id "
                    "on the emergency-stop body."
                ),
            },
        )

    if customer_tank_id:
        repo = _get_customer_tank_repository()
        tank = await repo.get(tenant_id, customer_tank_id)
        if tank is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "customer_tank_not_found",
                    "customer_tank_id": customer_tank_id,
                },
            )
        return {"lat": float(tank.location_lat), "lon": float(tank.location_lon)}

    # station_id path — fuel_stations is the canonical retail index.
    es = _get_es()
    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {"station_id": station_id}},
                ]
            }
        },
        "_source": ["station_id", "latitude", "longitude", "location"],
        "size": 1,
    }
    try:
        resp = await es.search_documents("fuel_stations", query, 1)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "fuel_ops.emergency_stop: ES lookup failed for station=%s",
            station_id,
        )
        raise HTTPException(status_code=500, detail=str(exc))
    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "station_not_found",
                "station_id": station_id,
            },
        )
    source = hits[0].get("_source", {}) or {}
    lat = source.get("latitude")
    lon = source.get("longitude")
    if (lat in (None, 0.0) and lon in (None, 0.0)) and isinstance(
        source.get("location"), dict
    ):
        loc = source["location"]
        lat = loc.get("lat")
        lon = loc.get("lon")
    if lat is None or lon is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "station_missing_coordinates",
                "message": "fuel_stations record has no lat/lon; cannot insert.",
                "station_id": station_id,
            },
        )
    return {"lat": float(lat), "lon": float(lon)}


async def _compute_remaining_capacity_by_grade(
    tenant_id: str, *, truck_id: str, plan_id: Optional[str]
) -> Dict[str, float]:
    """Sum remaining gallons per canonical product_code across the truck's
    compartments.

    Capacity is sourced from the ``truck_compartments`` index (persisted
    in liters per the legacy fuel-distribution MVP) and converted to
    gallons via the 3.785411784 conversion used everywhere else in
    fuel-ops. When a ``plan_id`` is supplied, the already-committed
    loading-plan assignments are subtracted from the total so the
    endpoint only sees truly-available capacity.
    """

    es = _get_es()

    # --- Compartment capacities (liters) + allowed grades ---------------
    compartment_query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {"truck_id": truck_id}},
                ]
            }
        },
        "_source": [
            "compartment_id",
            "capacity_liters",
            "allowed_grades",
            "last_loaded_product",
            "state",
        ],
        "size": 200,
    }

    try:
        resp = await es.search_documents(
            TRUCK_COMPARTMENTS_INDEX, compartment_query, 200
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "fuel_ops.emergency_stop: truck_compartments lookup failed "
            "for truck=%s tenant=%s",
            truck_id,
            tenant_id,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    per_compartment: Dict[str, Dict[str, Any]] = {}
    for hit in resp.get("hits", {}).get("hits", []):
        source = hit.get("_source", {}) or {}
        comp_id = str(source.get("compartment_id") or "")
        if not comp_id:
            continue
        raw_grades = source.get("allowed_grades") or []
        canonical_grades = []
        for grade in raw_grades:
            try:
                canonical_grades.append(canonicalize(grade))
            except UnknownFuelProductError:
                # Preserve unknown codes so operators see the raw value
                # when tracing why a compartment was skipped.
                canonical_grades.append(str(grade))
        per_compartment[comp_id] = {
            "capacity_liters": float(source.get("capacity_liters") or 0.0),
            "allowed_grades": canonical_grades,
        }

    # --- Existing loading-plan assignments (liters per compartment) ----
    allocated_liters_by_compartment: Dict[str, float] = {}
    if plan_id:
        plan_query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"plan_id": plan_id}},
                    ]
                }
            },
            "size": 1,
        }
        try:
            plan_resp = await es.search_documents(
                MVP_LOAD_PLANS_INDEX, plan_query, 1
            )
        except Exception:
            # Plan lookup failures degrade gracefully to "nothing
            # allocated" so a missing plan does not block a legitimate
            # emergency insertion.
            logger.exception(
                "fuel_ops.emergency_stop: load plan lookup failed "
                "(tenant=%s plan=%s)",
                tenant_id,
                plan_id,
            )
            plan_resp = {"hits": {"hits": []}}
        hits = plan_resp.get("hits", {}).get("hits", [])
        if hits:
            assignments = (
                hits[0].get("_source", {}).get("assignments") or []
            )
            for assignment in assignments:
                comp_id = str(assignment.get("compartment_id") or "")
                qty = float(assignment.get("quantity_liters") or 0.0)
                if not comp_id:
                    continue
                allocated_liters_by_compartment[comp_id] = (
                    allocated_liters_by_compartment.get(comp_id, 0.0) + qty
                )

    # --- Convert to gallons + aggregate per product_code ----------------
    gallons_per_liter = 1.0 / 3.785411784
    remaining_by_grade: Dict[str, float] = {}
    for comp_id, data in per_compartment.items():
        capacity_l = data["capacity_liters"]
        allocated_l = allocated_liters_by_compartment.get(comp_id, 0.0)
        available_l = max(0.0, capacity_l - allocated_l)
        if available_l <= 0:
            continue
        available_gal = available_l * gallons_per_liter
        # A compartment contributes its remaining capacity to every grade
        # it is rated for, which matches the solver's coarse "aggregate
        # remaining gallons per grade" contract for the emergency-
        # insertion path (see insert_emergency_stop docstring). Loading
        # planners still enforce mutual-exclusion between grades; this
        # function only answers the question "is there at least this
        # much capacity available for the requested grade?"
        for grade in data["allowed_grades"] or []:
            remaining_by_grade[grade] = (
                remaining_by_grade.get(grade, 0.0) + available_gal
            )

    return remaining_by_grade


def _route_to_solver_dict(
    route_source: Dict[str, Any],
    *,
    remaining_capacity_by_grade: Dict[str, float],
) -> Dict[str, Any]:
    """Project a ``mvp_routes`` document into the shape the solver expects.

    Route documents persisted by :class:`RoutePlanningAgent` carry
    ``stops`` (each with ``station_id``, ``eta``, ``drop``, ``sequence``)
    but not explicit ``lat/lon`` — the solver needs coordinates for its
    Haversine + traffic-matrix lookups. We preserve the document's
    ``stops`` as-is and let the solver call through to its per-stop key
    helpers; when lat/lon are missing the solver's distance function
    raises a clear ValueError which surfaces to the endpoint as a 400.
    """

    stops_out: List[Dict[str, Any]] = []
    for stop in route_source.get("stops") or []:
        new_stop: Dict[str, Any] = dict(stop)
        # The solver indexes stops by _stop_key which prefers
        # ``stop_id`` → ``station_id`` → ``customer_tank_id``; ensure a
        # stable id so the traffic-matrix key is deterministic.
        if not any(
            new_stop.get(k)
            for k in ("stop_id", "station_id", "customer_tank_id")
        ):
            new_stop["stop_id"] = (
                f"stop_{len(stops_out)}"
            )
        stops_out.append(new_stop)

    solver_route: Dict[str, Any] = {
        "stops": stops_out,
        "remaining_capacity_by_grade": dict(remaining_capacity_by_grade),
    }
    # Depot coordinates are stored directly on the persisted route when
    # the agent wrote them; otherwise fall back to the route's depot
    # geometry when available. Callers that have no depot on the route
    # must provide ``start_depot``/``depot`` before invoking the
    # endpoint — a missing depot surfaces as a 400 from
    # insert_emergency_stop itself.
    for key in ("start_depot", "end_depot", "depot"):
        if key in route_source and isinstance(route_source[key], dict):
            solver_route[key] = dict(route_source[key])
    for passthrough in (
        "shift_end_hours",
        "start_time_hours",
    ):
        if passthrough in route_source and route_source[passthrough] is not None:
            solver_route[passthrough] = route_source[passthrough]
    return solver_route


def _persist_replan_event(
    *,
    es: Any,
    tenant_id: str,
    route_id: str,
    run_id: str,
    plan_id: str,
    diff: FlatReplanDiff,
    insertion: Dict[str, Any],
    body: "EmergencyStopRequest",
    canonical_grade: str,
    approval_id: Optional[str],
    risk_level: str,
    sla_at_risk: bool,
) -> str:
    """Return the event_id for an emergency-stop Replan_Diff persistence.

    Kept as a standalone helper so the endpoint's risk/classification
    path is easy to read. The actual ES write happens inline in the
    handler so any persistence failure raises inside the same request
    scope and surfaces as HTTP 500 — consistent with the existing
    Exception_Replanning_Agent persistence contract. Reusing
    ``diff.diff_id`` as the event_id correlates the persisted record,
    the WebSocket envelope, and the HTTP response.
    """

    return diff.diff_id


@mvp_router.post(
    "/routes/{route_id}/emergency-stop",
    response_model=EmergencyStopResponse,
)
async def insert_route_emergency_stop(
    route_id: str,
    body: EmergencyStopRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> EmergencyStopResponse:
    """Insert an urgent delivery into an active route (Req 2.4.1).

    The endpoint:

    1. Loads the target route from ``mvp_routes`` (tenant-scoped).
    2. Resolves the emergency stop's coordinates from either the
       ``customer_tanks`` or ``fuel_stations`` index.
    3. Computes the truck's remaining compartment capacity per
       canonical product_code (subtracting any already-committed
       loading-plan assignments).
    4. Calls :func:`insert_emergency_stop` to pick the cheapest-
       insertion position that preserves capacity, downstream SLAs,
       and the driver's shift window.
    5. Derives a structured :class:`ReplanDiff` between the original
       and patched routes.
    6. Classifies the change as HIGH risk when 3+ stops shift or any
       SLA is at risk (Req 2.4.5), else MEDIUM.
    7. Routes the mutation through :class:`ConfirmationProtocol` with
       the matching tool name so the platform-wide autonomy matrix
       decides whether to auto-approve or queue for human review.
    8. Persists the Replan_Diff to ``mvp_replan_events`` with
       ``replan_type="emergency_insertion"`` (Req 2.4.6).
    9. Broadcasts an ``emergency_stop_inserted`` event on
       ``/ws/fuel-planning`` (Req 2.4.6).

    Validates: Requirements 2.4.1, 2.4.3, 2.4.5, 2.4.6.
    """

    confirmation_protocol = _get_confirmation_protocol()
    es = _get_es()

    # ---- Canonicalize fuel_grade ----
    try:
        canonical_grade = canonicalize(body.fuel_grade)
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "unknown_product_code",
                "message": "Unknown fuel product code.",
                "fuel_product_code": exc.code_or_alias,
            },
        )

    # ---- Load the active route from ES ----
    route_source = await _load_route(tenant.tenant_id, route_id)
    truck_id = route_source.get("truck_id", "")
    plan_id = route_source.get("plan_id") or None
    run_id = route_source.get("run_id", "")
    if not truck_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "route_missing_truck_id",
                "route_id": route_id,
            },
        )

    # ---- Resolve emergency stop coordinates ----
    emergency_coords = await _resolve_destination_coordinates(
        tenant.tenant_id,
        station_id=body.station_id,
        customer_tank_id=body.customer_tank_id,
    )

    # ---- Build solver inputs ----
    remaining_by_grade = await _compute_remaining_capacity_by_grade(
        tenant.tenant_id, truck_id=truck_id, plan_id=plan_id
    )

    solver_route = _route_to_solver_dict(
        route_source,
        remaining_capacity_by_grade=remaining_by_grade,
    )

    emergency_stop_id = (
        body.customer_tank_id
        or body.station_id
        or f"emergency_{route_id}"
    )
    emergency_dict: Dict[str, Any] = {
        "stop_id": emergency_stop_id,
        "lat": emergency_coords["lat"],
        "lon": emergency_coords["lon"],
        "fuel_grade": canonical_grade,
        "requested_gallons": float(body.requested_gallons),
        "priority_reason": body.priority_reason,
    }
    if body.station_id:
        emergency_dict["station_id"] = body.station_id
    if body.customer_tank_id:
        emergency_dict["customer_tank_id"] = body.customer_tank_id

    # ---- Run cheapest-insertion ----
    try:
        insertion = insert_emergency_stop(solver_route, emergency_dict)
    except InfeasibleInsertion as exc:
        # Req 2.4.4: structured reason codes on HTTP 409.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": exc.reason,
                "message": f"Emergency stop insertion infeasible: {exc.reason}",
                "reason": exc.reason,
                "details": dict(exc.details or {}),
            },
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_route_shape",
                "message": str(exc),
                "route_id": route_id,
            },
        )

    # ---- Compute Replan_Diff between original and patched routes ----
    original_route_view: Dict[str, Any] = {
        "route_id": route_id,
        "truck_id": truck_id,
        "stops": solver_route.get("stops") or [],
    }
    patched_route_view: Dict[str, Any] = {
        # Reuse the same route_id — emergency insertion patches the
        # existing route in place rather than minting a new document.
        "route_id": route_id,
        "truck_id": truck_id,
        "stops": insertion.get("new_stops") or [],
    }
    diff = compute_replan_diff(original_route_view, patched_route_view)

    # ---- Risk classification (Req 2.4.5) ----
    stops_shifted_count = int(insertion.get("stops_shifted_count") or 0)
    sla_at_risk = False
    # Any existing-stop ETA shift into or past the stop's SLA window
    # flags the insertion as SLA-at-risk. ``sla_by_hours`` on a stop
    # is the solver's deadline; we treat its absence as "no SLA" to
    # avoid false positives.
    etas = insertion.get("new_etas") or []
    patched_stops = insertion.get("new_stops") or []
    insert_idx = int(insertion.get("insert_index") or 0)
    for idx in range(insert_idx, len(patched_stops)):
        stop = patched_stops[idx]
        sla_by = stop.get("sla_by_hours") if isinstance(stop, dict) else None
        if sla_by is None:
            continue
        if idx < len(etas) and float(etas[idx]) > float(sla_by) - 1e-9:
            # Any stop whose ETA is now within 1e-9h of its SLA deadline
            # counts as "at risk" — we stop at the first match because
            # the whole change is already HIGH-risk at that point.
            sla_at_risk = True
            break

    high_risk = (
        stops_shifted_count >= EMERGENCY_STOP_HIGH_RISK_SHIFT_THRESHOLD
        or sla_at_risk
    )
    risk_tool_name = (
        EMERGENCY_STOP_TOOL_HIGH if high_risk else EMERGENCY_STOP_TOOL_MEDIUM
    )

    # ---- Route through ConfirmationProtocol ----
    mutation_parameters: Dict[str, Any] = {
        "route_id": route_id,
        "truck_id": truck_id,
        "plan_id": plan_id or "",
        "run_id": run_id,
        "station_id": body.station_id,
        "customer_tank_id": body.customer_tank_id,
        "fuel_grade": canonical_grade,
        "requested_gallons": float(body.requested_gallons),
        "priority_reason": body.priority_reason,
        "SLA_by": body.SLA_by,
        "insert_index": insertion.get("insert_index"),
        "stops_shifted_count": stops_shifted_count,
        "sla_at_risk": sla_at_risk,
        "added_distance_km": insertion.get("added_distance_km"),
        "tenant_id": tenant.tenant_id,
    }
    mutation_request = MutationRequest(
        tool_name=risk_tool_name,
        parameters=mutation_parameters,
        tenant_id=tenant.tenant_id,
        agent_id="fuel_ops_emergency_stop_endpoint",
        user_id=tenant.user_id,
    )
    try:
        mutation_result = await confirmation_protocol.process_mutation(
            mutation_request
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "fuel_ops.emergency_stop: ConfirmationProtocol failed "
            "route=%s tenant=%s",
            route_id,
            tenant.tenant_id,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    approval_id = mutation_result.approval_id
    risk_level = mutation_result.risk_level or ("high" if high_risk else "medium")

    # ---- Persist Replan_Diff to mvp_replan_events (Req 2.4.6) ----
    event_id = _persist_replan_event(
        es=es,
        tenant_id=tenant.tenant_id,
        route_id=route_id,
        run_id=run_id,
        plan_id=plan_id or "",
        diff=diff,
        insertion=insertion,
        body=body,
        canonical_grade=canonical_grade,
        approval_id=approval_id,
        risk_level=risk_level,
        sla_at_risk=sla_at_risk,
    )
    # Build the persisted document inline here so we keep the helper
    # above pure (for unit-test isolation) while still guaranteeing the
    # write happens on the hot path.
    try:
        await es.index_document(
            MVP_REPLAN_EVENTS_INDEX,
            event_id,
            {
                "event_id": event_id,
                "original_plan_id": plan_id or "",
                "patched_plan_id": plan_id or "",
                "trigger_signal_id": f"emergency_stop:{event_id}",
                "replan_type": "emergency_insertion",
                "diff": {
                    "stops_reordered": [
                        entry.stop_id for entry in diff.reordered_stops
                    ],
                    "volumes_reallocated": {
                        "__flat_replan_diff__": diff.model_dump(mode="json"),
                        "__emergency_stop__": {
                            "station_id": body.station_id,
                            "customer_tank_id": body.customer_tank_id,
                            "fuel_grade": canonical_grade,
                            "requested_gallons": body.requested_gallons,
                            "priority_reason": body.priority_reason,
                            "SLA_by": body.SLA_by,
                            "insert_index": insertion.get("insert_index"),
                            "added_distance_km": insertion.get(
                                "added_distance_km"
                            ),
                            "stops_shifted_count": stops_shifted_count,
                            "approval_id": approval_id,
                            "risk_level": risk_level,
                            "sla_at_risk": sla_at_risk,
                        },
                    },
                    "truck_swapped": None,
                    "stations_deferred": [],
                    "stations_added": [
                        entry.stop_id for entry in diff.added_stops
                    ],
                },
                "status": "applied",
                "tenant_id": tenant.tenant_id,
                "run_id": run_id,
                "timestamp": diff.generated_at.isoformat(),
            },
        )
    except Exception as exc:
        logger.exception(
            "fuel_ops.emergency_stop: failed to persist replan event "
            "route=%s tenant=%s event=%s",
            route_id,
            tenant.tenant_id,
            event_id,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    # ---- Broadcast WebSocket event (Req 2.4.6) ----
    if _fuel_planning_ws_manager is not None:
        try:
            summary = diff.summary_counts()
            summary["diff_id"] = diff.diff_id
            summary["original_route_id"] = diff.original_route_id
            summary["patched_route_id"] = diff.patched_route_id
            await _fuel_planning_ws_manager.broadcast_emergency_stop_inserted(
                run_id=run_id,
                tenant_id=tenant.tenant_id,
                route_id=route_id,
                diff_summary=summary,
                extra={
                    "event_id": event_id,
                    "risk_level": risk_level,
                    "approval_id": approval_id,
                    "insert_index": insertion.get("insert_index"),
                    "stops_shifted_count": stops_shifted_count,
                    "sla_at_risk": sla_at_risk,
                },
            )
        except Exception:  # pragma: no cover - never block on WS
            logger.exception(
                "fuel_ops.emergency_stop: WS broadcast failed route=%s tenant=%s",
                route_id,
                tenant.tenant_id,
            )

    logger.info(
        "fuel_ops.emergency_stop: tenant=%s route=%s event=%s risk=%s "
        "shifts=%d sla_at_risk=%s confirmation=%s",
        tenant.tenant_id,
        route_id,
        event_id,
        risk_level,
        stops_shifted_count,
        sla_at_risk,
        mutation_result.confirmation_method,
    )
    return EmergencyStopResponse(
        event_id=event_id,
        route_id=route_id,
        tenant_id=tenant.tenant_id,
        insert_index=int(insertion.get("insert_index") or 0),
        stops_shifted_count=stops_shifted_count,
        added_distance_km=float(insertion.get("added_distance_km") or 0.0),
        risk_level="high" if high_risk else "medium",
        confirmation_method=mutation_result.confirmation_method,
        approval_id=approval_id,
        sla_at_risk=sla_at_risk,
        diff=diff,
    )


# ---------------------------------------------------------------------------
# Terminal CRUD endpoints (Task 7.2 — Req 8.1.2, 8.1.4)
# ---------------------------------------------------------------------------
#
# Six endpoints back the Terminal admin + validator path:
#
# * ``GET    /api/fuel/terminals``                          — paginated list
# * ``POST   /api/fuel/terminals``                          — create
# * ``GET    /api/fuel/terminals/{terminal_id}``            — fetch
# * ``PATCH  /api/fuel/terminals/{terminal_id}``            — partial update
# * ``DELETE /api/fuel/terminals/{terminal_id}``            — hard delete
# * ``POST   /api/fuel/terminals/{terminal_id}/proposed-load`` — operating-
#   hours / supported-product validator that surfaces the Req 8.1.4
#   ``terminal_closed`` reason with a next-open-window suggestion before
#   a dispatcher (or the Route_Planning_Agent) commits a load.
#
# Shape mirrors the Depot CRUD endpoints (Task 4.3) so front-end
# pagination helpers can consume both surfaces uniformly:
# ``{items, total, page, page_size, has_next}``. The router stamps
# ``tenant_id`` from the JWT context on every write so the caller cannot
# spoof ownership. Cross-tenant accesses surface through the shared
# :func:`_translate_terminal_cross_tenant_error` helper as HTTP 403
# ``cross_tenant_access_denied``.
#
# The ``operator`` filter on the list endpoint is a case-insensitive
# substring match applied on the post-query result set because
# :meth:`TerminalRepository.list_for_tenant` only supports exact
# equality. Substring filtering at the repository layer would need a
# ``match``/ ``wildcard`` query and a re-mapped analyzer on the
# ``operator`` keyword field; that is out of scope here and the seeded
# terminal count per tenant is small enough that client-side filtering
# on the fetched window is fine for now.
#
# Validates: Requirements 8.1.2, 8.1.4.


class TerminalCreateRequest(BaseModel):
    """Body for ``POST /api/fuel/terminals`` (Req 8.1.2).

    Mirrors :class:`fuel.terminal_models.Terminal` but omits repository-
    managed fields (``tenant_id``, ``created_at``, ``updated_at``) and
    makes ``terminal_id`` optional so the repository can mint one
    (``term_<uuid4>``). Coordinate bounds are enforced at the Pydantic
    layer so invalid values surface as a clean 422 from FastAPI's
    request validation before reaching the repository.
    """

    model_config = ConfigDict(extra="forbid")

    terminal_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional client-supplied identifier. When omitted the "
            "repository mints a uuid4-based id (``term_<uuid4>``)."
        ),
    )
    name: str = Field(..., min_length=1)
    operator: str = Field(..., min_length=1)
    location_lat: float = Field(..., ge=-90.0, le=90.0)
    location_lon: float = Field(..., ge=-180.0, le=180.0)
    address: str = Field(..., min_length=1)
    timezone: str = Field(..., min_length=1)
    operating_hours: List[OperatingHours] = Field(default_factory=list)
    supported_products: List[str] = Field(default_factory=list)
    branded: bool = Field(default=False)
    supplier_brand: Optional[str] = Field(default=None)
    status: TerminalActiveStatus = "active"


class TerminalUpdateRequest(BaseModel):
    """Body for ``PATCH /api/fuel/terminals/{terminal_id}`` (Req 8.1.2).

    Every field is optional so callers can send just the delta. The
    repository refuses to overwrite immutable fields (``terminal_id``,
    ``tenant_id``, ``created_at``); those are not exposed here so
    malicious or accidental payloads are rejected by the
    ``extra="forbid"`` Pydantic policy before reaching the repository.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1)
    operator: Optional[str] = Field(default=None, min_length=1)
    location_lat: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    location_lon: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    address: Optional[str] = Field(default=None, min_length=1)
    timezone: Optional[str] = Field(default=None, min_length=1)
    operating_hours: Optional[List[OperatingHours]] = None
    supported_products: Optional[List[str]] = None
    branded: Optional[bool] = None
    supplier_brand: Optional[str] = None
    status: Optional[TerminalActiveStatus] = None


class TerminalListResponse(BaseModel):
    """Envelope for ``GET /api/fuel/terminals`` (Req 8.1.2)."""

    model_config = ConfigDict(extra="forbid")

    items: List[Terminal]
    total: int
    page: int
    page_size: int
    has_next: bool


class ProposedLoadRequest(BaseModel):
    """Body for ``POST /api/fuel/terminals/{terminal_id}/proposed-load``.

    A dispatcher (or the Route_Planning_Agent preflighting a
    Loading_Plan) submits a ``{product_code, volume_gallons, as_of}``
    triple to ask "can this terminal accept this load right now?"
    without actually committing anything. The endpoint surfaces the
    same ``terminal_closed`` reason the Sourcing_Recommender uses
    (Req 8.1.4) plus a structured ``next_open_window`` so the caller
    can schedule the load for the next viable slot.
    """

    model_config = ConfigDict(extra="forbid")

    product_code: str = Field(
        ...,
        min_length=1,
        description=(
            "Canonical catalog product_code, or a legacy alias "
            "(AGO, PMS, ATK, LPG) which is canonicalized before the "
            "``supported_products`` membership check."
        ),
    )
    volume_gallons: float = Field(..., gt=0)
    as_of: Optional[datetime] = Field(
        default=None,
        description=(
            "When the load would happen. Defaults to now() so a "
            "dispatcher clicking 'load here' evaluates against the "
            "current operating-hours window. Naive datetimes are "
            "assumed UTC."
        ),
    )


class ProposedLoadNextOpenWindow(BaseModel):
    """Next-viable-open-window surfaced inside a ``terminal_closed`` 400.

    Computed by walking forward up to 7 days from ``as_of`` in the
    terminal's local timezone and returning the first
    :class:`OperatingHours` window whose open time is on or after the
    requested ``as_of``. ``starts_at_utc`` is the UTC conversion of that
    local open time so the caller can schedule against an absolute
    reference clock without re-doing the timezone math.
    """

    model_config = ConfigDict(extra="forbid")

    day_of_week: str
    open_local: str = Field(..., description="Local HH:MM open time.")
    close_local: str = Field(..., description="Local HH:MM close time.")
    starts_at_utc: datetime = Field(
        ...,
        description=(
            "Absolute UTC start of the window. Convenient for callers "
            "who want to re-evaluate at the exact open moment without "
            "re-running the timezone resolution."
        ),
    )


class ProposedLoadResponse(BaseModel):
    """200 body for ``POST /terminals/{id}/proposed-load`` when the
    load is permitted. The 400 path surfaces a structured detail
    payload instead (see :func:`propose_load_at_terminal`).
    """

    model_config = ConfigDict(extra="forbid")

    allowed: bool = Field(default=True)
    terminal_id: str
    product_code: str = Field(
        ..., description="The canonical product_code the load was evaluated against."
    )
    volume_gallons: float = Field(..., gt=0)
    as_of: datetime


def _compute_next_open_window(
    terminal: Terminal, as_of: datetime
) -> Optional[ProposedLoadNextOpenWindow]:
    """Return the first viable open window on/after ``as_of``, or ``None``.

    Walks forward up to 7 days from ``as_of`` in the terminal's local
    timezone. For the same day as ``as_of`` we only accept a window
    whose ``open`` is still in the future; later days accept the
    earliest window for that day. Tenants with an empty
    ``operating_hours`` list never reach this helper — the 24/7 path in
    :meth:`Terminal.is_open_at` short-circuits before the validator
    surfaces a ``terminal_closed`` reason.
    """

    if not terminal.operating_hours:
        return None

    # Build a day-code → OperatingHours map for O(1) lookup.
    by_day: Dict[str, OperatingHours] = {
        w.day_of_week: w for w in terminal.operating_hours
    }

    local_now = _terminal_local_datetime(terminal, as_of)
    if local_now is None:
        # Unknown timezone — ``is_open_at`` degrades to True in that case
        # so we would never be called; guard defensively anyway.
        return None

    day_codes = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    for offset in range(0, 8):
        probe = local_now + timedelta(days=offset)
        day_code = day_codes[probe.weekday()]
        window = by_day.get(day_code)
        if window is None:
            continue
        open_h, open_m = (int(x) for x in window.open.split(":"))
        candidate_local = probe.replace(
            hour=open_h, minute=open_m, second=0, microsecond=0
        )
        if offset == 0 and candidate_local <= local_now:
            # Open already passed earlier today — walk forward.
            continue
        starts_utc = candidate_local.astimezone(timezone.utc)
        return ProposedLoadNextOpenWindow(
            day_of_week=day_code,
            open_local=window.open,
            close_local=window.close,
            starts_at_utc=starts_utc,
        )
    return None


def _terminal_local_datetime(
    terminal: Terminal, value: datetime
) -> Optional[datetime]:
    """Return ``value`` in the terminal's local timezone, or ``None``.

    Thin wrapper over the shared ``_to_local_datetime`` helper in
    :mod:`fuel.terminal_models` so the endpoint layer is not forced to
    reach into the model module's underscore-prefixed helpers.
    """

    # Imported lazily to avoid a circular import: fuel_ops_endpoints
    # already imports from fuel.terminal_models at module load, so the
    # symbol exists by the time this helper runs, but the underscore
    # prefix keeps us honest about the private contract.
    from fuel.terminal_models import _to_local_datetime  # type: ignore

    return _to_local_datetime(value, terminal.timezone)


def _ensure_fuel_admin_role(tenant: TenantContext) -> None:
    """Admin-gate the Terminal / Supplier_Contract management surface.

    Task 8.1 of cross-module-entity-linkage scopes the canonical Terminal
    and Supplier_Contract records to a *thin* admin-managed surface:
    reads (list / get) stay open to any authenticated tenant member so
    the Sourcing UI and the ``<EntityLink>`` resolver can resolve a
    reference, but every state-changing operation (create / update /
    deactivate / delete) is restricted to the canonical ``admin`` role
    via the shared exact-match :func:`auth.authorization.require_role`
    helper. A non-admin caller receives HTTP 403 ``INSUFFICIENT_ROLE``
    without the held-role lexicon being echoed back.

    Validates: Requirements 9.1, 9.2.
    """

    require_role(tenant, "admin")


@router.get("/terminals", response_model=TerminalListResponse)
async def list_terminals(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    status_filter: Optional[TerminalActiveStatus] = Query(
        default=None,
        alias="status",
        description="Restrict to a single status (active | inactive).",
    ),
    operator: Optional[str] = Query(
        default=None,
        description=(
            "Case-insensitive substring match against the terminal's "
            "``operator`` field (e.g. ``buckeye`` matches ``Buckeye`` "
            "and ``Buckeye Terminals``). Applied after the tenant-"
            "scoped ES query so the filter respects every terminal the "
            "caller owns."
        ),
    ),
    product_code: Optional[str] = Query(
        default=None,
        description=(
            "Filter by supported fuel product. Accepts the canonical "
            "product_code (e.g. DIESEL_2) or a legacy alias (AGO / "
            "PMS / ATK / LPG); aliases are resolved through the fuel "
            "product catalog before the ES query is issued."
        ),
    ),
    page: int = Query(1, ge=1, description="Page number, 1-indexed."),
    size: int = Query(50, ge=1, le=500, description="Page size (1–500)."),
) -> TerminalListResponse:
    """Return the paginated list of Terminals for the tenant.

    Flow:

        1. Fetch a window of ``page * size + 1`` records so we can
           compute ``has_next`` deterministically without an extra
           round-trip.
        2. Apply the client-side ``operator`` substring filter.
        3. Slice into the requested page.

    Validates: Requirement 8.1.2.
    """

    repo = _get_terminal_repository()

    try:
        window = await repo.list_for_tenant(
            tenant_id=tenant.tenant_id,
            status=status_filter,
            supported_product=product_code,
            size=page * size + 1,
        )
    except UnknownFuelProductError:
        # An unknown product_code filter is a miss (not a 400) so we
        # match the depot-list behavior.
        window = []
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if operator:
        needle = operator.strip().lower()
        if needle:
            window = [t for t in window if needle in t.operator.lower()]

    total = len(window)
    start = (page - 1) * size
    end = start + size
    page_items = window[start:end]
    has_next = len(window) > end

    logger.debug(
        "fuel_ops.terminals.list: tenant=%s page=%d size=%d total=%d returned=%d",
        tenant.tenant_id,
        page,
        size,
        total,
        len(page_items),
    )
    return TerminalListResponse(
        items=page_items,
        total=total,
        page=page,
        page_size=size,
        has_next=has_next,
    )


@router.post(
    "/terminals",
    response_model=Terminal,
    status_code=status.HTTP_201_CREATED,
)
async def create_terminal(
    body: TerminalCreateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Terminal:
    """Create a new Terminal scoped to the requesting tenant.

    The router stamps ``tenant_id`` from the verified JWT context so the
    caller cannot spoof ownership. ``supported_products`` entries are
    canonicalized inside :class:`Terminal` via its field validator,
    which surfaces :class:`UnknownFuelProductError` — we map that to a
    400 with a structured ``unknown_product_code`` payload so clients
    can distinguish "bad product" from generic validation errors.

    Validates: Requirement 8.1.2.
    """

    _ensure_fuel_admin_role(tenant)

    repo = _get_terminal_repository()

    payload: Dict[str, Any] = body.model_dump(exclude_none=True)
    payload["tenant_id"] = tenant.tenant_id

    try:
        terminal = await repo.create(tenant.tenant_id, payload)
    except TerminalCrossTenantAccessError as exc:
        raise _translate_terminal_cross_tenant_error(exc)
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "unknown_product_code",
                "message": "Unknown fuel product code.",
                "fuel_product_code": exc.code_or_alias,
            },
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    logger.info(
        "fuel_ops.terminals.create: tenant=%s terminal=%s",
        tenant.tenant_id,
        terminal.terminal_id,
    )
    return terminal


@router.get("/terminals/{terminal_id}", response_model=Terminal)
async def get_terminal(
    terminal_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Terminal:
    """Fetch a single Terminal owned by the tenant.

    Missing / cross-tenant records surface as HTTP 404 with
    ``terminal_not_found`` via :func:`_ensure_terminal_owned` — we never
    leak existence of another tenant's terminals.

    Validates: Requirement 8.1.2.
    """

    return await _ensure_terminal_owned(tenant.tenant_id, terminal_id)


@router.patch("/terminals/{terminal_id}", response_model=Terminal)
async def update_terminal(
    terminal_id: str,
    body: TerminalUpdateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Terminal:
    """Apply a partial update to an owned Terminal.

    Returns 404 when the terminal does not exist. Returns 403 when it
    belongs to another tenant (:class:`CrossTenantAccessError` from the
    repository). Returns 422 when the merged record would fail Pydantic
    validation (invalid timezone, branded/supplier_brand mismatch,
    coordinates out of range). Returns 400 when ``supported_products``
    contains an unknown product code.

    Validates: Requirement 8.1.2.
    """

    _ensure_fuel_admin_role(tenant)

    repo = _get_terminal_repository()

    patch = body.model_dump(exclude_none=True)
    if not patch:
        existing = await repo.get(tenant.tenant_id, terminal_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "terminal_not_found",
                    "terminal_id": terminal_id,
                },
            )
        return existing

    try:
        updated = await repo.update(
            tenant_id=tenant.tenant_id,
            terminal_id=terminal_id,
            patch=patch,
        )
    except TerminalCrossTenantAccessError as exc:
        raise _translate_terminal_cross_tenant_error(exc)
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "unknown_product_code",
                "message": "Unknown fuel product code.",
                "fuel_product_code": exc.code_or_alias,
            },
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "terminal_not_found",
                "terminal_id": terminal_id,
            },
        )

    logger.info(
        "fuel_ops.terminals.update: tenant=%s terminal=%s fields=%s",
        tenant.tenant_id,
        terminal_id,
        sorted(patch.keys()),
    )
    return updated


@router.delete(
    "/terminals/{terminal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_terminal(
    terminal_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> None:
    """Hard-delete a Terminal owned by the tenant.

    * Owned + deleted → HTTP 204 (no body).
    * Not-found → HTTP 404 with structured ``terminal_not_found`` detail.
    * Cross-tenant → HTTP 403 with ``cross_tenant_access_denied``.

    Validates: Requirement 8.1.2.
    """

    _ensure_fuel_admin_role(tenant)

    repo = _get_terminal_repository()

    try:
        deleted = await repo.delete(tenant.tenant_id, terminal_id)
    except TerminalCrossTenantAccessError as exc:
        raise _translate_terminal_cross_tenant_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "terminal_not_found",
                "terminal_id": terminal_id,
            },
        )
    logger.info(
        "fuel_ops.terminals.delete: tenant=%s terminal=%s",
        tenant.tenant_id,
        terminal_id,
    )
    return None


@router.post("/terminals/{terminal_id}/deactivate", response_model=Terminal)
async def deactivate_terminal(
    terminal_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Terminal:
    """Deactivate a Terminal by flipping its ``status`` to ``inactive``.

    The minimal admin management surface (Task 8.1) prefers a reversible
    deactivation over the hard ``DELETE`` so a terminal referenced by
    historical sourcing recommendations, terminal BOLs, or wait reports
    still resolves through the ``<EntityLink>`` resolver (the reference
    stays "linked" rather than dangling as "unlinked") while being
    excluded from the active picker. Deactivation is idempotent — calling
    it on an already-inactive terminal returns the record unchanged.

    * Owned → HTTP 200 with the updated Terminal (``status=inactive``).
    * Not-found / cross-tenant read → HTTP 404 ``terminal_not_found``.
    * Cross-tenant write → HTTP 403 ``cross_tenant_access_denied``.
    * Non-admin caller → HTTP 403 ``INSUFFICIENT_ROLE``.

    Validates: Requirements 9.1, 9.2.
    """

    _ensure_fuel_admin_role(tenant)

    repo = _get_terminal_repository()

    # Load-or-404 first so a missing/cross-tenant id never leaks existence
    # and an already-inactive terminal short-circuits to an idempotent 200.
    existing = await _ensure_terminal_owned(tenant.tenant_id, terminal_id)
    if existing.status == "inactive":
        return existing

    try:
        updated = await repo.update(
            tenant_id=tenant.tenant_id,
            terminal_id=terminal_id,
            patch={"status": "inactive"},
        )
    except TerminalCrossTenantAccessError as exc:
        raise _translate_terminal_cross_tenant_error(exc)
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    if updated is None:
        raise terminal_not_found(
            f"Terminal {terminal_id} not found.",
            details={"terminal_id": terminal_id},
        )

    logger.info(
        "fuel_ops.terminals.deactivate: tenant=%s terminal=%s",
        tenant.tenant_id,
        terminal_id,
    )
    return updated


@router.post(
    "/terminals/{terminal_id}/proposed-load",
    response_model=ProposedLoadResponse,
)
async def propose_load_at_terminal(
    terminal_id: str,
    body: ProposedLoadRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> ProposedLoadResponse:
    """Validate whether a proposed load would be accepted at the terminal.

    Flow:

        1. Verify the terminal exists and is owned by the tenant
           (404 on miss via :func:`_ensure_terminal_owned`).
        2. Canonicalize the requested ``product_code`` and 400 with
           ``unknown_product_code`` if the catalog rejects it.
        3. Confirm the canonical code is in
           :attr:`Terminal.supported_products`; 400 with
           ``product_not_supported`` otherwise.
        4. Evaluate :meth:`Terminal.is_open_at` at ``as_of`` (default
           now). When the terminal is closed, return 400 with
           ``terminal_closed`` and a computed ``next_open_window``
           (or ``null`` when no window lands within the next 7 days —
           typical for a terminal that has been inactivated).
        5. Otherwise return 200 with the canonicalized product_code so
           the caller can pin the exact code the load would commit.

    Validates: Requirement 8.1.4.
    """

    terminal = await _ensure_terminal_owned(tenant.tenant_id, terminal_id)

    try:
        canonical_product = canonicalize(body.product_code)
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "unknown_product_code",
                "message": "Unknown fuel product code.",
                "fuel_product_code": exc.code_or_alias,
            },
        )

    if canonical_product not in terminal.supported_products:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "product_not_supported",
                "message": (
                    "Terminal does not load the requested product."
                ),
                "terminal_id": terminal_id,
                "product_code": canonical_product,
                "supported_products": list(terminal.supported_products),
            },
        )

    as_of = body.as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    if not terminal.is_open_at(as_of):
        next_window = _compute_next_open_window(terminal, as_of)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "terminal_closed",
                "message": (
                    "Terminal is closed at the requested time."
                ),
                "terminal_id": terminal_id,
                "as_of": as_of.isoformat(),
                "next_open_window": (
                    next_window.model_dump(mode="json")
                    if next_window is not None
                    else None
                ),
            },
        )

    logger.info(
        "fuel_ops.terminals.proposed_load: tenant=%s terminal=%s product=%s "
        "volume=%.2f allowed=True",
        tenant.tenant_id,
        terminal_id,
        canonical_product,
        float(body.volume_gallons),
    )
    return ProposedLoadResponse(
        allowed=True,
        terminal_id=terminal_id,
        product_code=canonical_product,
        volume_gallons=float(body.volume_gallons),
        as_of=as_of,
    )


# ---------------------------------------------------------------------------
# Terminal_Wait endpoints (Task 7.7 — Req 8.4.2, 8.4.4)
# ---------------------------------------------------------------------------


#: Width of the rolling-window for Req 8.4.4. The summary endpoint
#: aggregates every :class:`TerminalWaitReport` whose ``observed_at``
#: falls within the trailing ``now - window`` timerange.
WAIT_SUMMARY_WINDOW: timedelta = timedelta(hours=2)

#: Redis TTL for the cached wait-summary payload. Set a bit longer than
#: the rolling window so a stale cache entry is still fresher than an
#: empty one — the summary endpoint refreshes the Redis key on every
#: successful read so the TTL only matters for idle terminals.
TERMINAL_WAIT_CACHE_TTL_SECONDS: int = 60 * 15  # 15 minutes

#: Redis key template mandated by the Task 7.7 brief.
TERMINAL_WAIT_CACHE_KEY_TEMPLATE: str = "terminal_wait:{tenant_id}:{terminal_id}"

#: Redis key template for the tenant-configurable wait-warning threshold
#: (Req 8.4.5). Task 7.7 surfaces the current threshold on the summary
#: response alongside the ``wait_warning_exceeded`` boolean so the
#: dispatcher UI can render a "wait warning" badge without a second
#: request. The Sourcing_Recommender (Task 7.9 / 7.11) uses the same
#: key so the two surfaces stay in lock-step.
TERMINAL_WAIT_WARNING_REDIS_KEY: str = "terminal_wait_warning_minutes:{tenant_id}"

#: Default wait-warning threshold when the tenant has no
#: ``terminal_wait_warning_minutes:{tenant_id}`` key configured
#: (Req 8.4.5). Mirrors the default in
#: :mod:`fuel.services.sourcing_recommender` so both surfaces agree.
DEFAULT_TERMINAL_WAIT_WARNING_MINUTES: float = 60.0


def _terminal_wait_cache_key(tenant_id: str, terminal_id: str) -> str:
    """Return the canonical Redis key for a terminal's wait-summary cache.

    Separated out so tests and the Sourcing_Recommender agree on the
    same key layout — never build this string ad-hoc in other modules.
    """

    return TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
        tenant_id=tenant_id, terminal_id=terminal_id
    )


class TerminalWaitReportCreateRequest(BaseModel):
    """Body for ``POST /api/fuel/terminals/{terminal_id}/wait-reports``.

    Mirrors :class:`fuel.terminal_models.TerminalWaitReport` but omits
    repository-managed fields (``report_id``, ``tenant_id``,
    ``terminal_id``, ``retrieved_at``, ``created_at``, ``updated_at``)
    and defaults ``source`` to ``driver_report`` — by far the most
    common submission path. Dispatcher submissions supply
    ``source=manual`` implicitly by still using ``driver_report`` with
    their own ``reporter_id`` so downstream analytics can weight the
    observation the same way.

    ``observed_at`` is optional; when omitted the server stamps it with
    ``now()`` so an offline driver app that posts on reconnection still
    produces a usable timestamp. Callers who supply their own
    ``observed_at`` (mobile-app offline queue, ELD replay) get that
    timestamp preserved verbatim.
    """

    model_config = ConfigDict(extra="forbid")

    wait_minutes: float = Field(
        ...,
        ge=0,
        description=(
            "Observed wait time in minutes. Must be non-negative. "
            "Upper bound is intentionally open-ended because a truly "
            "jammed terminal can easily exceed 120 minutes."
        ),
    )
    source: WaitReportSource = Field(
        default="driver_report",
        description=(
            "Where the observation came from. Defaults to driver_report; "
            "dispatchers submitting on a driver's behalf should still "
            "supply driver_report with their own reporter_id so the "
            "Sourcing_Recommender treats it as first-hand data."
        ),
    )
    reporter_id: Optional[str] = Field(
        default=None,
        description=(
            "User id of the submitter. Required when ``source`` is "
            "``driver_report`` (the model validator enforces this)."
        ),
    )
    truck_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional truck id for ELD-derived reports. Present on "
            "eld_geofence submissions so we can attribute a geofence "
            "dwell to a specific truck for later diagnosis."
        ),
    )
    observed_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Client-observed timestamp (driver wall-clock or geofence "
            "exit). Omit to have the server stamp it with now()."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=1000,
        description=(
            "Optional free-form note from the dispatcher or driver "
            "explaining the observation. Persisted verbatim after "
            "whitespace-stripping (the model coerces empty strings to "
            "None)."
        ),
    )


class TerminalWaitSummaryResponse(BaseModel):
    """Envelope for ``GET /api/fuel/terminals/{terminal_id}/wait-summary``.

    Mirrors the fields the Sourcing_Recommender (Task 7.9) consumes from
    the cached Redis payload. ``sample_count`` lets the consumer fall
    back to a default when too few observations exist in the window —
    Req 8.4.4 treats an absence of observations as "wait unknown" rather
    than "wait is zero".
    """

    model_config = ConfigDict(extra="forbid")

    terminal_id: str
    tenant_id: str
    window_minutes: int = Field(
        ...,
        description="Width of the rolling window in minutes (defaults to 120).",
    )
    avg_wait_minutes: float = Field(
        ...,
        ge=0,
        description=(
            "Rolling mean of ``wait_minutes`` over the window. ``0.0`` "
            "when no samples are available — consumers should check "
            "``sample_count`` before using this value for scoring."
        ),
    )
    sample_count: int = Field(
        ..., ge=0, description="Number of reports aggregated into the mean."
    )
    max_wait_minutes: Optional[float] = Field(
        default=None,
        description="Maximum wait observed in the window, or null when empty.",
    )
    most_recent_report_at: Optional[datetime] = Field(
        default=None,
        description=(
            "``observed_at`` of the newest report in the window, or null "
            "when empty. Surfaced so the dispatcher UI can show 'last "
            "seen N minutes ago' without a second request."
        ),
    )
    wait_warning_threshold_minutes: float = Field(
        ...,
        ge=0,
        description=(
            "Tenant-configured wait-warning threshold read from Redis "
            "key ``terminal_wait_warning_minutes:{tenant_id}`` with the "
            "shipped default of 60 minutes (Req 8.4.5)."
        ),
    )
    wait_warning_exceeded: bool = Field(
        ...,
        description=(
            "``True`` when ``avg_wait_minutes`` is strictly above "
            "``wait_warning_threshold_minutes`` and ``sample_count > 0``. "
            "An empty window never trips the warning — we treat an "
            "absence of observations as 'unknown' rather than 'severe'."
        ),
    )
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    source: Literal["cache", "computed"] = Field(
        ...,
        description=(
            "Whether the payload came from the Redis cache (``cache``) "
            "or was just recomputed from the ``terminal_wait_reports`` "
            "index (``computed``). Debugging field — clients can safely "
            "ignore it but we surface it so the Sourcing_Recommender "
            "can pin a specific provenance in its audit trail."
        ),
    )


def _translate_terminal_cross_tenant_error(
    exc: TerminalCrossTenantAccessError,
) -> HTTPException:
    """Map :class:`fuel.terminal_models.CrossTenantAccessError` to HTTP 403.

    We surface a generic ``cross_tenant_access_denied`` reason code so
    the caller cannot discover what kind of entity triggered the check
    (terminal vs wait-report vs contract) — the endpoint path already
    tells them that.
    """

    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error_code": "cross_tenant_access_denied",
            "message": "Entity belongs to a different tenant.",
            "entity_type": exc.entity_type,
            "entity_id": exc.entity_id,
        },
    )


async def _ensure_terminal_owned(
    tenant_id: str, terminal_id: str
) -> Terminal:
    """Fetch the terminal and 404 when missing or cross-tenant.

    :class:`TerminalRepository.get` already degrades cross-tenant reads
    to ``None`` so the same code path covers both "doesn't exist" and
    "belongs to another tenant" without leaking existence. Callers that
    need a distinct 403 for cross-tenant access should use the
    repository's ``update`` / ``delete`` paths, which raise
    :class:`CrossTenantAccessError` instead.
    """

    repo = _get_terminal_repository()
    terminal = await repo.get(tenant_id, terminal_id)
    if terminal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "terminal_not_found",
                "terminal_id": terminal_id,
            },
        )
    return terminal


async def _compute_wait_summary(
    *,
    tenant_id: str,
    terminal_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Aggregate the rolling 2-hour wait average from ``terminal_wait_reports``.

    The helper queries the repository for every report with
    ``observed_at >= now - WAIT_SUMMARY_WINDOW`` and returns the mean,
    max, and sample count. Failures inside the repository propagate as
    ``RuntimeError`` so the endpoint can map them to HTTP 500; the
    helper itself never catches persistence errors (that would mask
    real outages).

    Returns a dict rather than a Pydantic model because the same shape
    is both (a) serialized into the Redis cache payload and (b) used to
    populate the :class:`TerminalWaitSummaryResponse`. Keeping it as a
    dict lets callers avoid the round-trip through the model when
    caching.
    """

    repo = _get_terminal_wait_report_repository()
    now_dt = now or datetime.now(timezone.utc)
    window_start = now_dt - WAIT_SUMMARY_WINDOW

    reports = await repo.list_for_tenant(
        tenant_id,
        terminal_id=terminal_id,
        observed_since=window_start,
        size=500,  # Generous cap; terminals rarely exceed ~20/hour.
    )

    # Belt-and-braces filter: exclude any report whose ``observed_at``
    # drifted outside the window between the query time and now
    # (e.g. the caller passed ``now`` but the DB is still returning
    # slightly older rows). This guards against time-skew corner
    # cases that would otherwise inflate ``sample_count``.
    filtered = [
        r for r in reports
        if r.observed_at >= window_start and r.observed_at <= now_dt
    ]

    sample_count = len(filtered)
    if sample_count == 0:
        avg_wait = 0.0
        max_wait: Optional[float] = None
        most_recent_iso: Optional[str] = None
    else:
        total_wait = sum(float(r.wait_minutes) for r in filtered)
        avg_wait = total_wait / sample_count
        max_wait = max(float(r.wait_minutes) for r in filtered)
        # ``observed_at`` is datetime on the model; isoformat keeps the
        # payload JSON-friendly when this dict is serialized into Redis.
        most_recent = max(filtered, key=lambda r: r.observed_at).observed_at
        most_recent_iso = most_recent.isoformat()

    return {
        "terminal_id": terminal_id,
        "tenant_id": tenant_id,
        "window_minutes": int(WAIT_SUMMARY_WINDOW.total_seconds() // 60),
        "avg_wait_minutes": round(avg_wait, 2),
        "sample_count": sample_count,
        "max_wait_minutes": None if max_wait is None else round(max_wait, 2),
        "most_recent_report_at": most_recent_iso,
        "window_start": window_start.isoformat(),
        "window_end": now_dt.isoformat(),
        "generated_at": now_dt.isoformat(),
    }


async def _load_wait_summary_from_cache(
    tenant_id: str, terminal_id: str
) -> Optional[Dict[str, Any]]:
    """Return a cached wait-summary payload, or ``None`` on miss.

    Any Redis failure is logged and downgraded to a cache miss so a
    flaky Redis cluster never blocks the wait-summary endpoint.
    """

    redis = _get_redis_client()
    if redis is None:
        return None
    key = _terminal_wait_cache_key(tenant_id, terminal_id)
    try:
        raw = await redis.get(key)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "fuel_ops.wait_summary: cache get failed tenant=%s terminal=%s: %s",
            tenant_id,
            terminal_id,
            exc,
        )
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "fuel_ops.wait_summary: cache decode failed tenant=%s terminal=%s: %s",
            tenant_id,
            terminal_id,
            exc,
        )
        return None
    if not isinstance(payload, dict):
        return None
    # Defense-in-depth: refuse to trust a cache entry whose
    # ``tenant_id`` / ``terminal_id`` doesn't match. A Redis with
    # coincident keys across tenants would otherwise leak one tenant's
    # average to another.
    if (
        payload.get("tenant_id") != tenant_id
        or payload.get("terminal_id") != terminal_id
    ):
        logger.warning(
            "fuel_ops.wait_summary: dropped cache with mismatched identity "
            "tenant=%s terminal=%s",
            tenant_id,
            terminal_id,
        )
        return None
    return payload


async def _store_wait_summary_in_cache(
    tenant_id: str, terminal_id: str, payload: Dict[str, Any]
) -> None:
    """Best-effort cache write. Never raises.

    The TTL is long enough that repeated reads of a quiet terminal
    still hit the cache; the write on every read keeps the cached
    value in sync with the latest observations without a separate
    background refresher.
    """

    redis = _get_redis_client()
    if redis is None:
        return
    key = _terminal_wait_cache_key(tenant_id, terminal_id)
    try:
        encoded = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "fuel_ops.wait_summary: cache encode failed tenant=%s terminal=%s: %s",
            tenant_id,
            terminal_id,
            exc,
        )
        return
    try:
        await redis.setex(key, TERMINAL_WAIT_CACHE_TTL_SECONDS, encoded)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "fuel_ops.wait_summary: cache set failed tenant=%s terminal=%s: %s",
            tenant_id,
            terminal_id,
            exc,
        )


async def _invalidate_wait_summary_cache(
    tenant_id: str, terminal_id: str
) -> None:
    """Drop the cached summary so the next read recomputes from ES.

    Called after a successful ``POST /wait-reports`` so the new
    observation is immediately reflected in the next wait-summary read
    rather than having to wait for the TTL to expire.
    """

    redis = _get_redis_client()
    if redis is None:
        return
    key = _terminal_wait_cache_key(tenant_id, terminal_id)
    try:
        await redis.delete(key)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "fuel_ops.wait_summary: cache invalidate failed tenant=%s terminal=%s: %s",
            tenant_id,
            terminal_id,
            exc,
        )


async def _load_wait_warning_threshold_minutes(tenant_id: str) -> float:
    """Resolve the tenant's ``terminal_wait_warning_minutes`` threshold.

    Redis key ``terminal_wait_warning_minutes:{tenant_id}`` — set by the
    tenant-admin config surface (Req 8.4.5). Falls back to
    :data:`DEFAULT_TERMINAL_WAIT_WARNING_MINUTES` (60) when the key is
    absent or malformed. Any Redis failure degrades to the default so
    a flaky Redis cluster never breaks the wait-summary read path.
    """

    redis = _get_redis_client()
    if redis is None:
        return DEFAULT_TERMINAL_WAIT_WARNING_MINUTES
    key = TERMINAL_WAIT_WARNING_REDIS_KEY.format(tenant_id=tenant_id)
    try:
        raw = await redis.get(key)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "fuel_ops.wait_summary: warning-threshold read failed tenant=%s: %s",
            tenant_id,
            exc,
        )
        return DEFAULT_TERMINAL_WAIT_WARNING_MINUTES
    if raw is None:
        return DEFAULT_TERMINAL_WAIT_WARNING_MINUTES
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning(
                "fuel_ops.wait_summary: undecodable warning-threshold for tenant=%s",
                tenant_id,
            )
            return DEFAULT_TERMINAL_WAIT_WARNING_MINUTES
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "fuel_ops.wait_summary: non-numeric warning-threshold %r for tenant=%s",
            raw,
            tenant_id,
        )
        return DEFAULT_TERMINAL_WAIT_WARNING_MINUTES
    if value < 0:
        logger.warning(
            "fuel_ops.wait_summary: negative warning-threshold %.2f for tenant=%s; using default",
            value,
            tenant_id,
        )
        return DEFAULT_TERMINAL_WAIT_WARNING_MINUTES
    return value


def _compute_wait_warning_fields(
    *, avg_wait_minutes: float, sample_count: int, threshold_minutes: float
) -> Dict[str, Any]:
    """Derive the ``wait_warning_*`` fields surfaced on the summary.

    Centralized so the cache-hit path and the compute path produce
    identical output. An empty window (``sample_count == 0``) never
    trips the warning — treating "no data" as "severe wait" would push
    quiet terminals out of the Sourcing_Recommender ranking for the
    wrong reason.
    """

    exceeded = sample_count > 0 and avg_wait_minutes > threshold_minutes
    return {
        "wait_warning_threshold_minutes": round(float(threshold_minutes), 2),
        "wait_warning_exceeded": bool(exceeded),
    }


@router.post(
    "/terminals/{terminal_id}/wait-reports",
    response_model=TerminalWaitReport,
    status_code=status.HTTP_201_CREATED,
)
async def submit_terminal_wait_report(
    terminal_id: str,
    body: TerminalWaitReportCreateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> TerminalWaitReport:
    """Persist a driver or dispatcher wait-time observation.

    Flow:

        1. Verify the terminal exists and is owned by the tenant. Missing
           or cross-tenant terminals return HTTP 404 with
           ``terminal_not_found`` so existence is never leaked.
        2. Stamp ``observed_at`` with ``now()`` when the caller did not
           supply it, and populate ``retrieved_at`` inside the
           repository so the server is always the source of truth for
           when the platform learned about the observation.
        3. Persist a :class:`TerminalWaitReport` through
           :meth:`TerminalWaitReportRepository.create`, which mints the
           ``report_id`` (``twr_<uuid4>``) and stamps ``tenant_id`` from
           the JWT context so the caller cannot spoof ownership.
        4. Invalidate the Redis cache at
           ``terminal_wait:{tenant_id}:{terminal_id}`` so the very next
           wait-summary read reflects the new observation.

    Validates: Requirement 8.4.2.
    """

    await _ensure_terminal_owned(tenant.tenant_id, terminal_id)
    repo = _get_terminal_wait_report_repository()

    observed_at = body.observed_at or datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "tenant_id": tenant.tenant_id,
        "terminal_id": terminal_id,
        "wait_minutes": float(body.wait_minutes),
        "source": body.source,
        "reporter_id": body.reporter_id or tenant.user_id,
        "truck_id": body.truck_id,
        "observed_at": observed_at,
        "notes": body.notes,
    }

    try:
        report = await repo.create(tenant.tenant_id, payload)
    except TerminalCrossTenantAccessError as exc:
        # Shouldn't happen — we stamped tenant_id ourselves — but the
        # repository is defensive and we match.
        raise _translate_terminal_cross_tenant_error(exc)
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "fuel_ops.wait_reports.create: persistence failed tenant=%s terminal=%s",
            tenant.tenant_id,
            terminal_id,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    # Invalidate the cached rolling average so the next summary read
    # picks up this observation immediately. Fire-and-forget — a Redis
    # outage must never fail a wait-report submission.
    await _invalidate_wait_summary_cache(tenant.tenant_id, terminal_id)

    logger.info(
        "fuel_ops.wait_reports.create: tenant=%s terminal=%s report=%s source=%s wait=%.2f",
        tenant.tenant_id,
        terminal_id,
        report.report_id,
        report.source,
        report.wait_minutes,
    )
    return report


@router.get(
    "/terminals/{terminal_id}/wait-summary",
    response_model=TerminalWaitSummaryResponse,
)
async def get_terminal_wait_summary(
    terminal_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> TerminalWaitSummaryResponse:
    """Return the rolling 2-hour mean ``wait_minutes`` for the terminal.

    Flow:

        1. Verify tenant ownership of the terminal (404 on miss).
        2. Resolve the tenant-configured warning threshold from Redis
           key ``terminal_wait_warning_minutes:{tenant_id}`` (default
           60).
        3. Try a Redis cache hit at
           ``terminal_wait:{tenant_id}:{terminal_id}``. When present and
           well-formed, return it directly with ``source=cache`` — the
           warning fields are recomputed on every read so a threshold
           change is reflected immediately without waiting for the TTL.
        4. On miss, aggregate the trailing 2-hour window from the
           ``terminal_wait_reports`` index via
           :func:`_compute_wait_summary`, cache the result, and return
           with ``source=computed``.

    The response payload includes:

        * ``avg_wait_minutes`` — rolling mean (0 when empty).
        * ``sample_count`` — number of reports in the window.
        * ``most_recent_report_at`` — newest ``observed_at`` in the
          window, or null when empty.
        * ``wait_warning_threshold_minutes`` — the current tenant
          threshold so the UI never has to read Redis directly.
        * ``wait_warning_exceeded`` — ``True`` when the mean strictly
          exceeds the threshold and at least one sample exists
          (Req 8.4.5).

    Tenant isolation is enforced at three points for defense-in-depth:
    the terminal-ownership check, the repository's tenant filter on the
    aggregation query, and an identity re-check on the cached payload.

    Validates: Requirements 8.4.4, 8.4.5.
    """

    await _ensure_terminal_owned(tenant.tenant_id, terminal_id)

    threshold = await _load_wait_warning_threshold_minutes(tenant.tenant_id)

    cached = await _load_wait_summary_from_cache(tenant.tenant_id, terminal_id)
    if cached is not None:
        logger.debug(
            "fuel_ops.wait_summary: cache hit tenant=%s terminal=%s",
            tenant.tenant_id,
            terminal_id,
        )
        # Recompute warning fields on every read using the current
        # threshold — we never cache the warning booleans themselves so
        # a threshold change is reflected immediately without waiting
        # for the TTL to expire.
        warning_fields = _compute_wait_warning_fields(
            avg_wait_minutes=float(cached.get("avg_wait_minutes", 0.0)),
            sample_count=int(cached.get("sample_count", 0)),
            threshold_minutes=threshold,
        )
        merged = {
            k: v
            for k, v in cached.items()
            if k
            not in (
                "source",
                "wait_warning_threshold_minutes",
                "wait_warning_exceeded",
            )
        }
        merged.setdefault("most_recent_report_at", None)
        return TerminalWaitSummaryResponse(
            source="cache",
            **merged,
            **warning_fields,
        )

    try:
        summary = await _compute_wait_summary(
            tenant_id=tenant.tenant_id, terminal_id=terminal_id
        )
    except Exception as exc:
        logger.exception(
            "fuel_ops.wait_summary: aggregation failed tenant=%s terminal=%s",
            tenant.tenant_id,
            terminal_id,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    # Write-through cache on the compute path so the next read is O(1).
    # The cached payload is the raw aggregation (no warning booleans)
    # so a threshold change applied after the cache is populated still
    # produces the correct warning flag on the next read.
    await _store_wait_summary_in_cache(
        tenant.tenant_id, terminal_id, {**summary, "source": "cache"}
    )

    warning_fields = _compute_wait_warning_fields(
        avg_wait_minutes=float(summary.get("avg_wait_minutes", 0.0)),
        sample_count=int(summary.get("sample_count", 0)),
        threshold_minutes=threshold,
    )

    logger.debug(
        "fuel_ops.wait_summary: computed tenant=%s terminal=%s samples=%d avg=%.2f warn=%s",
        tenant.tenant_id,
        terminal_id,
        summary["sample_count"],
        summary["avg_wait_minutes"],
        warning_fields["wait_warning_exceeded"],
    )
    return TerminalWaitSummaryResponse(
        source="computed", **summary, **warning_fields
    )


# ---------------------------------------------------------------------------
# GET /api/fuel/sourcing/recommendations (Task 7.10 — Req 8.4.5, 8.5.4, 8.5.5)
# ---------------------------------------------------------------------------
#
# Request-response surface for the Sourcing_Recommender. The handler:
#
#   1. Validates query params (canonicalizes aliases, coerces as_of to a
#      UTC datetime, parses the optional ``terminal_ids`` CSV filter).
#   2. Invokes the already-wired
#      :class:`fuel.services.sourcing_recommender.SourcingRecommender`
#      (Task 7.9) which handles disqualification, rack-price fetch,
#      wait lookup, scoring, and ranking.
#   3. Persists the :class:`SourcingRecommendation` to the
#      ``sourcing_recommendations`` ES index for audit (Req 8.5.4).
#   4. Emits a ``sourcing_recommendation_ready`` event on
#      ``/ws/fuel-planning`` (Req 8.5.4 / Task 7.10) with a compact
#      top-pick summary so dispatcher UIs can render the recommendation
#      without an immediate follow-up fetch.
#   5. Returns the persisted SourcingRecommendation as the response.
#
# Tenant isolation comes exclusively from the JWT-derived
# :class:`TenantContext`; query-param or header tenant_ids are ignored.
#
# Validates: Requirements 8.4.5, 8.5.4, 8.5.5.


def _parse_terminal_ids_filter(raw: Optional[str]) -> Optional[List[str]]:
    """Parse the optional ``terminal_ids`` CSV filter.

    Returns ``None`` when the filter is unset (no restriction),
    a list of stripped non-empty terminal ids otherwise. Duplicate ids
    are collapsed preserving insertion order so the recommender sees
    each terminal at most once — the ES repository would deduplicate
    anyway but doing it here keeps the audit record compact.
    """

    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    seen: set[str] = set()
    out: List[str] = []
    for entry in stripped.split(","):
        tid = entry.strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
    return out or None


def _parse_sourcing_as_of(raw: Optional[str]) -> datetime:
    """Parse the optional ``as_of`` ISO-8601 query param.

    Returns the current UTC time when ``raw`` is ``None`` or blank so
    callers can omit it entirely. Accepts both naive and tz-aware ISO
    strings; naive strings are assumed UTC. Invalid values surface as
    HTTP 422 via Pydantic-style errors rather than a silent fallback so
    a fat-finger does not produce a stale recommendation.
    """

    if raw is None or not raw.strip():
        return datetime.now(timezone.utc)
    try:
        # ``fromisoformat`` accepts ``...Z`` terminators in Py3.11+.
        candidate = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "invalid_as_of",
                "message": f"as_of must be an ISO-8601 timestamp: {exc}",
            },
        )
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    return candidate.astimezone(timezone.utc)


@router.get(
    "/sourcing/recommendations",
    response_model=SourcingRecommendation,
)
async def get_sourcing_recommendations(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    product_code: str = Query(
        ...,
        min_length=1,
        description=(
            "Canonical product_code (e.g. ``DIESEL_2``, ``PROPANE``) or "
            "legacy alias (``AGO``, ``PMS``, ``ATK``, ``LPG``). Aliases "
            "are canonicalized by the recommender before ranking."
        ),
    ),
    volume_gallons: float = Query(
        ...,
        gt=0,
        description="Load volume in gallons. Must be a positive finite number.",
    ),
    origin_lat: float = Query(
        ...,
        ge=-90.0,
        le=90.0,
        description="Origin latitude (WGS84, -90..90).",
    ),
    origin_lon: float = Query(
        ...,
        ge=-180.0,
        le=180.0,
        description="Origin longitude (WGS84, -180..180).",
    ),
    as_of: Optional[str] = Query(
        default=None,
        description=(
            "Optional ISO-8601 timestamp of the load. Defaults to now "
            "(UTC). Used to filter terminals closed at that time and to "
            "fetch rack prices for the matching 15-minute bucket."
        ),
    ),
    branded: Optional[bool] = Query(
        default=None,
        description=(
            "``true`` to require branded terminals, ``false`` to require "
            "unbranded. Omit to include both."
        ),
    ),
    truck_id: Optional[str] = Query(
        default=None,
        description="Optional truck id stamped on the audit record for traceability.",
    ),
    run_id: Optional[str] = Query(
        default=None,
        description="Optional pipeline run id stamped on the audit record for traceability.",
    ),
    terminal_ids: Optional[str] = Query(
        default=None,
        description=(
            "Optional comma-separated list of terminal ids to restrict "
            "the candidate slate. Used by the Route_Planning_Agent when "
            "a Loading_Plan already has a short-list of eligible "
            "terminals. Omit to consult every active terminal."
        ),
    ),
) -> SourcingRecommendation:
    """Rank loading terminals for a (product, volume, origin, as_of) query.

    Flow:

        1. Canonicalize inputs (product_code aliases, ISO as_of, CSV
           terminal_ids filter) and validate ranges.
        2. Invoke the already-wired
           :class:`fuel.services.sourcing_recommender.SourcingRecommender`
           to produce a :class:`SourcingRecommendation`.
        3. Persist the recommendation to the
           ``sourcing_recommendations`` ES index for audit (Req 8.5.4).
        4. Emit ``sourcing_recommendation_ready`` on
           ``/ws/fuel-planning`` (Req 8.5.4) with a compact top-pick
           summary so dispatcher UIs can render the recommendation
           without a follow-up fetch.
        5. Return the persisted SourcingRecommendation.

    Tenant isolation is enforced at two points for defense-in-depth:
    the recommender's repositories filter on ``tenant_id`` and the audit
    repository re-validates ``tenant_id`` before persistence.

    Validates: Requirements 8.4.5, 8.5.4, 8.5.5.
    """

    recommender = _get_sourcing_recommender()
    repo = _get_sourcing_recommendation_repository()

    restrict_to = _parse_terminal_ids_filter(terminal_ids)
    effective_as_of = _parse_sourcing_as_of(as_of)

    truck_id_clean = truck_id.strip() if isinstance(truck_id, str) and truck_id.strip() else None
    run_id_clean = run_id.strip() if isinstance(run_id, str) and run_id.strip() else None

    try:
        recommendation = await recommender.recommend(
            tenant_id=tenant.tenant_id,
            product_code=product_code,
            volume_gallons=float(volume_gallons),
            origin_lat_lon=(float(origin_lat), float(origin_lon)),
            as_of=effective_as_of,
            branded=branded,
            truck_id=truck_id_clean,
            run_id=run_id_clean,
            terminal_ids=restrict_to,
        )
    except InvalidBrandedPreferenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "invalid_branded_preference",
                "message": str(exc),
            },
        )
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "unknown_product_code",
                "product_code": product_code,
                "message": str(exc),
            },
        )
    except ValueError as exc:
        # The recommender validates ranges defensively; surface the
        # underlying message so developers see which constraint tripped.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "invalid_sourcing_request",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.exception(
            "fuel_ops.sourcing: recommender failed for tenant=%s product=%s: %s",
            tenant.tenant_id,
            product_code,
            exc,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    # Persist for audit (Req 8.5.4). Failures here must not prevent
    # returning the ranking to the caller — the persistence path is
    # advisory-only. Fall back to the already-ranked in-memory record
    # and log the persistence failure so ops can replay later.
    persisted: SourcingRecommendation = recommendation
    try:
        persisted = await repo.create(tenant.tenant_id, recommendation)
    except Exception as exc:
        logger.exception(
            "fuel_ops.sourcing: failed to persist sourcing_recommendations "
            "recommendation_id=%s tenant=%s: %s",
            recommendation.recommendation_id,
            tenant.tenant_id,
            exc,
        )

    # Emit WS event (Req 8.5.4). WS failures must never block the
    # response — we log and continue so HTTP callers always get the
    # ranked list back.
    if _fuel_planning_ws_manager is not None:
        try:
            candidates = persisted.candidates
            top_terminal_id = candidates[0].terminal_id if candidates else None
            top_score = candidates[0].score if candidates else None
            await _fuel_planning_ws_manager.broadcast_sourcing_recommendation_ready(
                recommendation_id=persisted.recommendation_id,
                request_id=persisted.request_id,
                tenant_id=tenant.tenant_id,
                product_code=persisted.product_code,
                volume_gallons=persisted.volume_gallons,
                candidate_count=len(candidates),
                top_terminal_id=top_terminal_id,
                top_score=top_score,
                rack_price_fallback=persisted.rack_price_fallback,
                wait_warning_terminal_ids=list(
                    persisted.wait_warning_terminal_ids
                ),
                truck_id=persisted.truck_id,
                run_id=persisted.run_id,
            )
        except Exception:  # pragma: no cover - never block on WS
            logger.exception(
                "fuel_ops.sourcing: WS broadcast failed recommendation=%s tenant=%s",
                persisted.recommendation_id,
                tenant.tenant_id,
            )

    logger.info(
        "fuel_ops.sourcing: tenant=%s product=%s volume=%.1f candidates=%d "
        "top_terminal=%s rack_fallback=%s recommendation_id=%s",
        tenant.tenant_id,
        persisted.product_code,
        persisted.volume_gallons,
        len(persisted.candidates),
        persisted.candidates[0].terminal_id if persisted.candidates else None,
        persisted.rack_price_fallback,
        persisted.recommendation_id,
    )
    return persisted


# ---------------------------------------------------------------------------
# Supplier_Contract request / response models (Task 7.6 — Req 8.3.2, 8.3.4)
# ---------------------------------------------------------------------------


class SupplierContractCreateRequest(BaseModel):
    """Body for ``POST /api/fuel/supplier-contracts`` (Req 8.3.2).

    Mirrors :class:`fuel.terminal_models.SupplierContract` but omits
    repository-managed fields (``tenant_id``, ``created_at``,
    ``updated_at``) and makes ``contract_id`` optional so the repository
    can mint one (``sc_<uuid4>``) when it isn't supplied by the caller.
    ``product_code`` is canonicalized inside :class:`SupplierContract`'s
    field validator so legacy Nigerian aliases (AGO, PMS, ATK, LPG)
    resolve to US product codes on write.
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional client-supplied identifier. When omitted the "
            "repository mints a uuid4-based id (``sc_<uuid4>``)."
        ),
    )
    supplier_name: str = Field(..., min_length=1)
    product_code: str = Field(..., min_length=1)
    preferred_terminal_ids: List[str] = Field(default_factory=list)
    contract_price_per_gallon_usd: Optional[float] = Field(default=None, ge=0)
    branded_required: bool = Field(default=False)
    minimum_lift_gallons_per_month: Optional[float] = Field(default=None, ge=0)
    rebate_terms: Optional[str] = None
    effective_from: Any = Field(
        ...,
        description="ISO date (YYYY-MM-DD) when the contract becomes active.",
    )
    effective_to: Optional[Any] = Field(
        default=None,
        description=(
            "Optional ISO date when the contract ends. Must be on or after "
            "``effective_from`` when supplied."
        ),
    )
    status: TerminalActiveStatus = "active"


class SupplierContractUpdateRequest(BaseModel):
    """Body for ``PATCH /api/fuel/supplier-contracts/{contract_id}`` (Req 8.3.2).

    Every field is optional so callers can send just the delta. The
    repository refuses to overwrite immutable fields (``contract_id``,
    ``tenant_id``, ``created_at``); those are not even exposed here so
    malicious or accidental payloads are rejected by the
    ``extra="forbid"`` policy before reaching the repository.
    """

    model_config = ConfigDict(extra="forbid")

    supplier_name: Optional[str] = Field(default=None, min_length=1)
    product_code: Optional[str] = Field(default=None, min_length=1)
    preferred_terminal_ids: Optional[List[str]] = None
    contract_price_per_gallon_usd: Optional[float] = Field(default=None, ge=0)
    branded_required: Optional[bool] = None
    minimum_lift_gallons_per_month: Optional[float] = Field(default=None, ge=0)
    rebate_terms: Optional[str] = None
    effective_from: Optional[Any] = None
    effective_to: Optional[Any] = None
    status: Optional[TerminalActiveStatus] = None


class SupplierContractLiftSummary(BaseModel):
    """Monthly rolling-lift summary embedded on a Supplier_Contract response.

    Surfaces the Redis counter described by Req 8.3.4 so the admin UI
    can render a "below minimum" warning without a second request.
    ``percent_of_minimum`` is ``None`` when the contract has no
    ``minimum_lift_gallons_per_month`` configured (so no progress bar is
    shown), else a non-negative float. ``below_minimum`` is ``True`` only
    when the contract has a positive minimum and the current-month
    counter has not yet reached it.
    """

    model_config = ConfigDict(extra="forbid")

    yyyy_mm: str = Field(
        ...,
        description="Current UTC-month bucket (``YYYY-MM``) the counter addresses.",
    )
    gallons_lifted_this_month: float = Field(..., ge=0.0)
    minimum_lift_gallons_per_month: Optional[float] = None
    percent_of_minimum: Optional[float] = None
    below_minimum: bool = False

    @classmethod
    def from_summary(cls, summary: ContractLiftSummary) -> "SupplierContractLiftSummary":
        return cls(
            yyyy_mm=summary.yyyy_mm,
            gallons_lifted_this_month=summary.gallons_lifted_this_month,
            minimum_lift_gallons_per_month=summary.minimum_lift_gallons_per_month,
            percent_of_minimum=summary.percent_of_minimum,
            below_minimum=summary.below_minimum,
        )


class SupplierContractResponse(BaseModel):
    """Single-record envelope for the Supplier_Contract CRUD endpoints.

    Embeds the contract record alongside the monthly rolling-lift
    counter (Req 8.3.4) so the admin UI can render both in a single
    request. When the contract has no ``minimum_lift_gallons_per_month``
    configured the counter is still populated — the UI simply omits the
    "% of minimum" progress bar.
    """

    model_config = ConfigDict(extra="forbid")

    contract: SupplierContract
    lift_summary: SupplierContractLiftSummary


class SupplierContractListResponse(BaseModel):
    """Envelope for ``GET /api/fuel/supplier-contracts``.

    Mirrors the ``items`` / ``total`` / ``page`` / ``page_size`` /
    ``has_next`` shape used by the other fuel-ops list endpoints.
    Each item carries both the contract and its lift summary so the
    admin UI can flag contracts trending below their minimum lift
    without a second round-trip per row.
    """

    model_config = ConfigDict(extra="forbid")

    items: List[SupplierContractResponse]
    total: int
    page: int
    page_size: int
    has_next: bool


def _translate_supplier_contract_cross_tenant_error(
    exc: TerminalCrossTenantAccessError,
) -> HTTPException:
    """Map :class:`fuel.terminal_models.CrossTenantAccessError` to HTTP 403.

    The owning tenant is deliberately not echoed back to the caller —
    ``contract_id`` is the only identifier needed to reconcile the
    response with the request, and leaking the owner would expose cross-
    tenant metadata.
    """

    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error_code": "cross_tenant_access_denied",
            "message": "Supplier contract belongs to a different tenant.",
            "contract_id": exc.entity_id,
        },
    )


async def _build_contract_response(
    contract: SupplierContract,
) -> SupplierContractResponse:
    """Assemble the admin-UI-ready envelope for a single Supplier_Contract.

    Looks up the current-month lift counter via
    :class:`ContractLiftService`. A Redis outage degrades to zero so an
    ES fetch for the contract never fails because of a counter glitch.
    """

    service = _get_contract_lift_service()
    summary = await service.get_summary(
        tenant_id=contract.tenant_id,
        contract_id=contract.contract_id,
        minimum_lift_gallons_per_month=contract.minimum_lift_gallons_per_month,
    )
    return SupplierContractResponse(
        contract=contract,
        lift_summary=SupplierContractLiftSummary.from_summary(summary),
    )


# ---------------------------------------------------------------------------
# GET /api/fuel/supplier-contracts (Req 8.3.2, Task 7.6)
# ---------------------------------------------------------------------------


@router.get(
    "/supplier-contracts",
    response_model=SupplierContractListResponse,
)
async def list_supplier_contracts(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    status_filter: Optional[TerminalActiveStatus] = Query(
        default=None,
        alias="status",
        description="Restrict to a single status (active | inactive).",
    ),
    supplier_name: Optional[str] = Query(
        default=None,
        description="Exact-match filter on ``supplier_name``.",
    ),
    product_code: Optional[str] = Query(
        default=None,
        description=(
            "Filter by product. Accepts the canonical product_code "
            "(e.g. DIESEL_2) or a legacy alias (e.g. AGO); aliases are "
            "resolved via the fuel product catalog before the ES query "
            "is issued."
        ),
    ),
    preferred_terminal_id: Optional[str] = Query(
        default=None,
        description="Restrict to contracts referencing this terminal_id.",
    ),
    page: int = Query(1, ge=1, description="Page number, 1-indexed."),
    size: int = Query(20, ge=1, le=500, description="Page size (1–500)."),
) -> SupplierContractListResponse:
    """Return the paginated list of Supplier_Contracts for the tenant.

    Each row carries the monthly rolling-lift counter from Redis
    (Req 8.3.4) so the admin UI can flag contracts trending below their
    ``minimum_lift_gallons_per_month`` without a second request.

    Validates: Requirements 8.3.2, 8.3.4.
    """

    repo = _get_supplier_contract_repository()

    try:
        window = await repo.list_for_tenant(
            tenant_id=tenant.tenant_id,
            status=status_filter,
            supplier_name=supplier_name,
            product_code=product_code,
            preferred_terminal_id=preferred_terminal_id,
            size=page * size + 1,
        )
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "unknown_product_code",
                "message": "Unknown fuel product code.",
                "fuel_product_code": exc.code_or_alias,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    total = len(window)
    start = (page - 1) * size
    end = start + size
    page_items = window[start:end]
    has_next = len(window) > end

    # Materialize the lift summary per item. ``_build_contract_response``
    # tolerates a missing / dead Redis so a counter glitch never blocks
    # an admin-UI load.
    items = [await _build_contract_response(contract) for contract in page_items]

    logger.debug(
        "fuel_ops.supplier_contracts.list: tenant=%s page=%d size=%d "
        "total_window=%d returned=%d",
        tenant.tenant_id,
        page,
        size,
        total,
        len(items),
    )
    return SupplierContractListResponse(
        items=items,
        total=total,
        page=page,
        page_size=size,
        has_next=has_next,
    )


# ---------------------------------------------------------------------------
# GET /api/fuel/supplier-contracts/{contract_id} (Req 8.3.2, Task 7.6)
# ---------------------------------------------------------------------------


@router.get(
    "/supplier-contracts/{contract_id}",
    response_model=SupplierContractResponse,
)
async def get_supplier_contract(
    contract_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> SupplierContractResponse:
    """Return a single Supplier_Contract for the tenant.

    Cross-tenant and missing records surface as HTTP 404 so existence is
    never leaked across tenants.

    Validates: Requirement 8.3.2, 8.3.4.
    """

    repo = _get_supplier_contract_repository()

    try:
        contract = await repo.get(tenant.tenant_id, contract_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if contract is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "supplier_contract_not_found",
                "contract_id": contract_id,
            },
        )
    return await _build_contract_response(contract)


# ---------------------------------------------------------------------------
# POST /api/fuel/supplier-contracts (Req 8.3.2, Task 7.6)
# ---------------------------------------------------------------------------


@router.post(
    "/supplier-contracts",
    response_model=SupplierContractResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_supplier_contract(
    body: SupplierContractCreateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> SupplierContractResponse:
    """Create a new Supplier_Contract scoped to the requesting tenant.

    The router stamps ``tenant_id`` from the verified JWT context so the
    caller cannot spoof ownership. ``product_code`` is canonicalized
    inside :class:`SupplierContract`'s field validator, which surfaces
    :class:`UnknownFuelProductError` — we map that to a 400 with a
    structured ``unknown_product_code`` payload so clients can
    distinguish "bad product" from generic validation errors.

    Validates: Requirement 8.3.2.
    """

    _ensure_fuel_admin_role(tenant)

    repo = _get_supplier_contract_repository()

    payload: Dict[str, Any] = body.model_dump(exclude_none=True)
    payload["tenant_id"] = tenant.tenant_id

    try:
        contract = await repo.create(tenant.tenant_id, payload)
    except TerminalCrossTenantAccessError as exc:
        # Defensive — we stamped tenant_id ourselves, but the repo
        # guards against cross-tenant payloads regardless.
        raise _translate_supplier_contract_cross_tenant_error(exc)
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "unknown_product_code",
                "message": "Unknown fuel product code.",
                "fuel_product_code": exc.code_or_alias,
            },
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    logger.info(
        "fuel_ops.supplier_contracts.create: tenant=%s contract=%s "
        "product=%s supplier=%s",
        tenant.tenant_id,
        contract.contract_id,
        contract.product_code,
        contract.supplier_name,
    )
    return await _build_contract_response(contract)


# ---------------------------------------------------------------------------
# PATCH /api/fuel/supplier-contracts/{contract_id} (Req 8.3.2, Task 7.6)
# ---------------------------------------------------------------------------


@router.patch(
    "/supplier-contracts/{contract_id}",
    response_model=SupplierContractResponse,
)
async def update_supplier_contract(
    contract_id: str,
    body: SupplierContractUpdateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> SupplierContractResponse:
    """Apply a partial update to an owned Supplier_Contract.

    Returns 404 when the contract does not exist. Returns 403 when it
    belongs to another tenant. Returns 422 when the merged record would
    fail Pydantic validation (e.g. ``effective_to`` before
    ``effective_from``). Returns 400 when ``product_code`` resolves to
    an unknown catalog entry.

    Validates: Requirement 8.3.2.
    """

    _ensure_fuel_admin_role(tenant)

    repo = _get_supplier_contract_repository()

    patch = body.model_dump(exclude_none=True)
    if not patch:
        # An empty patch is a no-op — load-or-404 so clients get a
        # consistent response shape when they accidentally send {}.
        existing = await repo.get(tenant.tenant_id, contract_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "supplier_contract_not_found",
                    "contract_id": contract_id,
                },
            )
        return await _build_contract_response(existing)

    try:
        updated = await repo.update(
            tenant_id=tenant.tenant_id,
            contract_id=contract_id,
            patch=patch,
        )
    except TerminalCrossTenantAccessError as exc:
        raise _translate_supplier_contract_cross_tenant_error(exc)
    except UnknownFuelProductError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "unknown_product_code",
                "message": "Unknown fuel product code.",
                "fuel_product_code": exc.code_or_alias,
            },
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "supplier_contract_not_found",
                "contract_id": contract_id,
            },
        )

    logger.info(
        "fuel_ops.supplier_contracts.update: tenant=%s contract=%s fields=%s",
        tenant.tenant_id,
        contract_id,
        sorted(patch.keys()),
    )
    return await _build_contract_response(updated)


# ---------------------------------------------------------------------------
# DELETE /api/fuel/supplier-contracts/{contract_id} (Req 8.3.2, Task 7.6)
# ---------------------------------------------------------------------------


@router.delete(
    "/supplier-contracts/{contract_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_supplier_contract(
    contract_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> None:
    """Delete a Supplier_Contract owned by the tenant.

    * Owned + deleted → HTTP 204 (no body).
    * Not-found → HTTP 404 with structured ``supplier_contract_not_found``
      detail.
    * Cross-tenant → HTTP 403 with structured
      ``cross_tenant_access_denied`` detail.

    The monthly-lift counter in Redis is intentionally left in place
    after a delete — contract deletion is an admin-facing action and
    historical lift data is still useful for retrospective reports. The
    counter's 62-day TTL reclaims the key automatically.

    Validates: Requirement 8.3.2.
    """

    _ensure_fuel_admin_role(tenant)

    repo = _get_supplier_contract_repository()

    try:
        deleted = await repo.delete(tenant.tenant_id, contract_id)
    except TerminalCrossTenantAccessError as exc:
        raise _translate_supplier_contract_cross_tenant_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "supplier_contract_not_found",
                "contract_id": contract_id,
            },
        )

    logger.info(
        "fuel_ops.supplier_contracts.delete: tenant=%s contract=%s",
        tenant.tenant_id,
        contract_id,
    )
    return None


# ---------------------------------------------------------------------------
# POST /api/fuel/supplier-contracts/{contract_id}/deactivate (Task 8.1)
# ---------------------------------------------------------------------------


@router.post(
    "/supplier-contracts/{contract_id}/deactivate",
    response_model=SupplierContractResponse,
)
async def deactivate_supplier_contract(
    contract_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> SupplierContractResponse:
    """Deactivate a Supplier_Contract by flipping ``status`` to ``inactive``.

    Mirrors :func:`deactivate_terminal`: the thin admin management
    surface (Task 8.1) prefers a reversible deactivation over the hard
    ``DELETE`` so a contract still referenced by historical sourcing
    recommendations resolves through the ``<EntityLink>`` resolver
    (the reference stays "linked") while dropping out of the active
    picker. Deactivation is idempotent.

    * Owned → HTTP 200 with the updated contract (``status=inactive``).
    * Not-found / cross-tenant read → HTTP 404 ``supplier_contract_not_found``.
    * Cross-tenant write → HTTP 403 ``cross_tenant_access_denied``.
    * Non-admin caller → HTTP 403 ``INSUFFICIENT_ROLE``.

    Validates: Requirements 9.1, 9.2.
    """

    _ensure_fuel_admin_role(tenant)

    repo = _get_supplier_contract_repository()

    # Load-or-404 first so a missing/cross-tenant id never leaks existence
    # and an already-inactive contract short-circuits to an idempotent 200.
    existing = await repo.get(tenant.tenant_id, contract_id)
    if existing is None:
        raise supplier_contract_not_found(
            f"Supplier contract {contract_id} not found.",
            details={"contract_id": contract_id},
        )
    if existing.status == "inactive":
        return await _build_contract_response(existing)

    try:
        updated = await repo.update(
            tenant_id=tenant.tenant_id,
            contract_id=contract_id,
            patch={"status": "inactive"},
        )
    except TerminalCrossTenantAccessError as exc:
        raise _translate_supplier_contract_cross_tenant_error(exc)
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    if updated is None:
        raise supplier_contract_not_found(
            f"Supplier contract {contract_id} not found.",
            details={"contract_id": contract_id},
        )

    logger.info(
        "fuel_ops.supplier_contracts.deactivate: tenant=%s contract=%s",
        tenant.tenant_id,
        contract_id,
    )
    return await _build_contract_response(updated)


__all__ = [
    "router",
    "mvp_router",
    "configure_fuel_ops_endpoints",
    "FuelProductItem",
    "FuelProductsResponse",
    "DeliveryDestinationsResponse",
    "RackPriceListResponse",
    "CustomerTankCreateRequest",
    "CustomerTankUpdateRequest",
    "CustomerTankListResponse",
    "DepotCreateRequest",
    "DepotUpdateRequest",
    "DepotListResponse",
    "ReplanDiffResponse",
    "CleaningEventCreateRequest",
    "TruckCompartmentStateItem",
    "TruckCompartmentListResponse",
    "LoadEligibilityCompartmentState",
    "LoadEligibilityResponse",
    "PriorityClusterCentroid",
    "PriorityClusterItem",
    "PriorityClustersResponse",
    "PriorityListResponse",
    "CombinableGroupListResponse",
    "EmergencyStopRequest",
    "EmergencyStopResponse",
    "EMERGENCY_STOP_TOOL_MEDIUM",
    "EMERGENCY_STOP_TOOL_HIGH",
    "EMERGENCY_STOP_HIGH_RISK_SHIFT_THRESHOLD",
    "SupplierContractCreateRequest",
    "SupplierContractUpdateRequest",
    "SupplierContractResponse",
    "SupplierContractListResponse",
    "SupplierContractLiftSummary",
    "HashProofResponse",
    "HashChainVerifyRequest",
    "HashChainVerifyResponse",
    "HashChainMismatch",
    "HASH_CHAIN_MAX_LIMIT",
    "HASH_CHAIN_DEFAULT_LIMIT",
    "BOLDownloadResponse",
    "BOL_DOWNLOAD_PRESIGN_TTL_SECONDS",
    "TerminalWaitReportCreateRequest",
    "TerminalWaitSummaryResponse",
    "WAIT_SUMMARY_WINDOW",
    "TERMINAL_WAIT_CACHE_TTL_SECONDS",
    "TERMINAL_WAIT_CACHE_KEY_TEMPLATE",
    "ROUTER_AUTH_POLICY",
]


# ---------------------------------------------------------------------------
# POD Hash-Chain Proof & Verification (Task 8.11, Req 4.5.3–4.5.5)
# ---------------------------------------------------------------------------
#
# Two endpoints that surface the tamper-evident POD hash chain introduced in
# Task 8.9 (canonicalization + SHA-256 helpers) and consumed by Task 8.10
# (POD persistence writes ``pod_hash`` + ``previous_pod_hash``):
#
# * ``GET /api/fuel/pod/{pod_id}/hash-proof`` (Req 4.5.3) — returns the
#   stored ``pod_hash``, the stored ``previous_pod_hash``, and the canonical
#   JSON payload used to derive ``pod_hash``. Auditors fetch this proof
#   alongside the source POD and re-hash the canonical payload locally to
#   confirm the stored hash is consistent.
#
# * ``POST /api/fuel/pod/hash-chain/verify`` (Req 4.5.4–4.5.5) — walks a
#   caller-supplied set of pods in insertion order and reports the first
#   mismatch. Two selector modes are supported:
#
#       1. An explicit ``pod_ids: [...]`` list ordered newest-last (the
#          order the caller believes the chain was written in).
#       2. A range addressed by ``from_pod_id`` / ``to_pod_id`` plus an
#          optional ``limit``; the server loads the matching POD rows
#          scoped to the caller's tenant ordered by ``timestamp`` ASC.
#
#   For each POD in the ordered set the handler:
#     * re-computes ``pod_hash`` via :func:`compute_pod_hash` against the
#       stored POD document;
#     * checks that the re-computed hash equals the stored ``pod_hash``;
#     * checks that the stored ``previous_pod_hash`` equals the previous
#       POD's stored ``pod_hash`` in the verification window.
#   The first failure stops the walk and is reported as ``first_mismatch``
#   with a structured reason code, satisfying Req 4.5.5.
#
# The canonicalization function :func:`canonicalize_pod` is imported from
# :mod:`services.pod_hash_chain` so the on-the-wire canonical payload and
# the server-side hash use the exact same bytes — any change to the
# canonicalization rules automatically flows through both endpoints.
#
# Tenant isolation is enforced twice: once by the ES ``term`` filter on
# ``tenant_id`` and once by a defensive re-check after we read the
# document, so a corrupt mapping can't leak cross-tenant data.
#
# Validates: Requirements 4.5.3, 4.5.4, 4.5.5.

import json as _hash_chain_json

from driver.services.driver_es_mappings import PROOF_OF_DELIVERY_INDEX
from services.pod_hash_chain import (
    ZERO_HASH,
    canonicalize_pod,
    compute_pod_hash,
)


# Hard cap on a single verification request — keeps a misbehaving client
# from scanning the entire tenant's POD chain in one shot. The default
# range-mode pagination window returns ``HASH_CHAIN_DEFAULT_LIMIT`` rows
# unless the caller asks for fewer.
HASH_CHAIN_MAX_LIMIT: int = 500
HASH_CHAIN_DEFAULT_LIMIT: int = 100


class HashProofResponse(BaseModel):
    """Envelope for ``GET /api/fuel/pod/{pod_id}/hash-proof`` (Req 4.5.3).

    ``canonical_payload`` is the parsed JSON object that :func:`canonicalize_pod`
    serialized and hashed on write. Auditors can re-serialize it with
    ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` and verify
    ``sha256(...) == pod_hash`` locally — there is no additional
    normalization required on the client.

    ``canonical_payload_bytes`` is the raw UTF-8 canonical JSON string (the
    exact bytes that were hashed) so clients that prefer byte-level
    verification don't have to reconstruct the serialization themselves.
    """

    model_config = ConfigDict(extra="forbid")

    pod_id: str
    tenant_id: str
    pod_hash: str
    previous_pod_hash: str
    canonical_payload: Dict[str, Any]
    canonical_payload_bytes: str


class HashChainVerifyRequest(BaseModel):
    """Body for ``POST /api/fuel/pod/hash-chain/verify`` (Req 4.5.4).

    Two selector modes are supported. Only one may be supplied per request
    — the handler rejects requests that mix them with HTTP 400.

    * **Explicit list.** ``pod_ids`` is an ordered list of pod_ids (oldest
      first) that the caller expects the chain to cover. Use this to
      re-verify a known chain after a suspected tamper event, or to
      verify a specific subset identified by an external audit tool.

    * **Range.** ``from_pod_id`` and ``to_pod_id`` address a contiguous
      window of the tenant's chain. The handler loads the matching POD
      rows ordered by ``timestamp`` ASC with a cap of ``limit`` (or
      :data:`HASH_CHAIN_DEFAULT_LIMIT` when omitted). Both endpoints are
      inclusive.

    At minimum two PODs must be supplied to verify a chain link; a single
    POD request is still valid and only checks the stored ``pod_hash``
    against the re-computed hash (no ``previous_pod_hash`` edge to verify).
    """

    model_config = ConfigDict(extra="forbid")

    pod_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Explicit ordered list of pod_ids to verify. When provided, "
            "``from_pod_id``/``to_pod_id`` must be omitted."
        ),
    )
    from_pod_id: Optional[str] = Field(
        default=None,
        description=(
            "Inclusive start of the range to verify. Paired with "
            "``to_pod_id``. Cannot be combined with ``pod_ids``."
        ),
    )
    to_pod_id: Optional[str] = Field(
        default=None,
        description="Inclusive end of the range to verify.",
    )
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=HASH_CHAIN_MAX_LIMIT,
        description=(
            "Maximum number of PODs to walk in range mode "
            f"(1–{HASH_CHAIN_MAX_LIMIT}, default {HASH_CHAIN_DEFAULT_LIMIT})."
        ),
    )


class HashChainMismatch(BaseModel):
    """A single mismatch reported by ``/api/fuel/pod/hash-chain/verify``.

    ``reason`` is one of:

    * ``pod_not_found`` — the pod_id was requested but no document exists
      for it in the tenant's POD index.
    * ``missing_stored_hash`` — the POD document exists but has no
      ``pod_hash`` field (pre-Task 8.10 rows).
    * ``stored_hash_mismatch`` — the re-computed hash does not equal the
      stored ``pod_hash`` → the canonical fields were mutated.
    * ``previous_hash_mismatch`` — the stored ``previous_pod_hash`` does
      not equal the prior POD's stored ``pod_hash`` → the chain linkage
      was broken.
    """

    model_config = ConfigDict(extra="forbid")

    pod_id: str
    reason: Literal[
        "pod_not_found",
        "missing_stored_hash",
        "stored_hash_mismatch",
        "previous_hash_mismatch",
    ]
    expected_hash: Optional[str] = None
    stored_hash: Optional[str] = None
    computed_hash: Optional[str] = None
    message: str


class HashChainVerifyResponse(BaseModel):
    """Envelope for ``POST /api/fuel/pod/hash-chain/verify`` (Req 4.5.4).

    ``valid`` is ``True`` iff every POD in the verification window has a
    matching stored hash and a matching chain linkage; otherwise it is
    ``False`` and ``first_mismatch`` carries the first failure encountered
    (Req 4.5.5). ``verified_count`` is the number of PODs that passed
    verification up to and including the first mismatch (or the whole
    window when the chain is intact).
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    verified_count: int
    total_requested: int
    valid: bool
    first_mismatch: Optional[HashChainMismatch] = None
    pod_ids_checked: List[str]


async def _fetch_pod_by_id(
    es: Any, tenant_id: str, pod_id: str
) -> Optional[Dict[str, Any]]:
    """Return the POD document for ``pod_id`` scoped to ``tenant_id``, or None.

    Uses ``search_documents`` rather than ``get_document`` for two reasons:
    1) the tenant filter is applied inside ES (so a cross-tenant row is
       invisible even under a corrupt mapping), and 2) the test doubles in
       this project mock ``search_documents`` uniformly across endpoints.
    """

    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"pod_id": pod_id}},
                    {"term": {"tenant_id": tenant_id}},
                ]
            }
        },
        "size": 1,
    }
    try:
        resp = await es.search_documents(PROOF_OF_DELIVERY_INDEX, query, 1)
    except Exception as exc:
        logger.exception(
            "fuel_ops.hash_proof: ES lookup failed for pod=%s tenant=%s",
            pod_id,
            tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error_code": "pod_store_unavailable",
                "message": "Proof-of-delivery store is unavailable.",
            },
        ) from exc

    hits = (resp or {}).get("hits", {}).get("hits", [])
    if not hits:
        return None
    source = hits[0].get("_source") or {}
    # Defensive re-check — the ES filter already enforced tenant_id but a
    # misconfigured mapping could still leak.
    if source.get("tenant_id") != tenant_id:
        logger.warning(
            "fuel_ops.hash_proof: dropped cross-tenant POD row for pod=%s "
            "(row tenant=%s, caller tenant=%s)",
            pod_id,
            source.get("tenant_id"),
            tenant_id,
        )
        return None
    return source


def _extract_hash_chain_fields(pod_doc: Mapping[str, Any]) -> Dict[str, Any]:
    """Project a stored POD document into the canonical-hash field set.

    The POD row persists ``geotag`` as a GeoJSON-ish ``{"lat", "lon"}``
    mapping and the delivery time as ``timestamp`` (ISO 8601). The hash
    chain canonicalizer expects ``geotag`` and ``delivered_at``, so this
    helper normalizes the field names without mutating the stored doc.

    ``delivered_gallons``, ``order_id``, and ``signature_ref`` fields live
    directly on the POD record when Task 8.10 has written them; pre-8.10
    rows may be missing some of these, in which case we fall back to
    sensible defaults so ``canonicalize_pod`` can still run.
    """

    # ``geotag`` stored as {"lat", "lon"}; canonicalize_pod accepts either.
    geotag = pod_doc.get("geotag")
    # ``delivered_at`` is persisted as ``timestamp`` on the POD row today;
    # Task 8.10 may add a dedicated ``delivered_at`` field. Prefer the
    # dedicated field when present.
    delivered_at = pod_doc.get("delivered_at") or pod_doc.get("timestamp")

    return {
        "tenant_id": pod_doc.get("tenant_id", ""),
        "pod_id": pod_doc.get("pod_id", ""),
        "order_id": pod_doc.get("order_id") or pod_doc.get("job_id", ""),
        "delivered_gallons": pod_doc.get("delivered_gallons") or 0.0,
        "recipient_name": pod_doc.get("recipient_name") or "",
        "signature_ref": pod_doc.get("signature_ref") or "",
        "photo_refs": pod_doc.get("photo_refs") or [],
        "geotag": geotag,
        "delivered_at": delivered_at,
        "previous_pod_hash": pod_doc.get("previous_pod_hash") or ZERO_HASH,
    }


@router.get("/pod/{pod_id}/hash-proof", response_model=HashProofResponse)
async def get_pod_hash_proof(
    pod_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> HashProofResponse:
    """Return the hash proof for a single POD.

    Returns HTTP 404 when the POD doesn't exist or belongs to another
    tenant (existence is masked so tenant membership can't be probed),
    HTTP 409 when the POD row exists but pre-dates the Task 8.10 chain
    rollout (no ``pod_hash`` field), and HTTP 502 when the ES backend is
    unavailable.

    Validates: Requirement 4.5.3.
    """

    es = _get_es()
    pod_doc = await _fetch_pod_by_id(es, tenant.tenant_id, pod_id)
    if pod_doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "pod_not_found",
                "pod_id": pod_id,
            },
        )

    stored_hash = pod_doc.get("pod_hash")
    stored_previous = pod_doc.get("previous_pod_hash") or ZERO_HASH
    if not stored_hash:
        # Pre-8.10 rows don't carry a hash. Surface a distinct 409 so the
        # caller can differentiate "POD missing" from "POD exists but
        # predates the hash-chain rollout" and trigger a backfill.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "pod_hash_unavailable",
                "pod_id": pod_id,
                "message": (
                    "This POD record has no stored pod_hash. It was "
                    "persisted before the hash-chain rollout and needs "
                    "to be backfilled."
                ),
            },
        )

    # Rebuild the canonical payload the same way the writer did so the
    # caller can re-hash it locally and confirm the chain.
    try:
        hash_fields = _extract_hash_chain_fields(pod_doc)
        canonical_bytes = canonicalize_pod(hash_fields)
    except (ValueError, TypeError) as exc:
        # Missing/invalid fields on a hash-bearing row is a data integrity
        # bug worth surfacing loudly rather than silently masking.
        logger.error(
            "fuel_ops.hash_proof: canonicalization failed for pod=%s "
            "tenant=%s: %s",
            pod_id,
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "pod_canonicalization_failed",
                "pod_id": pod_id,
                "message": str(exc),
            },
        )

    canonical_payload = _hash_chain_json.loads(canonical_bytes.decode("utf-8"))

    logger.debug(
        "fuel_ops.hash_proof: tenant=%s pod=%s returned stored_hash=%s…",
        tenant.tenant_id,
        pod_id,
        stored_hash[:12],
    )

    return HashProofResponse(
        pod_id=pod_id,
        tenant_id=tenant.tenant_id,
        pod_hash=stored_hash,
        previous_pod_hash=stored_previous,
        canonical_payload=canonical_payload,
        canonical_payload_bytes=canonical_bytes.decode("utf-8"),
    )


async def _resolve_pod_ids_for_verify(
    es: Any,
    tenant_id: str,
    body: HashChainVerifyRequest,
) -> List[str]:
    """Resolve the ordered list of pod_ids to walk for a verify request.

    * When ``body.pod_ids`` is supplied we use it verbatim (the caller is
      responsible for the ordering).
    * When range mode is used we load both anchor rows, sort the matching
      timestamps and return every pod_id between them (inclusive) up to
      the supplied ``limit``.
    """

    # Explicit list mode ---------------------------------------------------
    if body.pod_ids is not None:
        if body.from_pod_id or body.to_pod_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "invalid_selector",
                    "message": (
                        "Provide either ``pod_ids`` or "
                        "``from_pod_id``/``to_pod_id``, not both."
                    ),
                },
            )
        if not body.pod_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "empty_pod_ids",
                    "message": "pod_ids must contain at least one pod_id.",
                },
            )
        if len(body.pod_ids) > HASH_CHAIN_MAX_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "pod_ids_exceeds_limit",
                    "message": (
                        f"pod_ids length {len(body.pod_ids)} exceeds the "
                        f"per-request cap of {HASH_CHAIN_MAX_LIMIT}."
                    ),
                },
            )
        # Strip whitespace and drop empties without re-ordering.
        cleaned = [pid.strip() for pid in body.pod_ids if pid and pid.strip()]
        if not cleaned:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "empty_pod_ids",
                    "message": "pod_ids contains no usable identifiers.",
                },
            )
        return cleaned

    # Range mode -----------------------------------------------------------
    if not (body.from_pod_id and body.to_pod_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "missing_selector",
                "message": (
                    "Provide either ``pod_ids`` or both ``from_pod_id`` "
                    "and ``to_pod_id``."
                ),
            },
        )

    limit = body.limit or HASH_CHAIN_DEFAULT_LIMIT

    start_doc = await _fetch_pod_by_id(es, tenant_id, body.from_pod_id)
    end_doc = await _fetch_pod_by_id(es, tenant_id, body.to_pod_id)
    if start_doc is None or end_doc is None:
        missing = body.from_pod_id if start_doc is None else body.to_pod_id
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "pod_not_found",
                "pod_id": missing,
                "message": (
                    "Range anchor POD was not found for this tenant. "
                    "Check that the pod_id exists and belongs to the "
                    "authenticated tenant."
                ),
            },
        )

    start_ts = start_doc.get("timestamp") or start_doc.get("delivered_at")
    end_ts = end_doc.get("timestamp") or end_doc.get("delivered_at")
    if not start_ts or not end_ts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "pod_timestamp_unavailable",
                "message": (
                    "Range anchor POD is missing a timestamp; cannot "
                    "resolve the verification window."
                ),
            },
        )

    # Normalize the range so callers can pass either order.
    lo, hi = (start_ts, end_ts) if start_ts <= end_ts else (end_ts, start_ts)

    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"range": {"timestamp": {"gte": lo, "lte": hi}}},
                ]
            }
        },
        "sort": [{"timestamp": {"order": "asc"}}],
        "size": limit,
    }
    try:
        resp = await es.search_documents(PROOF_OF_DELIVERY_INDEX, query, limit)
    except Exception as exc:
        logger.exception(
            "fuel_ops.hash_chain.verify: range scan failed for tenant=%s "
            "from=%s to=%s",
            tenant_id,
            body.from_pod_id,
            body.to_pod_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error_code": "pod_store_unavailable",
                "message": "Proof-of-delivery store is unavailable.",
            },
        ) from exc

    hits = (resp or {}).get("hits", {}).get("hits", [])
    ordered_ids: List[str] = []
    for hit in hits:
        source = hit.get("_source") or {}
        if source.get("tenant_id") != tenant_id:
            continue  # defensive
        pod_id = source.get("pod_id")
        if pod_id:
            ordered_ids.append(pod_id)
    if not ordered_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "hash_chain_range_empty",
                "message": (
                    "No PODs were found in the requested timestamp range."
                ),
            },
        )
    return ordered_ids


@router.post(
    "/pod/hash-chain/verify",
    response_model=HashChainVerifyResponse,
)
async def verify_pod_hash_chain(
    body: HashChainVerifyRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> HashChainVerifyResponse:
    """Re-verify a range of POD records against the stored hash chain.

    For each POD in the ordered list we re-compute ``pod_hash`` using the
    canonical serialization defined in
    :mod:`services.pod_hash_chain` and compare it against the stored
    ``pod_hash``. We then confirm that the POD's stored
    ``previous_pod_hash`` equals the prior POD's stored ``pod_hash``.

    The first failure (if any) is returned in ``first_mismatch`` and the
    walk stops, satisfying Req 4.5.5's "report the first mismatch
    pod_id" guarantee. ``valid=False`` implies tampering (or a
    persistence bug) somewhere at or before ``first_mismatch.pod_id``.

    Validates: Requirements 4.5.4, 4.5.5.
    """

    es = _get_es()
    pod_ids = await _resolve_pod_ids_for_verify(es, tenant.tenant_id, body)

    verified_count = 0
    first_mismatch: Optional[HashChainMismatch] = None
    previous_stored_hash: Optional[str] = None  # None on first iteration

    for index, pod_id in enumerate(pod_ids):
        pod_doc = await _fetch_pod_by_id(es, tenant.tenant_id, pod_id)
        if pod_doc is None:
            first_mismatch = HashChainMismatch(
                pod_id=pod_id,
                reason="pod_not_found",
                message=(
                    "POD does not exist for this tenant. Hash chain "
                    "cannot be verified at or beyond this pod_id."
                ),
            )
            break

        stored_hash = pod_doc.get("pod_hash")
        stored_previous = pod_doc.get("previous_pod_hash") or ZERO_HASH

        if not stored_hash:
            first_mismatch = HashChainMismatch(
                pod_id=pod_id,
                reason="missing_stored_hash",
                message=(
                    "POD has no stored pod_hash. Rows written before the "
                    "hash-chain rollout must be backfilled before they can "
                    "be verified."
                ),
            )
            break

        # Re-compute the hash from the stored canonical fields.
        try:
            computed = compute_pod_hash(_extract_hash_chain_fields(pod_doc))
        except (ValueError, TypeError) as exc:
            logger.error(
                "fuel_ops.hash_chain.verify: canonicalization failed for "
                "pod=%s tenant=%s: %s",
                pod_id,
                tenant.tenant_id,
                exc,
            )
            first_mismatch = HashChainMismatch(
                pod_id=pod_id,
                reason="stored_hash_mismatch",
                stored_hash=stored_hash,
                message=(
                    "Canonicalization failed for this POD — cannot "
                    "recompute pod_hash. Stored fields may be corrupt."
                ),
            )
            break

        if computed != stored_hash:
            first_mismatch = HashChainMismatch(
                pod_id=pod_id,
                reason="stored_hash_mismatch",
                expected_hash=computed,
                stored_hash=stored_hash,
                computed_hash=computed,
                message=(
                    "Recomputed pod_hash does not match the stored "
                    "pod_hash. The canonical fields have been mutated "
                    "since the POD was persisted."
                ),
            )
            break

        # Chain-link check. Skipped on the first POD of the verification
        # window (we have no prior hash to compare against — the verifier
        # cannot know whether this was the genesis POD for the tenant).
        if previous_stored_hash is not None:
            if stored_previous != previous_stored_hash:
                first_mismatch = HashChainMismatch(
                    pod_id=pod_id,
                    reason="previous_hash_mismatch",
                    expected_hash=previous_stored_hash,
                    stored_hash=stored_previous,
                    message=(
                        "Stored previous_pod_hash does not match the "
                        "previous POD's stored pod_hash in this "
                        "verification window. Chain linkage is broken."
                    ),
                )
                break

        verified_count += 1
        previous_stored_hash = stored_hash

    valid = first_mismatch is None

    logger.info(
        "fuel_ops.hash_chain.verify: tenant=%s requested=%d verified=%d "
        "valid=%s first_mismatch=%s",
        tenant.tenant_id,
        len(pod_ids),
        verified_count,
        valid,
        first_mismatch.pod_id if first_mismatch else None,
    )

    return HashChainVerifyResponse(
        tenant_id=tenant.tenant_id,
        verified_count=verified_count,
        total_requested=len(pod_ids),
        valid=valid,
        first_mismatch=first_mismatch,
        pod_ids_checked=pod_ids,
    )


# ---------------------------------------------------------------------------
# GET /api/fuel/pod/{pod_id}/bol (Task 8.6, Req 4.3.4, 4.3.5)
# ---------------------------------------------------------------------------
#
# Returns a short-lived presigned download URL for the BOL PDF associated
# with a finalized POD. Tenant-scoped — cross-tenant PODs surface as HTTP
# 404 (existence is masked so tenant membership can't be probed).
#
# The BOL record itself is written by :class:`driver.services.pod_bol_finalizer.PODBOLFinalizer`
# synchronously on POD finalization, gated by the ``overlay.bol_generation``
# feature flag. When generation fails the finalizer persists a stub record
# with ``status: pending_regeneration`` so this endpoint can surface the
# failure state back to operators / dashboards.
#
# Response shape:
#
#     {
#       "bol_id": "bol-tenant-a-...",
#       "pod_id": "pod-0001",
#       "status": "generated",                    # or "pending_regeneration"
#       "hash": "<sha256>",
#       "generated_at": "2025-01-15T14:30:00+00:00",
#       "file_ref": "tenants/tenant-a/bol/2025/01/15/....pdf",
#       "download_url": "https://....",          # absent when pending_regeneration
#       "expires_at": "2025-01-15T14:45:00+00:00" # absent when pending_regeneration
#     }
#
# Status codes:
#
#   * 200 — BOL present. ``download_url`` and ``expires_at`` are populated
#           when the BOL was successfully generated. For
#           ``pending_regeneration`` rows the body is returned with
#           ``download_url`` omitted so operators can still discover the
#           pending state.
#   * 404 — No BOL record exists for this POD under the requesting tenant.
#           Either the POD is missing, the tenant is not the owner, or the
#           ``overlay.bol_generation`` flag was disabled at POD finalization
#           time (in which case the finalizer no-ops and no row is written).
#   * 502 — Presigning failed (S3 outage). Returned as a structured error
#           so the caller can retry.

from services.file_storage_service import FileStorageService as _FileStorageService  # noqa: E402
from fuel.services.fuel_ops_es_mappings import BILL_OF_LADING_INDEX as _BILL_OF_LADING_INDEX  # noqa: E402


#: Presigned download URL TTL for BOL artifacts. Matches the
#: platform-wide :data:`services.file_storage_service.DEFAULT_PRESIGN_TTL_SECONDS`
#: default of 15 minutes so the POD / BOL surface stays consistent.
BOL_DOWNLOAD_PRESIGN_TTL_SECONDS: int = 900


class BOLDownloadResponse(BaseModel):
    """Envelope for ``GET /api/fuel/pod/{pod_id}/bol`` (Task 8.6, Req 4.3.4).

    ``download_url`` and ``expires_at`` are omitted when the BOL is in
    ``pending_regeneration`` state (no rendered PDF exists yet) so clients
    can unambiguously distinguish the success and pending states from the
    response shape alone.
    """

    model_config = ConfigDict(extra="forbid")

    bol_id: str
    pod_id: str
    status: str
    hash: str
    generated_at: Optional[str] = None
    file_ref: Optional[str] = None
    download_url: Optional[str] = None
    expires_at: Optional[str] = None
    tenant_id: str


async def _fetch_bol_for_pod(
    es: Any, tenant_id: str, pod_id: str
) -> Optional[Dict[str, Any]]:
    """Return the most recent BOL row for ``pod_id`` scoped to ``tenant_id``.

    A POD may accumulate multiple rows across regenerations (Task 8.6's
    failure path persists a ``pending_regeneration`` stub; a subsequent
    successful regeneration persists a ``generated`` row). The endpoint
    surfaces the most recent row keyed by ``generated_at`` so operators
    always see the current state.

    Uses ``search_documents`` rather than ``get_document`` for consistency
    with the rest of the fuel-ops endpoints (test doubles mock
    ``search_documents`` uniformly).
    """
    if not pod_id or not pod_id.strip():
        return None
    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"pod_id": pod_id}},
                    {"term": {"tenant_id": tenant_id}},
                ]
            }
        },
        "sort": [{"generated_at": {"order": "desc"}}],
        "size": 1,
    }
    try:
        resp = await es.search_documents(_BILL_OF_LADING_INDEX, query, 1)
    except Exception as exc:
        logger.exception(
            "fuel_ops.bol_download: ES lookup failed for pod=%s tenant=%s",
            pod_id,
            tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error_code": "bol_store_unavailable",
                "message": "Bill-of-lading store is unavailable.",
            },
        ) from exc

    hits = (resp or {}).get("hits", {}).get("hits", [])
    if not hits:
        return None
    source = hits[0].get("_source") or {}
    # Defensive re-check — the ES filter already enforced tenant_id but a
    # misconfigured mapping could still leak across tenants.
    if source.get("tenant_id") != tenant_id:
        logger.warning(
            "fuel_ops.bol_download: dropped cross-tenant BOL row for pod=%s "
            "(row tenant=%s, caller tenant=%s)",
            pod_id,
            source.get("tenant_id"),
            tenant_id,
        )
        return None
    return source


@router.get("/pod/{pod_id}/bol", response_model=BOLDownloadResponse)
async def get_pod_bol(
    pod_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> BOLDownloadResponse:
    """Return a presigned download URL for the BOL PDF tied to ``pod_id``.

    Tenant-scoped: the BOL row is located via a tenant-bounded ES filter
    and cross-tenant rows are dropped defensively. Cross-tenant requests
    receive HTTP 404 with an ``error_code: bol_not_found`` detail so
    tenant membership is not leaked.

    When the BOL is in ``pending_regeneration`` (the finalizer hit a
    failure on the synchronous generation path), the response still
    returns HTTP 200 with the row metadata but omits ``download_url`` /
    ``expires_at``. Operators and retry pipelines can filter on the
    ``status`` field to find records that need regeneration.

    Validates: Requirements 4.3.4, 4.3.5.
    """
    if not pod_id or not pod_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "invalid_pod_id", "pod_id": pod_id},
        )

    es = _get_es()
    bol_doc = await _fetch_bol_for_pod(es, tenant.tenant_id, pod_id)
    if bol_doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "bol_not_found",
                "pod_id": pod_id,
                "message": (
                    "No BOL record exists for this POD. The POD may not "
                    "exist, the tenant may not own it, or "
                    "overlay.bol_generation was disabled at finalization."
                ),
            },
        )

    bol_id = str(bol_doc.get("bol_id") or "")
    status_value = str(bol_doc.get("status") or "")
    hash_value = str(bol_doc.get("hash") or "")
    generated_at = bol_doc.get("generated_at")
    file_ref = str(bol_doc.get("file_ref") or "") or None

    download_url: Optional[str] = None
    expires_at: Optional[str] = None

    # Only attempt to presign when we actually have a PDF in S3. The
    # ``pending_regeneration`` stub carries an empty ``file_ref`` on
    # purpose — there is nothing to download yet.
    if file_ref and status_value != "pending_regeneration":
        file_storage = _file_storage_service
        if file_storage is None:
            # Fail loudly rather than silently returning a row without a
            # URL — if we reach this branch with a file_ref the operator
            # expects a working link. This mirrors the driver POD presign
            # endpoint which also demands FileStorageService at bootstrap.
            logger.error(
                "fuel_ops.bol_download: FileStorageService unavailable "
                "tenant=%s pod=%s",
                tenant.tenant_id,
                pod_id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "file_storage_unavailable",
                    "message": (
                        "File storage service is not configured. "
                        "BOL download URLs cannot be issued."
                    ),
                },
            )
        try:
            presigned = file_storage.presign_get(
                tenant_id=tenant.tenant_id,
                file_ref=file_ref,
                ttl_seconds=BOL_DOWNLOAD_PRESIGN_TTL_SECONDS,
                actor=tenant.user_id,
            )
        except PermissionError as exc:
            # A cross-tenant file_ref on a tenant-owned BOL row is a
            # data-integrity bug, not a client error. Log loudly and
            # surface as 500.
            logger.error(
                "fuel_ops.bol_download: cross-tenant file_ref on owned BOL "
                "row tenant=%s pod=%s bol_id=%s: %s",
                tenant.tenant_id,
                pod_id,
                bol_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error_code": "bol_file_ref_corrupt",
                    "pod_id": pod_id,
                    "bol_id": bol_id,
                },
            )
        except ValueError as exc:
            # Malformed TTL / tenant_id on server side — should not happen
            # with the constants above, but surface cleanly if it does.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "invalid_presign_request", "reason": str(exc)},
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "fuel_ops.bol_download: presign failed tenant=%s pod=%s: %s",
                tenant.tenant_id,
                pod_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error_code": "presign_failed",
                    "pod_id": pod_id,
                    "message": "Failed to issue presigned download URL.",
                },
            )
        download_url = presigned.get("download_url") if isinstance(presigned, dict) else None
        expires_at = presigned.get("expires_at") if isinstance(presigned, dict) else None

    generated_at_str: Optional[str] = None
    if generated_at is not None:
        if isinstance(generated_at, datetime):
            generated_at_str = generated_at.isoformat()
        else:
            generated_at_str = str(generated_at)

    logger.info(
        "fuel_ops.bol_download: tenant=%s pod=%s bol_id=%s status=%s "
        "has_download_url=%s",
        tenant.tenant_id,
        pod_id,
        bol_id,
        status_value,
        bool(download_url),
    )

    return BOLDownloadResponse(
        bol_id=bol_id,
        pod_id=pod_id,
        status=status_value,
        hash=hash_value,
        generated_at=generated_at_str,
        file_ref=file_ref,
        download_url=download_url,
        expires_at=expires_at,
        tenant_id=tenant.tenant_id,
    )


# ---------------------------------------------------------------------------
# GET /api/fuel/storm-mode/status (Req 9.1.6, 9.4.3, Task 10.4)
# ---------------------------------------------------------------------------


class StormModeTriggeringAlert(BaseModel):
    """Condensed :class:`WeatherAlert` view embedded in the status response.

    Mirrors the fields the dispatcher banner needs (alert_id, alert_type,
    severity, headline, time window, affected ZIPs, source) without
    surfacing the full persistence shape. The endpoint hydrates this
    model from the ``weather_alerts`` ES index so the banner can render
    a human-readable "triggering alert" section without a follow-up
    fetch from the client.

    Validates: Requirement 9.1.6.
    """

    model_config = ConfigDict(extra="forbid")

    alert_id: str
    alert_type: str
    severity: WeatherAlertSeverity
    headline: Optional[str] = None
    description: Optional[str] = None
    expected_start_at: datetime
    expected_end_at: Optional[datetime] = None
    affected_zip_codes: List[str] = Field(default_factory=list)
    source: WeatherAlertSource
    activation_status: WeatherAlertStatus


class StormModeActiveOverride(BaseModel):
    """Condensed :class:`StormModeOverride` view embedded in the response.

    Mirrors Req 9.4.3's contract: ``override_active: true`` plus the
    override's action / reason / actor / expiry. We intentionally do not
    surface the override's ``created_at`` / ``updated_at`` timestamps so
    the banner payload stays focused on what the dispatcher needs to
    render.

    Validates: Requirement 9.4.3.
    """

    model_config = ConfigDict(extra="forbid")

    override_id: str
    action: StormModeOverrideAction
    reason: str
    actor_id: str
    expires_at: Optional[datetime] = None


class StormModeActivationWindow(BaseModel):
    """Activation window info surfaced on every status response.

    The window carries both the evaluator's configured parameters
    (``lookahead_hours`` + ``severity_threshold`` = "how does the
    platform decide to activate") and the concrete transition
    timestamps for the current state (``activated_at`` / ``clears_at``
    = "when did this posture start and when do we expect it to end").
    Nullable transition fields keep the shape stable across both
    ``active`` and ``inactive`` states — the dispatcher UI renders the
    nulls as "–" rather than hiding the block.

    Validates: Requirement 9.1.6.
    """

    model_config = ConfigDict(extra="forbid")

    lookahead_hours: int
    severity_threshold: WeatherAlertSeverity
    activated_at: Optional[datetime] = None
    clears_at: Optional[datetime] = None


class StormModeStatusResponse(BaseModel):
    """Envelope for ``GET /api/fuel/storm-mode/status``.

    Fields:

    * ``tenant_id`` — echoed from the caller's JWT context so the client
      can cross-check the response.
    * ``state`` — the effective state after override precedence
      (``active`` / ``inactive``). The dispatcher banner renders off
      this field.
    * ``computed_state`` — the state the evaluator would have selected
      from alerts alone, before any override was applied. When an
      override is active, this can differ from ``state`` and tells the
      UI "the platform wanted this but the override forced otherwise".
    * ``override_active`` — ``true`` when an ``activate`` / ``deactivate``
      / ``snooze`` override is currently in effect (Req 9.4.3).
    * ``override`` — the :class:`StormModeActiveOverride` describing the
      override. ``None`` when ``override_active`` is ``false``.
    * ``triggering_alerts`` — the hydrated :class:`StormModeTriggeringAlert`
      list the evaluator last pinned state on. When ``state`` is
      ``inactive`` and no override is active, this list is typically
      empty. The evaluator persists up to a few alert ids, so the list
      is capped to the same cardinality.
    * ``activation_window`` — the :class:`StormModeActivationWindow`
      (lookahead hours, severity threshold, transition timestamps).
    * ``updated_at`` — when the persisted state was last written (or
      ``None`` when the tenant has never transitioned).
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    state: str
    computed_state: str
    override_active: bool
    override: Optional[StormModeActiveOverride] = None
    triggering_alerts: List[StormModeTriggeringAlert] = Field(default_factory=list)
    activation_window: StormModeActivationWindow
    updated_at: Optional[datetime] = None


def _get_storm_mode_evaluator() -> StormModeEvaluator:
    """Return the module-wired :class:`StormModeEvaluator` singleton.

    When the evaluator has not been configured, the endpoint surfaces
    HTTP 503 ``storm_mode_evaluator_unavailable`` so tests and early
    bootstrap states fail loudly rather than silently rendering stale
    state.
    """
    if _storm_mode_evaluator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "storm_mode_evaluator_unavailable",
                "message": (
                    "Storm_Mode evaluator is not configured. Finish the "
                    "bootstrap wire-up (see bootstrap/agents.py) before "
                    "calling /api/fuel/storm-mode/status."
                ),
            },
        )
    return _storm_mode_evaluator


async def _hydrate_triggering_alerts(
    es: Any,
    tenant_id: str,
    alert_ids: List[str],
) -> List[StormModeTriggeringAlert]:
    """Look up the persisted triggering WeatherAlerts by id.

    The evaluator persists a short list of alert ids next to the state;
    this helper hydrates them from the ``weather_alerts`` index so the
    dispatcher banner can render alert metadata without a second round
    trip from the client. Tenant isolation is enforced twice:

    1. The ES ``bool.filter`` clause pins both ``tenant_id`` and
       ``alert_id`` via ``terms``.
    2. Every returned ``_source`` is re-validated against the caller's
       tenant_id; any mis-labelled row is dropped with a warning.

    Malformed rows are dropped (not raised) so a single bad document
    cannot break the banner. Returns the alerts in the same order the
    evaluator persisted them when possible.
    """
    if not alert_ids:
        return []

    query: Dict[str, Any] = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"tenant_id": tenant_id}},
                    {"terms": {"alert_id": list(alert_ids)}},
                ]
            }
        },
        "size": max(len(alert_ids), 1),
    }

    try:
        resp = await es.search_documents(
            WEATHER_ALERTS_INDEX, query, query["size"]
        )
    except Exception as exc:
        logger.warning(
            "fuel_ops.storm_mode_status: failed to hydrate triggering "
            "alerts for tenant=%s: %s",
            tenant_id,
            exc,
        )
        return []

    hits = ((resp or {}).get("hits") or {}).get("hits") or []
    by_id: Dict[str, StormModeTriggeringAlert] = {}
    for hit in hits:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            continue
        if source.get("tenant_id") != tenant_id:
            logger.warning(
                "fuel_ops.storm_mode_status: dropping alert row with "
                "mismatched tenant_id %s (expected %s)",
                source.get("tenant_id"),
                tenant_id,
            )
            continue
        try:
            alert = WeatherAlert.model_validate(source)
        except ValidationError as exc:
            logger.warning(
                "fuel_ops.storm_mode_status: dropping malformed alert "
                "(alert_id=%s) for tenant=%s: %s",
                source.get("alert_id"),
                tenant_id,
                exc,
            )
            continue
        by_id[alert.alert_id] = StormModeTriggeringAlert(
            alert_id=alert.alert_id,
            alert_type=alert.alert_type,
            severity=alert.severity,
            headline=alert.headline,
            description=alert.description,
            expected_start_at=alert.expected_start_at,
            expected_end_at=alert.expected_end_at,
            affected_zip_codes=list(alert.affected_zip_codes),
            source=alert.source,
            activation_status=alert.activation_status,
        )

    # Preserve the evaluator's ordering when possible so the UI renders
    # the "primary" triggering alert first.
    ordered: List[StormModeTriggeringAlert] = []
    for alert_id in alert_ids:
        hit = by_id.pop(alert_id, None)
        if hit is not None:
            ordered.append(hit)
    # Append any remaining rows (should be empty unless the evaluator
    # persisted duplicates) so nothing is silently dropped.
    ordered.extend(by_id.values())
    return ordered


async def _fetch_active_storm_override(
    es: Any,
    tenant_id: str,
    now: datetime,
) -> Optional[StormModeOverride]:
    """Return the most-recent non-expired override for ``tenant_id``.

    Mirrors the evaluator's internal lookup but is invoked directly from
    the REST layer so the status endpoint can report the override even
    when Redis state lags behind the most-recent override (e.g. the
    dispatcher just submitted a ``deactivate`` and the next tick has
    not yet run). Tenant isolation is enforced on both the ES filter
    and a defensive re-check.
    """

    query: Dict[str, Any] = {
        "query": {
            "bool": {
                "filter": [{"term": {"tenant_id": tenant_id}}],
                "should": [
                    {
                        "bool": {
                            "must_not": [{"exists": {"field": "expires_at"}}]
                        }
                    },
                    {"range": {"expires_at": {"gt": now.isoformat()}}},
                ],
                "minimum_should_match": 1,
            }
        },
        "sort": [{"created_at": {"order": "desc"}}],
        "size": 10,
    }

    try:
        resp = await es.search_documents(
            STORM_MODE_OVERRIDES_INDEX, query, 10
        )
    except Exception as exc:
        logger.warning(
            "fuel_ops.storm_mode_status: failed to fetch overrides for "
            "tenant=%s: %s",
            tenant_id,
            exc,
        )
        return None

    hits = ((resp or {}).get("hits") or {}).get("hits") or []
    for hit in hits:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            continue
        if source.get("tenant_id") != tenant_id:
            continue
        try:
            override = StormModeOverride.model_validate(source)
        except ValidationError as exc:
            logger.debug(
                "fuel_ops.storm_mode_status: skipping malformed override "
                "for tenant=%s: %s",
                tenant_id,
                exc,
            )
            continue
        if (
            override.expires_at is not None
            and override.expires_at <= now
        ):
            continue
        return override
    return None


def _override_forces_active(action: StormModeOverrideAction) -> Optional[str]:
    """Return the state an override forces, or ``None`` for ``clear``.

    Mirrors :func:`fuel.services.storm_mode_evaluator._override_forces_state`
    so the status endpoint computes the same effective state the
    evaluator would on its next tick — important when an operator just
    submitted an override and has not yet waited for the 5-minute poll.
    """
    if action == "activate":
        return STORM_MODE_ACTIVE
    if action in ("deactivate", "snooze"):
        return STORM_MODE_INACTIVE
    return None


@router.get(
    "/storm-mode/status",
    response_model=StormModeStatusResponse,
)
async def get_storm_mode_status(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> StormModeStatusResponse:
    """Return the current Storm_Mode state for the tenant.

    The response carries the effective state (after override precedence),
    the underlying computed state, any active manual override, the
    hydrated triggering :class:`WeatherAlert`\\ s, and the activation
    window. The endpoint is strictly read-only: it calls
    :meth:`StormModeEvaluator.get_state` to pull the last-persisted
    evaluator state out of Redis (or the in-memory fallback) and then
    hydrates auxiliary fields from ``weather_alerts`` and
    ``storm_mode_overrides`` — it does **not** trigger a new evaluation
    tick. The 5-minute poll loop owns state transitions.

    When an ``activate`` / ``deactivate`` / ``snooze`` override is in
    effect at read time, the response reflects the override:
    ``state`` switches to the override's forced value,
    ``override_active`` flips to ``true``, and the override details
    surface in ``override``. The computed (alert-derived) state is
    always preserved in ``computed_state`` so operators can see "the
    platform wanted X but the override forced Y" (Req 9.4.3).

    Validates: Requirements 9.1.6, 9.4.3.
    """

    evaluator = _get_storm_mode_evaluator()
    es = _get_es()
    now = datetime.now(timezone.utc)

    persisted = await evaluator.get_state(tenant.tenant_id)
    computed_state = persisted.state

    override = await _fetch_active_storm_override(es, tenant.tenant_id, now)
    forced = _override_forces_active(override.action) if override else None
    effective_state = forced if forced is not None else computed_state
    override_active = override is not None and forced is not None

    triggering_alerts = await _hydrate_triggering_alerts(
        es, tenant.tenant_id, list(persisted.triggering_alert_ids)
    )

    activation_window = StormModeActivationWindow(
        lookahead_hours=DEFAULT_ACTIVATION_WINDOW_HOURS,
        severity_threshold=DEFAULT_ACTIVATION_SEVERITY,
        activated_at=persisted.updated_at
        if computed_state == STORM_MODE_ACTIVE
        else None,
        clears_at=persisted.expected_end_at
        if computed_state == STORM_MODE_ACTIVE
        else None,
    )

    override_payload: Optional[StormModeActiveOverride] = None
    if override is not None and override_active:
        override_payload = StormModeActiveOverride(
            override_id=override.override_id,
            action=override.action,
            reason=override.reason,
            actor_id=override.actor_id,
            expires_at=override.expires_at,
        )

    logger.debug(
        "fuel_ops.storm_mode_status: tenant=%s state=%s computed=%s "
        "override_active=%s triggering_alerts=%d",
        tenant.tenant_id,
        effective_state,
        computed_state,
        override_active,
        len(triggering_alerts),
    )

    return StormModeStatusResponse(
        tenant_id=tenant.tenant_id,
        state=effective_state,
        computed_state=computed_state,
        override_active=override_active,
        override=override_payload,
        triggering_alerts=triggering_alerts,
        activation_window=activation_window,
        updated_at=persisted.updated_at,
    )


# ---------------------------------------------------------------------------
# POST /api/fuel/storm-mode/override (Req 9.4.2, 9.4.4, Task 10.5)
# ---------------------------------------------------------------------------


class StormModeOverrideCreateRequest(BaseModel):
    """Request body for ``POST /api/fuel/storm-mode/override``.

    Mirrors :class:`fuel.storm_mode_models.StormModeOverride` but omits
    repository-managed fields (``override_id``, ``tenant_id``,
    ``actor_id``, ``created_at``, ``updated_at``) so the caller cannot
    spoof ownership, audit attribution, or reuse an existing override id.
    ``action`` is constrained to the same enum the
    :class:`StormModeOverride` model enforces.

    ``actor_id`` is intentionally **not** part of the request body: the
    router derives it server-side from the verified session
    (``tenant.user_id``) so the audit actor cannot be spoofed by a
    client-supplied value (Req 5.5).

    The request body intentionally accepts ``expires_at`` as optional:
    ``clear`` overrides are instantaneous by design (they remove any
    prior override without changing state), and operators are allowed
    to submit an indefinite ``activate`` / ``deactivate`` / ``snooze``
    when they know a storm will outlast any reasonable TTL. The
    :class:`StormModeEvaluator` tolerates both shapes.

    Validates: Requirement 9.4.2.
    """

    model_config = ConfigDict(extra="forbid")

    action: StormModeOverrideAction = Field(
        ...,
        description=(
            "Override action. ``activate`` forces Storm_Mode on, "
            "``deactivate`` forces it off, ``snooze`` suppresses "
            "automatic activation until ``expires_at``, ``clear`` "
            "removes any prior override without changing state."
        ),
    )
    reason: str = Field(
        ...,
        min_length=1,
        description=(
            "Human-readable justification captured for audit (Req 9.4.4). "
            "Required so every override is explainable at incident review."
        ),
    )
    expires_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Optional UTC datetime at which the override lapses. "
            "Nullable because ``clear`` overrides are instantaneous and "
            "because operators may submit an indefinite override when "
            "the storm duration is unknown."
        ),
    )


def _ensure_storm_mode_override_role(tenant: TenantContext) -> None:
    """Enforce the Req 9.4.4 role gate on Storm_Mode override submissions.

    Thin wrapper over the shared :func:`auth.authorization.require_role`
    helper so every router applies one consistent authorization mechanism
    (Req 4.7). Matching is **exact**: only the canonical ``dispatcher`` /
    ``admin`` roles satisfy the gate — a tenant role lexicon such as
    ``dispatcher_lead`` / ``admin_ops`` no longer passes (the previous
    substring match was over-permissive; exact matching is the security
    fix mandated by Req 4.2). Raises HTTP 403 ``INSUFFICIENT_ROLE`` when
    the caller holds neither role; the shared helper deliberately does not
    echo the caller's held roles back so the tenant's role lexicon is not
    leaked to a probing attacker.
    """

    require_role(tenant, "dispatcher", "admin")


@router.post(
    "/storm-mode/override",
    response_model=StormModeOverride,
    status_code=status.HTTP_201_CREATED,
)
async def submit_storm_mode_override(
    body: StormModeOverrideCreateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> StormModeOverride:
    """Persist a dispatcher or admin Storm_Mode override.

    Flow:

        1. Enforce the role gate (Req 9.4.4) — only ``dispatcher`` or
           ``admin`` roles may submit. Other callers receive HTTP 403
           with ``INSUFFICIENT_ROLE``.
        2. Validate the body through :class:`StormModeOverride` itself
           so the same invariants enforced on evaluator-side reads
           (non-blank reason / actor, enum action) are enforced on the
           write path. The router stamps ``tenant_id`` from the JWT
           context, derives ``actor_id`` from the verified session
           (``tenant.user_id``) so audit attribution cannot be spoofed
           by a client-supplied value (Req 5.5), and mints an
           ``override_id`` (``smo_<uuid4>``) so the caller cannot spoof
           ownership or reuse an existing id.
        3. Persist to the ``storm_mode_overrides`` ES index via
           :meth:`es.index_document`. Bootstrap indexing is idempotent
           — the mapping is ``dynamic: strict`` so any rogue field the
           request model misses surfaces as an ES validation error
           which we map back to 422.
        4. Return the persisted :class:`StormModeOverride`. The next
           :class:`StormModeEvaluator` tick will pick the override up
           from ES automatically; the status endpoint (Task 10.4) also
           reads overrides directly so the dispatcher banner reflects
           the submission within the 5-minute poll interval.

    The :class:`StormModeEvaluator` is intentionally **not** nudged
    here — the endpoint's job is durable persistence, and coupling it
    to the evaluator's tick scheduler would make the write path depend
    on a background service that may be down. The evaluator polls at a
    5-minute cadence; the status endpoint reads overrides out of band
    so the banner reflects the override immediately.

    Error modes:

        * 403 ``INSUFFICIENT_ROLE`` — caller lacks dispatcher/admin role.
        * 422 ``validation_error`` — body failed Pydantic validation
          (blank reason, unknown action, etc.).
        * 503 — persistence layer unreachable. We surface ``str(exc)``
          in the detail so the operator can diagnose upstream issues.

    Validates: Requirements 9.4.2, 9.4.4.
    """

    _ensure_storm_mode_override_role(tenant)

    es = _get_es()
    now = datetime.now(timezone.utc)
    override_id = f"smo_{uuid4().hex}"

    try:
        override = StormModeOverride(
            override_id=override_id,
            tenant_id=tenant.tenant_id,
            action=body.action,
            reason=body.reason,
            actor_id=tenant.user_id,
            expires_at=body.expires_at,
            created_at=now,
            updated_at=now,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    try:
        await es.index_document(
            STORM_MODE_OVERRIDES_INDEX,
            override.override_id,
            override.model_dump(mode="json"),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "fuel_ops.storm_mode_override: persistence failed tenant=%s "
            "override=%s action=%s",
            tenant.tenant_id,
            override.override_id,
            override.action,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "storm_mode_override_persistence_failed",
                "message": str(exc),
            },
        )

    logger.info(
        "fuel_ops.storm_mode_override: tenant=%s override=%s action=%s "
        "actor=%s expires=%s",
        tenant.tenant_id,
        override.override_id,
        override.action,
        override.actor_id,
        override.expires_at.isoformat() if override.expires_at else "never",
    )

    return override


# ---------------------------------------------------------------------------
# POST /api/fuel/storm-mode/road-restrictions (Req 9.3.3, 9.3.5, Task 10.8)
# GET  /api/fuel/storm-mode/road-restrictions (Req 9.3.5, Task 10.8)
# ---------------------------------------------------------------------------


class StormRoadRestrictionCreateRequest(BaseModel):
    """Request body for ``POST /api/fuel/storm-mode/road-restrictions``.

    Mirrors :class:`fuel.storm_mode_models.StormRoadRestriction` but omits
    repository-managed fields (``restriction_id``, ``tenant_id``,
    ``created_at``, ``updated_at``) so callers cannot spoof ownership or
    reuse an existing restriction id. Validation is delegated to the
    :class:`StormRoadRestriction` model itself so the polygon geometry,
    severity enum, and effective-window invariants are enforced on both
    the upload path and any subsequent read.

    Validates: Requirement 9.3.3.
    """

    model_config = ConfigDict(extra="forbid")

    polygon: Dict[str, Any] = Field(
        ...,
        description=(
            "GeoJSON ``Polygon`` or ``MultiPolygon`` geometry. "
            "Coordinates are WGS84 (``[lon, lat]``) and rings must be "
            "closed per RFC 7946. Validated for coordinate bounds and "
            "closure by the :class:`StormRoadRestriction` model."
        ),
    )
    effective_from: datetime = Field(
        ...,
        description=(
            "UTC datetime at which the restriction begins to apply. "
            "Route segments whose ``eta`` precedes this value are not "
            "affected."
        ),
    )
    effective_to: Optional[datetime] = Field(
        default=None,
        description=(
            "Optional UTC datetime at which the restriction lapses. "
            "``None`` for open-ended closures."
        ),
    )
    source: str = Field(
        ...,
        min_length=1,
        description=(
            "Free-form provenance label (``manual``, ``dot_feed``, "
            "``ops_team``, etc.). Surfaced on the dispatcher map so "
            "operators can tell DOT feeds from ad-hoc uploads."
        ),
    )
    severity: WeatherAlertSeverity = Field(
        ...,
        description=(
            "One of minor/moderate/severe/extreme. The "
            "Route_Planning_Agent applies the restriction only when "
            "severity is ``severe`` or ``extreme`` (Req 9.3.4)."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        description=(
            "Optional human-readable justification displayed on the "
            "dispatcher UI alongside the polygon."
        ),
    )


class StormRoadRestrictionListResponse(BaseModel):
    """Envelope for ``GET /api/fuel/storm-mode/road-restrictions``.

    Matches the ``{items, total}`` shape used across other list endpoints
    in this module so the dispatcher UI's pagination / map-render helpers
    can consume it uniformly.
    """

    model_config = ConfigDict(extra="forbid")

    items: List[StormRoadRestriction]
    total: int


# Maximum number of active restrictions returned by the GET endpoint in a
# single call. Tenants rarely carry more than a handful of concurrent
# road-closure polygons (DOT feeds rotate with the storm), so a hard cap
# here protects the dispatcher UI from runaway payloads without needing
# pagination. Above the cap the response is truncated and the caller can
# narrow with filters in a future iteration.
_STORM_ROAD_RESTRICTION_LIST_CEILING: int = 500


@router.post(
    "/storm-mode/road-restrictions",
    response_model=StormRoadRestriction,
    status_code=status.HTTP_201_CREATED,
)
async def upload_storm_road_restriction(
    body: StormRoadRestrictionCreateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> StormRoadRestriction:
    """Persist a tenant-uploaded Storm_Mode road-restriction polygon.

    Flow:

        1. Enforce the Req 9.4.4 role gate — only ``dispatcher`` or
           ``admin`` roles may upload restrictions. Other callers
           receive HTTP 403 with ``INSUFFICIENT_ROLE``. The same role
           lexicon the Storm_Mode override endpoint uses is applied
           here so tenants don't need a parallel authorization
           surface.
        2. Validate the body through :class:`StormRoadRestriction`
           itself so the same invariants enforced on agent-side reads
           (polygon geometry, severity enum, effective-window order)
           are enforced on the write path. The router stamps
           ``tenant_id`` from the JWT context and mints a
           ``restriction_id`` (``srr_<uuid4>``) so the caller cannot
           spoof ownership or reuse an existing id.
        3. Persist to the ``storm_road_restrictions`` ES index via
           :meth:`es.index_document`. The mapping is ``dynamic:
           strict`` so any field the request model misses surfaces as
           an ES validation error which we map back to 422.
        4. Return the persisted :class:`StormRoadRestriction`. The
           :class:`RoutePlanningAgent` will pick up new restrictions
           on its next plan build; the GET endpoint below reads the
           index directly so the dispatcher map reflects the upload
           immediately.

    Error modes:

        * 403 ``INSUFFICIENT_ROLE`` — caller lacks dispatcher/admin role.
        * 422 ``validation_error`` — body failed Pydantic validation
          (malformed polygon, unknown severity, closed ring missing,
          etc.).
        * 503 — persistence layer unreachable. We surface ``str(exc)``
          in the detail so the operator can diagnose upstream issues.

    Validates: Requirements 9.3.3, 9.3.5.
    """

    _ensure_storm_mode_override_role(tenant)

    es = _get_es()
    now = datetime.now(timezone.utc)
    restriction_id = f"srr_{uuid4().hex}"

    try:
        restriction = StormRoadRestriction(
            restriction_id=restriction_id,
            tenant_id=tenant.tenant_id,
            polygon=body.polygon,
            effective_from=body.effective_from,
            effective_to=body.effective_to,
            source=body.source,
            severity=body.severity,
            reason=body.reason,
            created_at=now,
            updated_at=now,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise _translate_validation_error(exc)

    try:
        await es.index_document(
            STORM_ROAD_RESTRICTIONS_INDEX,
            restriction.restriction_id,
            restriction.model_dump(mode="json"),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "fuel_ops.storm_road_restriction: persistence failed tenant=%s "
            "restriction=%s severity=%s source=%s",
            tenant.tenant_id,
            restriction.restriction_id,
            restriction.severity,
            restriction.source,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "storm_road_restriction_persistence_failed",
                "message": str(exc),
            },
        )

    logger.info(
        "fuel_ops.storm_road_restriction: tenant=%s restriction=%s "
        "severity=%s source=%s effective_from=%s effective_to=%s",
        tenant.tenant_id,
        restriction.restriction_id,
        restriction.severity,
        restriction.source,
        restriction.effective_from.isoformat(),
        restriction.effective_to.isoformat()
        if restriction.effective_to
        else "open",
    )

    return restriction


@router.get(
    "/storm-mode/road-restrictions",
    response_model=StormRoadRestrictionListResponse,
)
async def list_storm_road_restrictions(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    severity: Optional[WeatherAlertSeverity] = Query(
        default=None,
        description=(
            "Filter the returned restrictions to a specific severity "
            "bucket (minor/moderate/severe/extreme). When omitted all "
            "active severities are returned so the dispatcher map can "
            "render them with distinct colours."
        ),
    ),
    include_expired: bool = Query(
        default=False,
        description=(
            "When ``true``, include restrictions whose ``effective_to`` "
            "is in the past. Defaults to ``false`` so the dispatcher "
            "map only shows currently-applicable closures. The "
            "Route_Planning_Agent uses its own per-plan effective-window "
            "filter so this flag does not affect routing."
        ),
    ),
) -> StormRoadRestrictionListResponse:
    """Return active Storm_Mode road-restriction polygons for the tenant.

    The dispatcher UI's map layer calls this endpoint to render every
    active polygon with its severity-coded colour, source label, and
    optional reason. By default only currently-applicable restrictions
    (``effective_from <= now`` and either ``effective_to`` is null or
    ``effective_to >= now``) are returned; pass ``include_expired=true``
    to include restrictions whose ``effective_to`` has passed for
    historical review.

    Tenant scoping is enforced both by the ES filter (``term`` on
    ``tenant_id``) and by a defensive per-row re-check that drops any
    document whose ``tenant_id`` does not match the caller.

    Error modes:

        * 503 — ES unreachable. We surface ``str(exc)`` in the detail
          so the operator can diagnose upstream issues.

    Validates: Requirement 9.3.5.
    """

    es = _get_es()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    filters: List[Dict[str, Any]] = [
        {"term": {"tenant_id": tenant.tenant_id}},
    ]
    if severity is not None:
        filters.append({"term": {"severity": severity}})
    if not include_expired:
        # Only surface restrictions whose ``effective_from`` has arrived
        # and whose ``effective_to`` (when set) has not yet passed.
        filters.append({"range": {"effective_from": {"lte": now_iso}}})
        filters.append(
            {
                "bool": {
                    "should": [
                        {
                            "bool": {
                                "must_not": {
                                    "exists": {"field": "effective_to"}
                                }
                            }
                        },
                        {"range": {"effective_to": {"gte": now_iso}}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )

    query: Dict[str, Any] = {
        "query": {"bool": {"filter": filters}},
        "sort": [{"effective_from": {"order": "desc"}}],
    }

    try:
        resp = await es.search_documents(
            STORM_ROAD_RESTRICTIONS_INDEX,
            query,
            _STORM_ROAD_RESTRICTION_LIST_CEILING,
        )
    except Exception as exc:
        logger.exception(
            "fuel_ops.storm_road_restriction: search failed tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "storm_road_restriction_search_failed",
                "message": str(exc),
            },
        )

    hits = (resp or {}).get("hits", {}).get("hits", []) or []
    items: List[StormRoadRestriction] = []
    for hit in hits:
        source = hit.get("_source") or {}
        if source.get("tenant_id") != tenant.tenant_id:
            logger.warning(
                "fuel_ops.storm_road_restriction: dropping row with "
                "mismatched tenant_id %s (expected %s)",
                source.get("tenant_id"),
                tenant.tenant_id,
            )
            continue
        try:
            items.append(StormRoadRestriction(**source))
        except ValidationError as exc:
            logger.warning(
                "fuel_ops.storm_road_restriction: dropping malformed row "
                "(restriction_id=%s) for tenant=%s: %s",
                source.get("restriction_id"),
                tenant.tenant_id,
                exc,
            )

    logger.debug(
        "fuel_ops.storm_road_restriction: tenant=%s returned %d "
        "restrictions (severity=%s include_expired=%s)",
        tenant.tenant_id,
        len(items),
        severity or "any",
        include_expired,
    )

    return StormRoadRestrictionListResponse(items=items, total=len(items))
