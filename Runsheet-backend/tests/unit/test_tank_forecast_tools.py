"""Unit tests for the tank run-out risk tool.

The agent used to answer "I am unable to identify the specific tanks most at
risk" while ``mvp_tank_forecasts`` held nearly two thousand forecasts. This tool
closes that gap, and these tests pin the four properties that stop it from
closing the gap with a wrong number instead:

1. **Pinned to one run.** The index accumulates a document per tank per cycle.
   Counting across runs multiplies every tank by the number of cycles retained.
2. **Total and page are different numbers.** ``total_at_risk`` is the match
   count; ``shown`` is the page length. Conflating them is what produced a reply
   claiming 3 and 912 unassigned orders in the same answer.
3. **Subject is never mislabelled.** One run holds depot/station forecasts and
   customer-tank forecasts. Verified against the demo tenant: run
   ``forecast_20260805_152725`` held 14 stations and 6 customer tanks. A station
   id under a key named ``customer_tank_id`` is a mislabel the model repeats.
4. **An unrecognised subject refuses.** Silently widening to "all" answers a
   broader question than the one asked and presents it as the narrow answer.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from Agents.tools._tenant_context import set_current_tenant  # noqa: E402
from Agents.tools.tank_forecast_tools import (  # noqa: E402
    FORECAST_INDEX,
    get_runout_risk_list,
)

TENANT = "tenant-a"
RUN = "forecast_20260101_000000"
OLDER_RUN = "forecast_20251231_000000"

_tool = getattr(get_runout_risk_list, "_tool_func", None) or get_runout_risk_list


def _station_row(tank: str = "FS-005", hours: float = 12.0) -> Dict[str, Any]:
    """Shape taken from a real document: ``customer_tank_id`` is null."""
    return {
        "station_id": tank,
        "customer_tank_id": None,
        "customer_id": None,
        "customer_type": None,
        "fuel_grade": "DIESEL_2",
        "fuel_type": None,
        "hours_to_runout_p50": hours,
        "hours_to_runout_p90": hours * 1.5,
        "runout_risk_24h": 0.5,
        "confidence": 0.1,
        "anomaly_flags": ["insufficient_data"],
        "weather_fallback": None,
        "scheduled_deliveries": [],
        "run_id": RUN,
        "timestamp": "2026-01-01T00:00:00Z",
        "tenant_id": TENANT,
    }


def _customer_row(tank: str = "TANK-001", hours: float = 20.0) -> Dict[str, Any]:
    return {
        "station_id": tank,
        "customer_tank_id": tank,
        "customer_id": "CUST-001",
        "customer_type": "residential",
        "fuel_grade": "KEROSENE",
        "fuel_type": "heating_oil",
        "hours_to_runout_p50": hours,
        "hours_to_runout_p90": hours * 1.5,
        "runout_risk_24h": 0.8,
        "confidence": 0.3,
        "anomaly_flags": ["insufficient_history", "weather_fallback"],
        "weather_fallback": True,
        "scheduled_deliveries": [],
        "run_id": RUN,
        "timestamp": "2026-01-01T00:00:00Z",
        "tenant_id": TENANT,
    }


def _response(rows: List[Dict[str, Any]], total: int | None = None) -> Dict[str, Any]:
    return {
        "hits": {
            "hits": [{"_source": row} for row in rows],
            "total": {"value": total if total is not None else len(rows)},
        }
    }


class _Recorder:
    """Captures every (index, body) pair the tool sends to ES.

    First call is the latest-run probe; the second is the risk query.
    """

    def __init__(self, latest: Dict[str, Any] | None, risk: Dict[str, Any]):
        self.calls: List[tuple[str, Dict[str, Any]]] = []
        self._latest = latest
        self._risk = risk

    def __call__(self, index: str, body: Dict[str, Any]):
        # Sync on purpose: AsyncMock only awaits a side_effect it recognises as
        # a coroutine function, which a callable instance is not.
        self.calls.append((index, body))
        if len(self.calls) == 1:
            return _response([self._latest] if self._latest else [])
        return self._risk

    @property
    def risk_body(self) -> Dict[str, Any]:
        assert len(self.calls) >= 2, f"tool never issued a risk query: {self.calls}"
        return self.calls[1][1]

    def filters(self) -> List[Dict[str, Any]]:
        """The tool's own filter clauses.

        ``inject_tenant_filter`` rewraps the body as
        ``{"bool": {"must": [original], "filter": [tenant term]}}``, so the
        tool's clauses end up one level down. Reading the outer ``filter`` would
        find only the tenant term and every assertion here would be vacuous.
        """
        return self.risk_body["query"]["bool"]["must"][0]["bool"]["filter"]

    @staticmethod
    def tenant_terms(body: Dict[str, Any]) -> List[Any]:
        return [
            f.get("term", {}).get("tenant_id")
            for f in body["query"]["bool"]["filter"]
        ]


async def _run(recorder: _Recorder, **kwargs) -> Dict[str, Any]:
    with patch(
        "Agents.tools.tank_forecast_tools.elasticsearch_service"
    ) as mock_es, set_current_tenant(TENANT):
        mock_es.search_documents = AsyncMock(side_effect=recorder)
        raw = await _tool(**kwargs)
    return json.loads(raw)


class TestPinnedToLatestRun:
    @pytest.mark.asyncio
    async def test_risk_query_filters_to_the_latest_run_id(self):
        rec = _Recorder(_station_row(), _response([_station_row()]))
        payload = await _run(rec, within_hours=48)

        assert {"term": {"run_id": RUN}} in rec.filters(), rec.filters()
        assert payload["forecast_run_id"] == RUN
        assert payload["as_of"] == "2026-01-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_latest_run_probe_sorts_by_timestamp_descending(self):
        rec = _Recorder(_station_row(), _response([_station_row()]))
        await _run(rec, within_hours=48)

        index, body = rec.calls[0]
        assert index == FORECAST_INDEX
        assert body["size"] == 1
        assert body["sort"][0]["timestamp"]["order"] == "desc"

    @pytest.mark.asyncio
    async def test_every_query_is_tenant_scoped(self):
        rec = _Recorder(_station_row(), _response([_station_row()]))
        await _run(rec, within_hours=48)

        for index, body in rec.calls:
            assert TENANT in _Recorder.tenant_terms(body), (index, body)


class TestTotalIsNotThePageLength:
    @pytest.mark.asyncio
    async def test_total_and_shown_are_reported_separately(self):
        # 40 match, the page holds 2.
        rows = [_station_row("FS-001"), _station_row("FS-002")]
        rec = _Recorder(_station_row(), _response(rows, total=40))
        payload = await _run(rec, within_hours=48, limit=2)

        assert payload["total_at_risk"] == 40
        assert payload["shown"] == 2

    @pytest.mark.asyncio
    async def test_limit_is_capped_to_a_sane_page(self):
        rec = _Recorder(_station_row(), _response([_station_row()]))
        await _run(rec, within_hours=48, limit=10_000)
        assert rec.risk_body["size"] <= 100


class TestSubjectIsNeverMislabelled:
    @pytest.mark.asyncio
    async def test_station_rows_are_labelled_station_and_omit_customer_fields(self):
        rec = _Recorder(_station_row(), _response([_station_row("FS-005")]))
        payload = await _run(rec, within_hours=48)

        tank = payload["tanks"][0]
        assert tank["tank_id"] == "FS-005"
        assert tank["subject"] == "station"
        assert "customer_id" not in tank, (
            "a station forecast has no customer; emitting a null invites "
            f"'customer: None' in a reply: {tank}"
        )
        assert payload["shown_station_tanks"] == 1
        assert payload["shown_customer_tanks"] == 0

    @pytest.mark.asyncio
    async def test_customer_rows_are_labelled_and_carry_the_customer(self):
        rec = _Recorder(_customer_row(), _response([_customer_row("TANK-001")]))
        payload = await _run(rec, within_hours=48)

        tank = payload["tanks"][0]
        assert tank["tank_id"] == "TANK-001"
        assert tank["subject"] == "customer_tank"
        assert tank["customer_id"] == "CUST-001"
        assert tank["customer_type"] == "residential"
        assert payload["shown_customer_tanks"] == 1

    @pytest.mark.asyncio
    async def test_customer_subject_requires_customer_tank_id(self):
        rec = _Recorder(_customer_row(), _response([_customer_row()]))
        await _run(rec, within_hours=48, subject="customer_tank")
        assert {"exists": {"field": "customer_tank_id"}} in rec.filters()

    @pytest.mark.asyncio
    async def test_station_subject_excludes_customer_tanks(self):
        rec = _Recorder(_station_row(), _response([_station_row()]))
        await _run(rec, within_hours=48, subject="station")
        assert {
            "bool": {"must_not": [{"exists": {"field": "customer_tank_id"}}]}
        } in rec.filters()

    @pytest.mark.asyncio
    async def test_all_subject_does_not_filter_on_subject(self):
        rec = _Recorder(_station_row(), _response([_station_row()]))
        await _run(rec, within_hours=48, subject="all")
        serialised = json.dumps(rec.filters())
        assert "customer_tank_id" not in serialised, serialised

    @pytest.mark.asyncio
    async def test_unknown_subject_refuses_instead_of_widening(self):
        rec = _Recorder(_station_row(), _response([_station_row()]))
        payload = await _run(rec, within_hours=48, subject="trucks")

        assert "error" in payload, payload
        assert "trucks" in payload["error"]
        assert not rec.calls, (
            "an unknown subject must refuse before querying, not answer the "
            "broader question and label it as the narrow one"
        )


class TestProductVocabulary:
    @pytest.mark.asyncio
    async def test_either_spelling_matches_both_field_names(self):
        """Stations carry ``fuel_grade``; customer tanks also carry ``fuel_type``.

        Verified live: ``heating_oil`` and ``KEROSENE`` both resolve TANK-001,
        whose ``fuel_grade`` is KEROSENE and ``fuel_type`` is heating_oil.
        """
        rec = _Recorder(_customer_row(), _response([_customer_row()]))
        await _run(rec, within_hours=720, product_code="heating_oil")

        clause = next(
            f for f in rec.filters() if "bool" in f and "should" in f["bool"]
        )
        should = clause["bool"]["should"]
        assert {"term": {"fuel_grade": "HEATING_OIL"}} in should, should
        assert {"term": {"fuel_type": "heating_oil"}} in should, should
        assert clause["bool"]["minimum_should_match"] == 1

    @pytest.mark.asyncio
    async def test_no_product_means_no_product_filter(self):
        rec = _Recorder(_station_row(), _response([_station_row()]))
        await _run(rec, within_hours=48)
        serialised = json.dumps(rec.filters())
        assert "fuel_grade" not in serialised, serialised


class TestHorizonAndOrdering:
    @pytest.mark.asyncio
    async def test_horizon_becomes_a_p50_range_filter(self):
        rec = _Recorder(_station_row(), _response([_station_row()]))
        await _run(rec, within_hours=36)
        assert {"range": {"hours_to_runout_p50": {"lte": 36}}} in rec.filters()

    @pytest.mark.asyncio
    async def test_most_urgent_first(self):
        rec = _Recorder(_station_row(), _response([_station_row()]))
        await _run(rec, within_hours=48)
        sort = rec.risk_body["sort"]
        assert sort[0]["hours_to_runout_p50"]["order"] == "asc"


class TestDataQualityIsPartOfTheAnswer:
    @pytest.mark.asyncio
    async def test_low_confidence_and_weather_fallback_are_counted(self):
        rows = [_customer_row("TANK-001"), _station_row("FS-005")]
        rec = _Recorder(_customer_row(), _response(rows))
        payload = await _run(rec, within_hours=48, limit=10)

        dq = payload["data_quality"]
        # The station row sits at 0.1 confidence; the customer row at 0.3 is on
        # the threshold, so not counted.
        assert dq["low_confidence_rows"] == 1, payload["tanks"]
        assert dq["weather_fallback_rows"] == 1
        assert dq["note"], "a low-confidence answer must say so"

    @pytest.mark.asyncio
    async def test_clean_rows_get_no_caveat(self):
        clean = _station_row()
        clean["confidence"] = 0.9
        clean["anomaly_flags"] = []
        rec = _Recorder(clean, _response([clean]))
        payload = await _run(rec, within_hours=48)

        assert payload["data_quality"]["low_confidence_rows"] == 0
        assert payload["data_quality"]["note"] is None


class TestNoForecastsIsNotZeroRisk:
    @pytest.mark.asyncio
    async def test_absent_forecasts_explain_themselves(self):
        rec = _Recorder(None, _response([]))
        payload = await _run(rec, within_hours=48)

        assert payload["total_at_risk"] == 0
        assert payload["as_of"] is None
        assert "no_data_reason" in payload, (
            "'no forecasts exist' and 'nothing is at risk' are different "
            "answers, and only one of them is actionable"
        )
        assert len(rec.calls) == 1, "should not run a risk query with no run to pin to"


class TestFailureIsReportedNotRaised:
    @pytest.mark.asyncio
    async def test_es_error_returns_an_error_payload(self):
        with patch(
            "Agents.tools.tank_forecast_tools.elasticsearch_service"
        ) as mock_es, set_current_tenant(TENANT):
            mock_es.search_documents = AsyncMock(side_effect=Exception("es down"))
            raw = await _tool(within_hours=48)

        payload = json.loads(raw)
        assert payload["error"] == "es down"

    @pytest.mark.asyncio
    async def test_missing_tenant_scope_raises(self):
        from Agents.tools._tenant_context import current_tenant_id_var

        token = current_tenant_id_var.set(None)
        try:
            with pytest.raises(RuntimeError):
                await _tool(within_hours=48)
        finally:
            current_tenant_id_var.reset(token)
