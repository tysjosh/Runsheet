"""In-Python job metrics aggregation over ``jobs_current`` documents.

This module reproduces the Elasticsearch aggregation output for the scheduling
metrics endpoints (job counts over time, completion stats, asset utilization,
delay stats) from a plain list of job documents. It is used on the read-cutover
path: when ``COMMERCE_READ_FROM_POSTGRES`` is on we fetch the matching job rows
from Postgres and aggregate here, producing the SAME shapes the ES
``date_histogram`` / ``terms`` aggregations produced — so the API response and
the dashboard are unchanged.

Faithfulness notes:
* ``date_histogram`` defaults to ``min_doc_count: 0``: empty interior buckets
  between the data's min and max ARE emitted. :func:`bucket_jobs_over_time`
  replicates that gap-filling.
* ``key_as_string`` uses millisecond precision with a ``Z`` suffix
  (e.g. ``2026-01-01T00:00:00.000Z``), matching the ES default for UTC.
* Calendar intervals are only ``1h`` (hourly) and ``1d`` (daily), so bucketing
  is a simple truncation to the hour / day boundary in UTC — no month/DST math.
* Duration math (completion minutes, active hours) mirrors the existing
  endpoint code exactly, including the "open job runs to now" rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _truncate(dt: datetime, interval: str) -> datetime:
    """Truncate to the hour (``1h``) or day (``1d``) boundary in UTC."""
    dt = dt.astimezone(timezone.utc)
    if interval == "1h":
        return dt.replace(minute=0, second=0, microsecond=0)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _step(interval: str) -> timedelta:
    return timedelta(hours=1) if interval == "1h" else timedelta(days=1)


def _key_as_string(dt: datetime) -> str:
    """ES default UTC format with millisecond precision and ``Z`` suffix."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def bucket_jobs_over_time(
    jobs: List[Dict[str, Any]], interval: str
) -> List[Dict[str, Any]]:
    """Replicate the ``over_time`` date_histogram with by_status / by_type.

    Returns a list of ``{timestamp, total, counts_by_status, counts_by_type}``
    ordered ascending, with empty interior buckets filled (min_doc_count=0).
    Jobs without a parseable ``scheduled_time`` are skipped (ES cannot bucket
    a doc that lacks the histogram field).
    """
    by_bucket: Dict[datetime, Dict[str, Any]] = {}
    for job in jobs:
        sched = _parse_iso(job.get("scheduled_time"))
        if sched is None:
            continue
        key = _truncate(sched, interval)
        slot = by_bucket.setdefault(
            key, {"total": 0, "by_status": {}, "by_type": {}}
        )
        slot["total"] += 1
        status = job.get("status")
        if status is not None:
            slot["by_status"][status] = slot["by_status"].get(status, 0) + 1
        job_type = job.get("job_type")
        if job_type is not None:
            slot["by_type"][job_type] = slot["by_type"].get(job_type, 0) + 1

    if not by_bucket:
        return []

    step = _step(interval)
    lo, hi = min(by_bucket), max(by_bucket)
    out: List[Dict[str, Any]] = []
    cursor = lo
    while cursor <= hi:
        slot = by_bucket.get(cursor)
        out.append({
            "timestamp": _key_as_string(cursor),
            "total": slot["total"] if slot else 0,
            "counts_by_status": dict(slot["by_status"]) if slot else {},
            "counts_by_type": dict(slot["by_type"]) if slot else {},
        })
        cursor += step
    return out


