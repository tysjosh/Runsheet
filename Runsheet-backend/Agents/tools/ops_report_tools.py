"""
Ops report tools for the AI agent.

Provides read-only report generation tools that produce structured markdown
reports for SLA violations, failure root causes, and rider productivity.
All tools enforce tenant scoping via TenantGuard, apply PII masking, and
check feature flags before executing.

Validates:
- Requirements 18.1-18.5: AI report templates (SLA, failure, rider productivity)
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

# Module-level service references, wired at startup via configure_ops_report_tools()
_ops_es_service = None
_pii_masker = PIIMasker()


def configure_ops_report_tools(ops_es_service) -> None:
    """
    Wire the OpsElasticsearchService into this module.

    Called once during application startup (lifespan) so that the report
    tool functions can query the ops Elasticsearch indices.
    """
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


def _report_header(report_name: str, start_date: str, end_date: str, tenant_id: str) -> str:
    """
    Build a standard markdown report header with timestamp, time range, and tenant scope.

    Validates: Requirement 18.4
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"# {report_name}\n\n"
        f"| Field | Value |\n"
        f"|---|---|\n"
        f"| Generated | {now} |\n"
        f"| Time Range | {start_date} — {end_date} |\n"
        f"| Tenant | {tenant_id} |\n\n"
    )


# ---------------------------------------------------------------------------
# SLA Report
# ---------------------------------------------------------------------------

