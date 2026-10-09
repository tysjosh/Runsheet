"""Weekly report (FR5.7, AC-46, AC-28): content, stage preference, rerun no-op, flags off."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from commerce.services.margin_jobs import previous_iso_week, run_margin_weekly_report_cycle
from config.settings import clear_settings_cache

from ._service_support import SweepStore, build_service
from .conftest import TENANT_A, TENANT_B

UTC = timezone.utc
CHICAGO = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)  # Wednesday; previous ISO week is 2026-W41
IN_WEEK = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)


@pytest.fixture
def report_flags(monkeypatch):
    monkeypatch.setenv("COMMERCE_MARGIN_FEED_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_BACKBONE_ENABLED", "true")
    clear_settings_cache()
    yield
    clear_settings_cache()


def test_previous_iso_week_in_settings_timezone():
    week, start, end = previous_iso_week(NOW, CHICAGO)
    assert week == "2026-W41"
    assert start == datetime(2026, 10, 5, 0, 0, tzinfo=CHICAGO).astimezone(UTC)
    assert end - start == timedelta(days=7)
    # Monday 03:00Z is still Sunday in Chicago: the previous week is W40 there.
    assert previous_iso_week(datetime(2026, 10, 12, 3, 0, tzinfo=UTC), CHICAGO)[0] == "2026-W40"


async def test_report_content_stage_preference_and_rerun(repo, report_flags, make_candidate, make_missing_cost):
    service = build_service(repo, SweepStore(), clock=lambda: NOW)
    # Order 1: delivery + invoice (invoice counts once, not twice).
    await repo.write_record(TENANT_A, make_candidate(stage="delivery", source_key="order:O1", order_id="O1", as_of=IN_WEEK), "live")
    await repo.write_record(
        TENANT_A,
        make_candidate(stage="invoice", source_key="invoice:I1:line:0", order_id="O1", invoice_id="I1", line_index=0, as_of=IN_WEEK),
        "live",
    )
    # Order 2: missing cost.
    await repo.write_record(
        TENANT_A, make_missing_cost(source_key="order:O2", order_id="O2", revenue_cents=200_000, as_of=IN_WEEK), "live"
    )
    # Order 3: below floor, costed.
    await repo.write_record(
        TENANT_A,
        make_candidate(
            source_key="order:O3", order_id="O3", margin_per_gallon_micros=50_000, margin_cents=5_000,
            cost_cents=295_000, flag_below_floor=True, as_of=IN_WEEK,
        ),
        "live",
    )
    # Outside the week.
    await repo.write_record(TENANT_A, make_candidate(source_key="order:O4", order_id="O4", as_of=IN_WEEK - timedelta(days=7)), "live")
    await repo.write_record(TENANT_B, make_candidate(as_of=IN_WEEK), "live")

    assert await run_margin_weekly_report_cycle(service) == 2  # one per tenant
    (report,) = await repo.list_reports(TENANT_A)
    assert report["iso_week"] == "2026-W41"
    assert report["records_total"] == 3
    assert report["revenue_cents"] == 600_000  # costed revenue: invoice O1 + O3
    assert report["revenue_cents_missing_cost"] == 200_000
    assert report["cost_cents"] == 250_000 + 295_000
    assert report["margin_cents"] == 50_000 + 5_000
    assert report["missing_cost_share_bp"] == 3_333  # 1 of 3 records, by count, half-up
    assert report["flag_counts"]["missing_cost"]["records"] == 1
    assert report["flag_counts"]["below_floor"] == {"records": 1, "gallons_ugal": 1_000_000_000}
    assert report["gallons_ugal_total"] == 3_000_000_000

    assert await run_margin_weekly_report_cycle(service) == 0  # rerun is a no-op
    assert len(await repo.list_reports(TENANT_A)) == 1


class _ExplodingDiscovery:
    async def tenants_with_records(self):
        raise AssertionError("no query when a flag is off")


@pytest.mark.parametrize(
    "env",
    [
        {"COMMERCE_MARGIN_FEED_ENABLED": "false", "COMMERCE_BACKBONE_ENABLED": "true"},
        {"COMMERCE_MARGIN_FEED_ENABLED": "true", "COMMERCE_BACKBONE_ENABLED": "false"},
    ],
    ids=["margin_off", "backbone_off"],
)
async def test_flag_off_returns_before_any_query(repo, monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    clear_settings_cache()
    service = build_service(repo, SweepStore(), clock=lambda: NOW)
    monkeypatch.setattr(repo, "discovery", _ExplodingDiscovery())
    assert await run_margin_weekly_report_cycle(service) == 0


async def test_persistence_off_returns_before_any_query(repo, report_flags, monkeypatch):
    service = build_service(repo, SweepStore(), clock=lambda: NOW)
    monkeypatch.setattr(repo, "discovery", _ExplodingDiscovery())
    monkeypatch.setattr("persistence.database.is_persistence_enabled", lambda: False)
    assert await run_margin_weekly_report_cycle(service) == 0
