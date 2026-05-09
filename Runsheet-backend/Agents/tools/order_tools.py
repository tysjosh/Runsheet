"""
Order-domain AI tools for the fuel order intake pipeline.

Provides tenant-scoped read-only query tools for fuel orders, drivers, order
events, and order metrics from the new fuel-order Elasticsearch indices.

Every tool calls ``get_current_tenant()`` at entry and wraps its ES query
through ``inject_tenant_filter`` so cross-tenant data never leaks.

Validates:
- Requirement 6.1.1: search_orders, search_drivers, get_order_events, get_orders_metrics
- Requirement 6.1.3: Tenant scoping via current_tenant_id_var ContextVar pattern
- Design §9 — AI tools
"""

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from strands import tool

from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import elasticsearch_service

from ._tenant_context import get_current_tenant
from .logging_wrapper import get_telemetry_service

logger = logging.getLogger(__name__)


def _log_tool_invocation(
    tool_name: str,
    input_params: dict,
    start_time: float,
    success: bool,
    error: str = None,
):
    """Helper to log tool invocations with telemetry service."""
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
        telemetry.record_metric(
            name="tool_invocation_duration_ms",
            value=duration_ms,
            tags={"tool_name": tool_name, "success": str(success).lower()},
        )
        telemetry.record_metric(
            name="tool_invocation_count",
            value=1,
            tags={"tool_name": tool_name, "success": str(success).lower()},
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
async def search_orders(
    status: Optional[str] = None,
    customer_id: Optional[str] = None,
    driver_id: Optional[str] = None,
    call_type: Optional[str] = None,
    product_code: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    intake_channel: Optional[str] = None,
    page: int = 1,
    size: int = 50,
) -> str:
    """
    Search fuel orders in the order intake pipeline.

    Tenant-scoped order search. Returns orders visible to the currently-bound
    tenant only — raises RuntimeError if no tenant is bound.

    Args:
        status: Filter by order status (placed, confirmed, scheduled, dispatched,
                in_transit, delivered, failed, cancelled, on_hold).
        customer_id: Filter by customer ID.
        driver_id: Filter by assigned driver ID.
        call_type: Filter by call type (will_call, auto_fill, keep_full, one_off).
        product_code: Filter by product code (DIESEL_2, GASOLINE_REG, etc.).
        start_date: Filter orders created on or after this ISO-8601 date.
        end_date: Filter orders created on or before this ISO-8601 date.
        intake_channel: Filter by intake channel (voice, web_portal, dispatcher,
                        csv, edi, api_partner, legacy).
        page: Page number (default 1).
        size: Results per page (default 50).

    Returns:
        JSON string with order results for the AI agent to interpret.
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    params = {
        "status": status,
        "customer_id": customer_id,
        "driver_id": driver_id,
        "call_type": call_type,
        "product_code": product_code,
        "start_date": start_date,
        "end_date": end_date,
        "intake_channel": intake_channel,
        "page": page,
        "size": size,
    }

    try:
        logger.info(
            "AI tool invocation: tool=search_orders tenant_id=%s params=%s",
            tenant_id,
            json.dumps(params, default=str),
        )

        filter_clauses: list[dict] = []

        if status:
            filter_clauses.append({"term": {"status": status}})
        if customer_id:
            filter_clauses.append({"term": {"customer_id": customer_id}})
        if driver_id:
            filter_clauses.append({"term": {"assigned_driver_id": driver_id}})
        if call_type:
            filter_clauses.append({"term": {"call_type": call_type}})
        if product_code:
            filter_clauses.append({"term": {"product_code": product_code}})
        if intake_channel:
            filter_clauses.append({"term": {"intake_channel": intake_channel}})

        if start_date or end_date:
            range_clause: dict = {}
            if start_date:
                range_clause["gte"] = start_date
            if end_date:
                range_clause["lte"] = end_date
            filter_clauses.append({"range": {"created_at": range_clause}})

        if filter_clauses:
            es_query: dict = {"query": {"bool": {"filter": filter_clauses}}}
        else:
            es_query = {"query": {"match_all": {}}}

        # Inject tenant scoping
        es_query = inject_tenant_filter(es_query, tenant_id)

        # Pagination
        from_offset = (page - 1) * size
        es_query["from"] = from_offset
        es_query["size"] = size
        es_query["sort"] = [{"updated_at": {"order": "desc"}}]

        response = await elasticsearch_service.search_documents(
            "fuel_orders_current", es_query
        )

        hits = _format_hits(response)
        total = _total_hits(response)

        result = {
            "tool": "search_orders",
            "total": total,
            "page": page,
            "size": size,
            "orders": hits,
        }

        success = True
        duration_ms = (time.time() - start_time) * 1000
        logger.info(
            "search_orders completed in %.1fms, %d results", duration_ms, total
        )
        return json.dumps(result, default=str)

    except Exception as e:
        error_msg = str(e)
        logger.error("search_orders failed: %s", e)
        return json.dumps({"tool": "search_orders", "error": str(e)})
    finally:
        _log_tool_invocation("search_orders", params, start_time, success, error_msg)


@tool
async def search_drivers(
    status: Optional[str] = None,
    availability: Optional[str] = None,
    min_active_orders: Optional[int] = None,
    max_active_orders: Optional[int] = None,
    hazmat_endorsement: Optional[bool] = None,
    page: int = 1,
    size: int = 20,
) -> str:
    """
    Search drivers in the fuel operations platform.

    Tenant-scoped driver search. Returns drivers visible to the currently-bound
    tenant only — raises RuntimeError if no tenant is bound.

    Args:
        status: Filter by driver status (active, inactive, on_break, off_duty).
        availability: Filter by availability.
        min_active_orders: Minimum active order count filter.
        max_active_orders: Maximum active order count filter.
        hazmat_endorsement: Filter by HAZMAT endorsement (true/false).
        page: Page number (default 1).
        size: Results per page (default 20).

    Returns:
        JSON string with driver results for the AI agent to interpret.
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    params = {
        "status": status,
        "availability": availability,
        "min_active_orders": min_active_orders,
        "max_active_orders": max_active_orders,
        "hazmat_endorsement": hazmat_endorsement,
        "page": page,
        "size": size,
    }

    try:
        logger.info(
            "AI tool invocation: tool=search_drivers tenant_id=%s params=%s",
            tenant_id,
            json.dumps(params, default=str),
        )

        filter_clauses: list[dict] = []

        if status:
            filter_clauses.append({"term": {"status": status}})
        if availability:
            filter_clauses.append({"term": {"availability": availability}})
        if hazmat_endorsement is not None:
            filter_clauses.append({"term": {"hazmat_endorsement": hazmat_endorsement}})

        if min_active_orders is not None or max_active_orders is not None:
            range_filter: dict = {}
            if min_active_orders is not None:
                range_filter["gte"] = min_active_orders
            if max_active_orders is not None:
                range_filter["lte"] = max_active_orders
            filter_clauses.append({"range": {"active_order_count": range_filter}})

        if filter_clauses:
            es_query: dict = {"query": {"bool": {"filter": filter_clauses}}}
        else:
            es_query = {"query": {"match_all": {}}}

        # Inject tenant scoping
        es_query = inject_tenant_filter(es_query, tenant_id)

        # Pagination
        from_offset = (page - 1) * size
        es_query["from"] = from_offset
        es_query["size"] = size
        es_query["sort"] = [{"last_seen": {"order": "desc"}}]

        response = await elasticsearch_service.search_documents(
            "drivers_current", es_query
        )

        hits = _format_hits(response)
        total = _total_hits(response)

        result = {
            "tool": "search_drivers",
            "total": total,
            "page": page,
            "size": size,
            "drivers": hits,
        }

        success = True
        duration_ms = (time.time() - start_time) * 1000
        logger.info(
            "search_drivers completed in %.1fms, %d results", duration_ms, total
        )
        return json.dumps(result, default=str)

    except Exception as e:
        error_msg = str(e)
        logger.error("search_drivers failed: %s", e)
        return json.dumps({"tool": "search_drivers", "error": str(e)})
    finally:
        _log_tool_invocation("search_drivers", params, start_time, success, error_msg)


@tool
async def get_order_events(
    order_id: str,
    page: int = 1,
    size: int = 50,
) -> str:
    """
    Get the event timeline for a specific fuel order.

    Retrieves the full event history of an order, ordered chronologically.
    Results are scoped to the currently-bound tenant — raises RuntimeError
    if no tenant is bound.

    Args:
        order_id: The order ID to look up events for.
        page: Page number (default 1).
        size: Results per page (default 50).

    Returns:
        JSON string with the order event timeline for the AI agent.
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    params = {"order_id": order_id, "page": page, "size": size}

    try:
        logger.info(
            "AI tool invocation: tool=get_order_events tenant_id=%s params=%s",
            tenant_id,
            json.dumps(params, default=str),
        )

        es_query: dict = {
            "query": {
                "bool": {
                    "filter": [{"term": {"order_id": order_id}}]
                }
            }
        }

        # Inject tenant scoping
        es_query = inject_tenant_filter(es_query, tenant_id)

        # Pagination
        from_offset = (page - 1) * size
        es_query["from"] = from_offset
        es_query["size"] = size
        es_query["sort"] = [{"event_timestamp": {"order": "asc"}}]

        response = await elasticsearch_service.search_documents(
            "fuel_order_events", es_query
        )

        hits = _format_hits(response)
        total = _total_hits(response)

        result = {
            "tool": "get_order_events",
            "order_id": order_id,
            "total": total,
            "page": page,
            "size": size,
            "events": hits,
        }

        success = True
        duration_ms = (time.time() - start_time) * 1000
        logger.info(
            "get_order_events completed in %.1fms, %d events for order %s",
            duration_ms,
            total,
            order_id,
        )
        return json.dumps(result, default=str)

    except Exception as e:
        error_msg = str(e)
        logger.error("get_order_events failed: %s", e)
        return json.dumps({"tool": "get_order_events", "error": str(e)})
    finally:
        _log_tool_invocation("get_order_events", params, start_time, success, error_msg)


@tool
async def get_orders_metrics(
    metric_type: str = "orders",
    bucket: str = "hourly",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    intake_channel: Optional[str] = None,
) -> str:
    """
    Get aggregated operational metrics for fuel orders.

    Retrieves summary statistics for orders, drivers, SLA compliance, or
    failure analysis over a specified time range. Results are scoped to the
    currently-bound tenant — raises RuntimeError if no tenant is bound.

    Args:
        metric_type: Type of metrics to retrieve. One of: orders, drivers, sla, failures.
        bucket: Time bucket granularity: "hourly" or "daily" (default "hourly").
        start_date: Start of time range (ISO-8601). Defaults to 24 hours ago.
        end_date: End of time range (ISO-8601). Defaults to now.
        intake_channel: Optional filter by intake channel for channel-specific metrics.

    Returns:
        JSON string with aggregated metrics for the AI agent to interpret.
    """
    start_time = time.time()
    success = False
    error_msg = None
    tenant_id = get_current_tenant()

    params = {
        "metric_type": metric_type,
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
        "intake_channel": intake_channel,
    }

    try:
        logger.info(
            "AI tool invocation: tool=get_orders_metrics tenant_id=%s params=%s",
            tenant_id,
            json.dumps(params, default=str),
        )

        # Default time range: last 24 hours
        now = datetime.utcnow()
        if not end_date:
            end_date = now.isoformat() + "Z"
        if not start_date:
            start_date = (now - timedelta(hours=24)).isoformat() + "Z"

        # Enforce daily granularity for ranges > 90 days
        try:
            sd = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            ed = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if (ed - sd).days > 90:
                bucket = "daily"
        except (ValueError, TypeError):
            pass

        interval = "1h" if bucket == "hourly" else "1d"

        if metric_type == "orders":
            result = await _order_metrics(
                tenant_id, start_date, end_date, interval, bucket, intake_channel
            )
        elif metric_type == "drivers":
            result = await _driver_metrics(tenant_id, start_date, end_date, interval, bucket)
        elif metric_type == "sla":
            result = await _sla_metrics(tenant_id, start_date, end_date, interval, bucket)
        elif metric_type == "failures":
            result = await _failure_metrics(
                tenant_id, start_date, end_date, interval, bucket, intake_channel
            )
        else:
            result = {
                "tool": "get_orders_metrics",
                "error": f"Unknown metric_type '{metric_type}'. Use: orders, drivers, sla, failures.",
            }

        success = True
        duration_ms = (time.time() - start_time) * 1000
        logger.info(
            "get_orders_metrics(%s) completed in %.1fms", metric_type, duration_ms
        )
        return json.dumps(result, default=str)

    except Exception as e:
        error_msg = str(e)
        logger.error("get_orders_metrics failed: %s", e)
        return json.dumps({"tool": "get_orders_metrics", "error": str(e)})
    finally:
        _log_tool_invocation(
            "get_orders_metrics", params, start_time, success, error_msg
        )


# ---------------------------------------------------------------------------
# Internal metric helpers
# ---------------------------------------------------------------------------


async def _order_metrics(
    tenant_id: str,
    start_date: str,
    end_date: str,
    interval: str,
    bucket: str,
    intake_channel: Optional[str] = None,
) -> dict:
    """Order counts aggregated by status in time buckets."""
    filter_clauses: list[dict] = [
        {"range": {"updated_at": {"gte": start_date, "lte": end_date}}},
    ]
    if intake_channel:
        filter_clauses.append({"term": {"intake_channel": intake_channel}})

    es_query: dict = {
        "query": {"bool": {"filter": filter_clauses}}
    }
    es_query = inject_tenant_filter(es_query, tenant_id)
    es_query["size"] = 0
    es_query["aggs"] = {
        "by_status": {
            "terms": {"field": "status", "size": 20},
        },
        "by_call_type": {
            "terms": {"field": "call_type", "size": 10},
        },
        "by_intake_channel": {
            "terms": {"field": "intake_channel", "size": 20},
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

    response = await elasticsearch_service.search_documents(
        "fuel_orders_current", es_query
    )
    aggs = response.get("aggregations", {})

    status_counts = {
        b["key"]: b["doc_count"]
        for b in aggs.get("by_status", {}).get("buckets", [])
    }
    call_type_counts = {
        b["key"]: b["doc_count"]
        for b in aggs.get("by_call_type", {}).get("buckets", [])
    }
    channel_counts = {
        b["key"]: b["doc_count"]
        for b in aggs.get("by_intake_channel", {}).get("buckets", [])
    }
    time_buckets = []
    for tb in aggs.get("over_time", {}).get("buckets", []):
        breakdown = {
            sb["key"]: sb["doc_count"]
            for sb in tb.get("by_status", {}).get("buckets", [])
        }
        time_buckets.append(
            {
                "timestamp": tb.get("key_as_string"),
                "count": tb["doc_count"],
                "breakdown": breakdown,
            }
        )

    return {
        "tool": "get_orders_metrics",
        "metric_type": "orders",
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
        "summary": status_counts,
        "by_call_type": call_type_counts,
        "by_intake_channel": channel_counts,
        "total": _total_hits(response),
        "time_series": time_buckets,
    }


async def _driver_metrics(
    tenant_id: str,
    start_date: str,
    end_date: str,
    interval: str,
    bucket: str,
) -> dict:
    """Driver utilization and availability metrics."""
    es_query: dict = {"query": {"match_all": {}}}
    es_query = inject_tenant_filter(es_query, tenant_id)
    es_query["size"] = 0
    es_query["aggs"] = {
        "by_status": {"terms": {"field": "status", "size": 20}},
        "by_availability": {"terms": {"field": "availability", "size": 20}},
        "avg_active_orders": {"avg": {"field": "active_order_count"}},
        "avg_completed_today": {"avg": {"field": "completed_today"}},
        "hazmat_count": {
            "filter": {"term": {"hazmat_endorsement": True}},
        },
    }

    response = await elasticsearch_service.search_documents(
        "drivers_current", es_query
    )
    aggs = response.get("aggregations", {})

    return {
        "tool": "get_orders_metrics",
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


async def _sla_metrics(
    tenant_id: str,
    start_date: str,
    end_date: str,
    interval: str,
    bucket: str,
) -> dict:
    """SLA compliance percentage and breach counts for fuel orders."""
    now_iso = datetime.utcnow().isoformat() + "Z"

    # Total orders in range
    total_query: dict = {
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

    total_resp = await elasticsearch_service.search_documents(
        "fuel_orders_current", total_query
    )
    total_count = _total_hits(total_resp)

    # Breached orders (delivery_window_end < now AND status not delivered/cancelled)
    breach_query: dict = {
        "query": {
            "bool": {
                "filter": [
                    {"range": {"created_at": {"gte": start_date, "lte": end_date}}},
                    {"range": {"delivery_window_end": {"lt": now_iso}}},
                ],
                "must_not": [
                    {"terms": {"status": ["delivered", "cancelled"]}},
                ],
            }
        }
    }
    breach_query = inject_tenant_filter(breach_query, tenant_id)
    breach_query["size"] = 0

    breach_resp = await elasticsearch_service.search_documents(
        "fuel_orders_current", breach_query
    )
    breach_count = _total_hits(breach_resp)

    compliance_pct = (
        round(((total_count - breach_count) / total_count) * 100, 2)
        if total_count > 0
        else 100.0
    )

    return {
        "tool": "get_orders_metrics",
        "metric_type": "sla",
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
        "total_orders": total_count,
        "sla_breaches": breach_count,
        "compliance_percentage": compliance_pct,
    }


async def _failure_metrics(
    tenant_id: str,
    start_date: str,
    end_date: str,
    interval: str,
    bucket: str,
    intake_channel: Optional[str] = None,
) -> dict:
    """Failure counts grouped by intake channel and over time."""
    filter_clauses: list[dict] = [
        {"term": {"status": "failed"}},
        {"range": {"updated_at": {"gte": start_date, "lte": end_date}}},
    ]
    if intake_channel:
        filter_clauses.append({"term": {"intake_channel": intake_channel}})

    es_query: dict = {
        "query": {"bool": {"filter": filter_clauses}}
    }
    es_query = inject_tenant_filter(es_query, tenant_id)
    es_query["size"] = 0
    es_query["aggs"] = {
        "by_intake_channel": {
            "terms": {"field": "intake_channel", "size": 20},
        },
        "by_call_type": {
            "terms": {"field": "call_type", "size": 10},
        },
        "over_time": {
            "date_histogram": {
                "field": "updated_at",
                "fixed_interval": interval,
                "min_doc_count": 0,
                "extended_bounds": {"min": start_date, "max": end_date},
            },
        },
    }

    response = await elasticsearch_service.search_documents(
        "fuel_orders_current", es_query
    )
    aggs = response.get("aggregations", {})

    by_channel = {
        b["key"]: b["doc_count"]
        for b in aggs.get("by_intake_channel", {}).get("buckets", [])
    }
    by_call_type = {
        b["key"]: b["doc_count"]
        for b in aggs.get("by_call_type", {}).get("buckets", [])
    }
    time_buckets = [
        {"timestamp": tb.get("key_as_string"), "count": tb["doc_count"]}
        for tb in aggs.get("over_time", {}).get("buckets", [])
    ]

    return {
        "tool": "get_orders_metrics",
        "metric_type": "failures",
        "bucket": bucket,
        "start_date": start_date,
        "end_date": end_date,
        "total_failures": _total_hits(response),
        "by_intake_channel": by_channel,
        "by_call_type": by_call_type,
        "time_series": time_buckets,
    }
