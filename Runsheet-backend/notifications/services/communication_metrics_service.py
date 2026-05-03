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
from typing import Optional

from notifications.services.notification_es_mappings import NOTIFICATIONS_CURRENT_INDEX
from scheduling.services.scheduling_es_mappings import JOB_EVENTS_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class CommunicationMetricsService:
    """Computes communication SLA metrics from ES aggregations.

    Uses Elasticsearch aggregation queries to efficiently compute latency
    and failure rate metrics without fetching individual documents.

    Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5
    """

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service

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
            {"term": {"tenant_id": tenant_id}},
            {"terms": {"event_type": ["assignment", "ack"]}},
        ]

        if start_date or end_date:
            time_range: dict = {}
            if start_date:
                time_range["gte"] = start_date
            if end_date:
                time_range["lte"] = end_date
            must_clauses.append({"range": {"event_timestamp": time_range}})

        query = {
            "query": {"bool": {"must": must_clauses}},
            "size": 0,
            "aggs": {
                "by_job": {
                    "terms": {"field": "job_id", "size": 10000},
                    "aggs": {
                        "assignment_time": {
                            "filter": {"term": {"event_type": "assignment"}},
                            "aggs": {
                                "ts": {"min": {"field": "event_timestamp"}}
                            },
                        },
                        "ack_time": {
                            "filter": {"term": {"event_type": "ack"}},
                            "aggs": {
                                "ts": {"min": {"field": "event_timestamp"}}
                            },
                        },
                        "latency_ms": {
                            "bucket_script": {
                                "buckets_path": {
                                    "ack": "ack_time>ts",
                                    "assign": "assignment_time>ts",
                                },
                                "script": "params.ack - params.assign",
                            }
                        },
                    },
                },
                "latency_stats": {
                    "stats_bucket": {
                        "buckets_path": "by_job>latency_ms",
                    }
                },
                "by_time_bucket": {
                    "date_histogram": {
                        "field": "event_timestamp",
                        "fixed_interval": interval,
                    },
                    "aggs": {
                        "by_job_in_bucket": {
                            "terms": {"field": "job_id", "size": 10000},
                            "aggs": {
                                "assignment_time": {
                                    "filter": {"term": {"event_type": "assignment"}},
                                    "aggs": {
                                        "ts": {"min": {"field": "event_timestamp"}}
                                    },
                                },
                                "ack_time": {
                                    "filter": {"term": {"event_type": "ack"}},
                                    "aggs": {
                                        "ts": {"min": {"field": "event_timestamp"}}
                                    },
                                },
                                "latency_ms": {
                                    "bucket_script": {
                                        "buckets_path": {
                                            "ack": "ack_time>ts",
                                            "assign": "assignment_time>ts",
                                        },
                                        "script": "params.ack - params.assign",
                                    }
                                },
                            },
                        },
                        "avg_latency": {
                            "avg_bucket": {
                                "buckets_path": "by_job_in_bucket>latency_ms",
                            }
                        },
                    },
                },
            },
        }

        try:
            response = await self._es.search_documents(
                JOB_EVENTS_INDEX, query, size=0
            )
        except Exception as exc:
            logger.error("Failed to compute ack_latency: %s", exc)
            return {"buckets": [], "overall": {}}

        aggs = response.get("aggregations", {})
        overall_stats = aggs.get("latency_stats", {})
        time_buckets = aggs.get("by_time_bucket", {}).get("buckets", [])

        buckets = []
        for bucket in time_buckets:
            avg_val = bucket.get("avg_latency", {}).get("value")
            buckets.append({
                "timestamp": bucket.get("key_as_string", bucket.get("key")),
                "doc_count": bucket.get("doc_count", 0),
                "avg_latency_seconds": round(avg_val / 1000, 2) if avg_val is not None else None,
            })

        return {
            "buckets": buckets,
            "overall": {
                "avg_seconds": round(overall_stats.get("avg", 0) / 1000, 2) if overall_stats.get("avg") else None,
                "min_seconds": round(overall_stats.get("min", 0) / 1000, 2) if overall_stats.get("min") else None,
                "max_seconds": round(overall_stats.get("max", 0) / 1000, 2) if overall_stats.get("max") else None,
                "count": overall_stats.get("count", 0),
            },
        }

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
            {"term": {"tenant_id": tenant_id}},
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

        query = {
            "query": {"bool": {"must": must_clauses}},
            "size": 0,
            "runtime_mappings": {
                "send_latency_ms": {
                    "type": "long",
                    "script": {
                        "source": (
                            "if (doc['sent_at'].size() > 0 && doc['created_at'].size() > 0) {"
                            "  emit(doc['sent_at'].value.toInstant().toEpochMilli() "
                            "    - doc['created_at'].value.toInstant().toEpochMilli());"
                            "}"
                        )
                    },
                }
            },
            "aggs": {
                "by_channel": {
                    "terms": {"field": "channel", "size": 50},
                    "aggs": {
                        "latency_stats": {
                            "stats": {"field": "send_latency_ms"}
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
                                "latency_stats": {
                                    "stats": {"field": "send_latency_ms"}
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
            logger.error("Failed to compute notification_send_latency: %s", exc)
            return {"by_channel": {}, "buckets": []}

        aggs = response.get("aggregations", {})

        by_channel = {}
        for bucket in aggs.get("by_channel", {}).get("buckets", []):
            stats = bucket.get("latency_stats", {})
            by_channel[bucket["key"]] = {
                "avg_seconds": round(stats.get("avg", 0) / 1000, 2) if stats.get("avg") else None,
                "min_seconds": round(stats.get("min", 0) / 1000, 2) if stats.get("min") else None,
                "max_seconds": round(stats.get("max", 0) / 1000, 2) if stats.get("max") else None,
                "count": int(stats.get("count", 0)),
            }

        buckets = []
        for bucket in aggs.get("by_time_bucket", {}).get("buckets", []):
            channel_data = {}
            for ch_bucket in bucket.get("by_channel", {}).get("buckets", []):
                stats = ch_bucket.get("latency_stats", {})
                channel_data[ch_bucket["key"]] = {
                    "avg_seconds": round(stats.get("avg", 0) / 1000, 2) if stats.get("avg") else None,
                    "count": int(stats.get("count", 0)),
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
            {"term": {"tenant_id": tenant_id}},
            {"terms": {"event_type": ["assignment", "accept", "reject"]}},
        ]

        if start_date or end_date:
            time_range: dict = {}
            if start_date:
                time_range["gte"] = start_date
            if end_date:
                time_range["lte"] = end_date
            must_clauses.append({"range": {"event_timestamp": time_range}})

        query = {
            "query": {"bool": {"must": must_clauses}},
            "size": 0,
            "aggs": {
                "by_job": {
                    "terms": {"field": "job_id", "size": 10000},
                    "aggs": {
                        "assignment_time": {
                            "filter": {"term": {"event_type": "assignment"}},
                            "aggs": {
                                "ts": {"min": {"field": "event_timestamp"}}
                            },
                        },
                        "response_time": {
                            "filter": {
                                "terms": {"event_type": ["accept", "reject"]}
                            },
                            "aggs": {
                                "ts": {"min": {"field": "event_timestamp"}}
                            },
                        },
                        "latency_ms": {
                            "bucket_script": {
                                "buckets_path": {
                                    "resp": "response_time>ts",
                                    "assign": "assignment_time>ts",
                                },
                                "script": "params.resp - params.assign",
                            }
                        },
                    },
                },
                "latency_stats": {
                    "stats_bucket": {
                        "buckets_path": "by_job>latency_ms",
                    }
                },
                "by_time_bucket": {
                    "date_histogram": {
                        "field": "event_timestamp",
                        "fixed_interval": interval,
                    },
                    "aggs": {
                        "by_job_in_bucket": {
                            "terms": {"field": "job_id", "size": 10000},
                            "aggs": {
                                "assignment_time": {
                                    "filter": {"term": {"event_type": "assignment"}},
                                    "aggs": {
                                        "ts": {"min": {"field": "event_timestamp"}}
                                    },
                                },
                                "response_time": {
                                    "filter": {
                                        "terms": {"event_type": ["accept", "reject"]}
                                    },
                                    "aggs": {
                                        "ts": {"min": {"field": "event_timestamp"}}
                                    },
                                },
                                "latency_ms": {
                                    "bucket_script": {
                                        "buckets_path": {
                                            "resp": "response_time>ts",
                                            "assign": "assignment_time>ts",
                                        },
                                        "script": "params.resp - params.assign",
                                    }
                                },
                            },
                        },
                        "avg_latency": {
                            "avg_bucket": {
                                "buckets_path": "by_job_in_bucket>latency_ms",
                            }
                        },
                    },
                },
            },
        }

        try:
            response = await self._es.search_documents(
                JOB_EVENTS_INDEX, query, size=0
            )
        except Exception as exc:
            logger.error("Failed to compute driver_response_latency: %s", exc)
            return {"buckets": [], "overall": {}}

        aggs = response.get("aggregations", {})
        overall_stats = aggs.get("latency_stats", {})
        time_buckets = aggs.get("by_time_bucket", {}).get("buckets", [])

        buckets = []
        for bucket in time_buckets:
            avg_val = bucket.get("avg_latency", {}).get("value")
            buckets.append({
                "timestamp": bucket.get("key_as_string", bucket.get("key")),
                "doc_count": bucket.get("doc_count", 0),
                "avg_latency_seconds": round(avg_val / 1000, 2) if avg_val is not None else None,
            })

        return {
            "buckets": buckets,
            "overall": {
                "avg_seconds": round(overall_stats.get("avg", 0) / 1000, 2) if overall_stats.get("avg") else None,
                "min_seconds": round(overall_stats.get("min", 0) / 1000, 2) if overall_stats.get("min") else None,
                "max_seconds": round(overall_stats.get("max", 0) / 1000, 2) if overall_stats.get("max") else None,
                "count": overall_stats.get("count", 0),
            },
        }

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
        must_clauses = [
            {"term": {"tenant_id": tenant_id}},
        ]

        if start_date or end_date:
            time_range: dict = {}
            if start_date:
                time_range["gte"] = start_date
            if end_date:
                time_range["lte"] = end_date
            must_clauses.append({"range": {"created_at": time_range}})

        query = {
            "query": {"bool": {"must": must_clauses}},
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
