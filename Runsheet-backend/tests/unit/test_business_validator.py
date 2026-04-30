"""
Unit tests for the Business Validator module.

Tests the ValidationResult dataclass, VALID_JOB_TRANSITIONS state machine,
BusinessValidator dispatcher, and all tool-specific validators including
tenant-scoped entity fetch helpers.

Requirements: 1.9, 1.10
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.business_validator import (
    BusinessValidator,
    ValidationResult,
    VALID_JOB_TRANSITIONS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _es_hit(doc: dict) -> dict:
    """Wrap a document in an ES search response with one hit."""
    return {
        "hits": {
            "hits": [{"_source": doc}],
            "total": {"value": 1},
        }
    }


def _es_empty() -> dict:
    """Return an ES search response with no hits."""
    return {
        "hits": {
            "hits": [],
            "total": {"value": 0},
        }
    }


def _make_es_mock() -> MagicMock:
    """Return a mock ElasticsearchService with default async methods."""
    es = MagicMock()
    es.search_documents = AsyncMock(return_value=_es_empty())
    return es


def _job_doc(
    job_id: str = "JOB_1",
    status: str = "scheduled",
    tenant_id: str = "t1",
) -> dict:
    """Return a minimal job document."""
    return {
        "job_id": job_id,
        "status": status,
        "tenant_id": tenant_id,
    }


def _asset_doc(
    asset_id: str = "ASSET_1",
    tenant_id: str = "t1",
) -> dict:
    """Return a minimal asset document."""
    return {
        "asset_id": asset_id,
        "plate_number": "ABC-123",
        "tenant_id": tenant_id,
    }


# ---------------------------------------------------------------------------
# Tests: ValidationResult dataclass
# ---------------------------------------------------------------------------


class TestValidationResult:
    def test_valid_result(self):
        result = ValidationResult(valid=True)
        assert result.valid is True
        assert result.reason is None

    def test_invalid_result_with_reason(self):
        result = ValidationResult(valid=False, reason="Something went wrong")
        assert result.valid is False
        assert result.reason == "Something went wrong"

    def test_invalid_result_without_reason(self):
        result = ValidationResult(valid=False)
        assert result.valid is False
        assert result.reason is None


# ---------------------------------------------------------------------------
# Tests: VALID_JOB_TRANSITIONS state machine
# ---------------------------------------------------------------------------


class TestValidJobTransitions:
    def test_scheduled_transitions(self):
        assert VALID_JOB_TRANSITIONS["scheduled"] == {"assigned", "cancelled"}

    def test_assigned_transitions(self):
        assert VALID_JOB_TRANSITIONS["assigned"] == {"in_progress", "cancelled"}

    def test_in_progress_transitions(self):
        assert VALID_JOB_TRANSITIONS["in_progress"] == {
            "completed",
            "failed",
            "cancelled",
        }

    def test_completed_is_terminal(self):
        assert VALID_JOB_TRANSITIONS["completed"] == set()

    def test_cancelled_is_terminal(self):
        assert VALID_JOB_TRANSITIONS["cancelled"] == set()

    def test_failed_is_terminal(self):
        assert VALID_JOB_TRANSITIONS["failed"] == set()

    def test_all_statuses_are_keys(self):
        expected_statuses = {
            "scheduled",
            "assigned",
            "in_progress",
            "completed",
            "cancelled",
            "failed",
        }
        assert set(VALID_JOB_TRANSITIONS.keys()) == expected_statuses


# ---------------------------------------------------------------------------
# Tests: BusinessValidator.validate dispatcher
# ---------------------------------------------------------------------------


class TestValidateDispatcher:
    async def test_unknown_tool_passes_validation(self):
        """Tools without a specific validator should pass by default."""
        es = _make_es_mock()
        validator = BusinessValidator(es)
        result = await validator.validate("some_unknown_tool", {}, "t1")
        assert result.valid is True

    async def test_dispatches_to_update_job_status(self):
        """validate() should route to _validate_update_job_status."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="scheduled"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "update_job_status",
            {"job_id": "JOB_1", "new_status": "assigned"},
            "t1",
        )
        assert result.valid is True

    async def test_dispatches_to_assign_asset_to_job(self):
        """validate() should route to _validate_assign_asset_to_job."""
        es = _make_es_mock()
        # First call returns job, second returns asset
        es.search_documents = AsyncMock(
            side_effect=[
                _es_hit(_job_doc(status="scheduled")),
                _es_hit(_asset_doc()),
            ]
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "assign_asset_to_job",
            {"job_id": "JOB_1", "asset_id": "ASSET_1"},
            "t1",
        )
        assert result.valid is True

    async def test_dispatches_to_cancel_job(self):
        """validate() should route to _validate_cancel_job."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="scheduled"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "cancel_job", {"job_id": "JOB_1"}, "t1"
        )
        assert result.valid is True

    async def test_dispatches_to_request_fuel_refill(self):
        """validate() should route to _validate_request_fuel_refill."""
        es = _make_es_mock()
        validator = BusinessValidator(es)
        result = await validator.validate(
            "request_fuel_refill",
            {"station_id": "S-1", "quantity_liters": 500},
            "t1",
        )
        assert result.valid is True


# ---------------------------------------------------------------------------
# Tests: _validate_update_job_status
# ---------------------------------------------------------------------------


class TestValidateUpdateJobStatus:
    async def test_valid_transition_scheduled_to_assigned(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="scheduled"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "update_job_status",
            {"job_id": "JOB_1", "new_status": "assigned"},
            "t1",
        )
        assert result.valid is True

    async def test_valid_transition_scheduled_to_cancelled(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="scheduled"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "update_job_status",
            {"job_id": "JOB_1", "new_status": "cancelled"},
            "t1",
        )
        assert result.valid is True

    async def test_valid_transition_assigned_to_in_progress(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="assigned"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "update_job_status",
            {"job_id": "JOB_1", "new_status": "in_progress"},
            "t1",
        )
        assert result.valid is True

    async def test_valid_transition_in_progress_to_completed(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="in_progress"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "update_job_status",
            {"job_id": "JOB_1", "new_status": "completed"},
            "t1",
        )
        assert result.valid is True

    async def test_invalid_transition_scheduled_to_completed(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="scheduled"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "update_job_status",
            {"job_id": "JOB_1", "new_status": "completed"},
            "t1",
        )
        assert result.valid is False
        assert "Invalid transition" in result.reason

    async def test_invalid_transition_completed_to_in_progress(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="completed"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "update_job_status",
            {"job_id": "JOB_1", "new_status": "in_progress"},
            "t1",
        )
        assert result.valid is False
        assert "Invalid transition" in result.reason

    async def test_job_not_found(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_empty())
        validator = BusinessValidator(es)
        result = await validator.validate(
            "update_job_status",
            {"job_id": "MISSING", "new_status": "assigned"},
            "t1",
        )
        assert result.valid is False
        assert "not found" in result.reason


# ---------------------------------------------------------------------------
# Tests: _validate_assign_asset_to_job
# ---------------------------------------------------------------------------


class TestValidateAssignAssetToJob:
    async def test_valid_assignment_scheduled_job(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            side_effect=[
                _es_hit(_job_doc(status="scheduled")),
                _es_hit(_asset_doc()),
            ]
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "assign_asset_to_job",
            {"job_id": "JOB_1", "asset_id": "ASSET_1"},
            "t1",
        )
        assert result.valid is True

    async def test_valid_assignment_assigned_job(self):
        """Reassigning an asset to an already-assigned job should be valid."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            side_effect=[
                _es_hit(_job_doc(status="assigned")),
                _es_hit(_asset_doc()),
            ]
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "assign_asset_to_job",
            {"job_id": "JOB_1", "asset_id": "ASSET_1"},
            "t1",
        )
        assert result.valid is True

    async def test_job_not_found(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_empty())
        validator = BusinessValidator(es)
        result = await validator.validate(
            "assign_asset_to_job",
            {"job_id": "MISSING", "asset_id": "ASSET_1"},
            "t1",
        )
        assert result.valid is False
        assert "Job MISSING not found" in result.reason

    async def test_asset_not_found(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            side_effect=[
                _es_hit(_job_doc(status="scheduled")),
                _es_empty(),
            ]
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "assign_asset_to_job",
            {"job_id": "JOB_1", "asset_id": "MISSING_ASSET"},
            "t1",
        )
        assert result.valid is False
        assert "Asset MISSING_ASSET not found" in result.reason

    async def test_job_in_progress_cannot_assign(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="in_progress"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "assign_asset_to_job",
            {"job_id": "JOB_1", "asset_id": "ASSET_1"},
            "t1",
        )
        assert result.valid is False
        assert "cannot assign asset" in result.reason

    async def test_completed_job_cannot_assign(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="completed"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "assign_asset_to_job",
            {"job_id": "JOB_1", "asset_id": "ASSET_1"},
            "t1",
        )
        assert result.valid is False
        assert "cannot assign asset" in result.reason


