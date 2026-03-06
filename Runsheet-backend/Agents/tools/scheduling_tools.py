"""
AI agent tools for logistics scheduling queries and reports.

All tools are read-only and tenant-scoped. They query the jobs_current,
job_events, and assets Elasticsearch indices to provide scheduling insights
through natural language interactions.

Validates:
- Requirement 14.1: search_jobs tool
- Requirement 14.2: get_job_details tool
- Requirement 14.3: find_available_assets tool
- Requirement 14.4: get_scheduling_summary tool
- Requirement 14.5: generate_dispatch_report tool
- Requirement 14.6: Tenant scoping enforcement
- Requirement 14.7: Read-only mode
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from strands import tool
from services.elasticsearch_service import elasticsearch_service
from .logging_wrapper import get_telemetry_service

logger = logging.getLogger(__name__)

DEFAULT_TENANT_ID = "default"

JOBS_CURRENT_INDEX = "jobs_current"
JOB_EVENTS_INDEX = "job_events"
ASSETS_INDEX = "trucks"


def _log_tool_invocation(tool_name: str, input_params: dict, start_time: float,
                         success: bool, error: str = None):
    """Helper to log tool invocations with telemetry service."""
    duration_ms = (time.time() - start_time) * 1000
    telemetry = get_telemetry_service()
    if telemetry:
        telemetry.log_tool_invocation(
            tool_name=tool_name,
            input_params=input_params,
            duration_ms=duration_ms,
            success=success,
            error=error
        )
        telemetry.record_metric(
            name="tool_invocation_duration_ms",
            value=duration_ms,
            tags={"tool_name": tool_name, "success": str(success).lower()}
        )
        telemetry.record_metric(
            name="tool_invocation_count",
            value=1,
            tags={"tool_name": tool_name, "success": str(success).lower()}
        )


@tool
async def search_jobs(job_type: str = None, status: str = None,
                      asset: str = None, origin: str = None,
                      destination: str = None, start_date: str = None,
                      end_date: str = None, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """
    Search logistics jobs by type, status, asset, location, or time range.
    All queries are tenant-scoped and read-only.

    Args:
        job_type: Optional job type filter. One of: "cargo_transport", "passenger_transport",
                  "vessel_movement", "airport_transfer", "crane_booking"
        status: Optional status filter. One of: "scheduled", "assigned", "in_progress",
                "completed", "cancelled", "failed"
        asset: Optional asset ID filter to find jobs assigned to a specific asset
        origin: Optional origin location filter (text search)
        destination: Optional destination location filter (text search)
        start_date: Optional start of date range filter (ISO 8601)
        end_date: Optional end of date range filter (ISO 8601)
        tenant_id: Tenant identifier for data scoping

    Returns:
        Formatted text listing matching jobs with key details
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        logger.info(
            f"📋 Searching jobs"
            + (f" job_type={job_type}" if job_type else "")
            + (f" status={status}" if status else "")
            + (f" asset={asset}" if asset else "")
            + (f" origin={origin}" if origin else "")
            + (f" destination={destination}" if destination else "")
        )

        filter_clauses = [{"term": {"tenant_id": tenant_id}}]
        must_clauses = []

        if job_type:
            filter_clauses.append({"term": {"job_type": job_type}})
        if status:
            filter_clauses.append({"term": {"status": status}})
        if asset:
            filter_clauses.append({"term": {"asset_assigned": asset}})
        if origin:
            must_clauses.append({"match": {"origin": origin}})
        if destination:
            must_clauses.append({"match": {"destination": destination}})
        if start_date or end_date:
            date_range = {}
            if start_date:
                date_range["gte"] = start_date
            if end_date:
                date_range["lte"] = end_date
            filter_clauses.append({"range": {"scheduled_time": date_range}})

        es_query = {
            "query": {
                "bool": {
                    "must": must_clauses if must_clauses else [{"match_all": {}}],
                    "filter": filter_clauses
                }
            },
            "sort": [{"scheduled_time": {"order": "desc"}}]
        }

        response = await elasticsearch_service.search_documents(JOBS_CURRENT_INDEX, es_query, 20)
        results = [hit["_source"] for hit in response["hits"]["hits"]]
        total = response["hits"]["total"]["value"]

        if not results:
            success = True
            return "No jobs found matching the specified filters."

        status_emoji = {
            "scheduled": "🔵", "assigned": "🟠", "in_progress": "🟢",
            "completed": "⚪", "cancelled": "⚫", "failed": "🔴"
        }

        response_text = f"📋 Found {total} job(s) (showing {len(results)}):\n\n"
        for job in results:
            emoji = status_emoji.get(job.get("status", ""), "⚪")
            delayed_tag = " ⚠️ DELAYED" if job.get("delayed") else ""
            response_text += f"{emoji} **{job.get('job_id', 'N/A')}** — {job.get('job_type', 'N/A')}{delayed_tag}\n"
            response_text += f"  Status: {job.get('status', 'N/A')}\n"
            response_text += f"  Route: {job.get('origin', 'N/A')} → {job.get('destination', 'N/A')}\n"
            response_text += f"  Scheduled: {job.get('scheduled_time', 'N/A')}\n"
            if job.get("asset_assigned"):
                response_text += f"  Asset: {job.get('asset_assigned')}\n"
            if job.get("estimated_arrival"):
                response_text += f"  ETA: {job.get('estimated_arrival')}\n"
            if job.get("priority") and job.get("priority") != "normal":
                response_text += f"  Priority: {job.get('priority')}\n"
            response_text += "\n"

        success = True
        return response_text
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error searching jobs: {e}")
        return f"Error searching jobs: {str(e)}"
    finally:
        _log_tool_invocation(
            "search_jobs",
            {"job_type": job_type, "status": status, "asset": asset,
             "origin": origin, "destination": destination,
             "start_date": start_date, "end_date": end_date, "tenant_id": tenant_id},
            start_time, success, error_msg
        )


