"""
Unit tests for JobService business rule evaluation (task 9.1).

Tests: _evaluate_business_rules, _get_tenant_policies, _check_pod_exists,
       and integration with transition_status.
Validates: Requirements 10.1, 10.2, 10.3
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from scheduling.models import JobStatus, StatusTransition
from scheduling.services.job_service import JobService


TENANT_ID = "tenant_test_001"


def _make_es_service():
    """Create a mock ElasticsearchService."""
    es = MagicMock()
    es.search_documents = AsyncMock()
    es.index_document = AsyncMock()
    es.update_document = AsyncMock()
    return es


def _make_job_doc(**overrides):
    """Build a minimal job document dict."""
    doc = {
        "job_id": "JOB_1",
        "job_type": "cargo_transport",
        "status": "in_progress",
        "tenant_id": TENANT_ID,
        "asset_assigned": "TRUCK_01",
        "origin": "Port A",
        "destination": "Port B",
        "scheduled_time": "2026-03-12T10:00:00+00:00",
        "estimated_arrival": "2026-03-12T14:00:00+00:00",
        "started_at": "2026-03-12T10:30:00+00:00",
        "completed_at": None,
        "created_at": "2026-03-12T08:00:00+00:00",
        "updated_at": "2026-03-12T10:30:00+00:00",
        "created_by": "user1",
        "priority": "normal",
        "delayed": False,
        "delay_duration_minutes": None,
        "failure_reason": None,
        "notes": None,
        "cargo_manifest": None,
    }
    doc.update(overrides)
    return doc


def _es_search_response(hits, total=None):
    """Build a mock ES search response."""
    if total is None:
        total = len(hits)
    return {
        "hits": {
            "hits": [{"_source": h} for h in hits],
            "total": {"value": total},
        }
    }


# ------------------------------------------------------------------ #
# _get_tenant_policies
# ------------------------------------------------------------------ #


class TestGetTenantPolicies:
    """Tests for JobService._get_tenant_policies() — Requirement 10.3."""

    @pytest.mark.asyncio
    async def test_returns_defaults_when_no_policy_exists(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        policies = await svc._get_tenant_policies(TENANT_ID)

        assert policies["pod_required"] is False
        assert policies["pod_radius_meters"] == 500
        assert policies["otp_required"] is False
        assert policies["nudge_timeout_minutes"] == 10

    @pytest.mark.asyncio
    async def test_returns_tenant_specific_policies(self):
        es = _make_es_service()
        policy_doc = {
            "tenant_id": TENANT_ID,
            "pod_required": True,
            "pod_radius_meters": 300,
            "otp_required": True,
            "nudge_timeout_minutes": 5,
        }
        es.search_documents.return_value = _es_search_response([policy_doc])

        svc = JobService(es, redis_url=None)
        policies = await svc._get_tenant_policies(TENANT_ID)

        assert policies["pod_required"] is True
        assert policies["pod_radius_meters"] == 300
        assert policies["otp_required"] is True
        assert policies["nudge_timeout_minutes"] == 5

    @pytest.mark.asyncio
    async def test_returns_defaults_on_es_error(self):
        es = _make_es_service()
        es.search_documents.side_effect = Exception("ES unavailable")

        svc = JobService(es, redis_url=None)
        policies = await svc._get_tenant_policies(TENANT_ID)

        assert policies["pod_required"] is False
        assert policies["pod_radius_meters"] == 500

    @pytest.mark.asyncio
    async def test_queries_tenant_job_policies_index(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc._get_tenant_policies(TENANT_ID)

        index_arg = es.search_documents.call_args[0][0]
        assert index_arg == "tenant_job_policies"

        query_body = es.search_documents.call_args[0][1]
        assert query_body["query"]["term"]["tenant_id"] == TENANT_ID

    @pytest.mark.asyncio
    async def test_partial_policy_fills_defaults(self):
        """When a tenant policy only has some fields, defaults fill the rest."""
        es = _make_es_service()
        policy_doc = {
            "tenant_id": TENANT_ID,
            "pod_required": True,
            # pod_radius_meters, otp_required, nudge_timeout_minutes missing
        }
        es.search_documents.return_value = _es_search_response([policy_doc])

        svc = JobService(es, redis_url=None)
        policies = await svc._get_tenant_policies(TENANT_ID)

        assert policies["pod_required"] is True
        assert policies["pod_radius_meters"] == 500  # default
        assert policies["otp_required"] is False  # default
        assert policies["nudge_timeout_minutes"] == 10  # default


# ------------------------------------------------------------------ #
# _check_pod_exists
# ------------------------------------------------------------------ #


class TestCheckPodExists:
    """Tests for JobService._check_pod_exists()."""

    @pytest.mark.asyncio
    async def test_returns_pod_when_accepted_exists(self):
        es = _make_es_service()
        pod_doc = {
            "pod_id": "pod_001",
            "job_id": "JOB_1",
            "status": "accepted",
            "tenant_id": TENANT_ID,
        }
        es.search_documents.return_value = _es_search_response([pod_doc])

        svc = JobService(es, redis_url=None)
        result = await svc._check_pod_exists("JOB_1", TENANT_ID)

        assert result is not None
        assert result["pod_id"] == "pod_001"
        assert result["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_accepted_pod(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        result = await svc._check_pod_exists("JOB_1", TENANT_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_es_error(self):
        es = _make_es_service()
        es.search_documents.side_effect = Exception("ES unavailable")

        svc = JobService(es, redis_url=None)
        result = await svc._check_pod_exists("JOB_1", TENANT_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_queries_proof_of_delivery_index(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc._check_pod_exists("JOB_1", TENANT_ID)

        index_arg = es.search_documents.call_args[0][0]
        assert index_arg == "proof_of_delivery"

    @pytest.mark.asyncio
    async def test_filters_by_job_tenant_and_accepted_status(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc._check_pod_exists("JOB_42", TENANT_ID)

        query_body = es.search_documents.call_args[0][1]
        must = query_body["query"]["bool"]["must"]

        job_terms = [c for c in must if "term" in c and "job_id" in c["term"]]
        assert len(job_terms) == 1
        assert job_terms[0]["term"]["job_id"] == "JOB_42"

        tenant_terms = [c for c in must if "term" in c and "tenant_id" in c["term"]]
        assert len(tenant_terms) == 1
        assert tenant_terms[0]["term"]["tenant_id"] == TENANT_ID

        status_terms = [c for c in must if "term" in c and "status" in c["term"]]
        assert len(status_terms) == 1
        assert status_terms[0]["term"]["status"] == "accepted"


# ------------------------------------------------------------------ #
# _evaluate_business_rules
# ------------------------------------------------------------------ #


class TestEvaluateBusinessRules:
    """Tests for JobService._evaluate_business_rules() — Requirements 10.1, 10.2."""

    @pytest.mark.asyncio
    async def test_returns_none_when_pod_not_required(self):
        """No violation when tenant does not require POD."""
        es = _make_es_service()
        # _get_tenant_policies query returns no policy (defaults)
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        job_doc = _make_job_doc()

        result = await svc._evaluate_business_rules(
            job_doc, JobStatus.COMPLETED, TENANT_ID
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_violation_when_pod_required_and_missing(self):
        """Violation when pod_required=True and no accepted POD exists."""
        es = _make_es_service()
        policy_doc = {
            "tenant_id": TENANT_ID,
            "pod_required": True,
            "pod_radius_meters": 500,
            "otp_required": False,
            "nudge_timeout_minutes": 10,
        }
        # First call: _get_tenant_policies
        # Second call: _check_pod_exists (no accepted POD)
        es.search_documents.side_effect = [
            _es_search_response([policy_doc]),
            _es_search_response([]),
        ]

        svc = JobService(es, redis_url=None)
        job_doc = _make_job_doc()

        result = await svc._evaluate_business_rules(
            job_doc, JobStatus.COMPLETED, TENANT_ID
        )

        assert result is not None
        assert result["rule"] == "pod_required"
        assert "accepted" in result["message"]
        assert "POST /api/driver/jobs/JOB_1/pod" in result["remediation"]

    @pytest.mark.asyncio
    async def test_returns_none_when_pod_required_and_accepted_pod_exists(self):
        """No violation when pod_required=True and an accepted POD exists."""
        es = _make_es_service()
        policy_doc = {
            "tenant_id": TENANT_ID,
            "pod_required": True,
            "pod_radius_meters": 500,
            "otp_required": False,
            "nudge_timeout_minutes": 10,
        }
        pod_doc = {
            "pod_id": "pod_001",
            "job_id": "JOB_1",
            "status": "accepted",
            "tenant_id": TENANT_ID,
        }
        # First call: _get_tenant_policies
        # Second call: _check_pod_exists (accepted POD found)
        es.search_documents.side_effect = [
            _es_search_response([policy_doc]),
            _es_search_response([pod_doc]),
        ]

        svc = JobService(es, redis_url=None)
        job_doc = _make_job_doc()

        result = await svc._evaluate_business_rules(
            job_doc, JobStatus.COMPLETED, TENANT_ID
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_skips_pod_check_for_non_completed_transitions(self):
        """POD check only applies to completed transitions."""
        es = _make_es_service()
        policy_doc = {
            "tenant_id": TENANT_ID,
            "pod_required": True,
            "pod_radius_meters": 500,
            "otp_required": False,
            "nudge_timeout_minutes": 10,
        }
        # Only one call: _get_tenant_policies (no POD check needed)
        es.search_documents.return_value = _es_search_response([policy_doc])

        svc = JobService(es, redis_url=None)
        job_doc = _make_job_doc(status="assigned")

        result = await svc._evaluate_business_rules(
            job_doc, JobStatus.IN_PROGRESS, TENANT_ID
        )

        assert result is None
        # Only one ES call (tenant policies), no POD check
        assert es.search_documents.call_count == 1

    @pytest.mark.asyncio
    async def test_violation_dict_has_required_keys(self):
        """Violation dict must contain rule, message, and remediation."""
        es = _make_es_service()
        policy_doc = {
            "tenant_id": TENANT_ID,
            "pod_required": True,
        }
        es.search_documents.side_effect = [
            _es_search_response([policy_doc]),
            _es_search_response([]),  # no POD
        ]

        svc = JobService(es, redis_url=None)
        job_doc = _make_job_doc()

        result = await svc._evaluate_business_rules(
            job_doc, JobStatus.COMPLETED, TENANT_ID
        )

        assert "rule" in result
        assert "message" in result
        assert "remediation" in result
        assert isinstance(result["rule"], str)
        assert isinstance(result["message"], str)
        assert isinstance(result["remediation"], str)


# ------------------------------------------------------------------ #
# transition_status integration with business rules
# ------------------------------------------------------------------ #


class TestTransitionStatusBusinessRules:
    """Tests for business rule enforcement in transition_status — Requirements 10.1, 10.2."""

    @pytest.mark.asyncio
    async def test_completed_blocked_when_pod_required_and_missing(self):
        """transition_status raises 400 when POD is required but missing."""
        es = _make_es_service()
        job_doc = _make_job_doc(status="in_progress")
        policy_doc = {
            "tenant_id": TENANT_ID,
            "pod_required": True,
        }

        # Call sequence:
        # 1. _get_job_doc (jobs_current search)
        # 2. _get_tenant_policies (tenant_job_policies search)
        # 3. _check_pod_exists (proof_of_delivery search)
        es.search_documents.side_effect = [
            _es_search_response([job_doc]),       # _get_job_doc
            _es_search_response([policy_doc]),     # _get_tenant_policies
            _es_search_response([]),               # _check_pod_exists (no POD)
        ]

        svc = JobService(es, redis_url=None)
        transition = StatusTransition(status=JobStatus.COMPLETED)

        with pytest.raises(Exception) as exc_info:
            await svc.transition_status("JOB_1", transition, TENANT_ID)

        exc = exc_info.value
        assert exc.status_code == 400
        assert exc.details["rule"] == "pod_required"
        assert "remediation" in exc.details

    @pytest.mark.asyncio
    async def test_completed_allowed_when_pod_required_and_accepted_exists(self):
        """transition_status succeeds when POD is required and accepted POD exists."""
        es = _make_es_service()
        job_doc = _make_job_doc(status="in_progress")
        policy_doc = {
            "tenant_id": TENANT_ID,
            "pod_required": True,
        }
        pod_doc = {
            "pod_id": "pod_001",
            "job_id": "JOB_1",
            "status": "accepted",
            "tenant_id": TENANT_ID,
        }

        # Call sequence:
        # 1. _get_job_doc
        # 2. _get_tenant_policies
        # 3. _check_pod_exists (accepted POD found)
        # 4. update_document (status update)
        # 5. index_document (event append)
        es.search_documents.side_effect = [
            _es_search_response([job_doc]),       # _get_job_doc
            _es_search_response([policy_doc]),     # _get_tenant_policies
            _es_search_response([pod_doc]),        # _check_pod_exists
        ]

        svc = JobService(es, redis_url=None)
        transition = StatusTransition(status=JobStatus.COMPLETED)

        result = await svc.transition_status("JOB_1", transition, TENANT_ID)

        assert result.status == JobStatus.COMPLETED
        # Verify update_document was called (transition executed)
        assert es.update_document.called

    @pytest.mark.asyncio
    async def test_completed_allowed_when_pod_not_required(self):
        """transition_status succeeds when tenant does not require POD."""
        es = _make_es_service()
        job_doc = _make_job_doc(status="in_progress")

        # Call sequence:
        # 1. _get_job_doc
        # 2. _get_tenant_policies (no policy, defaults)
        # 3. update_document
        # 4. index_document
        es.search_documents.side_effect = [
            _es_search_response([job_doc]),  # _get_job_doc
            _es_search_response([]),          # _get_tenant_policies (defaults)
        ]

        svc = JobService(es, redis_url=None)
        transition = StatusTransition(status=JobStatus.COMPLETED)

        result = await svc.transition_status("JOB_1", transition, TENANT_ID)

        assert result.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_error_response_contains_rule_and_remediation(self):
        """400 error details include rule name and remediation action."""
        es = _make_es_service()
        job_doc = _make_job_doc(status="in_progress")
        policy_doc = {
            "tenant_id": TENANT_ID,
            "pod_required": True,
        }

        es.search_documents.side_effect = [
            _es_search_response([job_doc]),
            _es_search_response([policy_doc]),
            _es_search_response([]),  # no POD
        ]

        svc = JobService(es, redis_url=None)
        transition = StatusTransition(status=JobStatus.COMPLETED)

        with pytest.raises(Exception) as exc_info:
            await svc.transition_status("JOB_1", transition, TENANT_ID)

        exc = exc_info.value
        assert exc.status_code == 400
        details = exc.details
        assert details["rule"] == "pod_required"
        assert details["remediation"].startswith("Submit proof of delivery")
        assert details["job_id"] == "JOB_1"
        assert details["target_status"] == "completed"

    @pytest.mark.asyncio
    async def test_non_completed_transition_skips_business_rules(self):
        """Business rules don't block non-completed transitions even with pod_required."""
        es = _make_es_service()
        job_doc = _make_job_doc(status="assigned", asset_assigned="TRUCK_01")
        policy_doc = {
            "tenant_id": TENANT_ID,
            "pod_required": True,
        }

        # Call sequence:
        # 1. _get_job_doc
        # 2. _get_tenant_policies (pod_required=True, but target is in_progress)
        # 3. update_document
        # 4. index_document
        es.search_documents.side_effect = [
            _es_search_response([job_doc]),       # _get_job_doc
            _es_search_response([policy_doc]),     # _get_tenant_policies
        ]

        svc = JobService(es, redis_url=None)
        transition = StatusTransition(status=JobStatus.IN_PROGRESS)

        result = await svc.transition_status("JOB_1", transition, TENANT_ID)

        assert result.status == JobStatus.IN_PROGRESS
