"""S4: the job detail keeps ``job_type: fuel_delivery``.

``JobService._normalize_job_doc`` used to map ``fuel_delivery`` to
``cargo_transport`` as legacy seed cleanup, so ``GET /api/scheduling/jobs/{id}``
reported ``cargo_transport`` while the list (raw docs) showed ``fuel_delivery``.
``JobType.FUEL_DELIVERY`` is a real member, so only the legacy ``delivery`` /
``pickup`` values are remapped now.

Lives under ``tests/`` (not ``scheduling/services/``) because pytest's
``testpaths = tests`` means CI collects nothing else.
"""
from __future__ import annotations

import pytest

from scheduling.models import JOB_ASSET_COMPATIBILITY, Job, JobType
from scheduling.services.job_service import JobService
from tests.unit.test_job_service_queries import (
    TENANT_ID,
    _es_search_response,
    _make_es_service,
    _make_job_doc,
)


async def _detail(job_doc: dict) -> Job:
    es = _make_es_service()
    es.search_documents.side_effect = [
        _es_search_response([job_doc]),  # jobs_current
        _es_search_response([]),  # job_events
    ]
    result = await JobService(es, redis_url=None).get_job("JOB_1", TENANT_ID)
    return result["job"]


@pytest.mark.asyncio
async def test_fuel_delivery_without_cargo_manifest_builds_a_job():
    """Runtime check: the CARGO_TRANSPORT manifest validator doesn't apply."""
    doc = _make_job_doc(job_type="fuel_delivery")
    doc.pop("cargo_manifest")

    job = await _detail(doc)

    assert job.job_type == JobType.FUEL_DELIVERY
    assert job.model_dump(mode="json")["job_type"] == "fuel_delivery"


@pytest.mark.asyncio
async def test_detail_job_type_matches_the_stored_list_value():
    doc = _make_job_doc(job_type="fuel_delivery")

    job = await _detail(dict(doc))

    assert job.model_dump(mode="json")["job_type"] == doc["job_type"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["delivery", "pickup"])
async def test_legacy_values_still_map_to_cargo_transport(legacy):
    """Pin: the seed-cleanup mapping for non-members is unchanged."""
    job = await _detail(_make_job_doc(job_type=legacy))

    assert job.job_type == JobType.CARGO_TRANSPORT


def test_fuel_delivery_has_an_asset_compatibility_entry():
    """Runtime check: read paths index JOB_ASSET_COMPATIBILITY by job_type."""
    assert JOB_ASSET_COMPATIBILITY[JobType.FUEL_DELIVERY] == ["vehicle"]