@tool
async def get_job_details(job_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """
    Get full details of a logistics job including event history and cargo manifest.
    Read-only and tenant-scoped.

    Args:
        job_id: The job identifier (e.g., "JOB_123")
        tenant_id: Tenant identifier for data scoping

    Returns:
        Formatted text with complete job details, event history, and cargo manifest
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        logger.info(f"📋 Fetching job details for: {job_id}")

        # Fetch the job document
        job_query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"job_id": job_id}}
                    ]
                }
            }
        }
        job_resp = await elasticsearch_service.search_documents(JOBS_CURRENT_INDEX, job_query, 1)
        job_hits = [hit["_source"] for hit in job_resp["hits"]["hits"]]

        if not job_hits:
            success = True
            return f"No job found with ID '{job_id}'."

        job = job_hits[0]

        status_emoji = {
            "scheduled": "🔵", "assigned": "🟠", "in_progress": "🟢",
            "completed": "⚪", "cancelled": "⚫", "failed": "🔴"
        }
        emoji = status_emoji.get(job.get("status", ""), "⚪")
        delayed_tag = " ⚠️ DELAYED" if job.get("delayed") else ""

        response_text = f"{emoji} **Job {job.get('job_id', 'N/A')}**{delayed_tag}\n\n"
        response_text += f"**Type:** {job.get('job_type', 'N/A')}\n"
        response_text += f"**Status:** {job.get('status', 'N/A')}\n"
        response_text += f"**Priority:** {job.get('priority', 'normal')}\n"
        response_text += f"**Route:** {job.get('origin', 'N/A')} → {job.get('destination', 'N/A')}\n"
        response_text += f"**Scheduled:** {job.get('scheduled_time', 'N/A')}\n"

        if job.get("asset_assigned"):
            response_text += f"**Asset:** {job.get('asset_assigned')}\n"
        if job.get("estimated_arrival"):
            response_text += f"**ETA:** {job.get('estimated_arrival')}\n"
        if job.get("started_at"):
            response_text += f"**Started:** {job.get('started_at')}\n"
        if job.get("completed_at"):
            response_text += f"**Completed:** {job.get('completed_at')}\n"
        if job.get("delayed") and job.get("delay_duration_minutes"):
            response_text += f"**Delay:** {job.get('delay_duration_minutes')} minutes\n"
        if job.get("failure_reason"):
            response_text += f"**Failure Reason:** {job.get('failure_reason')}\n"
        if job.get("notes"):
            response_text += f"**Notes:** {job.get('notes')}\n"
        if job.get("created_by"):
            response_text += f"**Created By:** {job.get('created_by')}\n"
        response_text += f"**Created:** {job.get('created_at', 'N/A')}\n"
        response_text += f"**Updated:** {job.get('updated_at', 'N/A')}\n"

        # Cargo manifest
        cargo = job.get("cargo_manifest")
        if cargo and len(cargo) > 0:
            response_text += f"\n**Cargo Manifest ({len(cargo)} items):**\n"
            cargo_status_emoji = {
                "pending": "⏳", "loaded": "📦", "in_transit": "🚚",
                "delivered": "✅", "damaged": "❌"
            }
            for item in cargo:
                c_emoji = cargo_status_emoji.get(item.get("item_status", ""), "⚪")
                response_text += f"  {c_emoji} {item.get('description', 'N/A')} — {item.get('weight_kg', 0)} kg"
                if item.get("container_number"):
                    response_text += f" (Container: {item.get('container_number')})"
                response_text += f" [{item.get('item_status', 'N/A')}]\n"

        # Fetch event history
        events_query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"job_id": job_id}}
                    ]
                }
            },
            "sort": [{"event_timestamp": {"order": "asc"}}]
        }
        events_resp = await elasticsearch_service.search_documents(JOB_EVENTS_INDEX, events_query, 50)
        events = [hit["_source"] for hit in events_resp["hits"]["hits"]]

        if events:
            response_text += f"\n**Event History ({len(events)} events):**\n"
            for event in events:
                ts = event.get("event_timestamp", "N/A")
                if isinstance(ts, str) and len(ts) > 16:
                    ts = ts[:16].replace("T", " ")
                response_text += f"  • [{ts}] {event.get('event_type', 'N/A')}"
                if event.get("actor_id"):
                    response_text += f" by {event.get('actor_id')}"
                response_text += "\n"

        success = True
        return response_text
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error fetching job details: {e}")
        return f"Error fetching job details: {str(e)}"
    finally:
        _log_tool_invocation(
            "get_job_details",
            {"job_id": job_id, "tenant_id": tenant_id},
            start_time, success, error_msg
        )


@tool
async def find_available_assets(asset_type: str = None, start_time_range: str = None,
                                 end_time_range: str = None,
                                 tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """
    Find assets not assigned to active jobs within a specified time window.
    Queries the assets index for all assets of the given type, then checks
    jobs_current for active jobs in the time window, and returns assets
    not in the active job set. Read-only and tenant-scoped.

    Args:
        asset_type: Optional asset type filter. One of: "vehicle", "vessel", "equipment", "container"
        start_time_range: Optional start of time window (ISO 8601). Defaults to now.
        end_time_range: Optional end of time window (ISO 8601). Defaults to 24 hours from now.
        tenant_id: Tenant identifier for data scoping

    Returns:
        Formatted text listing available assets not assigned to active jobs
    """
    tool_start = time.time()
    success = False
    error_msg = None

    try:
        logger.info(
            f"🔍 Finding available assets"
            + (f" type={asset_type}" if asset_type else "")
            + (f" from={start_time_range}" if start_time_range else "")
            + (f" to={end_time_range}" if end_time_range else "")
        )

        # Step 1: Query all assets of the given type
        asset_filters = [{"term": {"tenant_id": tenant_id}}]
        if asset_type:
            asset_filters.append({"term": {"asset_type": asset_type}})

        asset_query = {
            "query": {
                "bool": {
                    "filter": asset_filters
                }
            }
        }
        asset_resp = await elasticsearch_service.search_documents(ASSETS_INDEX, asset_query, 100)
        all_assets = [hit["_source"] for hit in asset_resp["hits"]["hits"]]

        if not all_assets:
            success = True
            type_msg = f" of type '{asset_type}'" if asset_type else ""
            return f"No assets found{type_msg}."

        # Step 2: Query active jobs in the time window to find busy assets
        now = datetime.now(timezone.utc)
        window_start = start_time_range or now.isoformat()
        window_end = end_time_range or (now + timedelta(hours=24)).isoformat()

        active_job_filters = [
            {"term": {"tenant_id": tenant_id}},
            {"terms": {"status": ["assigned", "in_progress"]}},
            {"range": {"scheduled_time": {"lte": window_end}}},
        ]

        active_jobs_query = {
            "query": {
                "bool": {
                    "filter": active_job_filters
                }
            }
        }
        jobs_resp = await elasticsearch_service.search_documents(JOBS_CURRENT_INDEX, active_jobs_query, 200)
        active_jobs = [hit["_source"] for hit in jobs_resp["hits"]["hits"]]

        # Build set of busy asset IDs
        busy_asset_ids = set()
        for job in active_jobs:
            if job.get("asset_assigned"):
                busy_asset_ids.add(job["asset_assigned"])

        # Step 3: Filter out busy assets
        available_assets = []
        for a in all_assets:
            asset_id = a.get("asset_id") or a.get("plate_number") or a.get("vessel_id") or a.get("equipment_id")
            if asset_id and asset_id not in busy_asset_ids:
                available_assets.append(a)

        if not available_assets:
            success = True
            type_msg = f" of type '{asset_type}'" if asset_type else ""
            return f"No available assets{type_msg} for the specified time window. All {len(all_assets)} assets are currently assigned to active jobs."

        response_text = f"🟢 Found {len(available_assets)} available asset(s) out of {len(all_assets)} total"
        if asset_type:
            response_text += f" (type: {asset_type})"
        response_text += f":\n\n"

        for a in available_assets:
            display_name = (a.get("asset_name") or a.get("plate_number") or
                          a.get("vessel_name") or a.get("equipment_model") or "Unknown")
            a_type = a.get("asset_type", "N/A")
            a_subtype = a.get("asset_subtype", "N/A")
            response_text += f"• **{display_name}** [{a_type}/{a_subtype}]\n"
            response_text += f"  Status: {a.get('status', 'N/A')}\n"
            if a.get("current_location", {}).get("name"):
                response_text += f"  Location: {a['current_location']['name']}\n"
            response_text += "\n"

        response_text += f"\n📊 {len(busy_asset_ids)} asset(s) currently assigned to active jobs."

        success = True
        return response_text
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error finding available assets: {e}")
        return f"Error finding available assets: {str(e)}"
    finally:
        _log_tool_invocation(
            "find_available_assets",
            {"asset_type": asset_type, "start_time_range": start_time_range,
             "end_time_range": end_time_range, "tenant_id": tenant_id},
            tool_start, success, error_msg
        )


@tool
async def get_scheduling_summary(tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """
    Get a summary of scheduling operations: active jobs count, delayed count,
    available assets, and upcoming scheduled jobs. Read-only and tenant-scoped.

    Args:
        tenant_id: Tenant identifier for data scoping

    Returns:
        Formatted text with scheduling summary metrics
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        logger.info("📊 Fetching scheduling summary")

        # Aggregation query for job counts by status
        summary_query = {
            "size": 0,
            "query": {"term": {"tenant_id": tenant_id}},
            "aggs": {
                "by_status": {"terms": {"field": "status"}},
                "delayed_count": {
                    "filter": {"term": {"delayed": True}}
                },
                "by_job_type": {"terms": {"field": "job_type"}}
            }
        }
        summary_resp = await elasticsearch_service.search_documents(JOBS_CURRENT_INDEX, summary_query, 0)
        total_jobs = summary_resp["hits"]["total"]["value"]
        aggs = summary_resp.get("aggregations", {})

        status_counts = {}
        for bucket in aggs.get("by_status", {}).get("buckets", []):
            status_counts[bucket["key"]] = bucket["doc_count"]

        delayed_count = aggs.get("delayed_count", {}).get("doc_count", 0)

        type_counts = {}
        for bucket in aggs.get("by_job_type", {}).get("buckets", []):
            type_counts[bucket["key"]] = bucket["doc_count"]

        active_count = (status_counts.get("scheduled", 0) +
                       status_counts.get("assigned", 0) +
                       status_counts.get("in_progress", 0))

        # Count available assets (total assets minus those with active jobs)
        asset_resp = await elasticsearch_service.search_documents(
            ASSETS_INDEX,
            {"size": 0, "query": {"term": {"tenant_id": tenant_id}}},
            0
        )
        total_assets = asset_resp["hits"]["total"]["value"]

        busy_query = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"status": ["assigned", "in_progress"]}},
                        {"exists": {"field": "asset_assigned"}}
                    ]
                }
            },
            "aggs": {
                "busy_assets": {"cardinality": {"field": "asset_assigned"}}
            }
        }
        busy_resp = await elasticsearch_service.search_documents(JOBS_CURRENT_INDEX, busy_query, 0)
        busy_count = busy_resp.get("aggregations", {}).get("busy_assets", {}).get("value", 0)
        available_assets = max(0, total_assets - busy_count)

        # Fetch upcoming scheduled jobs (next 5)
        upcoming_query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"status": ["scheduled", "assigned"]}}
                    ]
                }
            },
            "sort": [{"scheduled_time": {"order": "asc"}}]
        }
        upcoming_resp = await elasticsearch_service.search_documents(JOBS_CURRENT_INDEX, upcoming_query, 5)
        upcoming_jobs = [hit["_source"] for hit in upcoming_resp["hits"]["hits"]]

        # Build response
        response_text = "📊 **Scheduling Summary**\n\n"
        response_text += f"**Total Jobs:** {total_jobs}\n"
        response_text += f"**Active Jobs:** {active_count}\n"
        response_text += f"  🔵 Scheduled: {status_counts.get('scheduled', 0)}\n"
        response_text += f"  🟠 Assigned: {status_counts.get('assigned', 0)}\n"
        response_text += f"  🟢 In Progress: {status_counts.get('in_progress', 0)}\n"
        response_text += f"**Completed:** {status_counts.get('completed', 0)}\n"
        response_text += f"**Cancelled:** {status_counts.get('cancelled', 0)}\n"
        response_text += f"**Failed:** {status_counts.get('failed', 0)}\n"
        response_text += f"**⚠️ Delayed:** {delayed_count}\n\n"

        response_text += f"**Assets:** {total_assets} total, {available_assets} available, {busy_count} assigned\n\n"

        if type_counts:
            response_text += "**Jobs by Type:**\n"
            for jt, count in sorted(type_counts.items()):
                response_text += f"  • {jt}: {count}\n"
            response_text += "\n"

        if upcoming_jobs:
            response_text += "**Upcoming Jobs:**\n"
            for job in upcoming_jobs:
                response_text += f"  • {job.get('job_id', 'N/A')} — {job.get('job_type', 'N/A')} [{job.get('status', 'N/A')}]\n"
                response_text += f"    {job.get('origin', 'N/A')} → {job.get('destination', 'N/A')} at {job.get('scheduled_time', 'N/A')}\n"

        success = True
        return response_text
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error fetching scheduling summary: {e}")
        return f"Error fetching scheduling summary: {str(e)}"
    finally:
        _log_tool_invocation(
            "get_scheduling_summary",
            {"tenant_id": tenant_id},
            start_time, success, error_msg
        )


