"""
Ops metrics tools for the AI agent.

Provides tenant-scoped, read-only operational metrics from the fuel order and
driver Elasticsearch indices. Search and event lookup live in ``order_tools``;
this module only keeps the shared ops metrics tool used by the specialist
agents.
"""

import json
import logging
import time
from datetime import datetime, timedelta

from strands import tool

from ops.middleware.tenant_guard import inject_tenant_filter

from .ops_feature_guard import check_ops_feature_flag

logger = logging.getLogger(__name__)
ES_SEARCH_TIMEOUT_SECONDS = 10
ORDER_INDEX = "fuel_orders_current"
DRIVER_INDEX = "drivers_current"

_ops_es_service = None


def configure_ops_search_tools(ops_es_service) -> None:
    """Wire the OpsElasticsearchService into this module at startup."""
    global _ops_es_service
    _ops_es_service = ops_es_service
    logger.info("Ops metrics tools configured with OpsElasticsearchService")


def _get_es():
    """Return the configured OpsElasticsearchService or raise."""
    if _ops_es_service is None:
        raise RuntimeError(
            "Ops metrics tools not configured. Call configure_ops_search_tools() during startup."
        )
    return _ops_es_service


async def _search(es, index: str, body: dict) -> dict:
    """Run a search through the service facade.

    Reaching ``es.client.search(...)`` directly bypassed the document-store
    backend switch, so this read would have kept going to Elasticsearch after the
    document plane moved to Postgres. ``search_documents`` is already async and
    returns the same response shape, so the sync/awaitable dance the raw client
    needed is gone too.
    """
    return await es.search_documents(
        index, body, request_timeout=ES_SEARCH_TIMEOUT_SECONDS
    )


def _log_tool_call(tool_name: str, params: dict, tenant_id: str, user_id: str = "ai_agent"):
    """Log an AI tool invocation for audit purposes."""
    logger.info(
        "AI tool invocation: tool=%s tenant_id=%s user_id=%s params=%s",
        tool_name,
        tenant_id,
        user_id,
        json.dumps(params, default=str),
    )


def _total_hits(response: dict) -> int:
    """Extract total hit count from an ES search response."""
    total = response.get("hits", {}).get("total", 0)
    if isinstance(total, dict):
        return total.get("value", 0)
    return total


@tool
async def get_ops_metrics(
    tenant_id: str,
    metric_type: str = "orders",
    bucket: str = "hourly",
    start_date: str = None,
    end_date: str = None,
) -> str:
    """
    Get aggregated operational metrics for orders, drivers, SLA, or failures.

    Results are scoped to the requesting tenant and returned as JSON for the
    AI agent to interpret.
    """
    start_time = time.time()
    params = {
        "metric_type": metric_type,
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
    }
    _log_tool_call("get_ops_metrics", params, tenant_id)

    disabled = await check_ops_feature_flag(tenant_id)
    if disabled:
        return disabled

    try:
        es = _get_es()
        now = datetime.utcnow()
        if not end_date:
            end_date = now.isoformat() + "Z"
        if not start_date:
            start_date = (now - timedelta(hours=24)).isoformat() + "Z"

        try:
            sd = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            ed = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if (ed - sd).days > 90:
                bucket = "daily"
        except (ValueError, TypeError):
            logger.warning(
                "Invalid ops metrics date range: start_date=%s end_date=%s",
                start_date,
                end_date,
                exc_info=True,
            )

        interval = "1h" if bucket == "hourly" else "1d"
        if metric_type == "orders":
            result = await _order_metrics(es, tenant_id, start_date, end_date, interval, bucket)
        elif metric_type == "drivers":
            result = await _driver_metrics(es, tenant_id, start_date, end_date, bucket)
        elif metric_type == "sla":
            result = await _sla_metrics(es, tenant_id, start_date, end_date, bucket)
        elif metric_type == "failures":
            result = await _failure_metrics(es, tenant_id, start_date, end_date, interval, bucket)
        else:
            result = {
                "tool": "get_ops_metrics",
                "error": (
                    f"Unknown metric_type '{metric_type}'. "
                    "Use: orders, drivers, sla, failures."
                ),
            }

        duration_ms = (time.time() - start_time) * 1000
        logger.info("get_ops_metrics(%s) completed in %.1fms", metric_type, duration_ms)
        return json.dumps(result, default=str)
    except Exception as e:
        logger.exception("get_ops_metrics failed")
        return json.dumps({"tool": "get_ops_metrics", "error": str(e)})


