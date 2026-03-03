"""
Ops search tools for the AI agent.

Provides read-only query tools for shipment, rider, and event data from the
Ops Intelligence Layer Elasticsearch indices. All tools enforce tenant scoping
via TenantGuard, apply PII masking, and check feature flags before executing.

Validates:
- Requirements 17.1-17.6: AI tools for querying shipment, rider, and event indices
- Requirements 19.1-19.2: Read-only access (no mutations)
- Requirement 19.5: Log all tool invocations with tool name, params, tenant_id, user_id
"""

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from strands import tool

from ops.middleware.pii_masker import PIIMasker
from ops.middleware.tenant_guard import inject_tenant_filter

from .ops_feature_guard import check_ops_feature_flag

logger = logging.getLogger(__name__)

# Module-level service references, wired at startup via configure_ops_search_tools()
_ops_es_service = None
_pii_masker = PIIMasker()


def configure_ops_search_tools(ops_es_service) -> None:
    """
    Wire the OpsElasticsearchService into this module.

    Called once during application startup (lifespan) so that the tool
    functions can query the ops Elasticsearch indices.
    """
    global _ops_es_service
    _ops_es_service = ops_es_service
    logger.info("Ops search tools configured with OpsElasticsearchService")


def _get_es():
    """Return the configured OpsElasticsearchService or raise."""
    if _ops_es_service is None:
        raise RuntimeError(
            "Ops search tools not configured. Call configure_ops_search_tools() during startup."
        )
    return _ops_es_service


def _log_tool_call(tool_name: str, params: dict, tenant_id: str, user_id: str = "ai_agent"):
    """
    Log an AI tool invocation for audit purposes.

    Validates: Requirement 19.5
    """
    logger.info(
        "AI tool invocation: tool=%s tenant_id=%s user_id=%s params=%s",
        tool_name,
        tenant_id,
        user_id,
        json.dumps(params, default=str),
    )


def _format_hits(response: dict) -> list[dict]:
    """Extract _source documents from an ES search response."""
    return [hit["_source"] for hit in response.get("hits", {}).get("hits", [])]


def _total_hits(response: dict) -> int:
    """Extract total hit count from an ES search response."""
    total = response.get("hits", {}).get("total", 0)
    if isinstance(total, dict):
        return total.get("value", 0)
    return total


@tool
async def search_shipments(
    tenant_id: str,
    status: str = None,
    rider_id: str = None,
    start_date: str = None,
    end_date: str = None,
    query: str = None,
    page: int = 1,
    size: int = 20,
) -> str:
    """
    Search shipments in the ops intelligence layer.

    Use this tool to find shipments by status, assigned rider, time range,
    or free-text query. Results are scoped to the requesting tenant.

    Args:
        tenant_id: The tenant ID (from authenticated context).
        status: Filter by shipment status (pending, in_transit, delivered, failed, returned).
        rider_id: Filter by assigned rider ID.
        start_date: Filter shipments created on or after this ISO-8601 date.
        end_date: Filter shipments created on or before this ISO-8601 date.
        query: Free-text search across origin and destination fields.
        page: Page number (default 1).
        size: Results per page (default 20).

    Returns:
        JSON string with shipment results for the AI agent to interpret.

    Validates: Requirements 17.1, 17.5, 17.6, 19.1, 19.2, 19.5
    """
    start_time = time.time()
    params = {
        "status": status,
        "rider_id": rider_id,
        "start_date": start_date,
        "end_date": end_date,
        "query": query,
        "page": page,
        "size": size,
    }
    _log_tool_call("search_shipments", params, tenant_id)

    # Feature flag check
    disabled = await check_ops_feature_flag(tenant_id)
    if disabled:
        return disabled

    try:
        es = _get_es()

        # Build the query
        must_clauses: list[dict] = []
        filter_clauses: list[dict] = []

        if status:
            filter_clauses.append({"term": {"status": status}})
        if rider_id:
            filter_clauses.append({"term": {"rider_id": rider_id}})

        # Time range on created_at
        if start_date or end_date:
            range_filter: dict = {}
            if start_date:
                range_filter["gte"] = start_date
            if end_date:
                range_filter["lte"] = end_date
            filter_clauses.append({"range": {"created_at": range_filter}})

        # Free-text search across origin/destination
        if query:
            must_clauses.append(
                {
                    "multi_match": {
                        "query": query,
                        "fields": ["origin", "destination"],
                        "type": "best_fields",
                    }
                }
            )

        es_query: dict = {"query": {"bool": {}}}
        if must_clauses:
            es_query["query"]["bool"]["must"] = must_clauses
        if filter_clauses:
            es_query["query"]["bool"]["filter"] = filter_clauses
        if not must_clauses and not filter_clauses:
            es_query = {"query": {"match_all": {}}}

        # Inject tenant scoping
        es_query = inject_tenant_filter(es_query, tenant_id)

        # Pagination
        from_offset = (page - 1) * size
        es_query["from"] = from_offset
        es_query["size"] = size
        es_query["sort"] = [{"updated_at": {"order": "desc"}}]

        response = await es.client.search(
            index=es.SHIPMENTS_CURRENT, body=es_query
        )

        hits = _format_hits(response)
        total = _total_hits(response)

        # PII mask all AI tool outputs (Req 22.2)
        masked_hits = [_pii_masker.mask_response(h, has_pii_access=False) for h in hits]

        result = {
            "tool": "search_shipments",
            "total": total,
            "page": page,
            "size": size,
            "shipments": masked_hits,
        }

        duration_ms = (time.time() - start_time) * 1000
        logger.info("search_shipments completed in %.1fms, %d results", duration_ms, total)
        return json.dumps(result, default=str)

    except Exception as e:
        logger.error("search_shipments failed: %s", e)
        return json.dumps({"tool": "search_shipments", "error": str(e)})


