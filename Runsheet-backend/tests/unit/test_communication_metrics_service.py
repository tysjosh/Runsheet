"""
Unit tests for CommunicationMetricsService — communication SLA metrics.

Tests all four metric computations and the combined get_all_metrics method
against a mocked ElasticsearchService.

Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from notifications.services.communication_metrics_service import (
    CommunicationMetricsService,
)
from notifications.services.notification_es_mappings import NOTIFICATIONS_CURRENT_INDEX
from scheduling.services.scheduling_es_mappings import JOB_EVENTS_INDEX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_es_mock() -> MagicMock:
    """Return a mock ElasticsearchService with default async methods."""
    es = MagicMock()
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}, "aggregations": {}}
    )
    return es


def _respond_with(*documents: dict) -> AsyncMock:
    """A search response carrying ``documents`` as hits.

    The latency metrics used to ask Elasticsearch for ``bucket_script`` and
    ``avg_bucket`` pipeline aggregations, and one of them computed its latency in a
    painless ``runtime_mappings`` field. The Postgres document store implements
    neither: the pipelines raised (and were caught, yielding an empty metric) and
    the runtime field was silently dropped, so ``stats`` aggregated a field that did
    not exist and the endpoint reported zero seconds as though measured.

    The pairing and subtraction happen in Python now, so these tests supply events
    rather than pre-aggregated numbers — which also means they exercise the
    arithmetic instead of the response parsing.
    """
    return AsyncMock(
        return_value={
            "hits": {
                "hits": [{"_source": document} for document in documents],
                "total": {"value": len(documents)},
            }
        }
    )


def _job_event(job_id: str, event_type: str, stamp: str) -> dict:
    return {"job_id": job_id, "event_type": event_type, "event_timestamp": stamp}


def _notification(channel: str, created: str, sent: str) -> dict:
    return {"channel": channel, "created_at": created, "sent_at": sent}


def _pair(job_id: str, start_type: str, end_type: str, seconds: int) -> list:
    """An event pair ``seconds`` apart, on 2026-01-01."""
    return [
        _job_event(job_id, start_type, "2026-01-01T00:00:00+00:00"),
        _job_event(job_id, end_type, f"2026-01-01T00:00:{seconds:02d}+00:00"),
    ]


# ---------------------------------------------------------------------------
# compute_ack_latency
# ---------------------------------------------------------------------------


class TestComputeAckLatency:
    """Tests for CommunicationMetricsService.compute_ack_latency."""

    async def test_queries_job_events_index(self):
        """compute_ack_latency queries the job_events index."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_ack_latency("tenant-1")

        call_args = es.search_documents.call_args
        assert call_args[0][0] == JOB_EVENTS_INDEX

    async def test_filters_by_tenant_and_event_types(self):
        """compute_ack_latency filters by tenant_id and assignment/ack event types."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_ack_latency("tenant-1")

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]
        filter_clause = query["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "tenant-1"}} in filter_clause
        assert {"terms": {"event_type": ["assignment", "ack"]}} in must

    async def test_applies_date_range_filter(self):
        """compute_ack_latency applies start_date and end_date filters."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_ack_latency(
            "tenant-1",
            start_date="2025-01-01T00:00:00Z",
            end_date="2025-01-31T23:59:59Z",
        )

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]

        time_range_clause = None
        for clause in must:
            if "range" in clause:
                time_range_clause = clause
                break

        assert time_range_clause is not None
        assert time_range_clause["range"]["event_timestamp"]["gte"] == "2025-01-01T00:00:00Z"
        assert time_range_clause["range"]["event_timestamp"]["lte"] == "2025-01-31T23:59:59Z"

    async def test_returns_overall_stats(self):
        """compute_ack_latency returns overall latency statistics."""
        es = _make_es_mock()
        es.search_documents = _respond_with(
            *_pair("JOB-1", "assignment", "ack", 10),
            *_pair("JOB-2", "assignment", "ack", 30),
            *_pair("JOB-3", "assignment", "ack", 50),
        )
        service = CommunicationMetricsService(es)

        result = await service.compute_ack_latency("tenant-1")

        assert result["overall"]["avg_seconds"] == 30.0
        assert result["overall"]["min_seconds"] == 10.0
        assert result["overall"]["max_seconds"] == 50.0
        assert result["overall"]["count"] == 3

    async def test_returns_time_buckets(self):
        """compute_ack_latency returns time-bucketed latency data."""
        es = _make_es_mock()
        es.search_documents = _respond_with(
            _job_event("JOB-1", "assignment", "2026-01-01T00:00:00+00:00"),
            _job_event("JOB-1", "ack", "2026-01-01T00:00:25+00:00"),
            _job_event("JOB-2", "assignment", "2026-01-02T00:00:00+00:00"),
            _job_event("JOB-2", "ack", "2026-01-02T00:00:35+00:00"),
        )
        service = CommunicationMetricsService(es)

        result = await service.compute_ack_latency("tenant-1")

        assert len(result["buckets"]) == 2
        assert result["buckets"][0]["timestamp"] == "2026-01-01T00:00:00Z"
        assert result["buckets"][0]["avg_latency_seconds"] == 25.0
        assert result["buckets"][1]["avg_latency_seconds"] == 35.0

    async def test_a_job_missing_one_half_of_the_pair_is_excluded(self):
        """``bucket_script`` produced no value when a buckets_path was missing.

        So an unacknowledged assignment contributed nothing rather than counting as
        zero latency — counting it would make the average improve as
        acknowledgements got worse.
        """
        es = _make_es_mock()
        es.search_documents = _respond_with(
            _job_event("JOB-1", "assignment", "2026-01-01T00:00:00+00:00"),
            *_pair("JOB-2", "assignment", "ack", 20),
        )
        service = CommunicationMetricsService(es)

        result = await service.compute_ack_latency("tenant-1")

        assert result["overall"]["count"] == 1
        assert result["overall"]["avg_seconds"] == 20.0

    async def test_handles_es_error_gracefully(self):
        """compute_ack_latency returns empty result on ES error."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(side_effect=Exception("ES error"))
        service = CommunicationMetricsService(es)

        result = await service.compute_ack_latency("tenant-1")

        assert result == {"buckets": [], "overall": {}}

    async def test_it_fetches_only_the_three_fields_it_pairs_on(self):
        """Was ``size: 0`` with pipeline aggregations; now it reads the events.

        ``_source`` is narrowed because job events carry an arbitrary payload, and
        pulling ten thousand full documents to subtract two timestamps would make a
        metric endpoint the heaviest read in the system.
        """
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_ack_latency("tenant-1")

        query = es.search_documents.call_args[0][1]
        assert query["size"] == CommunicationMetricsService.MAX_EVENTS_SCANNED
        assert set(query["_source"]) == {"job_id", "event_type", "event_timestamp"}
        assert query["sort"] == [{"event_timestamp": {"order": "asc"}}]

    async def test_it_warns_rather_than_truncating_silently(self, caplog):
        """Past the scan cap the metric covers part of the range.

        A quietly truncated latency metric is indistinguishable from a genuinely
        fast day, so it says so.
        """
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": event} for event in _pair("JOB-1", "assignment", "ack", 10)],
                    "total": {"value": CommunicationMetricsService.MAX_EVENTS_SCANNED + 1},
                }
            }
        )
        service = CommunicationMetricsService(es)

        with caplog.at_level("WARNING"):
            await service.compute_ack_latency("tenant-1")

        assert "scan cap" in caplog.text


# ---------------------------------------------------------------------------
# compute_notification_send_latency
# ---------------------------------------------------------------------------


class TestComputeNotificationSendLatency:
    """Tests for CommunicationMetricsService.compute_notification_send_latency."""

    async def test_queries_notifications_index(self):
        """compute_notification_send_latency queries the notifications_current index."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_notification_send_latency("tenant-1")

        call_args = es.search_documents.call_args
        assert call_args[0][0] == NOTIFICATIONS_CURRENT_INDEX

    async def test_filters_by_tenant_and_sent_at_exists(self):
        """compute_notification_send_latency filters for notifications with sent_at."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_notification_send_latency("tenant-1")

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]
        filter_clause = query["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "tenant-1"}} in filter_clause
        assert {"exists": {"field": "sent_at"}} in must
        assert {"exists": {"field": "created_at"}} in must

    async def test_returns_by_channel_stats(self):
        """compute_notification_send_latency returns per-channel latency stats.

        The numbers are the assertion, and they are why this test matters: the
        painless ``runtime_mappings`` field this method used was silently dropped by
        the Postgres store, so every one of these came back null or zero with no
        error raised.
        """
        es = _make_es_mock()
        es.search_documents = _respond_with(
            _notification("sms", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
            _notification("sms", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:03+00:00"),
            _notification("email", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:03+00:00"),
        )
        service = CommunicationMetricsService(es)

        result = await service.compute_notification_send_latency("tenant-1")

        assert result["by_channel"]["sms"] == {
            "avg_seconds": 2.0,
            "min_seconds": 1.0,
            "max_seconds": 3.0,
            "count": 2,
        }
        assert result["by_channel"]["email"]["avg_seconds"] == 3.0

    async def test_applies_date_range_filter(self):
        """compute_notification_send_latency applies date range filter."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_notification_send_latency(
            "tenant-1", start_date="2025-01-01T00:00:00Z"
        )

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]

        time_range_clause = None
        for clause in must:
            if "range" in clause:
                time_range_clause = clause
                break

        assert time_range_clause is not None
        assert time_range_clause["range"]["created_at"]["gte"] == "2025-01-01T00:00:00Z"

    async def test_handles_es_error_gracefully(self):
        """compute_notification_send_latency returns empty result on ES error."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(side_effect=Exception("ES error"))
        service = CommunicationMetricsService(es)

        result = await service.compute_notification_send_latency("tenant-1")

        assert result == {"by_channel": {}, "buckets": []}

    async def test_returns_time_buckets_with_channel_breakdown(self):
        """compute_notification_send_latency returns time buckets with per-channel data."""
        es = _make_es_mock()
        es.search_documents = _respond_with(
            _notification("sms", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
            _notification("sms", "2026-01-01T06:00:00+00:00", "2026-01-01T06:00:03+00:00"),
        )
        service = CommunicationMetricsService(es)

        result = await service.compute_notification_send_latency("tenant-1")

        assert len(result["buckets"]) == 1
        bucket = result["buckets"][0]
        assert bucket["timestamp"] == "2026-01-01T00:00:00Z"
        assert bucket["doc_count"] == 2
        assert bucket["by_channel"]["sms"] == {"avg_seconds": 2.0, "count": 2}

    async def test_a_queued_notification_is_excluded(self):
        """The painless field emitted nothing without both timestamps, so a
        notification that never sent contributed to no statistic — rather than
        counting as zero latency, which would improve the metric."""
        es = _make_es_mock()
        es.search_documents = _respond_with(
            {"channel": "sms", "created_at": "2026-01-01T00:00:00+00:00"},
        )
        service = CommunicationMetricsService(es)

        result = await service.compute_notification_send_latency("tenant-1")

        assert result == {"by_channel": {}, "buckets": []}


# ---------------------------------------------------------------------------
# compute_driver_response_latency
# ---------------------------------------------------------------------------


class TestComputeDriverResponseLatency:
    """Tests for CommunicationMetricsService.compute_driver_response_latency."""

    async def test_queries_job_events_index(self):
        """compute_driver_response_latency queries the job_events index."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_driver_response_latency("tenant-1")

        call_args = es.search_documents.call_args
        assert call_args[0][0] == JOB_EVENTS_INDEX

    async def test_filters_by_assignment_accept_reject_events(self):
        """compute_driver_response_latency filters for assignment, accept, reject events."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_driver_response_latency("tenant-1")

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]
        filter_clause = query["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "tenant-1"}} in filter_clause
        assert {"terms": {"event_type": ["assignment", "accept", "reject"]}} in must

    async def test_returns_overall_stats(self):
        """compute_driver_response_latency returns overall latency statistics."""
        es = _make_es_mock()
        es.search_documents = _respond_with(
            *_pair("JOB-1", "assignment", "accept", 10),
            *_pair("JOB-2", "assignment", "reject", 20),
            *_pair("JOB-3", "assignment", "accept", 30),
        )
        service = CommunicationMetricsService(es)

        result = await service.compute_driver_response_latency("tenant-1")

        assert result["overall"]["avg_seconds"] == 20.0
        assert result["overall"]["min_seconds"] == 10.0
        assert result["overall"]["max_seconds"] == 30.0
        assert result["overall"]["count"] == 3

    async def test_a_reject_counts_as_a_response(self):
        """Excluding rejections would report only the drivers who said yes."""
        es = _make_es_mock()
        es.search_documents = _respond_with(*_pair("JOB-1", "assignment", "reject", 15))
        service = CommunicationMetricsService(es)

        result = await service.compute_driver_response_latency("tenant-1")

        assert result["overall"]["count"] == 1
        assert result["overall"]["avg_seconds"] == 15.0

    async def test_an_ack_is_not_a_response(self):
        """The two metrics measure different things and must not share events."""
        es = _make_es_mock()
        es.search_documents = _respond_with(*_pair("JOB-1", "assignment", "ack", 15))
        service = CommunicationMetricsService(es)

        result = await service.compute_driver_response_latency("tenant-1")

        assert result["overall"]["count"] == 0

    async def test_applies_date_range_filter(self):
        """compute_driver_response_latency applies date range filter."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_driver_response_latency(
            "tenant-1",
            start_date="2025-01-01T00:00:00Z",
            end_date="2025-01-31T23:59:59Z",
        )

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]

        time_range_clause = None
        for clause in must:
            if "range" in clause:
                time_range_clause = clause
                break

        assert time_range_clause is not None

    async def test_handles_es_error_gracefully(self):
        """compute_driver_response_latency returns empty result on ES error."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(side_effect=Exception("ES error"))
        service = CommunicationMetricsService(es)

        result = await service.compute_driver_response_latency("tenant-1")

        assert result == {"buckets": [], "overall": {}}


# ---------------------------------------------------------------------------
# compute_failed_notification_rate
# ---------------------------------------------------------------------------


class TestComputeFailedNotificationRate:
    """Tests for CommunicationMetricsService.compute_failed_notification_rate."""

    async def test_queries_notifications_index(self):
        """compute_failed_notification_rate queries the notifications_current index."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_failed_notification_rate("tenant-1")

        call_args = es.search_documents.call_args
        assert call_args[0][0] == NOTIFICATIONS_CURRENT_INDEX

    async def test_filters_by_tenant(self):
        """compute_failed_notification_rate filters by tenant_id."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_failed_notification_rate("tenant-1")

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        filter_clause = query["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "tenant-1"}} in filter_clause

    async def test_returns_by_channel_failure_rates(self):
        """compute_failed_notification_rate returns per-channel failure rates."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value={
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_channel": {
                    "buckets": [
                        {
                            "key": "sms",
                            "doc_count": 100,
                            "total": {"value": 100},
                            "failed": {"doc_count": 5},
                        },
                        {
                            "key": "email",
                            "doc_count": 50,
                            "total": {"value": 50},
                            "failed": {"doc_count": 10},
                        },
                    ]
                },
                "by_time_bucket": {"buckets": []},
            },
        })
        service = CommunicationMetricsService(es)

        result = await service.compute_failed_notification_rate("tenant-1")

        assert result["by_channel"]["sms"]["total"] == 100
        assert result["by_channel"]["sms"]["failed"] == 5
        assert result["by_channel"]["sms"]["rate"] == 0.05
        assert result["by_channel"]["email"]["total"] == 50
        assert result["by_channel"]["email"]["failed"] == 10
        assert result["by_channel"]["email"]["rate"] == 0.2

    async def test_handles_zero_total(self):
        """compute_failed_notification_rate handles zero total gracefully."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value={
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_channel": {
                    "buckets": [
                        {
                            "key": "sms",
                            "doc_count": 0,
                            "total": {"value": 0},
                            "failed": {"doc_count": 0},
                        },
                    ]
                },
                "by_time_bucket": {"buckets": []},
            },
        })
        service = CommunicationMetricsService(es)

        result = await service.compute_failed_notification_rate("tenant-1")

        assert result["by_channel"]["sms"]["rate"] == 0.0

    async def test_returns_time_buckets_with_channel_breakdown(self):
        """compute_failed_notification_rate returns time buckets with per-channel data."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value={
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_channel": {"buckets": []},
                "by_time_bucket": {
                    "buckets": [
                        {
                            "key_as_string": "2025-01-01T00:00:00.000Z",
                            "key": 1735689600000,
                            "doc_count": 50,
                            "by_channel": {
                                "buckets": [
                                    {
                                        "key": "sms",
                                        "doc_count": 30,
                                        "total": {"value": 30},
                                        "failed": {"doc_count": 3},
                                    },
                                    {
                                        "key": "email",
                                        "doc_count": 20,
                                        "total": {"value": 20},
                                        "failed": {"doc_count": 0},
                                    },
                                ]
                            },
                        }
                    ]
                },
            },
        })
        service = CommunicationMetricsService(es)

        result = await service.compute_failed_notification_rate("tenant-1")

        assert len(result["buckets"]) == 1
        bucket = result["buckets"][0]
        assert bucket["by_channel"]["sms"]["rate"] == 0.1
        assert bucket["by_channel"]["email"]["rate"] == 0.0

    async def test_applies_date_range_filter(self):
        """compute_failed_notification_rate applies date range filter."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.compute_failed_notification_rate(
            "tenant-1", start_date="2025-01-01T00:00:00Z"
        )

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]

        time_range_clause = None
        for clause in must:
            if "range" in clause:
                time_range_clause = clause
                break

        assert time_range_clause is not None
        assert time_range_clause["range"]["created_at"]["gte"] == "2025-01-01T00:00:00Z"

    async def test_handles_es_error_gracefully(self):
        """compute_failed_notification_rate returns empty result on ES error."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(side_effect=Exception("ES error"))
        service = CommunicationMetricsService(es)

        result = await service.compute_failed_notification_rate("tenant-1")

        assert result == {"by_channel": {}, "buckets": []}


