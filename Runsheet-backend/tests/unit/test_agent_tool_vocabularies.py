"""
Agent tool vocabularies are derived from the domain enums (F5).

Staging finding F5: ``search_jobs``' docstring (and so its Strands tool spec)
listed five job types and omitted ``fuel_delivery``, so the model never asked
for fuel-delivery jobs; and ``search_fleet_data("delayed trucks")`` sent the
whole phrase to ``multi_match``, which no asset contains, so a delayed truck
was never found.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from Agents.tools._tenant_context import set_current_tenant
from Agents.tools.mutation_tools import create_job
from Agents.tools.scheduling_tools import (
    JOB_STATUS_VALUES,
    JOB_TYPE_VALUES,
    search_jobs,
)
from Agents.tools.search_tools import FLEET_ASSET_STATUSES, search_fleet_data
from persistence.document_matcher import matches
from scheduling.models import JobStatus, JobType
from services.schema_templates import SchemaTemplates

TENANT = "t"


def _empty_response():
    return {"hits": {"hits": [], "total": {"value": 0}}}


# ---------------------------------------------------------------------------
# search_jobs / scheduling vocabularies
# ---------------------------------------------------------------------------


def test_job_vocabularies_match_the_enums():
    assert JOB_TYPE_VALUES == tuple(t.value for t in JobType)
    assert JOB_STATUS_VALUES == tuple(s.value for s in JobStatus)
    assert "fuel_delivery" in JOB_TYPE_VALUES


def test_search_jobs_tool_spec_lists_every_job_type_and_status():
    spec = json.dumps(search_jobs.tool_spec)
    for value in JOB_TYPE_VALUES + JOB_STATUS_VALUES:
        assert value in spec, value
    # The placeholders were filled before Strands built the spec.
    assert "{job_types}" not in spec and "{statuses}" not in spec


def test_create_job_tool_spec_lists_fuel_delivery():
    spec = json.dumps(create_job.tool_spec)
    assert "fuel_delivery" in spec
    assert "{job_types}" not in spec


@pytest.mark.asyncio
async def test_search_jobs_normalizes_job_type_and_status():
    with patch("Agents.tools.scheduling_tools.elasticsearch_service") as es, \
            set_current_tenant(TENANT):
        es.search_documents = AsyncMock(return_value=_empty_response())
        await search_jobs(job_type="Fuel Delivery", status=" Scheduled ")

    body = es.search_documents.call_args[0][1]
    filters = body["query"]["bool"]["filter"]
    assert {"term": {"job_type": "fuel_delivery"}} in filters
    assert {"term": {"status": "scheduled"}} in filters
    assert {"term": {"tenant_id": TENANT}} in filters


@pytest.mark.asyncio
async def test_search_jobs_unknown_job_type_returns_valid_list_without_query():
    with patch("Agents.tools.scheduling_tools.elasticsearch_service") as es, \
            set_current_tenant(TENANT):
        es.search_documents = AsyncMock(return_value=_empty_response())
        result = await search_jobs(job_type="cargo")

    es.search_documents.assert_not_called()
    assert "Valid job types:" in result
    assert "fuel_delivery" in result


@pytest.mark.asyncio
async def test_search_jobs_unknown_status_returns_valid_list_without_query():
    with patch("Agents.tools.scheduling_tools.elasticsearch_service") as es, \
            set_current_tenant(TENANT):
        es.search_documents = AsyncMock(return_value=_empty_response())
        result = await search_jobs(status="delayed")

    es.search_documents.assert_not_called()
    assert "Valid statuses:" in result
    assert "in_progress" in result


def test_scheduling_agent_prompt_lists_fuel_delivery():
    from Agents.specialists.scheduling_agent import SchedulingAgent

    for value in JOB_TYPE_VALUES:
        assert value in SchedulingAgent.SYSTEM_PROMPT, value


def test_main_agent_prompt_lists_fuel_delivery():
    import inspect

    from Agents import mainagent

    source = inspect.getsource(mainagent.LogisticsAgent.__init__)
    # The search_jobs line is built from the constants, not a literal list.
    assert "', '.join(JOB_TYPE_VALUES)" in source
    assert "crane_booking." not in source


# ---------------------------------------------------------------------------
# search_fleet_data / status words
# ---------------------------------------------------------------------------


def test_fleet_statuses_match_vehicle_template_enum():
    fleet = SchemaTemplates.TEMPLATES["fleet"]
    status_field = next(f for f in fleet.fields if f.name == "status")
    assert tuple(status_field.enum_values) == FLEET_ASSET_STATUSES


async def _fleet_query(**kwargs):
    with patch("Agents.tools.search_tools.elasticsearch_service") as es, \
            set_current_tenant(TENANT):
        es.search_documents = AsyncMock(return_value=_empty_response())
        result = await search_fleet_data(**kwargs)
    return es, result


@pytest.mark.asyncio
async def test_delayed_trucks_matches_a_delayed_asset():
    es, _ = await _fleet_query(query="delayed trucks")
    body = es.search_documents.call_args[0][1]
    query = body["query"]

    delayed = {"tenant_id": TENANT, "status": "delayed", "asset_name": "QA-Agents Truck AG-01"}
    on_time = {"tenant_id": TENANT, "status": "on_time", "asset_name": "QA-Agents Truck AG-02"}
    other_tenant = {"tenant_id": "other", "status": "delayed", "asset_name": "X"}

    assert matches(delayed, query)
    assert not matches(on_time, query)
    assert not matches(other_tenant, query)


@pytest.mark.asyncio
async def test_status_words_become_a_term_filter_and_match_all():
    es, _ = await _fleet_query(query="show me all idle vehicles")
    inner = es.search_documents.call_args[0][1]["query"]["bool"]["must"][0]
    assert inner["bool"]["must"] == [{"match_all": {}}]
    assert {"term": {"status": "idle"}} in inner["bool"]["filter"]


@pytest.mark.asyncio
async def test_on_time_phrase_is_detected():
    es, _ = await _fleet_query(query="trucks on time")
    inner = es.search_documents.call_args[0][1]["query"]["bool"]["must"][0]
    assert {"term": {"status": "on_time"}} in inner["bool"]["filter"]


@pytest.mark.asyncio
async def test_remaining_free_text_still_uses_multi_match():
    es, _ = await _fleet_query(query="delayed trucks carrying perishables")
    inner = es.search_documents.call_args[0][1]["query"]["bool"]["must"][0]
    assert inner["bool"]["must"][0]["multi_match"]["query"] == "carrying perishables"
    assert {"term": {"status": "delayed"}} in inner["bool"]["filter"]


@pytest.mark.asyncio
async def test_explicit_status_is_validated():
    es, result = await _fleet_query(query="trucks", status="broken")
    es.search_documents.assert_not_called()
    assert "Valid statuses:" in result
    assert "maintenance" in result


@pytest.mark.asyncio
async def test_explicit_status_with_asset_type():
    es, _ = await _fleet_query(query="anything", asset_type="vehicle", status="Maintenance")
    inner = es.search_documents.call_args[0][1]["query"]["bool"]["must"][0]
    assert inner["bool"]["filter"] == [
        {"term": {"asset_type": "vehicle"}},
        {"term": {"status": "maintenance"}},
    ]


@pytest.mark.asyncio
async def test_several_status_words_use_terms():
    es, _ = await _fleet_query(query="idle or delayed trucks")
    inner = es.search_documents.call_args[0][1]["query"]["bool"]["must"][0]
    status_filter = inner["bool"]["filter"][0]
    assert set(status_filter["terms"]["status"]) == {"idle", "delayed"}
    assert inner["bool"]["must"] == [{"match_all": {}}]
