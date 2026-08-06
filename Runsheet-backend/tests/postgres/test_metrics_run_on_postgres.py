"""The four communication metrics and the inventory summary, over the real store.

All five used Elasticsearch features the Postgres document store does not
implement, and they failed in three different ways — which is the point of testing
them against the store rather than against a mock:

* ``compute_ack_latency`` and ``compute_driver_response_latency`` used
  ``bucket_script`` / ``stats_bucket`` / ``avg_bucket``. Those raise
  ``UnsupportedAggregationError``, and both methods caught the exception and
  returned ``{"buckets": [], "overall": {}}`` — an empty metric, logged at ERROR
  and invisible to the caller.
* ``compute_notification_send_latency`` computed its latency in a painless
  ``runtime_mappings`` field. The store never read the key, so ``stats`` ran over a
  field that did not exist and the endpoint reported **zero seconds of send
  latency** as though it had measured it. No error anywhere.
* ``InventoryService.get_summary`` used a script-valued ``sum`` and has no
  ``except``, so it was a 500.

The assertions are therefore about numbers, not about the absence of exceptions: a
metric that returns an empty result without raising is the failure mode here, and
``assert not raises`` would pass on it.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

TENANT = "demo-tenant"


class _PostgresBackedFacade:
    """``ElasticsearchService`` with ``_pg_store()`` pinned to the test store."""

    from services.elasticsearch_service import ElasticsearchService as _Real

    search_documents = _Real.search_documents
    index_document = _Real.index_document
    get_document = _Real.get_document
    _is_retired_index = _Real._is_retired_index
    del _Real

    def __init__(self, store: Any) -> None:
        self._store = store

    def _pg_store(self) -> Any:
        return self._store


def _job_event(event_id: str, job_id: str, event_type: str, stamp: str) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "job_id": job_id,
        "tenant_id": TENANT,
        "event_type": event_type,
        "event_timestamp": stamp,
    }


# ---------------------------------------------------------------------------
# Paired-event latency
# ---------------------------------------------------------------------------


@pytest.fixture
def metrics(store, monkeypatch, index_name):
    from notifications.services import communication_metrics_service as module

    monkeypatch.setattr(module, "JOB_EVENTS_INDEX", index_name)
    monkeypatch.setattr(module, "NOTIFICATIONS_CURRENT_INDEX", index_name)
    return module.CommunicationMetricsService(_PostgresBackedFacade(store))


class TestAckLatency:
    async def test_it_measures_the_gap_between_assignment_and_ack(
        self, metrics, store, index_name
    ):
        """A number, not an empty result.

        Two events 90 seconds apart. Asserting the value is what distinguishes this
        from the old behaviour, which raised internally and returned no buckets at
        all without failing.
        """
        for event in (
            _job_event("e1", "JOB-1", "assignment", "2026-01-01T00:00:00+00:00"),
            _job_event("e2", "JOB-1", "ack", "2026-01-01T00:01:30+00:00"),
        ):
            await store.index_document(index_name, event["event_id"], event)

        result = await metrics.compute_ack_latency(TENANT)

        assert result["overall"]["count"] == 1
        assert result["overall"]["avg_seconds"] == 90.0
        assert result["buckets"] == [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "doc_count": 1,
                "avg_latency_seconds": 90.0,
            }
        ]

    async def test_a_job_with_no_ack_contributes_nothing(
        self, metrics, store, index_name
    ):
        """``bucket_script`` produced no value when a buckets_path was absent, so an
        unacknowledged job was excluded rather than counted as zero latency —
        counting it would make the average look better the worse things got."""
        event = _job_event("e1", "JOB-1", "assignment", "2026-01-01T00:00:00+00:00")
        await store.index_document(index_name, "e1", event)

        result = await metrics.compute_ack_latency(TENANT)

        assert result["overall"]["count"] == 0
        assert result["buckets"] == []

    async def test_it_pairs_the_first_ack_not_the_last(
        self, metrics, store, index_name
    ):
        """Both halves were ``min``, so a duplicate ack must not move the number."""
        for event in (
            _job_event("e1", "JOB-1", "assignment", "2026-01-01T00:00:00+00:00"),
            _job_event("e2", "JOB-1", "ack", "2026-01-01T00:00:10+00:00"),
            _job_event("e3", "JOB-1", "ack", "2026-01-01T00:05:00+00:00"),
        ):
            await store.index_document(index_name, event["event_id"], event)

        result = await metrics.compute_ack_latency(TENANT)

        assert result["overall"]["avg_seconds"] == 10.0

    async def test_min_max_and_avg_across_jobs(self, metrics, store, index_name):
        for job, seconds in (("JOB-1", 10), ("JOB-2", 30), ("JOB-3", 50)):
            await store.index_document(
                index_name,
                f"{job}-a",
                _job_event(f"{job}-a", job, "assignment", "2026-01-01T00:00:00+00:00"),
            )
            await store.index_document(
                index_name,
                f"{job}-b",
                _job_event(
                    f"{job}-b", job, "ack", f"2026-01-01T00:00:{seconds:02d}+00:00"
                ),
            )

        result = await metrics.compute_ack_latency(TENANT)

        assert result["overall"] == {
            "avg_seconds": 30.0,
            "min_seconds": 10.0,
            "max_seconds": 50.0,
            "count": 3,
        }

    async def test_it_does_not_pair_across_tenants(self, metrics, store, index_name):
        """A tenant filter dropped from a metric leaks another tenant's operations."""
        mine = _job_event("e1", "JOB-1", "assignment", "2026-01-01T00:00:00+00:00")
        theirs = _job_event("e2", "JOB-1", "ack", "2026-01-01T00:10:00+00:00")
        theirs["tenant_id"] = "other-tenant"
        await store.index_document(index_name, "e1", mine)
        await store.index_document(index_name, "e2", theirs)

        result = await metrics.compute_ack_latency(TENANT)

        assert result["overall"]["count"] == 0

    async def test_buckets_split_on_the_interval(self, metrics, store, index_name):
        for day in ("01", "03"):
            await store.index_document(
                index_name,
                f"a{day}",
                _job_event(f"a{day}", f"JOB-{day}", "assignment", f"2026-01-{day}T00:00:00+00:00"),
            )
            await store.index_document(
                index_name,
                f"b{day}",
                _job_event(f"b{day}", f"JOB-{day}", "ack", f"2026-01-{day}T00:00:20+00:00"),
            )

        result = await metrics.compute_ack_latency(TENANT, interval="1d")

        assert [bucket["timestamp"] for bucket in result["buckets"]] == [
            "2026-01-01T00:00:00Z",
            "2026-01-03T00:00:00Z",
        ]