# ---------------------------------------------------------------------------
# Tests: _validate_cancel_job
# ---------------------------------------------------------------------------


class TestValidateCancelJob:
    async def test_cancel_scheduled_job(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="scheduled"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "cancel_job", {"job_id": "JOB_1"}, "t1"
        )
        assert result.valid is True

    async def test_cancel_assigned_job(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="assigned"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "cancel_job", {"job_id": "JOB_1"}, "t1"
        )
        assert result.valid is True

    async def test_cancel_in_progress_job(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="in_progress"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "cancel_job", {"job_id": "JOB_1"}, "t1"
        )
        assert result.valid is True

    async def test_cannot_cancel_completed_job(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="completed"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "cancel_job", {"job_id": "JOB_1"}, "t1"
        )
        assert result.valid is False
        assert "already completed" in result.reason

    async def test_cannot_cancel_already_cancelled_job(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="cancelled"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "cancel_job", {"job_id": "JOB_1"}, "t1"
        )
        assert result.valid is False
        assert "already cancelled" in result.reason

    async def test_cannot_cancel_failed_job(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(status="failed"))
        )
        validator = BusinessValidator(es)
        result = await validator.validate(
            "cancel_job", {"job_id": "JOB_1"}, "t1"
        )
        assert result.valid is False
        assert "already failed" in result.reason

    async def test_job_not_found(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_empty())
        validator = BusinessValidator(es)
        result = await validator.validate(
            "cancel_job", {"job_id": "MISSING"}, "t1"
        )
        assert result.valid is False
        assert "not found" in result.reason