@tool
async def generate_dispatch_report(days: int = 7, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """
    Generate a markdown dispatch report covering job completion rates,
    delay analysis, asset utilization, and recommendations for a specified
    time range. Read-only and tenant-scoped.

    Args:
        days: Number of days to cover in the report (default: 7)
        tenant_id: Tenant identifier for data scoping

    Returns:
        Markdown-formatted dispatch operations report
    """
    start_time = time.time()
    success = False
    error_msg = None

    try:
        logger.info(f"📋 Generating dispatch report for last {days} days")

        now = datetime.now(timezone.utc)
        start_date = now - timedelta(days=days)
        report_date = now.strftime("%Y-%m-%d %H:%M UTC")

        # --- 1. Job counts by status and type ---
        jobs_agg_query = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"range": {"created_at": {"gte": start_date.isoformat(), "lte": now.isoformat()}}}
                    ]
                }
            },
            "aggs": {
                "by_status": {"terms": {"field": "status"}},
                "by_type": {"terms": {"field": "job_type"}},
                "delayed_count": {"filter": {"term": {"delayed": True}}},
                "avg_delay_minutes": {
                    "filter": {"term": {"delayed": True}},
                    "aggs": {
                        "avg_delay": {"avg": {"field": "delay_duration_minutes"}}
                    }
                },
                "completion_time": {
                    "filter": {"term": {"status": "completed"}},
                    "aggs": {
                        "by_type": {
                            "terms": {"field": "job_type"},
                            "aggs": {
                                "count": {"value_count": {"field": "job_id"}}
                            }
                        }
                    }
                }
            }
        }
        jobs_resp = await elasticsearch_service.search_documents(JOBS_CURRENT_INDEX, jobs_agg_query, 0)
        total_jobs = jobs_resp["hits"]["total"]["value"]
        j_aggs = jobs_resp.get("aggregations", {})

        status_counts = {}
        for bucket in j_aggs.get("by_status", {}).get("buckets", []):
            status_counts[bucket["key"]] = bucket["doc_count"]

        type_counts = {}
        for bucket in j_aggs.get("by_type", {}).get("buckets", []):
            type_counts[bucket["key"]] = bucket["doc_count"]

        delayed_count = j_aggs.get("delayed_count", {}).get("doc_count", 0)
        avg_delay = j_aggs.get("avg_delay_minutes", {}).get("avg_delay", {}).get("value", 0) or 0

        completed = status_counts.get("completed", 0)
        failed = status_counts.get("failed", 0)
        cancelled = status_counts.get("cancelled", 0)
        completion_rate = (completed / total_jobs * 100) if total_jobs > 0 else 0

        # --- 2. Asset utilization ---
        asset_util_query = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"exists": {"field": "asset_assigned"}},
                        {"range": {"created_at": {"gte": start_date.isoformat(), "lte": now.isoformat()}}}
                    ]
                }
            },
            "aggs": {
                "unique_assets": {"cardinality": {"field": "asset_assigned"}},
                "top_assets": {
                    "terms": {"field": "asset_assigned", "size": 10, "order": {"_count": "desc"}}
                }
            }
        }
        asset_resp = await elasticsearch_service.search_documents(JOBS_CURRENT_INDEX, asset_util_query, 0)
        a_aggs = asset_resp.get("aggregations", {})
        unique_assets_used = a_aggs.get("unique_assets", {}).get("value", 0)
        top_assets = a_aggs.get("top_assets", {}).get("buckets", [])

        # --- 3. Delay analysis by job type ---
        delay_by_type_query = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"delayed": True}},
                        {"range": {"created_at": {"gte": start_date.isoformat(), "lte": now.isoformat()}}}
                    ]
                }
            },
            "aggs": {
                "by_type": {
                    "terms": {"field": "job_type"},
                    "aggs": {
                        "avg_delay": {"avg": {"field": "delay_duration_minutes"}}
                    }
                }
            }
        }
        delay_resp = await elasticsearch_service.search_documents(JOBS_CURRENT_INDEX, delay_by_type_query, 0)
        delay_by_type = delay_resp.get("aggregations", {}).get("by_type", {}).get("buckets", [])

        # --- Build the report ---
        report = f"# 📋 Dispatch Operations Report\n"
        report += f"**Period:** Last {days} days | **Generated:** {report_date}\n\n"

        # Job overview
        report += "## Job Overview\n\n"
        report += f"| Metric | Value |\n"
        report += f"|--------|-------|\n"
        report += f"| Total Jobs | {total_jobs} |\n"
        report += f"| Completed | {completed} |\n"
        report += f"| Failed | {failed} |\n"
        report += f"| Cancelled | {cancelled} |\n"
        report += f"| Completion Rate | {completion_rate:.1f}% |\n"
        report += f"| Delayed | {delayed_count} |\n"
        report += f"| Avg Delay | {avg_delay:.0f} min |\n\n"

        # Jobs by type
        if type_counts:
            report += "## Jobs by Type\n\n"
            report += "| Job Type | Count |\n"
            report += "|----------|-------|\n"
            for jt, count in sorted(type_counts.items()):
                report += f"| {jt} | {count} |\n"
            report += "\n"

        # Delay analysis
        report += "## Delay Analysis\n\n"
        if delay_by_type:
            report += "| Job Type | Delayed Jobs | Avg Delay (min) |\n"
            report += "|----------|-------------|----------------|\n"
            for bucket in delay_by_type:
                jt = bucket.get("key", "N/A")
                count = bucket.get("doc_count", 0)
                avg_d = bucket.get("avg_delay", {}).get("value", 0) or 0
                report += f"| {jt} | {count} | {avg_d:.0f} |\n"
            report += "\n"
        else:
            report += "No delayed jobs in this period. ✅\n\n"

        # Asset utilization
        report += "## Asset Utilization\n\n"
        report += f"**Unique Assets Used:** {unique_assets_used}\n\n"
        if top_assets:
            report += "**Most Active Assets:**\n\n"
            report += "| Asset | Jobs Assigned |\n"
            report += "|-------|---------------|\n"
            for bucket in top_assets:
                report += f"| {bucket.get('key', 'N/A')} | {bucket.get('doc_count', 0)} |\n"
            report += "\n"

        # Recommendations
        report += "## Recommendations\n\n"
        recommendations = []

        if completion_rate < 80:
            recommendations.append(
                f"⚠️ **Low completion rate** ({completion_rate:.1f}%). Investigate failed/cancelled jobs for root causes."
            )
        if delayed_count > 0 and total_jobs > 0:
            delay_pct = delayed_count / total_jobs * 100
            if delay_pct > 20:
                recommendations.append(
                    f"🚨 **High delay rate** ({delay_pct:.1f}%). Review scheduling and asset allocation."
                )
        if avg_delay > 60:
            recommendations.append(
                f"⏰ **Average delay is {avg_delay:.0f} minutes.** Consider adjusting ETA estimates or adding buffer time."
            )
        if failed > 0:
            recommendations.append(
                f"🔴 **{failed} failed job(s).** Review failure reasons and implement preventive measures."
            )
        if not recommendations:
            recommendations.append("✅ Operations are running smoothly. No immediate action required.")

        for rec in recommendations:
            report += f"- {rec}\n"

        success = True
        return report
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error generating dispatch report: {e}")
        return f"Error generating dispatch report: {str(e)}"
    finally:
        _log_tool_invocation(
            "generate_dispatch_report",
            {"days": days, "tenant_id": tenant_id},
            start_time, success, error_msg
        )
