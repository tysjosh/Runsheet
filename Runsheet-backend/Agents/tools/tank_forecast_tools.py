"""Tank run-out risk — the question this market asks most.

For a US fuel marketer selling propane and heating oil, "who is about to run
dry?" is the business. A keep-full or auto-fill customer running out is a
contract breach and, in January, a no-heat call. The platform already computes
the answer: the Tank_Forecasting_Agent writes a forecast per
(customer_tank, product) into ``mvp_tank_forecasts`` with p50/p90 hours to
run-out and a 24-hour risk probability.

Until this module existed no specialist could read it, so the agent answered
"I am unable to identify the specific tanks most at risk" against an index
holding nearly two thousand forecasts.

Three deliberate properties, because this answer is sold and, in an audited
industry, has to be checkable:

* **One authoritative number.** The tool returns the true match count *and* how
  many rows it is showing, separately. A capped page reported as a total is what
  let the agent claim two different order counts in one reply.
* **Provenance.** Every answer carries the forecast run id and its timestamp, so
  a dispatcher can tell fresh data from stale.
* **Data quality is part of the answer, not hidden.** A forecast with no history
  carries ``confidence: 0.1`` and ``anomaly_flags: ["no_history"]``, and one
  computed without degree-days carries ``weather_fallback: true``. Reporting a
  confident-sounding hour count off a 0.1-confidence guess would be worse than
  saying so. Low-confidence coverage is also a tank-monitor upsell signal rather
  than a failure.

One more thing the live index forced, verified against the demo tenant's 1,983
documents: a single run holds **two kinds of subject**. Depot/station forecasts
carry ``station_id`` with ``customer_tank_id`` null; customer-tank forecasts
carry both plus ``customer_id``, ``customer_type``, ``fuel_type`` and
``weather_fallback``. In the run ``forecast_20260805_152725`` that was 14
stations and 6 customer tanks. Returning a station id under a key named
``customer_tank_id`` would be a mislabel the model would faithfully repeat, so
each row states its ``subject`` and the caller can filter on it — "which
customers are about to run dry" and "which of our depots is about to run dry"
are different questions with different consequences.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from strands import tool

from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import elasticsearch_service

from ._tenant_context import get_current_tenant
from .logging_wrapper import get_telemetry_service

logger = logging.getLogger(__name__)

FORECAST_INDEX = "mvp_tank_forecasts"

#: Below this, a forecast is a guess rather than a projection. Surfaced in the
#: answer instead of being filtered out, so the gap is visible.
LOW_CONFIDENCE_THRESHOLD = 0.3

#: Accepted spellings for the ``subject`` argument. The model picks the wording,
#: so accept the obvious synonyms rather than silently returning everything.
_CUSTOMER_SUBJECTS = {"customer_tank", "customer", "customers", "customer_tanks"}
_STATION_SUBJECTS = {"station", "stations", "depot", "depots", "own"}


def _log_tool_invocation(tool_name, input_params, start_time, success, error=None) -> None:
    duration_ms = (time.time() - start_time) * 1000
    telemetry = get_telemetry_service()
    if telemetry:
        telemetry.log_tool_invocation(
            tool_name=tool_name,
            input_params=input_params,
            duration_ms=duration_ms,
            success=success,
            error=error,
        )


def _format_hits(response: dict) -> List[Dict[str, Any]]:
    return [hit["_source"] for hit in response.get("hits", {}).get("hits", [])]


def _total_hits(response: dict) -> int:
    total = response.get("hits", {}).get("total", 0)
    if isinstance(total, dict):
        return total.get("value", 0)
    return total


async def _latest_run(tenant_id: str) -> Optional[Dict[str, Any]]:
    """Return the most recent forecast run for the tenant.

    Forecasts accumulate one document per tank per cycle, so the index holds many
    runs at once. Counting across all of them would multiply every tank by the
    number of cycles retained and produce a confidently wrong number — exactly
    the failure mode this module is meant to avoid.
    """
    query = inject_tenant_filter({"query": {"match_all": {}}}, tenant_id)
    query["size"] = 1
    query["sort"] = [{"timestamp": {"order": "desc", "unmapped_type": "date"}}]
    response = await elasticsearch_service.search_documents(FORECAST_INDEX, query)
    hits = _format_hits(response)
    return hits[0] if hits else None


@tool
async def get_runout_risk_list(
    within_hours: int = 48,
    limit: int = 15,
    product_code: Optional[str] = None,
    subject: str = "all",
) -> str:
    """List the tanks most at risk of running out of fuel.

    Answers "which tanks are at risk in the next N hours?" — the core planning
    question for propane and heating-oil delivery. Reads the latest forecast run
    only, and reports how many tanks match in total alongside the rows shown.

    Args:
        within_hours: Horizon in hours. A tank is at risk when its median
            (p50) hours-to-runout falls inside this window. Default 48.
        limit: Maximum tanks to list, most urgent first. Default 15.
        product_code: Optionally restrict to one product. Either vocabulary
            works: DIESEL_2, HEATING_OIL, PROPANE, GASOLINE_REG, KEROSENE, or
            diesel / heating_oil / propane / gasoline.
        subject: "customer_tank" for customer tanks only (a run-out here is a
            keep-full contract breach and, in winter, a no-heat call),
            "station" for own depot/station tanks only, or "all". Default "all".

    Returns:
        JSON with ``total_at_risk``, ``shown``, the ranked ``tanks`` (each
        stating its own ``subject``), the forecast run's ``as_of``, and a
        ``data_quality`` block.
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()
    params = {
        "within_hours": within_hours,
        "limit": limit,
        "product_code": product_code,
        "subject": subject,
    }

    try:
        logger.info(
            "AI tool invocation: tool=get_runout_risk_list tenant_id=%s params=%s",
            tenant_id,
            json.dumps(params, default=str),
        )

        subject_key = (subject or "all").strip().lower()
        if (
            subject_key not in _CUSTOMER_SUBJECTS
            and subject_key not in _STATION_SUBJECTS
            and subject_key not in {"all", ""}
        ):
            # Widening silently would report an "all tanks" number as if it were
            # the narrower answer that was asked for.
            success = True
            return json.dumps(
                {
                    "tool": "get_runout_risk_list",
                    "error": (
                        f"Unknown subject {subject!r}. Use 'customer_tank', "
                        "'station', or 'all'."
                    ),
                }
            )

        latest = await _latest_run(tenant_id)
        if latest is None:
            # No forecasts at all is a different answer from "nothing at risk",
            # and the distinction is actionable: it means the forecaster has not
            # run or has no tanks to work with.
            success = True
            return json.dumps(
                {
                    "tool": "get_runout_risk_list",
                    "subject": subject_key,
                    "total_at_risk": 0,
                    "shown": 0,
                    "tanks": [],
                    "as_of": None,
                    "no_data_reason": (
                        "No tank forecasts exist for this tenant yet. The tank "
                        "forecasting agent may not have run, or no customer "
                        "tanks are configured."
                    ),
                }
            )

        run_id = latest.get("run_id")
        as_of = latest.get("timestamp")

        filters: List[Dict[str, Any]] = [
            {"range": {"hours_to_runout_p50": {"lte": within_hours}}}
        ]
        if run_id:
            # Pin to one run so each tank is counted once.
            filters.append({"term": {"run_id": run_id}})
        if product_code:
            # The two subjects use two vocabularies for the same product:
            # stations carry ``fuel_grade`` ("DIESEL_2"), customer tanks carry
            # that plus ``fuel_type`` ("diesel"). Match either so the caller
            # does not have to know which, and neither does the model.
            code = product_code.strip()
            filters.append(
                {
                    "bool": {
                        "should": [
                            {"term": {"fuel_grade": code.upper()}},
                            {"term": {"fuel_type": code.lower()}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )

        if subject_key in _CUSTOMER_SUBJECTS:
            filters.append({"exists": {"field": "customer_tank_id"}})
        elif subject_key in _STATION_SUBJECTS:
            filters.append(
                {"bool": {"must_not": [{"exists": {"field": "customer_tank_id"}}]}}
            )

        query = inject_tenant_filter(
            {"query": {"bool": {"filter": filters}}}, tenant_id
        )
        query["size"] = max(1, min(limit, 100))
        query["sort"] = [
            {"hours_to_runout_p50": {"order": "asc"}},
            {"runout_risk_24h": {"order": "desc"}},
        ]

        response = await elasticsearch_service.search_documents(FORECAST_INDEX, query)
        rows = _format_hits(response)
        total = _total_hits(response)

        tanks = []
        low_confidence = 0
        weather_fallback = 0
        customer_tanks = 0
        station_tanks = 0
        for row in rows:
            confidence = row.get("confidence")
            if isinstance(confidence, (int, float)) and confidence < LOW_CONFIDENCE_THRESHOLD:
                low_confidence += 1
            if row.get("weather_fallback"):
                weather_fallback += 1
            customer_tank_id = row.get("customer_tank_id")
            is_customer = bool(customer_tank_id)
            if is_customer:
                customer_tanks += 1
            else:
                station_tanks += 1

            entry: Dict[str, Any] = {
                # One id key regardless of subject, and ``subject`` says what it
                # is. A station id under a key called ``customer_tank_id`` is a
                # mislabel the model would repeat verbatim.
                "tank_id": customer_tank_id or row.get("station_id"),
                "subject": "customer_tank" if is_customer else "station",
                "product": row.get("fuel_grade") or row.get("fuel_type"),
                "hours_to_runout_p50": row.get("hours_to_runout_p50"),
                "hours_to_runout_p90": row.get("hours_to_runout_p90"),
                "runout_risk_24h": row.get("runout_risk_24h"),
                "confidence": confidence,
                "anomaly_flags": row.get("anomaly_flags") or [],
                "scheduled_deliveries": len(row.get("scheduled_deliveries") or []),
            }
            if is_customer:
                # Only present on customer-tank forecasts; emitting nulls for
                # station rows invites "customer: None" in a reply.
                entry["customer_id"] = row.get("customer_id")
                entry["customer_type"] = row.get("customer_type")
            tanks.append(entry)

        result = {
            "tool": "get_runout_risk_list",
            "within_hours": within_hours,
            "subject": subject_key,
            "total_at_risk": total,
            "shown": len(tanks),
            "shown_customer_tanks": customer_tanks,
            "shown_station_tanks": station_tanks,
            "as_of": as_of,
            "forecast_run_id": run_id,
            "tanks": tanks,
            "data_quality": {
                "low_confidence_rows": low_confidence,
                "weather_fallback_rows": weather_fallback,
                "note": (
                    "Rows flagged no_history or below "
                    f"{LOW_CONFIDENCE_THRESHOLD} confidence are estimates without "
                    "consumption history — a tank-monitor feed would firm them up."
                )
                if low_confidence
                else None,
            },
        }

        success = True
        logger.info(
            "get_runout_risk_list: %d at risk within %dh (showing %d), run=%s",
            total,
            within_hours,
            len(tanks),
            run_id,
        )
        return json.dumps(result, default=str)

    except Exception as exc:  # noqa: BLE001 — tool must return, not raise
        error_msg = str(exc)
        logger.error("get_runout_risk_list failed: %s", exc)
        return json.dumps({"tool": "get_runout_risk_list", "error": str(exc)})
    finally:
        _log_tool_invocation(
            "get_runout_risk_list", params, start_time, success, error_msg
        )


__all__ = ["get_runout_risk_list", "FORECAST_INDEX", "LOW_CONFIDENCE_THRESHOLD"]