@tool
async def search_riders(
    tenant_id: str,
    status: str = None,
    availability: str = None,
    min_utilization: int = None,
    max_utilization: int = None,
    page: int = 1,
    size: int = 20,
) -> str:
    """
    Search riders in the ops intelligence layer.

    Use this tool to find riders by status, availability, or utilization level.
    Results are scoped to the requesting tenant.

    Args:
        tenant_id: The tenant ID (from authenticated context).
        status: Filter by rider status (active, idle, offline).
        availability: Filter by availability (available, busy, offline).
        min_utilization: Minimum active shipment count filter.
        max_utilization: Maximum active shipment count filter.
        page: Page number (default 1).
        size: Results per page (default 20).

    Returns:
        JSON string with rider results for the AI agent to interpret.

    Validates: Requirements 17.2, 17.5, 17.6, 19.1, 19.2, 19.5
    """
    start_time = time.time()
    params = {
        "status": status,
        "availability": availability,
        "min_utilization": min_utilization,
        "max_utilization": max_utilization,
        "page": page,
        "size": size,
    }
    _log_tool_call("search_riders", params, tenant_id)

    disabled = await check_ops_feature_flag(tenant_id)
    if disabled:
        return disabled

    try:
        es = _get_es()

        filter_clauses: list[dict] = []

        if status:
            filter_clauses.append({"term": {"status": status}})
        if availability:
            filter_clauses.append({"term": {"availability": availability}})

        # Utilization filters on active_shipment_count
        if min_utilization is not None or max_utilization is not None:
            range_filter: dict = {}
            if min_utilization is not None:
                range_filter["gte"] = min_utilization
            if max_utilization is not None:
                range_filter["lte"] = max_utilization
            filter_clauses.append({"range": {"active_shipment_count": range_filter}})

        if filter_clauses:
            es_query: dict = {"query": {"bool": {"filter": filter_clauses}}}
        else:
            es_query = {"query": {"match_all": {}}}

        es_query = inject_tenant_filter(es_query, tenant_id)

        from_offset = (page - 1) * size
        es_query["from"] = from_offset
        es_query["size"] = size
        es_query["sort"] = [{"last_seen": {"order": "desc"}}]

        response = await es.client.search(
            index=es.RIDERS_CURRENT, body=es_query
        )

        hits = _format_hits(response)
        total = _total_hits(response)

        masked_hits = [_pii_masker.mask_response(h, has_pii_access=False) for h in hits]

        result = {
            "tool": "search_riders",
            "total": total,
            "page": page,
            "size": size,
            "riders": masked_hits,
        }

        duration_ms = (time.time() - start_time) * 1000
        logger.info("search_riders completed in %.1fms, %d results", duration_ms, total)
        return json.dumps(result, default=str)

    except Exception as e:
        logger.error("search_riders failed: %s", e)
        return json.dumps({"tool": "search_riders", "error": str(e)})