@tool
async def generate_sla_report(
    start_date: str,
    end_date: str,
    tenant_id: str,
) -> str:
    """
    Generate an SLA violations report for a specified time range.

    Produces a structured markdown report listing shipments that breached
    their estimated delivery time, including breach duration and summary
    statistics. Results are scoped to the requesting tenant.

    Args:
        start_date: Start of time range (ISO-8601).
        end_date: End of time range (ISO-8601).
        tenant_id: The tenant ID (from authenticated context).

    Returns:
        Structured markdown report string for the AI agent to present.

    Validates: Requirements 18.1, 18.4, 18.5, 19.1, 19.2, 19.5
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

        total_resp = es.client.search(index=es.SHIPMENTS_CURRENT, body=total_query)
        total_count = _total_hits(total_resp)

        # Breached shipments: estimated_delivery < now AND status != delivered
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
        breach_query["size"] = 100
        breach_query["sort"] = [{"estimated_delivery": {"order": "asc"}}]

        breach_resp = es.client.search(index=es.SHIPMENTS_CURRENT, body=breach_query)
        breached_shipments = _format_hits(breach_resp)
        breach_count = _total_hits(breach_resp)

        # PII mask all results (Req 22.2)
        breached_shipments = [
            _pii_masker.mask_response(s, has_pii_access=False) for s in breached_shipments
        ]

        compliance_pct = (
            round(((total_count - breach_count) / total_count) * 100, 2)
            if total_count > 0
            else 100.0
        )

        # Build markdown report
        md = _report_header("SLA Violations Report", start_date, end_date, tenant_id)

        md += "## Summary\n\n"
        md += f"| Metric | Value |\n"
        md += f"|---|---|\n"
        md += f"| Total Shipments | {total_count} |\n"
        md += f"| SLA Breaches | {breach_count} |\n"
        md += f"| Compliance Rate | {compliance_pct}% |\n\n"

        if breached_shipments:
            md += "## Breached Shipments\n\n"
            md += "| Shipment ID | Status | Rider | Estimated Delivery | Breach Duration | Origin | Destination |\n"
            md += "|---|---|---|---|---|---|---|\n"

            now_dt = datetime.utcnow()
            for s in breached_shipments:
                est_del = s.get("estimated_delivery", "")
                breach_dur = "N/A"
                if est_del:
                    try:
                        est_dt = datetime.fromisoformat(str(est_del).replace("Z", "+00:00"))
                        delta = now_dt.replace(tzinfo=est_dt.tzinfo) - est_dt
                        hours = int(delta.total_seconds() // 3600)
                        minutes = int((delta.total_seconds() % 3600) // 60)
                        breach_dur = f"{hours}h {minutes}m"
                    except (ValueError, TypeError):
                        pass

                md += (
                    f"| {s.get('shipment_id', 'N/A')} "
                    f"| {s.get('status', 'N/A')} "
                    f"| {s.get('rider_id', 'N/A')} "
                    f"| {est_del} "
                    f"| {breach_dur} "
                    f"| {s.get('origin', 'N/A')} "
                    f"| {s.get('destination', 'N/A')} |\n"
                )
        else:
            md += "*No SLA breaches found in the specified time range.*\n"

        duration_ms = (time.time() - start_time) * 1000
        logger.info("generate_sla_report completed in %.1fms, %d breaches", duration_ms, breach_count)
        return md

    except Exception as e:
        logger.error("generate_sla_report failed: %s", e)
        return json.dumps({"tool": "generate_sla_report", "error": str(e)})


# ---------------------------------------------------------------------------
# Failure Report
# ---------------------------------------------------------------------------

@tool
async def generate_failure_report(
    start_date: str,
    end_date: str,
    tenant_id: str,
) -> str:
    """
    Generate a failure analysis report for a specified time range.

    Produces a structured markdown report grouping failures by root cause
    with counts, affected shipments, and trend indicators. Results are
    scoped to the requesting tenant.

    Args:
        start_date: Start of time range (ISO-8601).
        end_date: End of time range (ISO-8601).
        tenant_id: The tenant ID (from authenticated context).

    Returns:
        Structured markdown report string for the AI agent to present.

    Validates: Requirements 18.2, 18.4, 18.5, 19.1, 19.2, 19.5
    """
    start_time = time.time()
    params = {"start_date": start_date, "end_date": end_date}
    _log_tool_call("generate_failure_report", params, tenant_id)

    disabled = await check_ops_feature_flag(tenant_id)
    if disabled:
        return disabled

    try:
        es = _get_es()

        # Failed shipments with aggregation by failure_reason and time trend
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
            "by_reason": {
                "terms": {"field": "failure_reason", "size": 50},
                "aggs": {
                    "sample_shipments": {
                        "top_hits": {
                            "size": 5,
                            "_source": ["shipment_id", "rider_id", "updated_at", "origin", "destination"],
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
        }

        response = es.client.search(index=es.SHIPMENTS_CURRENT, body=es_query)
        aggs = response.get("aggregations", {})
        total_failures = _total_hits(response)

        reason_buckets = aggs.get("by_reason", {}).get("buckets", [])
        time_buckets = aggs.get("over_time", {}).get("buckets", [])

        # Build markdown report
        md = _report_header("Failure Analysis Report", start_date, end_date, tenant_id)

        md += "## Summary\n\n"
        md += f"| Metric | Value |\n"
        md += f"|---|---|\n"
        md += f"| Total Failures | {total_failures} |\n"
        md += f"| Distinct Root Causes | {len(reason_buckets)} |\n\n"

        if reason_buckets:
            md += "## Failures by Root Cause\n\n"
            md += "| Root Cause | Count | % of Total |\n"
            md += "|---|---|---|\n"
            for rb in reason_buckets:
                reason = rb["key"]
                count = rb["doc_count"]
                pct = round((count / total_failures) * 100, 1) if total_failures > 0 else 0
                md += f"| {reason} | {count} | {pct}% |\n"

            md += "\n## Affected Shipments by Root Cause\n\n"
            for rb in reason_buckets:
                reason = rb["key"]
                samples = _format_hits(rb.get("sample_shipments", {}))
                samples = [_pii_masker.mask_response(s, has_pii_access=False) for s in samples]

                md += f"### {reason} ({rb['doc_count']} failures)\n\n"
                if samples:
                    md += "| Shipment ID | Rider | Last Updated | Origin | Destination |\n"
                    md += "|---|---|---|---|---|\n"
                    for s in samples:
                        md += (
                            f"| {s.get('shipment_id', 'N/A')} "
                            f"| {s.get('rider_id', 'N/A')} "
                            f"| {s.get('updated_at', 'N/A')} "
                            f"| {s.get('origin', 'N/A')} "
                            f"| {s.get('destination', 'N/A')} |\n"
                        )
                md += "\n"
        else:
            md += "*No failures found in the specified time range.*\n\n"

        # Trend section
        if time_buckets:
            md += "## Daily Failure Trend\n\n"
            md += "| Date | Failures |\n"
            md += "|---|---|\n"
            for tb in time_buckets:
                md += f"| {tb.get('key_as_string', 'N/A')} | {tb['doc_count']} |\n"

        duration_ms = (time.time() - start_time) * 1000
        logger.info("generate_failure_report completed in %.1fms, %d failures", duration_ms, total_failures)
        return md

    except Exception as e:
        logger.error("generate_failure_report failed: %s", e)
        return json.dumps({"tool": "generate_failure_report", "error": str(e)})


# ---------------------------------------------------------------------------
# Rider Productivity Report
# ---------------------------------------------------------------------------

@tool
async def generate_rider_productivity_report(
    start_date: str,
    end_date: str,
    tenant_id: str,
) -> str:
    """
    Generate a rider productivity report for a specified time range.

    Produces a structured markdown report showing per-rider metrics including
    deliveries completed, average delivery time, failure rate, and utilization
    percentage. Results are scoped to the requesting tenant.

    Args:
        start_date: Start of time range (ISO-8601).
        end_date: End of time range (ISO-8601).
        tenant_id: The tenant ID (from authenticated context).

    Returns:
        Structured markdown report string for the AI agent to present.

    Validates: Requirements 18.3, 18.4, 18.5, 19.1, 19.2, 19.5
    """
    start_time = time.time()
    params = {"start_date": start_date, "end_date": end_date}
    _log_tool_call("generate_rider_productivity_report", params, tenant_id)

    disabled = await check_ops_feature_flag(tenant_id)
    if disabled:
        return disabled

    try:
        es = _get_es()

        # --- Per-rider delivery stats from shipment_events ---
        # Count delivered and failed events per rider in the time range
        events_query = {
            "query": {
                "bool": {
                    "filter": [
                        {"terms": {"event_type": ["shipment_delivered", "shipment_failed"]}},
                        {"range": {"event_timestamp": {"gte": start_date, "lte": end_date}}},
                    ]
                }
            }
        }
        events_query = inject_tenant_filter(events_query, tenant_id)
        events_query["size"] = 0
        events_query["aggs"] = {
            "by_rider": {
                "terms": {"field": "shipment_id", "size": 10000},
            }
        }

        # We need to aggregate by rider from shipments_current instead,
        # since shipment_events may not have rider_id directly.
        # Use shipments_current with rider-level aggregation.

        # Deliveries per rider
        delivered_query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"status": "delivered"}},
                        {"range": {"updated_at": {"gte": start_date, "lte": end_date}}},
                    ]
                }
            }
        }
        delivered_query = inject_tenant_filter(delivered_query, tenant_id)
        delivered_query["size"] = 0
        delivered_query["aggs"] = {
            "by_rider": {
                "terms": {"field": "rider_id", "size": 500},
            }
        }

        delivered_resp = es.client.search(index=es.SHIPMENTS_CURRENT, body=delivered_query)
        delivered_aggs = delivered_resp.get("aggregations", {})
        delivered_by_rider = {
            b["key"]: b["doc_count"]
            for b in delivered_aggs.get("by_rider", {}).get("buckets", [])
        }

        # Failures per rider
        failed_query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"status": "failed"}},
                        {"range": {"updated_at": {"gte": start_date, "lte": end_date}}},
                    ]
                }
            }
        }
        failed_query = inject_tenant_filter(failed_query, tenant_id)
        failed_query["size"] = 0
        failed_query["aggs"] = {
            "by_rider": {
                "terms": {"field": "rider_id", "size": 500},
            }
        }

        failed_resp = es.client.search(index=es.SHIPMENTS_CURRENT, body=failed_query)
        failed_aggs = failed_resp.get("aggregations", {})
        failed_by_rider = {
            b["key"]: b["doc_count"]
            for b in failed_aggs.get("by_rider", {}).get("buckets", [])
        }

        # Rider current state for utilization info
        riders_query = {"query": {"match_all": {}}}
        riders_query = inject_tenant_filter(riders_query, tenant_id)
        riders_query["size"] = 500

        riders_resp = es.client.search(index=es.RIDERS_CURRENT, body=riders_query)
        riders = _format_hits(riders_resp)
        riders = [_pii_masker.mask_response(r, has_pii_access=False) for r in riders]

        # Average delivery time from events (delivered events with timestamps)
        avg_time_query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"status": "delivered"}},
                        {"range": {"updated_at": {"gte": start_date, "lte": end_date}}},
                        {"exists": {"field": "created_at"}},
                    ]
                }
            }
        }
        avg_time_query = inject_tenant_filter(avg_time_query, tenant_id)
        avg_time_query["size"] = 0
        avg_time_query["aggs"] = {
            "by_rider": {
                "terms": {"field": "rider_id", "size": 500},
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
        }

        avg_time_resp = es.client.search(index=es.SHIPMENTS_CURRENT, body=avg_time_query)
        avg_time_aggs = avg_time_resp.get("aggregations", {})
        avg_time_by_rider = {}
        for b in avg_time_aggs.get("by_rider", {}).get("buckets", []):
            val = b.get("avg_delivery_time", {}).get("value")
            if val is not None:
                avg_time_by_rider[b["key"]] = round(val, 1)

        # Collect all rider IDs
        all_rider_ids = set(delivered_by_rider.keys()) | set(failed_by_rider.keys())
        rider_info = {r.get("rider_id"): r for r in riders}

        # Build markdown report
        md = _report_header("Rider Productivity Report", start_date, end_date, tenant_id)

        total_delivered = sum(delivered_by_rider.values())
        total_failed = sum(failed_by_rider.values())
        total_assignments = total_delivered + total_failed

        md += "## Summary\n\n"
        md += "| Metric | Value |\n"
        md += "|---|---|\n"
        md += f"| Total Riders | {len(all_rider_ids)} |\n"
        md += f"| Total Deliveries | {total_delivered} |\n"
        md += f"| Total Failures | {total_failed} |\n"
        md += f"| Overall Failure Rate | {round((total_failed / total_assignments) * 100, 1) if total_assignments > 0 else 0}% |\n\n"

        if all_rider_ids:
            md += "## Per-Rider Metrics\n\n"
            md += "| Rider ID | Deliveries | Failures | Failure Rate | Avg Delivery Time (hrs) | Active Shipments | Status |\n"
            md += "|---|---|---|---|---|---|---|\n"

            # Sort by deliveries descending
            sorted_riders = sorted(
                all_rider_ids,
                key=lambda rid: delivered_by_rider.get(rid, 0),
                reverse=True,
            )

            for rid in sorted_riders:
                deliveries = delivered_by_rider.get(rid, 0)
                failures = failed_by_rider.get(rid, 0)
                total = deliveries + failures
                fail_rate = round((failures / total) * 100, 1) if total > 0 else 0
                avg_hrs = avg_time_by_rider.get(rid, "N/A")
                info = rider_info.get(rid, {})
                active = info.get("active_shipment_count", "N/A")
                status = info.get("status", "N/A")

                md += (
                    f"| {rid} "
                    f"| {deliveries} "
                    f"| {failures} "
                    f"| {fail_rate}% "
                    f"| {avg_hrs} "
                    f"| {active} "
                    f"| {status} |\n"
                )
        else:
            md += "*No rider activity found in the specified time range.*\n"

        duration_ms = (time.time() - start_time) * 1000
        logger.info(
            "generate_rider_productivity_report completed in %.1fms, %d riders",
            duration_ms,
            len(all_rider_ids),
        )
        return md

    except Exception as e:
        logger.error("generate_rider_productivity_report failed: %s", e)
        return json.dumps({"tool": "generate_rider_productivity_report", "error": str(e)})
