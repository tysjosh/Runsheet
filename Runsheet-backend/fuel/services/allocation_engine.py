"""
Critical-infrastructure allocation and rationing engine.

When available gallons are constrained during storms, DOT declarations, or
terminal outages, dispatch needs deterministic allocation decisions instead of
first-come-first-served fulfillment. This module scores customer requests by
criticality, runout risk, generator/continuous-service flags, and firm-order
status, then allocates the constrained supply in that order.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fuel.services.fuel_product_catalog import canonicalize
from fuel.storm_mode_models import CriticalityTier


AllocationReason = Literal[
    "critical_infrastructure",
    "generator_fuel",
    "continuous_service",
    "runout_risk",
    "firm_order",
    "minimum_allocation_applied",
    "rationed_partial",
    "rationed_none",
]


TIER_PRIORITY: Dict[CriticalityTier, float] = {
    "medical": 1.0,
    "data_center": 0.92,
    "industrial_critical": 0.84,
    "keep_full_residential": 0.72,
    "commercial": 0.45,
    "standard": 0.2,
}


class AllocationRequest(BaseModel):
    """A single customer demand competing for constrained supply."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(..., min_length=1)
    customer_id: str = Field(..., min_length=1)
    product_code: str = Field(..., min_length=1)
    requested_gallons: float = Field(..., gt=0)
    criticality_tier: CriticalityTier = "standard"
    current_level_pct: Optional[float] = Field(default=None, ge=0, le=100)
    hours_to_runout_p90: Optional[float] = Field(default=None, ge=0)
    is_generator_fuel: bool = False
    requires_continuous_service: bool = False
    firm_order: bool = False

    @field_validator("tenant_id", "customer_id", "product_code", mode="before")
    @classmethod
    def _strip_required_strings(cls, value):
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("product_code", mode="after")
    @classmethod
    def _canonical_product(cls, value: str) -> str:
        return canonicalize(value)


class AllocationPolicy(BaseModel):
    """Supply and rationing knobs for one allocation run."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(..., min_length=1)
    product_code: str = Field(..., min_length=1)
    available_gallons: float = Field(..., ge=0)
    minimum_allocation_gallons: float = Field(default=0, ge=0)
    minimum_allocation_for_critical_only: bool = True

    @field_validator("tenant_id", "product_code", mode="before")
    @classmethod
    def _strip_required_strings(cls, value):
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("product_code", mode="after")
    @classmethod
    def _canonical_product(cls, value: str) -> str:
        return canonicalize(value)


class AllocationDecision(BaseModel):
    """Allocation result for one customer request."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    customer_id: str
    product_code: str
    requested_gallons: float
    approved_gallons: float = Field(..., ge=0)
    rationed_gallons: float = Field(..., ge=0)
    priority_score: float = Field(..., ge=0, le=1)
    reason_codes: List[AllocationReason] = Field(default_factory=list)


@dataclass(frozen=True)
class _ScoredRequest:
    request: AllocationRequest
    score: float
    reasons: Tuple[AllocationReason, ...]


class AllocationEngine:
    """Allocate constrained supply across critical-infrastructure requests."""

    def allocate(
        self,
        *,
        policy: AllocationPolicy,
        requests: Sequence[AllocationRequest],
    ) -> List[AllocationDecision]:
        if not requests:
            return []

        scoped = [
            request
            for request in requests
            if request.tenant_id == policy.tenant_id
            and request.product_code == policy.product_code
        ]
        scored = sorted(
            (self._score(request) for request in scoped),
            key=lambda item: (
                -item.score,
                item.request.hours_to_runout_p90
                if item.request.hours_to_runout_p90 is not None
                else float("inf"),
                item.request.customer_id,
            ),
        )

        remaining = float(policy.available_gallons)
        decisions: List[AllocationDecision] = []
        for item in scored:
            request = item.request
            approved = min(request.requested_gallons, remaining)
            reasons = list(item.reasons)

            if approved > 0 and self._eligible_for_minimum(policy, request):
                minimum = min(policy.minimum_allocation_gallons, request.requested_gallons)
                if approved < minimum and remaining >= minimum:
                    approved = minimum
                    reasons.append("minimum_allocation_applied")

            remaining = max(0.0, remaining - approved)
            rationed = max(0.0, request.requested_gallons - approved)
            if rationed >= request.requested_gallons:
                reasons.append("rationed_none")
            elif rationed > 0:
                reasons.append("rationed_partial")

            decisions.append(
                AllocationDecision(
                    tenant_id=request.tenant_id,
                    customer_id=request.customer_id,
                    product_code=request.product_code,
                    requested_gallons=request.requested_gallons,
                    approved_gallons=round(approved, 3),
                    rationed_gallons=round(rationed, 3),
                    priority_score=round(item.score, 6),
                    reason_codes=_dedupe(reasons),
                )
            )

        return decisions

    def _score(self, request: AllocationRequest) -> _ScoredRequest:
        score = TIER_PRIORITY[request.criticality_tier]
        reasons: List[AllocationReason] = []

        if request.criticality_tier in {
            "medical",
            "data_center",
            "industrial_critical",
            "keep_full_residential",
        }:
            reasons.append("critical_infrastructure")

        if request.is_generator_fuel:
            score += 0.08
            reasons.append("generator_fuel")
        if request.requires_continuous_service:
            score += 0.08
            reasons.append("continuous_service")
        if request.firm_order:
            score += 0.04
            reasons.append("firm_order")

        runout_score = _runout_score(
            hours_to_runout_p90=request.hours_to_runout_p90,
            current_level_pct=request.current_level_pct,
        )
        if runout_score > 0:
            score += runout_score
            reasons.append("runout_risk")

        return _ScoredRequest(
            request=request,
            score=max(0.0, min(1.0, score)),
            reasons=tuple(reasons),
        )

    @staticmethod
    def _eligible_for_minimum(
        policy: AllocationPolicy, request: AllocationRequest
    ) -> bool:
        if policy.minimum_allocation_gallons <= 0:
            return False
        if not policy.minimum_allocation_for_critical_only:
            return True
        return request.criticality_tier in {
            "medical",
            "data_center",
            "industrial_critical",
            "keep_full_residential",
        }


def _runout_score(
    *, hours_to_runout_p90: Optional[float], current_level_pct: Optional[float]
) -> float:
    if hours_to_runout_p90 is not None:
        if hours_to_runout_p90 <= 12:
            return 0.18
        if hours_to_runout_p90 <= 24:
            return 0.12
        if hours_to_runout_p90 <= 48:
            return 0.06
    if current_level_pct is not None:
        if current_level_pct <= 10:
            return 0.14
        if current_level_pct <= 20:
            return 0.08
        if current_level_pct <= 30:
            return 0.04
    return 0.0


def _dedupe(values: Iterable[AllocationReason]) -> List[AllocationReason]:
    out: List[AllocationReason] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


__all__ = [
    "AllocationDecision",
    "AllocationEngine",
    "AllocationPolicy",
    "AllocationReason",
    "AllocationRequest",
    "TIER_PRIORITY",
]
