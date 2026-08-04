"""
Compartment data models for the Loading Agent.

Extends base compartment models with min_drop_liters, uncertainty_buffer_pct,
and max_weight_kg constraints.

Validates: Requirements 3.1, 3.5, 3.7, 3.8, 3.10
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from Agents.support.fuel_distribution_models import FuelGrade


class Compartment(BaseModel):
    compartment_id: str
    truck_id: str
    capacity_liters: float = Field(gt=0)
    allowed_grades: List[FuelGrade] = Field(min_length=1)
    position_index: int = Field(ge=0)
    tenant_id: str


class DeliveryRequest(BaseModel):
    station_id: str
    # Preserve the originating FuelOrder through the loading solver.  Legacy
    # station-priority requests have no order and therefore leave this unset.
    order_id: Optional[str] = None
    fuel_grade: FuelGrade
    # Canonical US product code (DIESEL_2, DEF, ...).  ``fuel_grade`` above is
    # the coarse family the solver matches compartments on, and nine catalog
    # products collapse onto four grades — so the grade alone cannot tell DEF
    # (1.09 kg/L) from diesel (0.85).  Axle weight needs the exact product,
    # hence this field.  Optional: legacy station-priority requests carry no
    # product code and fall back to the grade.
    product_code: Optional[str] = None
    quantity_liters: float = Field(gt=0)
    min_drop_liters: float = Field(default=500.0, ge=0)


class CompartmentAssignment(BaseModel):
    compartment_id: str
    station_id: str
    # Exact FuelOrder carried by this load assignment.  Optional keeps plans
    # written before the send-to-driver workflow readable.
    order_id: Optional[str] = None
    fuel_grade: str
    # Canonical product code carried through from the DeliveryRequest so a
    # persisted plan can be re-weighed correctly.  Without it, recomputing
    # ``total_weight_kg`` from a stored plan would have to re-derive the
    # product from the order, and would weigh DEF as diesel if it could not.
    product_code: Optional[str] = None
    quantity_liters: float = Field(gt=0)
    compartment_capacity_liters: float = Field(gt=0)


class LoadingPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    truck_id: str
    assignments: List[CompartmentAssignment]
    total_utilization_pct: float = Field(ge=0.0, le=100.0)
    unserved_demand_liters: float = Field(default=0.0, ge=0.0)
    total_weight_kg: float = Field(default=0.0, ge=0.0)
    tenant_id: str
    run_id: str = ""
    #: Optional :class:`fuel.terminal_models.SupplierContract` id that
    #: sourced the load. When set, committing the plan bumps the
    #: tenant's monthly rolling-lift counter for this contract in Redis
    #: (Task 7.6 / Req 8.3.4). Defaults to ``None`` so legacy call
    #: sites that do not yet go through the Sourcing_Recommender keep
    #: working unchanged.
    contract_id: Optional[str] = None
    #: Optional terminal id carried alongside ``contract_id`` so audit
    #: queries can correlate the counter bump with the terminal lift.
    terminal_id: Optional[str] = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    status: str = "proposed"


class ConstraintViolation(BaseModel):
    violation_type: str
    fuel_grade: Optional[str] = None
    shortfall_liters: Optional[float] = None
    message: str


class FeasibilityResult(BaseModel):
    feasible: bool
    max_utilization_pct: float = 0.0
    violations: List[ConstraintViolation] = Field(default_factory=list)