@tool
async def get_shipment_events(
    shipment_id: str,
    tenant_id: str,
    page: int = 1,
    size: int = 50,
) -> str:
    """
    Get the event timeline for a specific shipment.

    Use this tool to retrieve the full event history of a shipment, ordered
    chronologically. Results are scoped to the requesting tenant.

    Args:
        shipment_id: The shipment ID to look up events for.
        tenant_id: The tenant ID (from authenticated context).
        page: Page number (default 1).
        size: Results per page (default 50).

    Returns:
        JSON string with the shipment event timeline for the AI agent.

    Validates: Requirements 17.3, 17.5, 17.6, 19.1, 19.2, 19.5
    """
    start_time = time.time()
    params = {"shipment_id": shipment_id, "page": page, "size": size}
    _log_tool_call("get_shipment_events", params, tenant_id)

    disabled = await check_ops_feature_flag(tenant_id)
    if disabled:
        return disabled

    try:
        es = _get_es()

        es_query: dict = {
            "query": {
                "bool": {
                    "filter": [{"term": {"shipment_id": shipment_id}}]
                }
            }
        }

        es_query = inject_tenant_filter(es_query, tenant_id)

        from_offset = (page - 1) * size
        es_query["from"] = from_offset
        es_query["size"] = size
        es_query["sort"] = [{"event_timestamp": {"order": "asc"}}]

        response = await es.client.search(
            index=es.SHIPMENT_EVENTS, body=es_query
        )

        hits = _format_hits(response)
        total = _total_hits(response)

        masked_hits = [_pii_masker.mask_response(h, has_pii_access=False) for h in hits]

        result = {
            "tool": "get_shipment_events",
            "shipment_id": shipment_id,
            "total": total,
            "page": page,
            "size": size,
            "events": masked_hits,
        }

        duration_ms = (time.time() - start_time) * 1000
        logger.info(
            "get_shipment_events completed in %.1fms, %d events for shipment %s",
            duration_ms,
            total,
            shipment_id,
        )
        return json.dumps(result, default=str)

    except Exception as e:
        logger.error("get_shipment_events failed: %s", e)
        return json.dumps({"tool": "get_shipment_events", "error": str(e)})


