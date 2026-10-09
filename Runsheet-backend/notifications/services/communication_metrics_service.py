"""
Communication SLA metrics service.

Computes communication SLA metrics from Elasticsearch aggregations:
- ack_latency: time between job assignment and driver acknowledgment
- notification_send_latency: time between notification creation and dispatch
- driver_response_latency: time between assignment and accept/reject
- failed_notification_rate: ratio of failed to total notifications by channel

All metrics are aggregated by time bucket (hourly/daily) and optionally
filtered by tenant and date range.

Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from notifications.services.notification_es_mappings import NOTIFICATIONS_CURRENT_INDEX
from scheduling.services.scheduling_es_mappings import JOB_EVENTS_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


#: ``fixed_interval`` strings these metrics accept, as timedeltas. Deliberately
#: not calendar units: ``1M`` cannot be a fixed timedelta, and approximating a
#: month as 30 days shifts every bucket boundary after the first. The document
#: store refuses those for the same reason.
_FIXED_INTERVALS = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "1w": timedelta(weeks=1),
}


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse a stored timestamp into an aware datetime, or ``None``.

    Accepts the ISO-8601 the indices store (including a trailing ``Z``) and the
    epoch milliseconds Elasticsearch returns from a ``min`` on a date field, since
    both shapes reach these metrics depending on the backend.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _bucket_key(stamp: datetime, interval: str) -> str:
    """The ``date_histogram`` bucket label ``stamp`` falls in.

    Floors to a multiple of the interval measured from the epoch, which is what
    Elasticsearch's ``fixed_interval`` does, and formats it the way both the
    Elasticsearch response and ``persistence.document_aggregations`` do — ISO-8601
    with ``Z`` — so a dashboard reading ``timestamp`` sees the same strings it
    always did.

    An unrecognised interval falls back to daily and says so. Silently bucketing
    everything into one bucket would look like a working chart.
    """
    delta = _FIXED_INTERVALS.get(str(interval))
    if delta is None:
        logger.warning(
            "unsupported histogram interval %r; bucketing daily instead", interval
        )
        delta = timedelta(days=1)
    step = int(delta.total_seconds() * 1000)
    millis = int(stamp.timestamp() * 1000)
    floored = millis - (millis % step)
    return (
        datetime.fromtimestamp(floored / 1000.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class CommunicationMetricsService:
    """Computes communication SLA metrics from ES aggregations.

    Uses Elasticsearch aggregation queries to efficiently compute latency
    and failure rate metrics without fetching individual documents.

    Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5
    """

    #: Cap on job events pulled back for a paired-event latency metric. The
    #: previous implementation asked Elasticsearch for ``terms(job_id, size:
    #: 10000)`` twice per query, so it had the same ceiling; this makes it one
    #: number in one place, and exceeding it logs rather than silently truncating
    #: to a number that looks like a measurement.
    MAX_EVENTS_SCANNED: int = 10_000

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service

    # ------------------------------------------------------------------
    # Paired-event latency (shared by ack and driver-response)
    # ------------------------------------------------------------------

    async def _paired_event_latency(
        self,
        tenant_id: str,
        *,
        must_clauses: list,
        start_event_type: str,
        end_event_types: list,
        interval: str,
        metric: str,
    ) -> dict:
        """Latency between the first ``start`` event and the first ``end`` event per job.

        Both callers previously expressed this as ``bucket_script`` subtracting two
        ``min`` sub-aggregations, wrapped in ``stats_bucket`` and ``avg_bucket``.
        Those are *pipeline* aggregations: Elasticsearch computes them over the
        output of other aggregations, and the Postgres document store refuses them
        (``UnsupportedAggregationError``). Both call sites caught the exception and
        returned ``{"buckets": [], "overall": {}}``, so after the cutover these
        endpoints would have reported no data at all — logged at ERROR, invisible to
        the caller.

        Pairing events per job is straightforward in Python and needs no pipeline
        aggregation, so that is what this does: fetch the matching events once, pair
        them by ``job_id``, and bucket the differences. The arithmetic is identical,
        including that the pair is ``min(start)`` to ``min(end)`` — the FIRST
        response, not the last — and that a job with only one half contributes
        nothing.
        """
        query = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                }
            },
            "size": self.MAX_EVENTS_SCANNED,
            "_source": ["job_id", "event_type", "event_timestamp"],
            "sort": [{"event_timestamp": {"order": "asc"}}],
        }

        try:
            response = await self._es.search_documents(
                JOB_EVENTS_INDEX, query, size=self.MAX_EVENTS_SCANNED
            )
        except Exception as exc:
            logger.error("Failed to compute %s: %s", metric, exc)
            return {"buckets": [], "overall": {}}

        hits = ((response or {}).get("hits") or {}).get("hits") or []
        total = (((response or {}).get("hits") or {}).get("total") or {})
        matched = total.get("value") if isinstance(total, dict) else total
        if isinstance(matched, int) and matched > self.MAX_EVENTS_SCANNED:
            # Said out loud, because a silently truncated latency metric is
            # indistinguishable from a genuinely fast day.
            logger.warning(
                "%s: %d matching events exceeds the %d scan cap; the metric covers "
                "the most recent events only",
                metric, matched, self.MAX_EVENTS_SCANNED,
            )

        first_start: dict = {}
        first_end: dict = {}
        for hit in hits:
            source = hit.get("_source") or {}
            job_id = source.get("job_id")
            stamp = _parse_timestamp(source.get("event_timestamp"))
            if not job_id or stamp is None:
                continue
            event_type = source.get("event_type")
            if event_type == start_event_type:
                if job_id not in first_start or stamp < first_start[job_id]:
                    first_start[job_id] = stamp
            elif event_type in end_event_types:
                if job_id not in first_end or stamp < first_end[job_id]:
                    first_end[job_id] = stamp

        # ``(latency_ms, bucket_key)`` per job that has both halves.
        paired: list = []
        for job_id, start in first_start.items():
            end = first_end.get(job_id)
            if end is None:
                continue
            latency_ms = (end - start).total_seconds() * 1000
            paired.append((latency_ms, _bucket_key(end, interval)))

        latencies = [latency for latency, _bucket in paired]
        by_bucket: dict = {}
        for latency, bucket_key in paired:
            by_bucket.setdefault(bucket_key, []).append(latency)

        buckets = [
            {
                "timestamp": bucket_key,
                "doc_count": len(values),
                "avg_latency_seconds": round(sum(values) / len(values) / 1000, 2),
            }
            for bucket_key, values in sorted(by_bucket.items())
        ]

        return {
            "buckets": buckets,
            "overall": {
                "avg_seconds": (
                    round(sum(latencies) / len(latencies) / 1000, 2)
                    if latencies
                    else None
                ),
                "min_seconds": round(min(latencies) / 1000, 2) if latencies else None,
                "max_seconds": round(max(latencies) / 1000, 2) if latencies else None,
                "count": len(latencies),
            },
        }

    async def compute_ack_latency(
        self,
        tenant_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        interval: str = "1d",
    ) -> dict:
        """Compute time between job assignment and driver ack.

        Queries the job_events index for pairs of 'assignment' and 'ack'
        events on the same job, then aggregates the time difference by
        time bucket.

        Validates: Requirements 13.1

        Args:
            tenant_id: Tenant scope.
            start_date: Optional ISO 8601 start date filter.
            end_date: Optional ISO 8601 end date filter.
            interval: Date histogram interval (default '1d').

        Returns:
            Dict with 'buckets' list containing time bucket, avg/min/max
            latency in seconds, and doc_count.
        """
        must_clauses = [
            {"terms": {"event_type": ["assignment", "ack"]}},
        ]

        if start_date or end_date:
            time_range: dict = {}
            if start_date:
                time_range["gte"] = start_date
            if end_date:
                time_range["lte"] = end_date
            must_clauses.append({"range": {"event_timestamp": time_range}})

        return await self._paired_event_latency(
            tenant_id,
            must_clauses=must_clauses,
            start_event_type="assignment",
            end_event_types=["ack"],
            interval=interval,
            metric="ack_latency",
        )

    async def compute_notification_send_latency(
        self,
        tenant_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        interval: str = "1d",
    ) -> dict:
        """Compute time between notification creation and dispatch.

        Queries the notifications_current index for notifications that
        have both created_at and sent_at timestamps, then aggregates
        the difference by channel and time bucket.

        Validates: Requirements 13.2

        Args:
            tenant_id: Tenant scope.
            start_date: Optional ISO 8601 start date filter.
            end_date: Optional ISO 8601 end date filter.
            interval: Date histogram interval (default '1d').

        Returns:
            Dict with 'by_channel' and 'buckets' containing latency stats.
        """
        must_clauses = [
            {"exists": {"field": "sent_at"}},
            {"exists": {"field": "created_at"}},
        ]

        if start_date or end_date:
            time_range: dict = {}
            if start_date:
                time_range["gte"] = start_date
            if end_date:
                time_range["lte"] = end_date
            must_clauses.append({"range": {"created_at": time_range}})

        # The latency used to be computed by a painless ``runtime_mappings`` field
        # and aggregated with ``stats``. The Postgres store reads no
        # ``runtime_mappings``, and — this is the part worth naming — it did not
        # complain: the key was dropped, ``stats`` ran against a field that does not
        # exist, and the endpoint reported zero seconds of send latency as though it
        # had measured it. Subtracting two stored timestamps in Python is the same
        # arithmetic with no runtime field to lose.
        query = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                }
            },
            "size": self.MAX_EVENTS_SCANNED,
            "_source": ["channel", "created_at", "sent_at"],
            "sort": [{"created_at": {"order": "asc"}}],
        }

        try:
            response = await self._es.search_documents(
                NOTIFICATIONS_CURRENT_INDEX, query, size=self.MAX_EVENTS_SCANNED
            )
        except Exception as exc:
            logger.error("Failed to compute notification_send_latency: %s", exc)
            return {"by_channel": {}, "buckets": []}

        hits = ((response or {}).get("hits") or {}).get("hits") or []

        # ``channel -> [latency_ms]`` and ``(bucket, channel) -> [latency_ms]``.
        per_channel: dict = {}
        per_bucket_channel: dict = {}
        bucket_doc_counts: dict = {}
        for hit in hits:
            source = hit.get("_source") or {}
            created = _parse_timestamp(source.get("created_at"))
            sent = _parse_timestamp(source.get("sent_at"))
            if created is None or sent is None:
                # The painless field emitted nothing unless both were present, so a
                # half-timestamped notification contributed to no statistic.
                continue
            channel = source.get("channel")
            latency_ms = (sent - created).total_seconds() * 1000
            per_channel.setdefault(channel, []).append(latency_ms)
            # Bucketed on ``created_at``, matching the original date_histogram
            # field — not on ``sent_at``, which would move a notification into a
            # later bucket than the one it was created in.
            bucket_key = _bucket_key(created, interval)
            per_bucket_channel.setdefault(bucket_key, {}).setdefault(
                channel, []
            ).append(latency_ms)
            bucket_doc_counts[bucket_key] = bucket_doc_counts.get(bucket_key, 0) + 1

        by_channel = {
            channel: {
                "avg_seconds": round(sum(values) / len(values) / 1000, 2),
                "min_seconds": round(min(values) / 1000, 2),
                "max_seconds": round(max(values) / 1000, 2),
                "count": len(values),
            }
            for channel, values in per_channel.items()
        }

        buckets = [
            {
                "timestamp": bucket_key,
                "doc_count": bucket_doc_counts[bucket_key],
                "by_channel": {
                    channel: {
                        "avg_seconds": round(sum(values) / len(values) / 1000, 2),
                        "count": len(values),
                    }
                    for channel, values in channels.items()
                },
            }
            for bucket_key, channels in sorted(per_bucket_channel.items())
        ]

        return {
            "by_channel": by_channel,
            "buckets": buckets,
        }

    async def compute_driver_response_latency(
        self,
        tenant_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        interval: str = "1d",
    ) -> dict:
        """Compute time between assignment and accept/reject.

        Queries the job_events index for pairs of 'assignment' and
        'accept'/'reject' events on the same job, then aggregates the
        time difference by time bucket.

        Validates: Requirements 13.3

        Args:
            tenant_id: Tenant scope.
            start_date: Optional ISO 8601 start date filter.
            end_date: Optional ISO 8601 end date filter.
            interval: Date histogram interval (default '1d').

        Returns:
            Dict with 'buckets' list and 'overall' stats.
        """
        must_clauses = [
            {"terms": {"event_type": ["assignment", "accept", "reject"]}},
        ]

        if start_date or end_date:
            time_range: dict = {}
            if start_date:
                time_range["gte"] = start_date
            if end_date:
                time_range["lte"] = end_date
            must_clauses.append({"range": {"event_timestamp": time_range}})

        return await self._paired_event_latency(
            tenant_id,
            must_clauses=must_clauses,
            start_event_type="assignment",
            end_event_types=["accept", "reject"],
            interval=interval,
            metric="driver_response_latency",
        )

    async def compute_failed_notification_rate(
        self,
        tenant_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        interval: str = "1d",
    ) -> dict:
        """Compute ratio of failed to total notifications by channel.

        Queries the notifications_current index and aggregates by channel,
        counting total and failed notifications to compute the failure rate.

        Validates: Requirements 13.4

        Args:
            tenant_id: Tenant scope.
            start_date: Optional ISO 8601 start date filter.
            end_date: Optional ISO 8601 end date filter.
            interval: Date histogram interval (default '1d').

        Returns:
            Dict with 'by_channel' failure rates and 'buckets' over time.
        """
        must_clauses = []

        if start_date or end_date:
            time_range: dict = {}
            if start_date:
                time_range["gte"] = start_date
            if end_date:
                time_range["lte"] = end_date
            must_clauses.append({"range": {"created_at": time_range}})

        query = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                }
            },
            "size": 0,
            "aggs": {
                "by_channel": {
                    "terms": {"field": "channel", "size": 50},
                    "aggs": {
                        "total": {"value_count": {"field": "notification_id"}},
                        "failed": {
                            "filter": {
                                "terms": {
                                    "delivery_status": ["failed", "dead_letter"]
                                }
                            },
                        },
                    },
                },
                "by_time_bucket": {
                    "date_histogram": {
                        "field": "created_at",
                        "fixed_interval": interval,
                    },
                    "aggs": {
                        "by_channel": {
                            "terms": {"field": "channel", "size": 50},
                            "aggs": {
                                "total": {
                                    "value_count": {"field": "notification_id"}
                                },
                                "failed": {
                                    "filter": {
                                        "terms": {
                                            "delivery_status": [
                                                "failed",
                                                "dead_letter",
                                            ]
                                        }
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }

        try:
            response = await self._es.search_documents(
                NOTIFICATIONS_CURRENT_INDEX, query, size=0
            )
        except Exception as exc:
            logger.error("Failed to compute failed_notification_rate: %s", exc)
            return {"by_channel": {}, "buckets": []}

        aggs = response.get("aggregations", {})

        by_channel = {}
        for bucket in aggs.get("by_channel", {}).get("buckets", []):
            total = bucket.get("total", {}).get("value", 0)
            failed = bucket.get("failed", {}).get("doc_count", 0)
            rate = round(failed / total, 4) if total > 0 else 0.0
            by_channel[bucket["key"]] = {
                "total": total,
                "failed": failed,
                "rate": rate,
            }

        buckets = []
        for bucket in aggs.get("by_time_bucket", {}).get("buckets", []):
            channel_data = {}
            for ch_bucket in bucket.get("by_channel", {}).get("buckets", []):
                total = ch_bucket.get("total", {}).get("value", 0)
                failed = ch_bucket.get("failed", {}).get("doc_count", 0)
                rate = round(failed / total, 4) if total > 0 else 0.0
                channel_data[ch_bucket["key"]] = {
                    "total": total,
                    "failed": failed,
                    "rate": rate,
                }
            buckets.append({
                "timestamp": bucket.get("key_as_string", bucket.get("key")),
                "doc_count": bucket.get("doc_count", 0),
                "by_channel": channel_data,
            })

        return {
            "by_channel": by_channel,
            "buckets": buckets,
        }

    async def get_all_metrics(
        self,
        tenant_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        interval: str = "1d",
    ) -> dict:
        """Compute all communication SLA metrics.

        Convenience method that calls all four metric computations and
        returns them in a single response.

        Validates: Requirements 13.5

        Args:
            tenant_id: Tenant scope.
            start_date: Optional ISO 8601 start date filter.
            end_date: Optional ISO 8601 end date filter.
            interval: Date histogram interval (default '1d').

        Returns:
            Dict with all four metric categories.
        """
        ack_latency = await self.compute_ack_latency(
            tenant_id, start_date, end_date, interval
        )
        notification_send_latency = await self.compute_notification_send_latency(
            tenant_id, start_date, end_date, interval
        )
        driver_response_latency = await self.compute_driver_response_latency(
            tenant_id, start_date, end_date, interval
        )
        failed_notification_rate = await self.compute_failed_notification_rate(
            tenant_id, start_date, end_date, interval
        )

        return {
            "ack_latency": ack_latency,
            "notification_send_latency": notification_send_latency,
            "driver_response_latency": driver_response_latency,
            "failed_notification_rate": failed_notification_rate,
        }