class TestDriverResponseLatency:
    @pytest.mark.parametrize("response_type", ["accept", "reject"])
    async def test_either_response_closes_the_pair(
        self, metrics, store, index_name, response_type
    ):
        """A reject is a response: excluding it would report only the drivers who
        said yes, which flatters the metric."""
        for event in (
            _job_event("e1", "JOB-1", "assignment", "2026-01-01T00:00:00+00:00"),
            _job_event("e2", "JOB-1", response_type, "2026-01-01T00:00:45+00:00"),
        ):
            await store.index_document(index_name, event["event_id"], event)

        result = await metrics.compute_driver_response_latency(TENANT)

        assert result["overall"]["count"] == 1
        assert result["overall"]["avg_seconds"] == 45.0

    async def test_an_ack_is_not_a_response(self, metrics, store, index_name):
        """The two metrics measure different things and must not borrow each
        other's events."""
        for event in (
            _job_event("e1", "JOB-1", "assignment", "2026-01-01T00:00:00+00:00"),
            _job_event("e2", "JOB-1", "ack", "2026-01-01T00:00:45+00:00"),
        ):
            await store.index_document(index_name, event["event_id"], event)

        result = await metrics.compute_driver_response_latency(TENANT)

        assert result["overall"]["count"] == 0


# ---------------------------------------------------------------------------
# Notification send latency — the silent-zero one
# ---------------------------------------------------------------------------


def _notification(notification_id: str, channel: str, created: str, sent) -> Dict[str, Any]:
    document = {
        "notification_id": notification_id,
        "tenant_id": TENANT,
        "channel": channel,
        "created_at": created,
        "delivery_status": "sent",
    }
    if sent is not None:
        document["sent_at"] = sent
    return document


