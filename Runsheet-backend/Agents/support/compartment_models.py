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
    fuel_grade: FuelGrade
    quantity_liters: float = Field(gt=0)
    min_drop_liters: float = Field(default=500.0, ge=0)


class CompartmentAssignment(BaseModel):
    compartment_id: str
    station_id: str
    fuel_grade: str
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