# ---------------------------------------------------------------------------
# Tests: _validate_request_fuel_refill
# ---------------------------------------------------------------------------


class TestValidateRequestFuelRefill:
    async def test_valid_refill(self):
        es = _make_es_mock()
        validator = BusinessValidator(es)
        result = await validator.validate(
            "request_fuel_refill",
            {"station_id": "S-1", "quantity_liters": 500},
            "t1",
        )
        assert result.valid is True

    async def test_zero_quantity_rejected(self):
        es = _make_es_mock()
        validator = BusinessValidator(es)
        result = await validator.validate(
            "request_fuel_refill",
            {"station_id": "S-1", "quantity_liters": 0},
            "t1",
        )
        assert result.valid is False
        assert "positive" in result.reason

    async def test_negative_quantity_rejected(self):
        es = _make_es_mock()
        validator = BusinessValidator(es)
        result = await validator.validate(
            "request_fuel_refill",
            {"station_id": "S-1", "quantity_liters": -100},
            "t1",
        )
        assert result.valid is False
        assert "positive" in result.reason

    async def test_excessive_quantity_rejected(self):
        es = _make_es_mock()
        validator = BusinessValidator(es)
        result = await validator.validate(
            "request_fuel_refill",
            {"station_id": "S-1", "quantity_liters": 200000},
            "t1",
        )
        assert result.valid is False
        assert "exceeds maximum" in result.reason

    async def test_missing_station_id_rejected(self):
        es = _make_es_mock()
        validator = BusinessValidator(es)
        result = await validator.validate(
            "request_fuel_refill",
            {"quantity_liters": 500},
            "t1",
        )
        assert result.valid is False
        assert "station_id" in result.reason

    async def test_empty_station_id_rejected(self):
        es = _make_es_mock()
        validator = BusinessValidator(es)
        result = await validator.validate(
            "request_fuel_refill",
            {"station_id": "", "quantity_liters": 500},
            "t1",
        )
        assert result.valid is False
        assert "station_id" in result.reason

    async def test_missing_quantity_defaults_to_zero(self):
        es = _make_es_mock()
        validator = BusinessValidator(es)
        result = await validator.validate(
            "request_fuel_refill",
            {"station_id": "S-1"},
            "t1",
        )
        assert result.valid is False
        assert "positive" in result.reason


# ---------------------------------------------------------------------------
# Tests: _fetch_job and _fetch_asset tenant scoping
# ---------------------------------------------------------------------------


class TestFetchHelpers:
    async def test_fetch_job_uses_tenant_scoping(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_job_doc(job_id="JOB_1", tenant_id="t1"))
        )
        validator = BusinessValidator(es)
        job = await validator._fetch_job("JOB_1", "t1")

        assert job is not None
        assert job["job_id"] == "JOB_1"
        # Verify the query includes tenant_id filter
        call_args = es.search_documents.call_args
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]
        tenant_filter = filters[0]
        assert tenant_filter == {"term": {"tenant_id": "t1"}}

    async def test_fetch_job_returns_none_when_not_found(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_empty())
        validator = BusinessValidator(es)
        job = await validator._fetch_job("MISSING", "t1")
        assert job is None

    async def test_fetch_job_returns_none_on_es_error(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(side_effect=Exception("ES down"))
        validator = BusinessValidator(es)
        job = await validator._fetch_job("JOB_1", "t1")
        assert job is None

    async def test_fetch_asset_uses_tenant_scoping(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_hit(_asset_doc(asset_id="ASSET_1", tenant_id="t1"))
        )
        validator = BusinessValidator(es)
        asset = await validator._fetch_asset("ASSET_1", "t1")

        assert asset is not None
        assert asset["asset_id"] == "ASSET_1"
        # Verify the query includes tenant_id filter
        call_args = es.search_documents.call_args
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]
        tenant_filter = filters[0]
        assert tenant_filter == {"term": {"tenant_id": "t1"}}

    async def test_fetch_asset_returns_none_when_not_found(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_empty())
        validator = BusinessValidator(es)
        asset = await validator._fetch_asset("MISSING", "t1")
        assert asset is None

    async def test_fetch_asset_returns_none_on_es_error(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(side_effect=Exception("ES down"))
        validator = BusinessValidator(es)
        asset = await validator._fetch_asset("ASSET_1", "t1")
        assert asset is None

    async def test_fetch_asset_queries_both_asset_id_and_plate_number(self):
        """The asset query should match on either asset_id or plate_number."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_empty())
        validator = BusinessValidator(es)
        await validator._fetch_asset("ABC-123", "t1")

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]
        should_clause = filters[1]["bool"]["should"]
        assert {"term": {"asset_id": "ABC-123"}} in should_clause
        assert {"term": {"plate_number": "ABC-123"}} in should_clause