class TestNotificationSendLatency:
    async def test_it_reports_a_real_latency_not_zero(
        self, metrics, store, index_name
    ):
        """The regression that had no error attached to it.

        With ``runtime_mappings`` dropped, ``stats`` aggregated a field that does not
        exist and every channel came back with nulls or zeros — a dashboard reading
        "0s" would have been reporting a field that was never computed.
        """
        await store.index_document(
            index_name,
            "n1",
            _notification("n1", "sms", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:05+00:00"),
        )

        result = await metrics.compute_notification_send_latency(TENANT)

        assert result["by_channel"]["sms"]["avg_seconds"] == 5.0
        assert result["by_channel"]["sms"]["count"] == 1

    async def test_channels_are_reported_separately(self, metrics, store, index_name):
        await store.index_document(
            index_name,
            "n1",
            _notification("n1", "sms", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:02+00:00"),
        )
        await store.index_document(
            index_name,
            "n2",
            _notification("n2", "email", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:20+00:00"),
        )

        result = await metrics.compute_notification_send_latency(TENANT)

        assert result["by_channel"]["sms"]["avg_seconds"] == 2.0
        assert result["by_channel"]["email"]["avg_seconds"] == 20.0

    async def test_an_unsent_notification_is_excluded(
        self, metrics, store, index_name
    ):
        """The painless field emitted nothing without both timestamps, so a queued
        notification contributed to no statistic rather than counting as zero."""
        await store.index_document(
            index_name, "n1", _notification("n1", "sms", "2026-01-01T00:00:00+00:00", None)
        )

        result = await metrics.compute_notification_send_latency(TENANT)

        assert result["by_channel"] == {}
        assert result["buckets"] == []

    async def test_buckets_are_keyed_on_created_at(self, metrics, store, index_name):
        """Matching the original ``date_histogram`` field. Bucketing on ``sent_at``
        would move a slow notification into a later bucket than the one it belongs
        to, so a backlog would appear to have happened later than it did."""
        await store.index_document(
            index_name,
            "n1",
            _notification(
                "n1", "sms", "2026-01-01T23:59:00+00:00", "2026-01-02T00:01:00+00:00"
            ),
        )

        result = await metrics.compute_notification_send_latency(TENANT)

        assert [bucket["timestamp"] for bucket in result["buckets"]] == [
            "2026-01-01T00:00:00Z"
        ]


class TestFailedNotificationRate:
    """This one needed no change — its aggregations were already supported. Pinned
    so a later rewrite of its neighbours cannot quietly break it."""

    async def test_the_rate_is_failed_over_total(self, metrics, store, index_name):
        for index, status in enumerate(["sent", "failed", "dead_letter", "sent"]):
            document = _notification(
                f"n{index}", "sms", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"
            )
            document["delivery_status"] = status
            await store.index_document(index_name, f"n{index}", document)

        result = await metrics.compute_failed_notification_rate(TENANT)

        assert result["by_channel"]["sms"]["total"] == 4
        assert result["by_channel"]["sms"]["failed"] == 2
        assert result["by_channel"]["sms"]["rate"] == 0.5


# ---------------------------------------------------------------------------
# Inventory summary — the 500
# ---------------------------------------------------------------------------


class TestInventorySummary:
    @pytest.fixture
    def service(self, store, monkeypatch, index_name):
        from inventory import service as module

        monkeypatch.setattr(module, "INVENTORY_INDEX", index_name)
        return module.InventoryService(_PostgresBackedFacade(store))

    async def _seed(self, store, index_name, items):
        for index, (quantity, unit_cost, status) in enumerate(items):
            document = {
                "item_id": f"INV_{index}",
                "tenant_id": TENANT,
                "name": f"Item {index}",
                "category": "fuel",
                "status": status,
                "quantity": quantity,
                "min_threshold": 1,
            }
            if unit_cost is not None:
                document["unit_cost"] = unit_cost
            await store.index_document(index_name, document["item_id"], document)

    async def test_total_value_is_quantity_times_unit_cost(
        self, service, store, index_name
    ):
        """Was a painless script-valued ``sum``, which the store refuses; this
        method has no ``except``, so the endpoint was a 500."""
        await self._seed(store, index_name, [(10, 2.5, "in_stock"), (4, 1.25, "in_stock")])

        summary = await service.get_summary(TENANT)

        assert summary.total_items == 2
        assert summary.total_value == pytest.approx(30.0)

    async def test_a_missing_unit_cost_counts_as_zero_not_as_a_skipped_item(
        self, service, store, index_name
    ):
        """What the painless ternary did. Skipping the row instead would also drop
        it from ``total_items``, which is a different number."""
        await self._seed(store, index_name, [(10, None, "in_stock")])

        summary = await service.get_summary(TENANT)

        assert summary.total_items == 1
        assert summary.total_value == 0.0

    async def test_status_and_category_counts_still_come_from_aggregations(
        self, service, store, index_name
    ):
        await self._seed(
            store,
            index_name,
            [(10, 1.0, "in_stock"), (0, 1.0, "out_of_stock"), (1, 1.0, "low_stock")],
        )

        summary = await service.get_summary(TENANT)

        assert summary.in_stock == 1
        assert summary.out_of_stock == 1
        assert summary.low_stock == 1
        assert summary.categories == {"fuel": 3}
