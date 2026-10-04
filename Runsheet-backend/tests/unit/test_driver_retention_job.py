"""
Unit tests for ``DriverRetentionJob`` — the per-data-class retention sweep.

What is asserted here is the part R10.16 and R10.20 actually constrain: one log
record per data class, each naming its ``data_class`` and its declared period;
one ``delete_by_query`` per class that has a period and **none** for
``driver_presence``; and the cutoff arithmetic per class, in the unit the class
declares.

Validates: Requirements 10.13, 10.16, 10.17, 10.18, 10.20
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from driver.services.driver_retention_job import (
    RETENTION_CLASSES,
    RETENTION_INTERVAL_SECONDS,
    RETENTION_LOG_EVENT,
    DriverRetentionJob,
    run_retention_cycle,
)
from services.no_cluster import ClusterRemovedError, NoClusterClient

RETENTION_LOGGER = "driver.services.driver_retention_job"

#: A fixed job start time with a day-of-month (31) that exercises the calendar
#: clamp on the month-based classes.
NOW = datetime(2026, 3, 31, 12, 30, 45, tzinfo=timezone.utc)


class UntouchableClient:
    """The removed cluster's client, recording any attempt to use it.

    Production's ``ElasticsearchService.client`` is a
    :class:`services.no_cluster.NoClusterClient`, whose data-plane methods raise
    :class:`ClusterRemovedError`. The sweep used to call
    ``client.delete_by_query`` and so failed every class, every day (F14).
    """

    def __init__(self) -> None:
        self.touched: List[str] = []
        self._inner = NoClusterClient()

    def __getattr__(self, name: str) -> Any:
        self.touched.append(name)
        return getattr(self._inner, name)


class FakeESService:
    """The service-level async ``delete_by_query`` the sweep must use.

    Records every call and returns a canned deleted count per index.
    """

    def __init__(self, deleted_by_index: Dict[str, int] | None = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._deleted = deleted_by_index or {}
        self.client = UntouchableClient()

    async def delete_by_query(self, index: str, query: Dict[str, Any]) -> int:
        self.calls.append({"index": index, "query": query})
        return self._deleted.get(index, 0)


@pytest.fixture
def es_service() -> FakeESService:
    return FakeESService()


@pytest.fixture
def job(es_service: FakeESService) -> DriverRetentionJob:
    return DriverRetentionJob(es_service=es_service)


def _records(caplog) -> List[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == RETENTION_LOGGER
        and record.getMessage().startswith(RETENTION_LOG_EVENT)
    ]


def _record_for(caplog, data_class: str) -> str:
    matches = [
        message
        for message in _records(caplog)
        if f"data_class={data_class} " in message
    ]
    assert len(matches) == 1, (
        f"expected exactly one record for {data_class}, got {matches}"
    )
    return matches[0]


def _cutoff_of(message: str) -> str:
    for token in message.split():
        if token.startswith("cutoff="):
            return token.split("=", 1)[1]
    raise AssertionError(f"no cutoff token in {message!r}")


# ---------------------------------------------------------------------------
# The declared table (R10.16)
# ---------------------------------------------------------------------------


class TestRetentionTable:
    def test_all_five_data_classes_are_enumerated(self):
        assert [spec.data_class for spec in RETENTION_CLASSES] == [
            "duty_status_event",
            "breadcrumb_sample",
            "driver_presence",
            "inspection_report",
            "idempotency_key",
        ]

    def test_driver_presence_is_enumerated_with_no_period(self):
        """R10.19 — one current record, so no period. Listed, not omitted."""
        presence = next(
            spec
            for spec in RETENTION_CLASSES
            if spec.data_class == "driver_presence"
        )
        assert presence.has_retention_period is False
        assert presence.period_token == "period=none"
        assert presence.cutoff(NOW) is None

    def test_declared_periods_and_anchors(self):
        declared = {
            spec.data_class: (spec.index, spec.anchor_field, spec.period_token)
            for spec in RETENTION_CLASSES
        }
        assert declared["duty_status_event"] == (
            "duty_status_events",
            "event_timestamp",
            "period_months=36",
        )
        assert declared["breadcrumb_sample"] == (
            "driver_breadcrumbs",
            "sample_timestamp",
            "period_days=90",
        )
        assert declared["inspection_report"] == (
            "vehicle_inspections",
            "inspection_timestamp",
            "period_months=15",
        )
        assert declared["idempotency_key"] == (
            "idempotency_keys",
            "created_at",
            "period_hours=24",
        )

    def test_cadence_is_at_most_24_hours(self):
        """R10.13 — at least once every 24 hours."""
        assert RETENTION_INTERVAL_SECONDS <= 24 * 60 * 60


# ---------------------------------------------------------------------------
# Cutoff arithmetic per class
# ---------------------------------------------------------------------------


class TestCutoffArithmetic:
    def test_duty_status_event_is_36_calendar_months(self):
        spec = next(
            s for s in RETENTION_CLASSES if s.data_class == "duty_status_event"
        )
        assert spec.cutoff(NOW) == NOW.replace(year=2023)

    def test_inspection_report_is_15_calendar_months_with_day_clamp(self):
        """31 March minus 15 months is 31 December; the clamp is a no-op here."""
        spec = next(
            s for s in RETENTION_CLASSES if s.data_class == "inspection_report"
        )
        assert spec.cutoff(NOW) == NOW.replace(year=2024, month=12, day=31)

    def test_month_arithmetic_clamps_to_the_shorter_month(self):
        spec = next(
            s for s in RETENTION_CLASSES if s.data_class == "inspection_report"
        )
        # 31 May minus 15 months is February, which has no 31st.
        may_31 = datetime(2026, 5, 31, 0, 0, tzinfo=timezone.utc)
        assert spec.cutoff(may_31) == datetime(
            2025, 2, 28, 0, 0, tzinfo=timezone.utc
        )

    def test_breadcrumb_sample_is_90_days(self):
        spec = next(
            s for s in RETENTION_CLASSES if s.data_class == "breadcrumb_sample"
        )
        assert spec.cutoff(NOW) == NOW - timedelta(days=90)

    def test_idempotency_key_is_24_hours(self):
        spec = next(
            s for s in RETENTION_CLASSES if s.data_class == "idempotency_key"
        )
        assert spec.cutoff(NOW) == NOW - timedelta(hours=24)


# ---------------------------------------------------------------------------
# One cycle: the queries issued and the records emitted
# ---------------------------------------------------------------------------


class TestRunCycle:
    @pytest.mark.asyncio
    async def test_one_log_record_per_data_class_naming_the_class(
        self, job, caplog
    ):
        """R10.20 — every record names its data class, and every class is named."""
        with caplog.at_level(logging.INFO, logger=RETENTION_LOGGER):
            await job.run_cycle(now=NOW)

        assert len(_records(caplog)) == len(RETENTION_CLASSES)
        for spec in RETENTION_CLASSES:
            record = _record_for(caplog, spec.data_class)
            assert record.startswith(f"{RETENTION_LOG_EVENT} ")
            assert spec.period_token in record
            assert "tenant_scope=all" in record

    @pytest.mark.asyncio
    async def test_the_sweep_uses_the_store_and_never_the_removed_cluster(
        self, job, es_service
    ):
        """F14: every class failed daily with ``ClusterRemovedError`` because the
        sweep called ``es.client.delete_by_query``."""
        results = await job.run_cycle(now=NOW)

        assert results == {
            "duty_status_event": 0,
            "breadcrumb_sample": 0,
            "driver_presence": 0,
            "inspection_report": 0,
            "idempotency_key": 0,
        }
        assert es_service.client.touched == []
        assert len(es_service.calls) == 4

    def test_the_double_really_is_the_removed_cluster(self, es_service):
        """Guards the test above: touching the client would have raised."""
        with pytest.raises(ClusterRemovedError):
            es_service.client.delete_by_query(index="x", body={})

    @pytest.mark.asyncio
    async def test_no_query_is_issued_for_driver_presence(
        self, job, es_service, caplog
    ):
        with caplog.at_level(logging.INFO, logger=RETENTION_LOGGER):
            await job.run_cycle(now=NOW)

        swept = [call["index"] for call in es_service.calls]
        assert "driver_presence" not in swept
        assert swept == [
            "duty_status_events",
            "driver_breadcrumbs",
            "vehicle_inspections",
            "idempotency_keys",
        ]
        # It is still accounted for in the log.
        assert "period=none" in _record_for(caplog, "driver_presence")
        assert "cutoff=none" in _record_for(caplog, "driver_presence")

    @pytest.mark.asyncio
    async def test_each_query_ranges_on_the_declared_anchor_and_cutoff(
        self, job, es_service, caplog
    ):
        with caplog.at_level(logging.INFO, logger=RETENTION_LOGGER):
            await job.run_cycle(now=NOW)

        by_index = {call["index"]: call["query"] for call in es_service.calls}
        for spec in RETENTION_CLASSES:
            if not spec.has_retention_period:
                continue
            # The clause itself, not an ES ``{"query": ...}`` body.
            range_clause = by_index[spec.index]["range"]
            assert list(range_clause) == [spec.anchor_field]
            logged_cutoff = _cutoff_of(_record_for(caplog, spec.data_class))
            # The queried cutoff and the logged cutoff are the same instant.
            assert range_clause[spec.anchor_field]["lt"] == logged_cutoff
            assert logged_cutoff.endswith("Z")

    @pytest.mark.asyncio
    async def test_deleted_count_is_reported_per_class(self, caplog):
        job = DriverRetentionJob(
            es_service=FakeESService({"idempotency_keys": 1204})
        )

        with caplog.at_level(logging.INFO, logger=RETENTION_LOGGER):
            results = await job.run_cycle(now=NOW)

        assert results["idempotency_key"] == 1204
        assert "deleted=1204" in _record_for(caplog, "idempotency_key")
        assert "deleted=0" in _record_for(caplog, "breadcrumb_sample")

    @pytest.mark.asyncio
    async def test_a_breadcrumb_deleted_count_is_reported_and_named(self, caplog):
        """The class deletes for real now that the index carries documents."""
        job = DriverRetentionJob(
            es_service=FakeESService({"driver_breadcrumbs": 8412})
        )

        with caplog.at_level(logging.INFO, logger=RETENTION_LOGGER):
            results = await job.run_cycle(now=NOW)

        assert results["breadcrumb_sample"] == 8412
        record = _record_for(caplog, "breadcrumb_sample")
        assert "deleted=8412" in record
        assert "period_days=90" in record

    @pytest.mark.asyncio
    async def test_a_failing_class_does_not_stop_the_others(self, caplog):
        class FailingService(FakeESService):
            async def delete_by_query(self, index: str, query):
                if index == "duty_status_events":
                    raise RuntimeError("statement timeout")
                return await super().delete_by_query(index, query)

        service = FailingService()
        job = DriverRetentionJob(es_service=service)

        with caplog.at_level(logging.INFO, logger=RETENTION_LOGGER):
            results = await job.run_cycle(now=NOW)

        assert results["duty_status_event"] is None
        assert results["idempotency_key"] == 0
        assert [call["index"] for call in service.calls] == [
            "driver_breadcrumbs",
            "vehicle_inspections",
            "idempotency_keys",
        ]

    @pytest.mark.asyncio
    async def test_run_retention_cycle_seam_sweeps_every_class(self, job, es_service):
        await run_retention_cycle(job)
        assert len(es_service.calls) == 4


# ---------------------------------------------------------------------------
# The breadcrumb class against documents of the shape telemetry writes
# ---------------------------------------------------------------------------


def _breadcrumb_doc(
    sample_timestamp: datetime, server_received_at: datetime
) -> Dict[str, Any]:
    """A ``driver_breadcrumbs`` document, field-for-field as telemetry writes it.

    ``sample_timestamp`` is the client's stamp and ``server_received_at`` sits
    beside it; the pair is the whole point of these tests.
    """
    epoch_ms = int(sample_timestamp.timestamp() * 1000)
    return {
        "breadcrumb_id": f"t_acme:drv_1:{epoch_ms}",
        "tenant_id": "t_acme",
        "driver_id": "drv_1",
        "location": {"lat": 6.45, "lon": 3.39},
        "sample_timestamp": sample_timestamp.isoformat(),
        "server_received_at": server_received_at.isoformat(),
        "accuracy_meters": 9.0,
        "speed_mph": 31.0,
        "heading_degrees": 180.0,
        "batch_id": "bcb_abc",
    }


class MatchingESService(FakeESService):
    """Applies the sweep's query to an in-memory document set.

    Uses :func:`persistence.document_matcher.matches`, the in-memory twin of the
    Postgres translation, so a string ``lt`` bound compares as text exactly as
    the store does. Answers *which documents would this sweep actually delete*,
    the question the anchor choice decides. The real-Postgres twin is in
    ``tests/postgres/test_document_store.py``.
    """

    def __init__(self, documents_by_index: Dict[str, List[Dict[str, Any]]]) -> None:
        super().__init__()
        self.remaining: Dict[str, List[Dict[str, Any]]] = {
            index: list(docs) for index, docs in documents_by_index.items()
        }

    async def delete_by_query(self, index: str, query: Dict[str, Any]) -> int:
        from persistence.document_matcher import matches

        self.calls.append({"index": index, "query": query})
        documents = self.remaining.get(index, [])
        kept = [document for document in documents if not matches(document, query)]
        self.remaining[index] = kept
        return len(documents) - len(kept)


class TestBreadcrumbSweep:
    """R10.17 — 90 days from ``sample_timestamp``, not from receipt."""

    @pytest.mark.asyncio
    async def test_samples_older_than_90_days_go_and_newer_ones_stay(self):
        just_inside = NOW - timedelta(days=89, hours=23)
        just_outside = NOW - timedelta(days=90, seconds=1)
        long_gone = NOW - timedelta(days=400)
        service = MatchingESService(
            {
                "driver_breadcrumbs": [
                    _breadcrumb_doc(just_inside, just_inside),
                    _breadcrumb_doc(just_outside, just_outside),
                    _breadcrumb_doc(long_gone, long_gone),
                ]
            }
        )
        job = DriverRetentionJob(es_service=service)

        results = await job.run_cycle(now=NOW)

        assert results["breadcrumb_sample"] == 2
        survivors = [
            document["sample_timestamp"]
            for document in service.remaining["driver_breadcrumbs"]
        ]
        assert survivors == [just_inside.isoformat()]

    @pytest.mark.asyncio
    async def test_a_late_submitted_old_sample_is_deleted_on_its_client_stamp(self):
        """An offline drain submits a day-old fix; the clock started when it was taken.

        ``sample_timestamp`` is 90 days and a minute old while
        ``server_received_at`` is 89 days old, so a sweep anchored on receipt
        would keep this document. Anchored on the client stamp, as R10.17
        declares, it goes.
        """
        taken_at = NOW - timedelta(days=90, minutes=1)
        received_at = taken_at + timedelta(hours=23)
        service = MatchingESService(
            {"driver_breadcrumbs": [_breadcrumb_doc(taken_at, received_at)]}
        )
        job = DriverRetentionJob(es_service=service)

        results = await job.run_cycle(now=NOW)

        assert results["breadcrumb_sample"] == 1
        assert service.remaining["driver_breadcrumbs"] == []

    @pytest.mark.asyncio
    async def test_the_sweep_never_ranges_on_server_received_at(self, job, es_service):
        await job.run_cycle(now=NOW)
        breadcrumb_call = next(
            call for call in es_service.calls if call["index"] == "driver_breadcrumbs"
        )
        assert list(breadcrumb_call["query"]["range"]) == ["sample_timestamp"]

    @pytest.mark.asyncio
    async def test_one_delete_by_query_per_cycle_for_the_class(self, job, es_service):
        """R10.13 — one sweep per class per cycle, and the cycle is 24 hours."""
        await job.run_cycle(now=NOW)
        breadcrumb_calls = [
            call for call in es_service.calls if call["index"] == "driver_breadcrumbs"
        ]
        assert len(breadcrumb_calls) == 1