# ---------------------------------------------------------------------------
# get_all_metrics
# ---------------------------------------------------------------------------


class TestGetAllMetrics:
    """Tests for CommunicationMetricsService.get_all_metrics."""

    async def test_returns_all_four_metric_categories(self):
        """get_all_metrics returns ack_latency, notification_send_latency,
        driver_response_latency, and failed_notification_rate."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        result = await service.get_all_metrics("tenant-1")

        assert "ack_latency" in result
        assert "notification_send_latency" in result
        assert "driver_response_latency" in result
        assert "failed_notification_rate" in result

    async def test_passes_date_range_to_all_metrics(self):
        """get_all_metrics passes start_date and end_date to all sub-methods."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.get_all_metrics(
            "tenant-1",
            start_date="2025-01-01T00:00:00Z",
            end_date="2025-01-31T23:59:59Z",
        )

        # Should have been called 4 times (once per metric)
        assert es.search_documents.call_count == 4

        # Each call should include the date range in the query
        for call in es.search_documents.call_args_list:
            query = call[0][1]
            must = query["query"]["bool"]["must"]
            has_range = any("range" in clause for clause in must)
            assert has_range, f"Missing date range filter in query: {query}"

    async def test_passes_interval_to_all_metrics(self):
        """get_all_metrics passes the interval parameter to all sub-methods."""
        es = _make_es_mock()
        service = CommunicationMetricsService(es)

        await service.get_all_metrics("tenant-1", interval="1h")

        # Verify interval is used in aggregation queries
        for call in es.search_documents.call_args_list:
            query = call[0][1]
            aggs = query.get("aggs", {})
            # Check for date_histogram with the correct interval
            found_interval = False
            for agg_name, agg_body in aggs.items():
                if "date_histogram" in agg_body:
                    assert agg_body["date_histogram"]["fixed_interval"] == "1h"
                    found_interval = True
                elif "aggs" in agg_body:
                    # Check nested aggs
                    for sub_name, sub_body in agg_body.get("aggs", {}).items():
                        if isinstance(sub_body, dict) and "date_histogram" in sub_body:
                            assert sub_body["date_histogram"]["fixed_interval"] == "1h"
                            found_interval = True