@tool
async def get_ops_metrics(
    tenant_id: str,
    metric_type: str = "shipments",
    bucket: str = "hourly",
    start_date: str = None,
    end_date: str = None,
) -> str:
    """
    Get aggregated operational metrics.

    Use this tool to retrieve summary statistics for shipments, riders, SLA
    compliance, or failure analysis over a specified time range.

    Args:
        tenant_id: The tenant ID (from authenticated context).
        metric_type: Type of metrics to retrieve. One of: shipments, riders, sla, failures.
        bucket: Time bucket granularity: "hourly" or "daily" (default "hourly").
        start_date: Start of time range (ISO-8601). Defaults to 24 hours ago.
        end_date: End of time range (ISO-8601). Defaults to now.

    Returns:
        JSON string with aggregated metrics for the AI agent to interpret.

    Validates: Requirements 17.4, 17.5, 17.6, 19.1, 19.2, 19.5
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

        # Default time range: last 24 hours
        now = datetime.utcnow()
        if not end_date:
            end_date = now.isoformat() + "Z"
        if not start_date:
            start_date = (now - timedelta(hours=24)).isoformat() + "Z"

        # Enforce daily granularity for ranges > 90 days (Req 11.5)
        try:
            sd = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            ed = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if (ed - sd).days > 90:
                bucket = "daily"
        except (ValueError, TypeError):
            pass

        interval = "1h" if bucket == "hourly" else "1d"

        if metric_type == "shipments":
            result = await _shipment_metrics(es, tenant_id, start_date, end_date, interval, bucket)
        elif metric_type == "riders":
            result = await _rider_metrics(es, tenant_id, start_date, end_date, interval, bucket)
        elif metric_type == "sla":
            result = await _sla_metrics(es, tenant_id, start_date, end_date, interval, bucket)
        elif metric_type == "failures":
            result = await _failure_metrics(es, tenant_id, start_date, end_date, interval, bucket)
        else:
            result = {
                "tool": "get_ops_metrics",
                "error": f"Unknown metric_type '{metric_type}'. Use: shipments, riders, sla, failures.",
            }

        duration_ms = (time.time() - start_time) * 1000
        logger.info("get_ops_metrics(%s) completed in %.1fms", metric_type, duration_ms)
        return json.dumps(result, default=str)

    except Exception as e:
        logger.error("get_ops_metrics failed: %s", e)
        return json.dumps({"tool": "get_ops_metrics", "error": str(e)})


# ---------------------------------------------------------------------------
# Internal metric helpers
# ---------------------------------------------------------------------------

async def _shipment_metrics(es, tenant_id, start_date, end_date, interval, bucket):
    """Shipment counts aggregated by status in time buckets."""
    es_query = {
        "query": {
            "bool": {
                "filter": [
                    {"range": {"updated_at": {"gte": start_date, "lte": end_date}}},
                ]
            }
        }
    }
    es_query = inject_tenant_filter(es_query, tenant_id)
    es_query["size"] = 0
    es_query["aggs"] = {
        "by_status": {
            "terms": {"field": "status", "size": 20},
        },
        "over_time": {
            "date_histogram": {
                "field": "updated_at",
                "fixed_interval": interval,
                "min_doc_count": 0,
                "extended_bounds": {"min": start_date, "max": end_date},
            },
            "aggs": {
                "by_status": {"terms": {"field": "status", "size": 20}},
            },
        },
    }

    response = await es.client.search(index=es.SHIPMENTS_CURRENT, body=es_query)
    aggs = response.get("aggregations", {})

    status_counts = {
        b["key"]: b["doc_count"] for b in aggs.get("by_status", {}).get("buckets", [])
    }
    time_buckets = []
    for tb in aggs.get("over_time", {}).get("buckets", []):
        breakdown = {
            sb["key"]: sb["doc_count"]
            for sb in tb.get("by_status", {}).get("buckets", [])
        }
        time_buckets.append({
            "timestamp": tb.get("key_as_string"),
            "count": tb["doc_count"],
            "breakdown": breakdown,
        })

    return {
        "tool": "get_ops_metrics",
        "metric_type": "shipments",
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
        "summary": status_counts,
        "total": _total_hits(response),
        "time_series": time_buckets,
    }


async def _rider_metrics(es, tenant_id, start_date, end_date, interval, bucket):
    """Rider utilization and availability metrics."""
    es_query = {"query": {"match_all": {}}}
    es_query = inject_tenant_filter(es_query, tenant_id)
    es_query["size"] = 0
    es_query["aggs"] = {
        "by_status": {"terms": {"field": "status", "size": 20}},
        "by_availability": {"terms": {"field": "availability", "size": 20}},
        "avg_active_shipments": {"avg": {"field": "active_shipment_count"}},
        "avg_completed_today": {"avg": {"field": "completed_today"}},
    }

    response = await es.client.search(index=es.RIDERS_CURRENT, body=es_query)
    aggs = response.get("aggregations", {})

    return {
        "tool": "get_ops_metrics",
        "metric_type": "riders",
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
        "total_riders": _total_hits(response),
        "by_status": {
            b["key"]: b["doc_count"]
            for b in aggs.get("by_status", {}).get("buckets", [])
        },
        "by_availability": {
            b["key"]: b["doc_count"]
            for b in aggs.get("by_availability", {}).get("buckets", [])
        },
        "avg_active_shipments": aggs.get("avg_active_shipments", {}).get("value"),
        "avg_completed_today": aggs.get("avg_completed_today", {}).get("value"),
    }


async def _sla_metrics(es, tenant_id, start_date, end_date, interval, bucket):
    """SLA compliance percentage and breach counts."""
    now_iso = datetime.utcnow().isoformat() + "Z"

    # Total shipments in range
    total_query = {
        "query": {
            "bool": {
                "filter": [
                    {"range": {"created_at": {"gte": start_date, "lte": end_date}}},
                ]
            }
        }
    }
    total_query = inject_tenant_filter(total_query, tenant_id)
    total_query["size"] = 0

    total_resp = await es.client.search(index=es.SHIPMENTS_CURRENT, body=total_query)
    total_count = _total_hits(total_resp)

    # Breached shipments (estimated_delivery < now AND status not delivered)
    breach_query = {
        "query": {
            "bool": {
                "filter": [
                    {"range": {"created_at": {"gte": start_date, "lte": end_date}}},
                    {"range": {"estimated_delivery": {"lt": now_iso}}},
                ],
                "must_not": [{"term": {"status": "delivered"}}],
            }
        }
    }
    breach_query = inject_tenant_filter(breach_query, tenant_id)
    breach_query["size"] = 0

    breach_resp = await es.client.search(index=es.SHIPMENTS_CURRENT, body=breach_query)
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
        "total_shipments": total_count,
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
        }
    }
    es_query = inject_tenant_filter(es_query, tenant_id)
    es_query["size"] = 0
    es_query["aggs"] = {
        "by_reason": {"terms": {"field": "failure_reason", "size": 50}},
        "over_time": {
            "date_histogram": {
                "field": "updated_at",
                "fixed_interval": interval,
                "min_doc_count": 0,
                "extended_bounds": {"min": start_date, "max": end_date},
            },
        },
    }

    response = await es.client.search(index=es.SHIPMENTS_CURRENT, body=es_query)
    aggs = response.get("aggregations", {})

    by_reason = {
        b["key"]: b["doc_count"]
        for b in aggs.get("by_reason", {}).get("buckets", [])
    }
    time_buckets = [
        {"timestamp": tb.get("key_as_string"), "count": tb["doc_count"]}
        for tb in aggs.get("over_time", {}).get("buckets", [])
    ]

    return {
        "tool": "get_ops_metrics",
        "metric_type": "failures",
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
        "total_failures": _total_hits(response),
        "by_reason": by_reason,
        "time_series": time_buckets,
    }