async def _order_metrics(es, tenant_id, start_date, end_date, interval, bucket):
    """Order counts aggregated by status, intake channel, and time bucket."""
    es_query = {
        "query": {
            "bool": {
                "filter": [
                    {"range": {"updated_at": {"gte": start_date, "lte": end_date}}},
                ]
            }
        },
        "size": 0,
        "aggs": {
            "by_status": {"terms": {"field": "status", "size": 20}},
            "by_intake_channel": {"terms": {"field": "intake_channel", "size": 20}},
            "over_time": {
                "date_histogram": {
                    "field": "updated_at",
                    "fixed_interval": interval,
                    "min_doc_count": 0,
                    "extended_bounds": {"min": start_date, "max": end_date},
                },
                "aggs": {"by_status": {"terms": {"field": "status", "size": 20}}},
            },
        },
    }
    es_query = inject_tenant_filter(es_query, tenant_id)
    response = await _search(es, ORDER_INDEX, es_query)
    aggs = response.get("aggregations", {})

    time_buckets = []
    for tb in aggs.get("over_time", {}).get("buckets", []):
        time_buckets.append(
            {
                "timestamp": tb.get("key_as_string"),
                "count": tb["doc_count"],
                "breakdown": {
                    sb["key"]: sb["doc_count"]
                    for sb in tb.get("by_status", {}).get("buckets", [])
                },
            }
        )

    return {
        "tool": "get_ops_metrics",
        "metric_type": "orders",
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
        "summary": {
            b["key"]: b["doc_count"]
            for b in aggs.get("by_status", {}).get("buckets", [])
        },
        "by_intake_channel": {
            b["key"]: b["doc_count"]
            for b in aggs.get("by_intake_channel", {}).get("buckets", [])
        },
        "total": _total_hits(response),
        "time_series": time_buckets,
    }


async def _driver_metrics(es, tenant_id, start_date, end_date, bucket):
    """Driver utilization and availability metrics."""
    es_query = {
        "query": {"match_all": {}},
        "size": 0,
        "aggs": {
            "by_status": {"terms": {"field": "status", "size": 20}},
            "by_availability": {"terms": {"field": "availability", "size": 20}},
            "avg_active_orders": {"avg": {"field": "active_order_count"}},
            "avg_completed_today": {"avg": {"field": "completed_today"}},
            "hazmat_count": {"filter": {"term": {"hazmat_endorsement": True}}},
        },
    }
    es_query = inject_tenant_filter(es_query, tenant_id)
    response = await _search(es, DRIVER_INDEX, es_query)
    aggs = response.get("aggregations", {})

    return {
        "tool": "get_ops_metrics",
        "metric_type": "drivers",
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
        "total_drivers": _total_hits(response),
        "by_status": {
            b["key"]: b["doc_count"]
            for b in aggs.get("by_status", {}).get("buckets", [])
        },
        "by_availability": {
            b["key"]: b["doc_count"]
            for b in aggs.get("by_availability", {}).get("buckets", [])
        },
        "avg_active_orders": aggs.get("avg_active_orders", {}).get("value"),
        "avg_completed_today": aggs.get("avg_completed_today", {}).get("value"),
        "hazmat_endorsed_count": aggs.get("hazmat_count", {}).get("doc_count", 0),
    }


async def _sla_metrics(es, tenant_id, start_date, end_date, bucket):
    """SLA compliance percentage and breach counts for fuel orders."""
    now_iso = datetime.utcnow().isoformat() + "Z"
    total_query = {
        "query": {
            "bool": {
                "filter": [
                    {"range": {"created_at": {"gte": start_date, "lte": end_date}}},
                ]
            }
        },
        "size": 0,
    }
    total_query = inject_tenant_filter(total_query, tenant_id)
    total_resp = await _search(es, ORDER_INDEX, total_query)
    total_count = _total_hits(total_resp)

    breach_query = {
        "query": {
            "bool": {
                "filter": [
                    {"range": {"created_at": {"gte": start_date, "lte": end_date}}},
                    {"range": {"delivery_window_end": {"lt": now_iso}}},
                ],
                "must_not": [{"terms": {"status": ["delivered", "cancelled"]}}],
            }
        },
        "size": 0,
    }
    breach_query = inject_tenant_filter(breach_query, tenant_id)
    breach_resp = await _search(es, ORDER_INDEX, breach_query)
    breach_count = _total_hits(breach_resp)
    compliance_pct = (
        round(((total_count - breach_count) / total_count) * 100, 2)
        if total_count > 0
        else 100.0
    )

    return {
        "tool": "get_ops_metrics",
        "metric_type": "sla",
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
        "total_orders": total_count,
        "sla_breaches": breach_count,
        "compliance_percentage": compliance_pct,
    }


async def _failure_metrics(es, tenant_id, start_date, end_date, interval, bucket):
    """Failure counts grouped by failure reason."""
    es_query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"status": "failed"}},
                    {"range": {"updated_at": {"gte": start_date, "lte": end_date}}},
                ]
            }
        },
        "size": 0,
        "aggs": {
            "by_reason": {"terms": {"field": "failure_reason", "size": 50}},
            "over_time": {
                "date_histogram": {
                    "field": "updated_at",
                    "fixed_interval": interval,
                    "min_doc_count": 0,
                    "extended_bounds": {"min": start_date, "max": end_date},
                },
            },
        },
    }
    es_query = inject_tenant_filter(es_query, tenant_id)
    response = await _search(es, ORDER_INDEX, es_query)
    aggs = response.get("aggregations", {})

    return {
        "tool": "get_ops_metrics",
        "metric_type": "failures",
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
        "by_reason": {
            b["key"]: b["doc_count"]
            for b in aggs.get("by_reason", {}).get("buckets", [])
        },
        "time_series": [
            {"timestamp": tb.get("key_as_string"), "count": tb["doc_count"]}
            for tb in aggs.get("over_time", {}).get("buckets", [])
        ],
        "total_failures": _total_hits(response),
    }
