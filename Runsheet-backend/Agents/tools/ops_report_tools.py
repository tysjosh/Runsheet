"""
Ops report tools for the AI agent.

Produces tenant-scoped markdown reports for order SLA violations, order failure
root causes, and driver productivity from the fuel-order Elasticsearch indices.
"""

import json
import logging
import time
from datetime import datetime
from typing import Optional

from strands import tool

from ops.middleware.pii_masker import PIIMasker
from ops.middleware.tenant_guard import inject_tenant_filter

from .ops_feature_guard import check_ops_feature_flag

logger = logging.getLogger(__name__)
ES_SEARCH_TIMEOUT_SECONDS = 10
ORDER_INDEX = "fuel_orders_current"
DRIVER_INDEX = "drivers_current"

_ops_es_service = None
_pii_masker = PIIMasker()


def configure_ops_report_tools(ops_es_service) -> None:
    """Wire the OpsElasticsearchService into this module at startup."""
    global _ops_es_service
    _ops_es_service = ops_es_service
    logger.info("Ops report tools configured with OpsElasticsearchService")


def _get_es():
    """Return the configured OpsElasticsearchService or raise."""
    if _ops_es_service is None:
        raise RuntimeError(
            "Ops report tools not configured. Call configure_ops_report_tools() during startup."
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


def _format_hits(response: dict) -> list[dict]:
    """Extract _source documents from an ES search response."""
    return [hit["_source"] for hit in response.get("hits", {}).get("hits", [])]


def _total_hits(response: dict) -> int:
    """Extract total hit count from an ES search response."""
    total = response.get("hits", {}).get("total", 0)
    if isinstance(total, dict):
        return total.get("value", 0)
    return total


def _report_header(report_name: str, start_date: str, end_date: str, tenant_id: str) -> str:
    """Build a standard markdown report header."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"# {report_name}\n\n"
        f"| Field | Value |\n"
        f"|---|---|\n"
        f"| Generated | {now} |\n"
        f"| Time Range | {start_date} - {end_date} |\n"
        f"| Tenant | {tenant_id} |\n\n"
    )


@tool
async def generate_sla_report(
    start_date: str,
    end_date: str,
    tenant_id: str,
) -> str:
    """
    Generate an SLA violations report for fuel orders in a time range.

    Lists orders whose delivery window has passed and are not yet delivered or
    cancelled. Results are scoped to the requesting tenant.
    """
    start_time = time.time()
    params = {"start_date": start_date, "end_date": end_date}
    _log_tool_call("generate_sla_report", params, tenant_id)

    disabled = await check_ops_feature_flag(tenant_id)
    if disabled:
        return disabled

    try:
        es = _get_es()
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
            "size": 100,
            "sort": [{"delivery_window_end": {"order": "asc"}}],
        }
        breach_query = inject_tenant_filter(breach_query, tenant_id)
        breach_resp = await _search(es, ORDER_INDEX, breach_query)
        breached_orders = [
            _pii_masker.mask_response(order, has_pii_access=False)
            for order in _format_hits(breach_resp)
        ]
        breach_count = _total_hits(breach_resp)

        compliance_pct = (
            round(((total_count - breach_count) / total_count) * 100, 2)
            if total_count > 0
            else 100.0
        )

        md = _report_header("SLA Violations Report", start_date, end_date, tenant_id)
        md += "## Summary\n\n"
        md += "| Metric | Value |\n"
        md += "|---|---|\n"
        md += f"| Total Orders | {total_count} |\n"
        md += f"| SLA Breaches | {breach_count} |\n"
        md += f"| Compliance Rate | {compliance_pct}% |\n\n"

        if breached_orders:
            md += "## Breached Orders\n\n"
            md += (
                "| Order ID | Status | Driver | Delivery Window End | "
                "Breach Duration | Customer | Destination |\n"
            )
            md += "|---|---|---|---|---|---|---|\n"
            now_dt = datetime.utcnow()
            for order in breached_orders:
                window_end = order.get("delivery_window_end", "")
                breach_dur = "N/A"
                if window_end:
                    try:
                        end_dt = datetime.fromisoformat(str(window_end).replace("Z", "+00:00"))
                        delta = now_dt.replace(tzinfo=end_dt.tzinfo) - end_dt
                        hours = int(delta.total_seconds() // 3600)
                        minutes = int((delta.total_seconds() % 3600) // 60)
                        breach_dur = f"{hours}h {minutes}m"
                    except (ValueError, TypeError):
                        logger.warning(
                            "Invalid delivery_window_end in SLA report: %s",
                            window_end,
                            exc_info=True,
                        )

                md += (
                    f"| {order.get('order_id', 'N/A')} "
                    f"| {order.get('status', 'N/A')} "
                    f"| {order.get('assigned_driver_id', 'N/A')} "
                    f"| {window_end} "
                    f"| {breach_dur} "
                    f"| {order.get('customer_id', 'N/A')} "
                    f"| {order.get('delivery_address', order.get('destination', 'N/A'))} |\n"
                )
        else:
            md += "*No SLA breaches found in the specified time range.*\n"

        duration_ms = (time.time() - start_time) * 1000
        logger.info("generate_sla_report completed in %.1fms, %d breaches", duration_ms, breach_count)
        return md
    except Exception as e:
        logger.exception("generate_sla_report failed")
        return json.dumps({"tool": "generate_sla_report", "error": str(e)})


@tool
async def generate_failure_report(
    start_date: str,
    end_date: str,
    tenant_id: str,
    intake_channel: Optional[str] = None,
) -> str:
    """
    Generate an order failure analysis report for a specified time range.

    Groups failed fuel orders by root cause and includes sample affected orders.
    """
    start_time = time.time()
    params = {"start_date": start_date, "end_date": end_date, "intake_channel": intake_channel}
    _log_tool_call("generate_failure_report", params, tenant_id)

    disabled = await check_ops_feature_flag(tenant_id)
    if disabled:
        return disabled

    try:
        es = _get_es()
        filter_clauses = [
            {"term": {"status": "failed"}},
            {"range": {"updated_at": {"gte": start_date, "lte": end_date}}},
        ]
        if intake_channel:
            filter_clauses.append({"term": {"intake_channel": intake_channel}})

        es_query = {
            "query": {"bool": {"filter": filter_clauses}},
            "size": 0,
            "aggs": {
                "by_reason": {
                    "terms": {"field": "failure_reason", "size": 50},
                    "aggs": {
                        "sample_orders": {
                            "top_hits": {
                                "size": 5,
                                "_source": [
                                    "order_id",
                                    "assigned_driver_id",
                                    "updated_at",
                                    "customer_id",
                                    "delivery_address",
                                    "destination",
                                ],
                                "sort": [{"updated_at": {"order": "desc"}}],
                            }
                        }
                    },
                },
                "over_time": {
                    "date_histogram": {
                        "field": "updated_at",
                        "fixed_interval": "1d",
                        "min_doc_count": 0,
                        "extended_bounds": {"min": start_date, "max": end_date},
                    },
                },
            },
        }
        es_query = inject_tenant_filter(es_query, tenant_id)
        response = await _search(es, ORDER_INDEX, es_query)
        aggs = response.get("aggregations", {})
        total_failures = _total_hits(response)

        reason_buckets = aggs.get("by_reason", {}).get("buckets", [])
        time_buckets = aggs.get("over_time", {}).get("buckets", [])

        md = _report_header("Failure Analysis Report", start_date, end_date, tenant_id)
        if intake_channel:
            md += f"**Intake Channel Filter:** {intake_channel}\n\n"

        md += "## Summary\n\n"
        md += "| Metric | Value |\n"
        md += "|---|---|\n"
        md += f"| Total Failures | {total_failures} |\n"
        md += f"| Distinct Root Causes | {len(reason_buckets)} |\n\n"

        if reason_buckets:
            md += "## Failures by Root Cause\n\n"
            md += "| Root Cause | Count | % of Total |\n"
            md += "|---|---|---|\n"
            for bucket in reason_buckets:
                reason = bucket["key"]
                count = bucket["doc_count"]
                pct = round((count / total_failures) * 100, 1) if total_failures else 0
                md += f"| {reason} | {count} | {pct}% |\n"

            md += "\n## Affected Orders by Root Cause\n\n"
            for bucket in reason_buckets:
                reason = bucket["key"]
                samples = [
                    _pii_masker.mask_response(order, has_pii_access=False)
                    for order in _format_hits(bucket.get("sample_orders", {}))
                ]
                md += f"### {reason} ({bucket['doc_count']} failures)\n\n"
                if samples:
                    md += "| Order ID | Driver | Last Updated | Customer | Destination |\n"
                    md += "|---|---|---|---|---|\n"
                    for order in samples:
                        md += (
                            f"| {order.get('order_id', 'N/A')} "
                            f"| {order.get('assigned_driver_id', 'N/A')} "
                            f"| {order.get('updated_at', 'N/A')} "
                            f"| {order.get('customer_id', 'N/A')} "
                            f"| {order.get('delivery_address', order.get('destination', 'N/A'))} |\n"
                        )
                md += "\n"
        else:
            md += "*No failures found in the specified time range.*\n\n"

        if time_buckets:
            md += "## Daily Failure Trend\n\n"
            md += "| Date | Failures |\n"
            md += "|---|---|\n"
            for bucket in time_buckets:
                md += f"| {bucket.get('key_as_string', 'N/A')} | {bucket['doc_count']} |\n"

        duration_ms = (time.time() - start_time) * 1000
        logger.info(
            "generate_failure_report completed in %.1fms, %d failures",
            duration_ms,
            total_failures,
        )
        return md
    except Exception as e:
        logger.exception("generate_failure_report failed")
        return json.dumps({"tool": "generate_failure_report", "error": str(e)})


@tool
async def generate_driver_productivity_report(
    start_date: str,
    end_date: str,
    tenant_id: str,
) -> str:
    """
    Generate a driver productivity report for a specified time range.

    Summarizes delivered and failed fuel orders, average delivery time, active
    order count, and current driver status.
    """
    start_time = time.time()
    params = {"start_date": start_date, "end_date": end_date}
    _log_tool_call("generate_driver_productivity_report", params, tenant_id)

    disabled = await check_ops_feature_flag(tenant_id)
    if disabled:
        return disabled

    try:
        es = _get_es()

        delivered_by_driver = await _orders_by_driver(
            es,
            tenant_id,
            start_date,
            end_date,
            status="delivered",
        )
        failed_by_driver = await _orders_by_driver(
            es,
            tenant_id,
            start_date,
            end_date,
            status="failed",
        )
        avg_time_by_driver = await _avg_delivery_time_by_driver(
            es,
            tenant_id,
            start_date,
            end_date,
        )

        drivers_query = {"query": {"match_all": {}}, "size": 500}
        drivers_query = inject_tenant_filter(drivers_query, tenant_id)
        drivers_resp = await _search(es, DRIVER_INDEX, drivers_query)
        drivers = [
            _pii_masker.mask_response(driver, has_pii_access=False)
            for driver in _format_hits(drivers_resp)
        ]

        all_driver_ids = set(delivered_by_driver.keys()) | set(failed_by_driver.keys())
        driver_info = {driver.get("driver_id"): driver for driver in drivers}

        md = _report_header("Driver Productivity Report", start_date, end_date, tenant_id)
        total_delivered = sum(delivered_by_driver.values())
        total_failed = sum(failed_by_driver.values())
        total_assignments = total_delivered + total_failed

        md += "## Summary\n\n"
        md += "| Metric | Value |\n"
        md += "|---|---|\n"
        md += f"| Total Drivers | {len(all_driver_ids)} |\n"
        md += f"| Total Deliveries | {total_delivered} |\n"
        md += f"| Total Failures | {total_failed} |\n"
        failure_rate = round((total_failed / total_assignments) * 100, 1) if total_assignments else 0
        md += f"| Overall Failure Rate | {failure_rate}% |\n\n"

        if all_driver_ids:
            md += "## Per-Driver Metrics\n\n"
            md += (
                "| Driver ID | Deliveries | Failures | Failure Rate | "
                "Avg Delivery Time (hrs) | Active Orders | Status |\n"
            )
            md += "|---|---|---|---|---|---|---|\n"
            for driver_id in sorted(
                all_driver_ids,
                key=lambda did: delivered_by_driver.get(did, 0),
                reverse=True,
            ):
                deliveries = delivered_by_driver.get(driver_id, 0)
                failures = failed_by_driver.get(driver_id, 0)
                total = deliveries + failures
                fail_rate = round((failures / total) * 100, 1) if total else 0
                info = driver_info.get(driver_id, {})
                md += (
                    f"| {driver_id} "
                    f"| {deliveries} "
                    f"| {failures} "
                    f"| {fail_rate}% "
                    f"| {avg_time_by_driver.get(driver_id, 'N/A')} "
                    f"| {info.get('active_order_count', 'N/A')} "
                    f"| {info.get('status', 'N/A')} |\n"
                )
        else:
            md += "*No driver activity found in the specified time range.*\n"

        duration_ms = (time.time() - start_time) * 1000
        logger.info(
            "generate_driver_productivity_report completed in %.1fms, %d drivers",
            duration_ms,
            len(all_driver_ids),
        )
        return md
    except Exception as e:
        logger.exception("generate_driver_productivity_report failed")
        return json.dumps({"tool": "generate_driver_productivity_report", "error": str(e)})


async def _orders_by_driver(es, tenant_id, start_date, end_date, status: str) -> dict[str, int]:
    """Return order counts grouped by assigned driver for a status."""
    query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"status": status}},
                    {"range": {"updated_at": {"gte": start_date, "lte": end_date}}},
                ]
            }
        },
        "size": 0,
        "aggs": {
            "by_driver": {"terms": {"field": "assigned_driver_id", "size": 500}},
        },
    }
    query = inject_tenant_filter(query, tenant_id)
    response = await _search(es, ORDER_INDEX, query)
    aggs = response.get("aggregations", {})
    return {
        bucket["key"]: bucket["doc_count"]
        for bucket in aggs.get("by_driver", {}).get("buckets", [])
    }


async def _avg_delivery_time_by_driver(es, tenant_id, start_date, end_date) -> dict[str, float]:
    """Return average delivery duration in hours grouped by assigned driver."""
    query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"status": "delivered"}},
                    {"range": {"updated_at": {"gte": start_date, "lte": end_date}}},
                    {"exists": {"field": "created_at"}},
                ]
            }
        },
        "size": 0,
        "aggs": {
            "by_driver": {
                "terms": {"field": "assigned_driver_id", "size": 500},
                "aggs": {
                    "avg_delivery_time": {
                        "avg": {
                            "script": {
                                "source": (
                                    "if (doc['updated_at'].size() > 0 && doc['created_at'].size() > 0) {"
                                    "  return (doc['updated_at'].value.toInstant().toEpochMilli() "
                                    "    - doc['created_at'].value.toInstant().toEpochMilli()) / 3600000.0;"
                                    "} return 0;"
                                ),
                                "lang": "painless",
                            }
                        }
                    }
                },
            }
        },
    }
    query = inject_tenant_filter(query, tenant_id)
    response = await _search(es, ORDER_INDEX, query)
    aggs = response.get("aggregations", {})
    out: dict[str, float] = {}
    for bucket in aggs.get("by_driver", {}).get("buckets", []):
        val = bucket.get("avg_delivery_time", {}).get("value")
        if val is not None:
            out[bucket["key"]] = round(val, 1)
    return out
