"""
Unit tests for the agent Pydantic models.

Tests the enums (RiskLevel, AutonomyLevel, ApprovalStatus), data models
(ApprovalEntry, ActivityLogEntry, MemoryEntry, FeedbackSignal), and
request models (AutonomyUpdateRequest, ApprovalRejectRequest).

Requirements: 2.1, 8.1, 10.1, 11.1, 12.3
"""

import pytest
from datetime import datetime, timezone
from pydantic import ValidationError

from Agents.agent_models import (
    RiskLevel,
    AutonomyLevel,
    ApprovalStatus,
    ApprovalEntry,
    ActivityLogEntry,
    MemoryEntry,
    FeedbackSignal,
    AutonomyUpdateRequest,
    ApprovalRejectRequest,
)


# ---------------------------------------------------------------------------
# Enum Tests
# ---------------------------------------------------------------------------


class TestRiskLevel:
    """Tests for the RiskLevel enum."""

    def test_values(self):
        assert RiskLevel.LOW == "low"
        assert RiskLevel.MEDIUM == "medium"
        assert RiskLevel.HIGH == "high"

    def test_is_string(self):
        for member in RiskLevel:
            assert isinstance(member, str)

    def test_from_value(self):
        assert RiskLevel("low") is RiskLevel.LOW
        assert RiskLevel("medium") is RiskLevel.MEDIUM
        assert RiskLevel("high") is RiskLevel.HIGH

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            RiskLevel("critical")

    def test_member_count(self):
        assert len(RiskLevel) == 3


class TestAutonomyLevel:
    """Tests for the AutonomyLevel enum."""

    def test_values(self):
        assert AutonomyLevel.SUGGEST_ONLY == "suggest-only"
        assert AutonomyLevel.AUTO_LOW == "auto-low"
        assert AutonomyLevel.AUTO_MEDIUM == "auto-medium"
        assert AutonomyLevel.FULL_AUTO == "full-auto"

    def test_is_string(self):
        for member in AutonomyLevel:
            assert isinstance(member, str)

    def test_from_value(self):
        assert AutonomyLevel("suggest-only") is AutonomyLevel.SUGGEST_ONLY
        assert AutonomyLevel("auto-low") is AutonomyLevel.AUTO_LOW
        assert AutonomyLevel("auto-medium") is AutonomyLevel.AUTO_MEDIUM
        assert AutonomyLevel("full-auto") is AutonomyLevel.FULL_AUTO

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            AutonomyLevel("manual")

    def test_member_count(self):
        assert len(AutonomyLevel) == 4


class TestApprovalStatus:
    """Tests for the ApprovalStatus enum."""

    def test_values(self):
        assert ApprovalStatus.PENDING == "pending"
        assert ApprovalStatus.APPROVED == "approved"
        assert ApprovalStatus.REJECTED == "rejected"
        assert ApprovalStatus.EXPIRED == "expired"
        assert ApprovalStatus.EXECUTED == "executed"

    def test_is_string(self):
        for member in ApprovalStatus:
            assert isinstance(member, str)

    def test_from_value(self):
        assert ApprovalStatus("pending") is ApprovalStatus.PENDING
        assert ApprovalStatus("executed") is ApprovalStatus.EXECUTED

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            ApprovalStatus("cancelled")

    def test_member_count(self):
        assert len(ApprovalStatus) == 5


# ---------------------------------------------------------------------------
# ApprovalEntry Tests
# ---------------------------------------------------------------------------


class TestApprovalEntry:
    """Tests for the ApprovalEntry model."""

    @pytest.fixture
    def valid_data(self):
        return {
            "action_id": "act-001",
            "action_type": "mutation",
            "tool_name": "cancel_job",
            "parameters": {"job_id": "JOB-123", "reason": "delayed"},
            "risk_level": "high",
            "proposed_by": "delay_response_agent",
            "proposed_at": "2024-06-15T10:30:00Z",
            "status": "pending",
            "expiry_time": "2024-06-15T11:30:00Z",
            "impact_summary": "Cancel job JOB-123 due to delay",
            "tenant_id": "tenant-1",
        }

    def test_valid_entry(self, valid_data):
        entry = ApprovalEntry(**valid_data)
        assert entry.action_id == "act-001"
        assert entry.risk_level == RiskLevel.HIGH
        assert entry.status == ApprovalStatus.PENDING
        assert entry.reviewed_by is None
        assert entry.reviewed_at is None
        assert entry.execution_result is None

    def test_optional_fields_populated(self, valid_data):
        valid_data["reviewed_by"] = "admin-user"
        valid_data["reviewed_at"] = "2024-06-15T10:45:00Z"
        valid_data["execution_result"] = {"success": True, "message": "Job cancelled"}
        entry = ApprovalEntry(**valid_data)
        assert entry.reviewed_by == "admin-user"
        assert entry.reviewed_at is not None
        assert entry.execution_result == {"success": True, "message": "Job cancelled"}

    def test_missing_required_field_raises(self, valid_data):
        del valid_data["action_id"]
        with pytest.raises(ValidationError):
            ApprovalEntry(**valid_data)

    def test_invalid_risk_level_raises(self, valid_data):
        valid_data["risk_level"] = "critical"
        with pytest.raises(ValidationError):
            ApprovalEntry(**valid_data)

    def test_invalid_status_raises(self, valid_data):
        valid_data["status"] = "cancelled"
        with pytest.raises(ValidationError):
            ApprovalEntry(**valid_data)

    def test_all_statuses_accepted(self, valid_data):
        for status in ApprovalStatus:
            valid_data["status"] = status.value
            entry = ApprovalEntry(**valid_data)
            assert entry.status == status

    def test_all_risk_levels_accepted(self, valid_data):
        for level in RiskLevel:
            valid_data["risk_level"] = level.value
            entry = ApprovalEntry(**valid_data)
            assert entry.risk_level == level

    def test_datetime_parsing(self, valid_data):
        entry = ApprovalEntry(**valid_data)
        assert isinstance(entry.proposed_at, datetime)
        assert isinstance(entry.expiry_time, datetime)

    def test_parameters_accepts_nested_dict(self, valid_data):
        valid_data["parameters"] = {
            "job_id": "JOB-123",
            "nested": {"key": "value", "list": [1, 2, 3]},
        }
        entry = ApprovalEntry(**valid_data)
        assert entry.parameters["nested"]["key"] == "value"


