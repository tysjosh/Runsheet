"""
Property-based tests for Scheduling Tenant Isolation.

# Feature: logistics-scheduling, Property 5: Tenant Isolation

**Validates: Requirements 8.1-8.5**

For any scheduling API query, the services SHALL inject a tenant_id filter
into every Elasticsearch query such that the response contains zero documents
belonging to a different tenant. Cross-tenant data access SHALL be impossible.

This test verifies that every query method in JobService, CargoService, and
DelayDetectionService includes a ``term`` filter for ``tenant_id`` matching
the provided value.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from hypothesis import given, settings, assume
from hypothesis.strategies import text, sampled_from

from scheduling.services.job_service import JobService
from scheduling.services.cargo_service import CargoService
from scheduling.services.delay_detection_service import DelayDetectionService


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
JOBS_CURRENT_INDEX = "jobs_current"
JOB_EVENTS_INDEX = "job_events"

# Query methods to test across all scheduling services
QUERY_METHODS = [
    "list_jobs",
    "get_active_jobs",
    "get_delayed_jobs",
    "get_job_events",
    "get_cargo_manifest",
    "search_cargo",
    "get_eta",
    "get_delay_metrics",
    "check_delays",
]

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
_tenant_ids = text(
    min_size=1, max_size=50,
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_",
)
_query_methods = sampled_from(QUERY_METHODS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_es_mock():
    """Return a mock ElasticsearchService with default async methods."""
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {
            "avg_delay": {"value": 0.0},
            "delays_by_job_type": {"buckets": []},
        },
    })
    es.update_document = AsyncMock(return_value={"result": "updated"})
    return es


def _make_job_service(es_mock):
    """Create a JobService with mocked dependencies."""
    with patch("scheduling.services.job_service.get_settings") as mock_settings:
        settings_obj = MagicMock()
        settings_obj.scheduling_default_eta_hours = 4
        mock_settings.return_value = settings_obj
        svc = JobService(es_service=es_mock, redis_url=None)
    svc._id_gen = MagicMock()
    svc._id_gen.next_id = AsyncMock(return_value="JOB_1")
    svc._ws_manager = None
    return svc


def _make_cargo_service(es_mock):
    """Create a CargoService with mocked dependencies."""
    svc = CargoService(es_service=es_mock)
    svc._ws_manager = None
    return svc


def _make_delay_service(es_mock):
    """Create a DelayDetectionService with mocked dependencies."""
    svc = DelayDetectionService(es_service=es_mock, ws_manager=None)
    return svc


def _extract_tenant_from_query(es_mock) -> str | None:
    """Extract the tenant_id value from the ES query body's must clauses.

    Inspects the most recent call to search_documents and looks for a
    ``{"term": {"tenant_id": <value>}}`` clause in the bool must array.

    Returns:
        The tenant_id string if found, None otherwise.
    """
    if not es_mock.search_documents.called:
        return None

    call_args = es_mock.search_documents.call_args
    args, kwargs = call_args

    # search_documents(index, query_body, ...) — query_body is the 2nd positional arg
    if len(args) >= 2:
        query_body = args[1]
    else:
        return None

    # Navigate into the bool query
    bool_query = query_body.get("query", {}).get("bool", {})
    must_clauses = bool_query.get("must", [])

    for clause in must_clauses:
        if isinstance(clause, dict) and "term" in clause:
            term = clause["term"]
            if "tenant_id" in term:
                return term["tenant_id"]

    return None


async def _call_query_method(
    method_name: str,
    tenant_id: str,
    job_svc: JobService,
    cargo_svc: CargoService,
    delay_svc: DelayDetectionService,
    es_mock: MagicMock,
):
    """Invoke the specified query method with the given tenant_id.

    Sets up appropriate mock responses so the method can execute without
    raising unrelated errors.
    """
    if method_name == "list_jobs":
        await job_svc.list_jobs(tenant_id=tenant_id)

    elif method_name == "get_active_jobs":
        await job_svc.get_active_jobs(tenant_id=tenant_id)

    elif method_name == "get_delayed_jobs":
        await job_svc.get_delayed_jobs(tenant_id=tenant_id)

    elif method_name == "get_job_events":
        await job_svc.get_job_events(job_id="JOB_1", tenant_id=tenant_id)

    elif method_name == "get_cargo_manifest":
        # get_cargo_manifest calls _get_job_doc which needs a hit
        job_doc = {
            "job_id": "JOB_1",
            "job_type": "cargo_transport",
            "status": "in_progress",
            "tenant_id": tenant_id,
            "cargo_manifest": [
                {
                    "item_id": "ITEM_1",
                    "description": "Test cargo",
                    "weight_kg": 100.0,
                    "item_status": "pending",
                }
            ],
        }
        es_mock.search_documents = AsyncMock(return_value={
            "hits": {"hits": [{"_source": job_doc}], "total": {"value": 1}},
        })
        await cargo_svc.get_cargo_manifest(job_id="JOB_1", tenant_id=tenant_id)

    elif method_name == "search_cargo":
        await cargo_svc.search_cargo(
            tenant_id=tenant_id,
            container_number="CONT-001",
        )

    elif method_name == "get_eta":
        # get_eta needs a hit to avoid 404
        job_doc = {
            "job_id": "JOB_1",
            "estimated_arrival": "2026-03-12T14:00:00Z",
            "delayed": False,
            "delay_duration_minutes": None,
            "status": "in_progress",
            "scheduled_time": "2026-03-12T10:00:00Z",
        }
        es_mock.search_documents = AsyncMock(return_value={
            "hits": {"hits": [{"_source": job_doc}], "total": {"value": 1}},
        })
        await delay_svc.get_eta(job_id="JOB_1", tenant_id=tenant_id)

    elif method_name == "get_delay_metrics":
        await delay_svc.get_delay_metrics(tenant_id=tenant_id)

    elif method_name == "check_delays":
        await delay_svc.check_delays(tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Property 5 – Every query includes tenant_id filter
# ---------------------------------------------------------------------------
class TestTenantIsolation:
    """**Validates: Requirements 8.1-8.5**"""

    @given(tenant_id=_tenant_ids, method=_query_methods)
    @settings(max_examples=150)
    @pytest.mark.asyncio
    async def test_every_query_includes_tenant_filter(
        self, tenant_id: str, method: str
    ):
        """
        For any tenant_id and any query method, the ES query body SHALL
        contain a term filter for tenant_id matching the provided value.

        **Validates: Requirements 8.1-8.5**
        """
        es_mock = _make_es_mock()
        job_svc = _make_job_service(es_mock)
        cargo_svc = _make_cargo_service(es_mock)
        delay_svc = _make_delay_service(es_mock)

        await _call_query_method(
            method, tenant_id, job_svc, cargo_svc, delay_svc, es_mock
        )

        extracted = _extract_tenant_from_query(es_mock)
        assert extracted == tenant_id, (
            f"Method '{method}' with tenant_id='{tenant_id}' did not include "
            f"tenant_id filter in ES query. Extracted: {extracted}"
        )


# ---------------------------------------------------------------------------
# Property 5b – Cross-tenant queries produce different filters
# ---------------------------------------------------------------------------
class TestCrossTenantIsolation:
    """**Validates: Requirements 8.1-8.5**"""

    @given(
        tenant_a=_tenant_ids,
        tenant_b=_tenant_ids,
        method=_query_methods,
    )
    @settings(max_examples=150)
    @pytest.mark.asyncio
    async def test_different_tenants_produce_different_filters(
        self, tenant_a: str, tenant_b: str, method: str
    ):
        """
        For any two different tenant_ids calling the same query method,
        the ES queries SHALL contain different tenant_id filter values,
        ensuring zero cross-tenant results.

        **Validates: Requirements 8.1-8.5**
        """
        assume(tenant_a != tenant_b)

        # Tenant A query
        es_mock_a = _make_es_mock()
        job_svc_a = _make_job_service(es_mock_a)
        cargo_svc_a = _make_cargo_service(es_mock_a)
        delay_svc_a = _make_delay_service(es_mock_a)

        await _call_query_method(
            method, tenant_a, job_svc_a, cargo_svc_a, delay_svc_a, es_mock_a
        )
        extracted_a = _extract_tenant_from_query(es_mock_a)

        # Tenant B query
        es_mock_b = _make_es_mock()
        job_svc_b = _make_job_service(es_mock_b)
        cargo_svc_b = _make_cargo_service(es_mock_b)
        delay_svc_b = _make_delay_service(es_mock_b)

        await _call_query_method(
            method, tenant_b, job_svc_b, cargo_svc_b, delay_svc_b, es_mock_b
        )
        extracted_b = _extract_tenant_from_query(es_mock_b)

        assert extracted_a == tenant_a, (
            f"Tenant A filter mismatch for '{method}': expected '{tenant_a}', got '{extracted_a}'"
        )
        assert extracted_b == tenant_b, (
            f"Tenant B filter mismatch for '{method}': expected '{tenant_b}', got '{extracted_b}'"
        )
        assert extracted_a != extracted_b, (
            f"Cross-tenant leak: both tenants produced the same filter value "
            f"'{extracted_a}' for method '{method}'"
        )
