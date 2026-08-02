"""
Support package for the Fuel Distribution MVP pipeline.

Contains shared data contracts, compartment models, solvers,
ES index mappings, pipeline coordinator, and REST endpoints.
"""

# Data contracts
from Agents.support.fuel_distribution_models import (
    FuelGrade,
    PriorityBucket,
    TankForecast,
    DeliveryPriority,
    DeliveryPriorityList,
    RoutePlan,
    RouteStop,
    ReplanDiff,
    ReplanEvent,
)

# Compartment models
from Agents.support.compartment_models import (
    Compartment,
    CompartmentAssignment,
    ConstraintViolation,
    DeliveryRequest,
    FeasibilityResult,
    LoadingPlan,
)

# Compartment solver
from Agents.support.compartment_solver import (
    check_feasibility,
    optimize_loading_plan,
    FUEL_DENSITY,
    DEFAULT_UNCERTAINTY_BUFFER_PCT,
)

# Route solver
from Agents.support.route_solver import (
    compute_distance,
    build_distance_matrix,
    nearest_neighbor_route,
    two_opt_improve,
    check_sla_windows,
    optimize_route,
    optimize_route_large,
    insert_emergency_stop,
    InfeasibleInsertion,
    StopCapExceededError,
    DEFAULT_ROUTING_MAX_STOPS,
    CLUSTERING_STOP_THRESHOLD,
    DEFAULT_MAX_STOPS_PER_CLUSTER,
    INSERT_REASON_CAPACITY,
    INSERT_REASON_SLA,
    INSERT_REASON_OFF_DUTY,
)

# ES mappings
from Agents.support.mvp_es_mappings import setup_mvp_indices

# Pipeline coordinator
from Agents.support.fuel_distribution_pipeline import (
    STAGE_RESULT_COMPLETED,
    STAGE_RESULT_DEGRADED,
    FuelDistributionPipeline,
    PipelineState,
    broadcast_pipeline_event,
    read_agent_degradation,
)

__all__ = [
    # Data contracts
    "FuelGrade",
    "PriorityBucket",
    "TankForecast",
    "DeliveryPriority",
    "DeliveryPriorityList",
    "RoutePlan",
    "RouteStop",
    "ReplanDiff",
    "ReplanEvent",
    # Compartment models
    "Compartment",
    "CompartmentAssignment",
    "ConstraintViolation",
    "DeliveryRequest",
    "FeasibilityResult",
    "LoadingPlan",
    # Compartment solver
    "check_feasibility",
    "optimize_loading_plan",
    "FUEL_DENSITY",
    "DEFAULT_UNCERTAINTY_BUFFER_PCT",
    # Route solver
    "compute_distance",
    "build_distance_matrix",
    "nearest_neighbor_route",
    "two_opt_improve",
    "check_sla_windows",
    "optimize_route",
    "optimize_route_large",
    "insert_emergency_stop",
    "InfeasibleInsertion",
    "StopCapExceededError",
    "DEFAULT_ROUTING_MAX_STOPS",
    "CLUSTERING_STOP_THRESHOLD",
    "DEFAULT_MAX_STOPS_PER_CLUSTER",
    "INSERT_REASON_CAPACITY",
    "INSERT_REASON_SLA",
    "INSERT_REASON_OFF_DUTY",
    # ES mappings
    "setup_mvp_indices",
    # Pipeline coordinator
    "FuelDistributionPipeline",
    "PipelineState",
    "STAGE_RESULT_COMPLETED",
    "STAGE_RESULT_DEGRADED",
    "broadcast_pipeline_event",
    "read_agent_degradation",
]
