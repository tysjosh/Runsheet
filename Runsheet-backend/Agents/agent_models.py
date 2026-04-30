"""
Pydantic v2 models for agent data structures.

Defines validated models matching the Elasticsearch index field structures
for the agentic AI transformation layer. These models are used for
request/response validation in REST endpoints and internal service
communication.

Requirements: 2.1, 8.1, 10.1, 11.1, 12.3
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RiskLevel(str, Enum):
    """Risk classification for mutation tools.

    Validates: Requirement 1.4
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AutonomyLevel(str, Enum):
    """Per-tenant autonomy level controlling agent authority.

    Validates: Requirement 10.1
    """

    SUGGEST_ONLY = "suggest-only"
    AUTO_LOW = "auto-low"
    AUTO_MEDIUM = "auto-medium"
    FULL_AUTO = "full-auto"


class ApprovalStatus(str, Enum):
    """Lifecycle status for approval queue entries.

    Valid transitions: pending → approved → executed,
    pending → rejected, pending → expired.

    Validates: Requirement 2.1
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"


# ---------------------------------------------------------------------------
# Data models (matching ES index field structures)
# ---------------------------------------------------------------------------


class ApprovalEntry(BaseModel):
    """Model for the ``agent_approval_queue`` Elasticsearch index.

    Validates: Requirement 2.1
    """

    action_id: str
    action_type: str
    tool_name: str
    parameters: Dict[str, Any]
    risk_level: RiskLevel
    proposed_by: str
    proposed_at: datetime
    status: ApprovalStatus
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    expiry_time: datetime
    impact_summary: str
    execution_result: Optional[Dict[str, Any]] = None
    tenant_id: str


class ActivityLogEntry(BaseModel):
    """Model for the ``agent_activity_log`` Elasticsearch index.

    Validates: Requirement 8.1
    """

    log_id: str
    agent_id: str
    action_type: str  # query, mutation, plan, monitoring_cycle, detection, approval_request
    tool_name: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    risk_level: Optional[RiskLevel] = None
    outcome: str  # success, failure, pending_approval, rejected
    duration_ms: float
    tenant_id: str
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    timestamp: datetime
    details: Optional[Dict[str, Any]] = None


class MemoryEntry(BaseModel):
    """Model for the ``agent_memory`` Elasticsearch index.

    Validates: Requirement 11.1
    """

    memory_id: str
    memory_type: str  # pattern, preference, feedback
    agent_id: str
    tenant_id: str
    content: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    created_at: datetime
    last_accessed: datetime
    access_count: int = 0
    tags: List[str] = []


class FeedbackSignal(BaseModel):
    """Model for the ``agent_feedback`` Elasticsearch index.

    Validates: Requirement 12.3
    """

    feedback_id: str
    agent_id: str
    action_type: str
    original_proposal: Dict[str, Any]
    user_action: Optional[Dict[str, Any]] = None
    feedback_type: str  # rejection, override, correction
    tenant_id: str
    user_id: str
    timestamp: datetime
    context: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AutonomyUpdateRequest(BaseModel):
    """Request body for updating a tenant's autonomy level.

    Validates: Requirement 10.1
    """

    level: AutonomyLevel


class ApprovalRejectRequest(BaseModel):
    """Request body for rejecting an approval queue entry.

    Validates: Requirement 2.1
    """

    reason: str = ""
