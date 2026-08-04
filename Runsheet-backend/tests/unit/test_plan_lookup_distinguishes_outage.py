"""``GET /api/fuel/mvp/plan/{plan_id}`` must not report absence it cannot verify.

MVP Bug 3. Both Elasticsearch lookups in ``get_plan`` sat inside
``except Exception`` blocks that only logged. With ES unreachable every attempt
failed, the plan stayed ``None``, and the endpoint answered::

    200  {"plan_id": "...", "loading_plan": null, "route_plan": null}

which is byte-identical to the answer for a plan id that does not exist. A
dispatcher could not tell "this run produced no plan" from "the plan store is
down", and neither could a metric or an alert: an outage looked like a quiet
afternoon.

The 200-with-nulls answer for a genuine miss is deliberate and stays — the
dispatcher UI polls this immediately after ``/plan/generate`` and relies on the
empty body while a run is still producing. What changed is that a *failed* query
no longer masquerades as a successful one: it answers 503
``ELASTICSEARCH_UNAVAILABLE``, which is retryable and visible.
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from errors.codes import ErrorCode
from errors.exceptions import AppException

TENANT_ID = "tenant-plan-lookup"
PLAN_ID = "run_20260803_deadbeef"


class _Tenant:
    tenant_id = TENANT_ID


def _es_raising() -> MagicMock:
    """Every search raises, as it would with the cluster unreachable."""
    es = MagicMock()
    es.search_documents = AsyncMock(
        side_effect=ConnectionError("elasticsearch: connection refused")
    )
    return es


def _es_empty() -> MagicMock:
    """Every search succeeds and matches nothing — a genuine miss."""
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return es


def _es_loading_only(loading_doc: Dict[str, Any]) -> MagicMock:
    """The load plan is found; the route query raises.

    The plan demonstrably exists, so a null ``route_plan`` would be a positive
    claim that it has no route — which this request cannot support.
    """

    async def _search(index, query, size=10, *args, **kwargs):
        if index == "mvp_load_plans":
            return {"hits": {"hits": [{"_source": loading_doc}]}}
        raise ConnectionError("elasticsearch: connection refused")

    es = MagicMock()
    es.search_documents = AsyncMock(side_effect=_search)
    return es


async def _call_get_plan(es: MagicMock) -> Any:
    """Invoke the route function directly with a patched ES accessor."""
    import Agents.support.mvp_endpoints as mvp

    original = mvp._get_es
    mvp._get_es = lambda: es  # type: ignore[assignment]
    try:
        return await mvp.get_plan(
            plan_id=PLAN_ID,
            request=MagicMock(),
            tenant=_Tenant(),
        )
    finally:
        mvp._get_es = original  # type: ignore[assignment]


class TestOutageIsNotReportedAsAbsence:
    @pytest.mark.asyncio
    async def test_a_failed_load_plan_lookup_is_503_not_200(self):
        with pytest.raises(AppException) as ei:
            await _call_get_plan(_es_raising())

        assert ei.value.error_code == ErrorCode.ELASTICSEARCH_UNAVAILABLE
        assert ei.value.status_code == 503

    @pytest.mark.asyncio
    async def test_the_503_names_the_index_it_could_not_read(self):
        with pytest.raises(AppException) as ei:
            await _call_get_plan(_es_raising())

        assert ei.value.details["index"] == "mvp_load_plans"
        assert ei.value.details["plan_id"] == PLAN_ID

    @pytest.mark.asyncio
    async def test_a_failed_route_lookup_is_503_even_though_the_plan_was_found(self):
        es = _es_loading_only({"plan_id": PLAN_ID, "tenant_id": TENANT_ID})

        with pytest.raises(AppException) as ei:
            await _call_get_plan(es)

        assert ei.value.error_code == ErrorCode.ELASTICSEARCH_UNAVAILABLE
        assert ei.value.details["index"] == "mvp_routes"


class TestGenuineAbsenceStillAnswers200:
    """The counterweight: mapping every empty result to 503 must not pass.

    Without this, raising on any missing plan would satisfy the tests above
    while breaking the dispatcher UI's poll-after-generate flow.
    """

    @pytest.mark.asyncio
    async def test_a_missing_plan_returns_nulls_and_does_not_raise(self):
        result = await _call_get_plan(_es_empty())

        assert result == {
            "plan_id": PLAN_ID,
            "loading_plan": None,
            "route_plan": None,
        }

    @pytest.mark.asyncio
    async def test_a_plan_with_no_route_yet_returns_the_plan_and_a_null_route(self):
        """A successful, empty route query is absence, not an outage."""
        loading_doc = {"plan_id": PLAN_ID, "tenant_id": TENANT_ID}

        async def _search(index, query, size=10, *args, **kwargs):
            if index == "mvp_load_plans":
                return {"hits": {"hits": [{"_source": loading_doc}]}}
            return {"hits": {"hits": []}}

        es = MagicMock()
        es.search_documents = AsyncMock(side_effect=_search)

        result = await _call_get_plan(es)

        assert result["loading_plan"] == loading_doc
        assert result["route_plan"] is None
