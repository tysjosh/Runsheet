"""In-Python shipment metrics aggregation over ``shipments_current`` documents.

Reproduces the Elasticsearch ``date_histogram`` aggregation output for the ops
shipment metrics endpoints (counts by status, SLA compliance, failures by
reason) from a plain list of shipment documents. Used on the read-cutover path:
when ``COMMERCE_READ_FROM_POSTGRES`` is on we fetch the matching shipment rows
from Postgres and aggregate here, producing the SAME ``MetricsBucket`` shapes
the ES aggregations produced — so the API response and dashboard are unchanged.

Faithfulness mirrors :mod:`scheduling.services.job_metrics_aggregator`:
* date_histogram defaults to ``min_doc_count: 0`` → empty interior buckets ARE
  emitted between the data's min and max bucket.
* ``timestamp`` uses ES default UTC millisecond precision with a ``Z`` suffix.
* Only ``1h`` / ``1d`` calendar intervals (truncate to hour / day in UTC).
* SLA breach mirrors the painless script: a shipment is breached when it has
  BOTH ``estimated_delivery`` and ``last_event_timestamp`` and the latter is
  strictly after the former.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _truncate(dt: datetime, interval: str) -> datetime:
    dt = dt.astimezone(timezone.utc)
    if interval == "1h":
        return dt.replace(minute=0, second=0, microsecond=0)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _step(interval: str) -> timedelta:
    return timedelta(hours=1) if interval == "1h" else timedelta(days=1)


def _key_as_string(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _bucketize(
    docs: List[Dict[str, Any]], date_field: str, interval: str
) -> List[tuple]:
    """Group docs into (bucket_dt, [docs]) ordered ascending with gaps filled.

    Docs whose ``date_field`` does not parse are skipped (ES cannot bucket a
    doc missing the histogram field).
    """
    by_bucket: Dict[datetime, List[Dict[str, Any]]] = {}
    for doc in docs:
        dt = _parse_iso(doc.get(date_field))
        if dt is None:
            continue
        by_bucket.setdefault(_truncate(dt, interval), []).append(doc)

    if not by_bucket:
        return []

    step = _step(interval)
    lo, hi = min(by_bucket), max(by_bucket)
    out: List[tuple] = []
    cursor = lo
    while cursor <= hi:
        out.append((cursor, by_bucket.get(cursor, [])))
        cursor += step
    return out


def shipment_status_buckets(
    docs: List[Dict[str, Any]], interval: str
) -> List[Dict[str, Any]]:
    """Reproduce ``metrics/shipments``: counts by status per ``updated_at`` bucket.

    Returns a list of ``{timestamp, values}`` where ``values`` holds each status
    count plus ``total`` — matching the ES path's ``MetricsBucket`` payload.
    """
    out: List[Dict[str, Any]] = []
    for bucket_dt, bucket_docs in _bucketize(docs, "updated_at", interval):
        values: Dict[str, Any] = {}
        for d in bucket_docs:
            status = d.get("status")
            if status is not None:
                values[status] = values.get(status, 0) + 1
        values["total"] = len(bucket_docs)
        out.append({"timestamp": _key_as_string(bucket_dt), "values": values})
    return out


def shipment_sla_buckets(
    docs: List[Dict[str, Any]], interval: str
) -> List[Dict[str, Any]]:
    """Reproduce ``metrics/sla``: compliance pct per ``updated_at`` bucket.

    ``docs`` must already be filtered to those with ``estimated_delivery``
    present (the ES path filters on ``exists: estimated_delivery``). A bucket's
    breached count is the number of docs whose ``last_event_timestamp`` is
    strictly after ``estimated_delivery`` (mirrors the painless script).
    """
    out: List[Dict[str, Any]] = []
    for bucket_dt, bucket_docs in _bucketize(docs, "updated_at", interval):
        total = len(bucket_docs)
        breached = 0
        for d in bucket_docs:
            est = _parse_iso(d.get("estimated_delivery"))
            last = _parse_iso(d.get("last_event_timestamp"))
            if est is not None and last is not None and last > est:
                breached += 1
        compliant = total - breached
        compliance_pct = round((compliant / total) * 100, 2) if total > 0 else 100.0
        out.append({
            "timestamp": _key_as_string(bucket_dt),
            "values": {
                "total": total,
                "breached": breached,
                "compliant": compliant,
                "compliance_pct": compliance_pct,
            },
        })
    return out


def shipment_failure_buckets(
    docs: List[Dict[str, Any]], interval: str
) -> List[Dict[str, Any]]:
    """Reproduce ``metrics/failures``: failure counts by reason per bucket.

    ``docs`` must already be filtered to ``status == "failed"``. Each bucket
    reports ``total_failures`` plus a count per ``failure_reason`` value
    (missing reasons are simply not counted, matching the ES terms agg which
    only buckets present field values).
    """
    out: List[Dict[str, Any]] = []
    for bucket_dt, bucket_docs in _bucketize(docs, "updated_at", interval):
        values: Dict[str, Any] = {"total_failures": len(bucket_docs)}
        for d in bucket_docs:
            reason = d.get("failure_reason")
            if reason is not None:
                values[reason] = values.get(reason, 0) + 1
        out.append({"timestamp": _key_as_string(bucket_dt), "values": values})
    return out