def completion_metrics(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Replicate the completion terms-agg + Python duration math.

    Per job_type: total count, completed count, completion_rate (%), and
    avg_completion_minutes over jobs that have BOTH started_at and completed_at.
    """
    type_stats: Dict[str, Dict[str, int]] = {}
    durations_by_type: Dict[str, List[float]] = {}

    for job in jobs:
        job_type = job.get("job_type")
        if job_type is None:
            continue
        stats = type_stats.setdefault(job_type, {"total": 0, "completed": 0})
        stats["total"] += 1
        if job.get("status") == "completed":
            stats["completed"] += 1
            start_dt = _parse_iso(job.get("started_at"))
            end_dt = _parse_iso(job.get("completed_at"))
            if start_dt is not None and end_dt is not None:
                minutes = (end_dt - start_dt).total_seconds() / 60.0
                durations_by_type.setdefault(job_type, []).append(minutes)

    metrics: List[Dict[str, Any]] = []
    for job_type, stats in type_stats.items():
        total = stats["total"]
        completed = stats["completed"]
        rate = round((completed / total) * 100, 2) if total > 0 else 0.0
        durs = durations_by_type.get(job_type, [])
        avg_minutes = round(sum(durs) / len(durs), 2) if durs else 0.0
        metrics.append({
            "job_type": job_type,
            "total": total,
            "completed": completed,
            "completion_rate": rate,
            "avg_completion_minutes": avg_minutes,
        })
    # ES terms agg orders buckets by doc_count desc, ties broken by key asc.
    metrics.sort(key=lambda m: (-m["total"], m["job_type"]))
    return metrics


def asset_utilization(
    jobs: List[Dict[str, Any]],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Replicate the by_asset terms-agg + Python active-hours / idle math.

    Only jobs with a non-empty ``asset_assigned`` participate (ES path filters
    on ``exists: asset_assigned``). Active hours sum each job's run; an open job
    (started_at but no completed_at) runs to "now". Idle hours = max(range -
    active, 0) when a start_date/end_date range is supplied.
    """
    asset_stats: Dict[str, Dict[str, int]] = {}
    hours_by_asset: Dict[str, float] = {}

    for job in jobs:
        asset_id = job.get("asset_assigned")
        if not asset_id:
            continue
        stats = asset_stats.setdefault(
            asset_id, {"total_jobs": 0, "active": 0, "completed": 0}
        )
        stats["total_jobs"] += 1
        status = job.get("status")
        if status in ("assigned", "in_progress"):
            stats["active"] += 1
        if status == "completed":
            stats["completed"] += 1

        start_dt = _parse_iso(job.get("started_at"))
        if start_dt is not None:
            end_dt = _parse_iso(job.get("completed_at"))
            if end_dt is None:
                end_dt = datetime.now(start_dt.tzinfo)
            hours = (end_dt - start_dt).total_seconds() / 3600.0
            hours_by_asset[asset_id] = hours_by_asset.get(asset_id, 0.0) + hours

    total_range_hours = 0.0
    range_start = _parse_iso(start_date)
    range_end = _parse_iso(end_date)
    if range_start is not None and range_end is not None:
        total_range_hours = (range_end - range_start).total_seconds() / 3600.0

    metrics: List[Dict[str, Any]] = []
    for asset_id, stats in asset_stats.items():
        active_hrs = round(hours_by_asset.get(asset_id, 0.0), 2)
        idle_hrs = (
            round(max(total_range_hours - active_hrs, 0.0), 2)
            if total_range_hours > 0 else 0.0
        )
        metrics.append({
            "asset_id": asset_id,
            "total_jobs": stats["total_jobs"],
            "active_jobs": stats["active"],
            "completed_jobs": stats["completed"],
            "total_active_hours": active_hrs,
            "idle_hours": idle_hrs,
        })
    # ES terms agg orders buckets by doc_count desc, ties broken by key asc.
    metrics.sort(key=lambda m: (-m["total_jobs"], m["asset_id"]))
    return metrics


def delay_metrics(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Replicate the delayed-jobs avg + by-job_type terms aggregation.

    ``jobs`` MUST already be filtered to ``delayed == True`` (the caller applies
    that filter so the count matches ES ``hits.total``). Averages are over
    ``delay_duration_minutes``; missing/None values are ignored in the average
    exactly as the ES ``avg`` metric skips missing fields.
    """
    total_delayed = len(jobs)

    all_delays: List[float] = []
    by_type_delays: Dict[str, List[float]] = {}
    by_type_count: Dict[str, int] = {}

    for job in jobs:
        job_type = job.get("job_type")
        if job_type is not None:
            by_type_count[job_type] = by_type_count.get(job_type, 0) + 1
        raw = job.get("delay_duration_minutes")
        if raw is not None:
            try:
                val = float(raw)
            except (ValueError, TypeError):
                val = None
            if val is not None:
                all_delays.append(val)
                if job_type is not None:
                    by_type_delays.setdefault(job_type, []).append(val)

    avg_delay_minutes = round(sum(all_delays) / len(all_delays), 2) if all_delays else 0.0

    delays_by_job_type: List[Dict[str, Any]] = []
    for job_type, count in by_type_count.items():
        durs = by_type_delays.get(job_type, [])
        avg = round(sum(durs) / len(durs), 2) if durs else 0.0
        delays_by_job_type.append({
            "job_type": job_type,
            "count": count,
            "avg_delay_minutes": avg,
        })
    # ES terms agg orders buckets by doc_count desc, ties broken by key asc.
    delays_by_job_type.sort(key=lambda d: (-d["count"], d["job_type"]))

    return {
        "total_delayed": total_delayed,
        "avg_delay_minutes": avg_delay_minutes,
        "delays_by_job_type": delays_by_job_type,
    }