# ---------------------------------------------------------------------------
# ActivityLogEntry Tests
# ---------------------------------------------------------------------------


class TestActivityLogEntry:
    """Tests for the ActivityLogEntry model."""

    @pytest.fixture
    def valid_data(self):
        return {
            "log_id": "log-001",
            "agent_id": "delay_response_agent",
            "action_type": "monitoring_cycle",
            "outcome": "success",
            "duration_ms": 150.5,
            "tenant_id": "tenant-1",
            "timestamp": "2024-06-15T10:30:00Z",
        }

    def test_valid_entry_minimal(self, valid_data):
        entry = ActivityLogEntry(**valid_data)
        assert entry.log_id == "log-001"
        assert entry.agent_id == "delay_response_agent"
        assert entry.action_type == "monitoring_cycle"
        assert entry.outcome == "success"
        assert entry.duration_ms == 150.5
        assert entry.tool_name is None
        assert entry.parameters is None
        assert entry.risk_level is None
        assert entry.user_id is None
        assert entry.session_id is None
        assert entry.details is None

    def test_valid_entry_full(self, valid_data):
        valid_data.update({
            "tool_name": "cancel_job",
            "parameters": {"job_id": "JOB-123"},
            "risk_level": "high",
            "user_id": "user-42",
            "session_id": "sess-abc",
            "details": {"extra": "info"},
        })
        entry = ActivityLogEntry(**valid_data)
        assert entry.tool_name == "cancel_job"
        assert entry.risk_level == RiskLevel.HIGH
        assert entry.user_id == "user-42"
        assert entry.details == {"extra": "info"}

    def test_missing_required_field_raises(self, valid_data):
        del valid_data["outcome"]
        with pytest.raises(ValidationError):
            ActivityLogEntry(**valid_data)

    def test_invalid_risk_level_raises(self, valid_data):
        valid_data["risk_level"] = "extreme"
        with pytest.raises(ValidationError):
            ActivityLogEntry(**valid_data)

    def test_duration_ms_accepts_zero(self, valid_data):
        valid_data["duration_ms"] = 0.0
        entry = ActivityLogEntry(**valid_data)
        assert entry.duration_ms == 0.0

    def test_timestamp_parsing(self, valid_data):
        entry = ActivityLogEntry(**valid_data)
        assert isinstance(entry.timestamp, datetime)


# ---------------------------------------------------------------------------
# MemoryEntry Tests
# ---------------------------------------------------------------------------


