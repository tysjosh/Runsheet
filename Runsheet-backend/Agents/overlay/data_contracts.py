"""
Standardized data contracts for inter-agent communication.

Defines RiskSignal, InterventionProposal, OutcomeRecord, and
PolicyChangeProposal as Pydantic v2 models with schema versioning,
JSON round-trip support, and strict field validation.

Validates: Requirements 1.1–1.8
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskClass(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskSignal(BaseModel):
    """Signal emitted by Layer 0 agents when a condition is detected.

    Validates: Requirement 1.1, 1.5, 1.6, 1.7
    """
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_agent: str
    entity_id: str
    entity_type: str
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    ttl_seconds: int = Field(gt=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    context: Dict[str, Any] = Field(default_factory=dict)
    tenant_id: str
    schema_version: str = "1.0.0"

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        return round(v, 4)


class InterventionProposal(BaseModel):
    """Proposal produced by Layer 1 agents containing ranked actions.

    Validates: Requirement 1.2, 1.5, 1.8
    """
    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_agent: str
    actions: List[Dict[str, Any]]
    expected_kpi_delta: Dict[str, float]
    risk_class: RiskClass
    confidence: float = Field(ge=0.0, le=1.0)
    priority: int = Field(ge=0)
    tenant_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = "1.0.0"


class OutcomeRecord(BaseModel):
    """Record of an executed intervention's before/after KPIs.

    Validates: Requirement 1.3
    """
    outcome_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    intervention_id: str  # References InterventionProposal.proposal_id
    before_kpis: Dict[str, float]
    after_kpis: Dict[str, float]
    realized_delta: Dict[str, float]
    execution_duration_ms: float = Field(ge=0.0)
    tenant_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "measured"  # measured | adverse | inconclusive
    schema_version: str = "1.0.0"


class PolicyChangeProposal(BaseModel):
    """Proposal to adjust a system parameter, produced by LearningPolicyAgent.

    Validates: Requirement 1.4
    """
    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_agent: str
    parameter: str
    old_value: Any
    new_value: Any
    evidence: List[str] = Field(default_factory=list)  # OutcomeRecord IDs
    rollback_plan: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    tenant_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = "1.0.0"