class TestMemoryEntry:
    """Tests for the MemoryEntry model."""

    @pytest.fixture
    def valid_data(self):
        return {
            "memory_id": "mem-001",
            "memory_type": "pattern",
            "agent_id": "orchestrator",
            "tenant_id": "tenant-1",
            "content": "Truck T-1042 is frequently delayed on Route 7",
            "confidence_score": 0.85,
            "created_at": "2024-06-15T10:30:00Z",
            "last_accessed": "2024-06-15T12:00:00Z",
        }

    def test_valid_entry_minimal(self, valid_data):
        entry = MemoryEntry(**valid_data)
        assert entry.memory_id == "mem-001"
        assert entry.memory_type == "pattern"
        assert entry.confidence_score == 0.85
        assert entry.access_count == 0
        assert entry.tags == []

    def test_valid_entry_with_tags(self, valid_data):
        valid_data["tags"] = ["route-7", "truck-T-1042", "delay"]
        valid_data["access_count"] = 5
        entry = MemoryEntry(**valid_data)
        assert entry.tags == ["route-7", "truck-T-1042", "delay"]
        assert entry.access_count == 5

    def test_confidence_score_at_boundaries(self, valid_data):
        valid_data["confidence_score"] = 0.0
        entry = MemoryEntry(**valid_data)
        assert entry.confidence_score == 0.0

        valid_data["confidence_score"] = 1.0
        entry = MemoryEntry(**valid_data)
        assert entry.confidence_score == 1.0

    def test_confidence_score_below_zero_raises(self, valid_data):
        valid_data["confidence_score"] = -0.1
        with pytest.raises(ValidationError):
            MemoryEntry(**valid_data)

    def test_confidence_score_above_one_raises(self, valid_data):
        valid_data["confidence_score"] = 1.1
        with pytest.raises(ValidationError):
            MemoryEntry(**valid_data)

    def test_missing_required_field_raises(self, valid_data):
        del valid_data["content"]
        with pytest.raises(ValidationError):
            MemoryEntry(**valid_data)

    def test_preference_memory_type(self, valid_data):
        valid_data["memory_type"] = "preference"
        valid_data["content"] = "Always notify me before reassignments"
        entry = MemoryEntry(**valid_data)
        assert entry.memory_type == "preference"

    def test_datetime_parsing(self, valid_data):
        entry = MemoryEntry(**valid_data)
        assert isinstance(entry.created_at, datetime)
        assert isinstance(entry.last_accessed, datetime)


# ---------------------------------------------------------------------------
# FeedbackSignal Tests
# ---------------------------------------------------------------------------


class TestFeedbackSignal:
    """Tests for the FeedbackSignal model."""

    @pytest.fixture
    def valid_data(self):
        return {
            "feedback_id": "fb-001",
            "agent_id": "delay_response_agent",
            "action_type": "reassign_rider",
            "original_proposal": {
                "tool_name": "reassign_rider",
                "parameters": {"shipment_id": "SH-100", "new_rider_id": "R-5"},
            },
            "feedback_type": "rejection",
            "tenant_id": "tenant-1",
            "user_id": "user-42",
            "timestamp": "2024-06-15T10:30:00Z",
        }

    def test_valid_entry_minimal(self, valid_data):
        entry = FeedbackSignal(**valid_data)
        assert entry.feedback_id == "fb-001"
        assert entry.feedback_type == "rejection"
        assert entry.user_action is None
        assert entry.context is None

    def test_valid_entry_full(self, valid_data):
        valid_data["user_action"] = {"action": "manual_reassign", "rider_id": "R-10"}
        valid_data["context"] = {"reason": "Rider R-5 is on break"}
        entry = FeedbackSignal(**valid_data)
        assert entry.user_action["rider_id"] == "R-10"
        assert entry.context["reason"] == "Rider R-5 is on break"

    def test_missing_required_field_raises(self, valid_data):
        del valid_data["user_id"]
        with pytest.raises(ValidationError):
            FeedbackSignal(**valid_data)

    def test_override_feedback_type(self, valid_data):
        valid_data["feedback_type"] = "override"
        entry = FeedbackSignal(**valid_data)
        assert entry.feedback_type == "override"

    def test_correction_feedback_type(self, valid_data):
        valid_data["feedback_type"] = "correction"
        entry = FeedbackSignal(**valid_data)
        assert entry.feedback_type == "correction"

    def test_original_proposal_nested_structure(self, valid_data):
        entry = FeedbackSignal(**valid_data)
        assert entry.original_proposal["tool_name"] == "reassign_rider"
        assert entry.original_proposal["parameters"]["shipment_id"] == "SH-100"

    def test_timestamp_parsing(self, valid_data):
        entry = FeedbackSignal(**valid_data)
        assert isinstance(entry.timestamp, datetime)


# ---------------------------------------------------------------------------
# Request Model Tests
# ---------------------------------------------------------------------------


class TestAutonomyUpdateRequest:
    """Tests for the AutonomyUpdateRequest model."""

    def test_valid_levels(self):
        for level in AutonomyLevel:
            req = AutonomyUpdateRequest(level=level)
            assert req.level == level

    def test_from_string_value(self):
        req = AutonomyUpdateRequest(level="suggest-only")
        assert req.level == AutonomyLevel.SUGGEST_ONLY

    def test_invalid_level_raises(self):
        with pytest.raises(ValidationError):
            AutonomyUpdateRequest(level="invalid-level")

    def test_missing_level_raises(self):
        with pytest.raises(ValidationError):
            AutonomyUpdateRequest()


class TestApprovalRejectRequest:
    """Tests for the ApprovalRejectRequest model."""

    def test_with_reason(self):
        req = ApprovalRejectRequest(reason="Not appropriate at this time")
        assert req.reason == "Not appropriate at this time"

    def test_default_empty_reason(self):
        req = ApprovalRejectRequest()
        assert req.reason == ""

    def test_empty_string_reason(self):
        req = ApprovalRejectRequest(reason="")
        assert req.reason == ""
