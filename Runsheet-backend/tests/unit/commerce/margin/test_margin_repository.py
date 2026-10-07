"""MarginRepository on SQLite: tenant scoping, entries, settings, reads, queue, runs.

Tenant isolation (AC-24) is checked at the data layer: two tenants with
identical product codes, terminal ids and source keys, where every read for A
returns no B row and no write for A changes B.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone

import pytest

from commerce.services.margin_repository import (
    MarginAlertNotFoundError,
    MarginAlertStateError,
    MarginDuplicateEntryError,
    MarginEntryKindMismatchError,
    MarginEntryNotActiveError,
    MarginEntryNotFoundError,
    MarginRecomputeRunningError,
    MarginRepository,
    RecordFilters,
)

from .conftest import AS_OF, TENANT_A, TENANT_B

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _entry(**overrides):
    values = dict(
        kind="purchase",
        product_code="DIESEL_2",
        terminal_id="TERM-1",
        effective_at=T0,
        unit_cost_micros=2_500_000,
        gallons_milli=1_000_000,
        natural_key="nk-1",
        source="manual",
        created_by="admin@example.com",
    )
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def _public_methods():
    return [
        (name, fn)
        for name, fn in inspect.getmembers(MarginRepository, inspect.isfunction)
        if not name.startswith("_")
    ]


def test_every_public_method_takes_tenant_id_first():
    methods = _public_methods()
    assert len(methods) >= 30
    for name, fn in methods:
        params = list(inspect.signature(fn).parameters)
        assert params[:2] == ["self", "tenant_id"], name


def test_discovery_returns_ids_only():
    """The only tenant-less surface is discovery, and it is not on the repository."""

    from commerce.services.margin_repository import MarginTenantDiscovery

    public = [n for n, _ in inspect.getmembers(MarginTenantDiscovery, inspect.isfunction)
              if not n.startswith("_")]
    assert sorted(public) == ["pending_work_tenants", "tenants_with_records"]


# ---------------------------------------------------------------------------
# Two-tenant isolation
# ---------------------------------------------------------------------------


async def test_two_tenants_with_identical_keys_never_mix(repo, make_candidate, make_missing_cost):
    # Same product, terminal, BOL, natural key and source key in both tenants.
    for tenant in (TENANT_A, TENANT_B):
        await repo.insert_entry(tenant, _entry(bol_id="BOL-1"))
        await repo.insert_entry(
            tenant, _entry(kind="override", gallons_milli=None, natural_key="nk-o")
        )
        await repo.ensure_activated(tenant)
        await repo.record_skip(tenant, stage="delivery", source_key="order:ORD-9",
                               error_type="x")
    b_record = (await repo.write_record(TENANT_B, make_missing_cost(), "live")).record

    # A write for A on the same key inserts A's own v1; B's record is untouched.
    a_result = await repo.write_record(TENANT_A, make_candidate(input_hash="other"), "live")
    assert a_result.outcome == "inserted" and a_result.record["version"] == 1
    b_after = await repo.get_record(TENANT_B, b_record["record_id"])
    assert (b_after["status"], b_after["version"]) == ("active", 1)

    # A's void and recompute never reach B.
    await repo.write_record(TENANT_A, make_candidate(), "void")
    assert (await repo.get_record(TENANT_B, b_record["record_id"]))["status"] == "active"

    # Every read for A returns A's rows only.
    assert await repo.get_record(TENANT_A, b_record["record_id"]) is None
    page = await repo.list_records(TENANT_A, RecordFilters(status="all"))
    assert {r["tenant_id"] for r in page.items} == {TENANT_A}
    assert await repo.count_records(TENANT_A, RecordFilters(status="all")) == 1
    latest = await repo.latest_for_keys(TENANT_A, "delivery", ["order:ORD-1"])
    assert latest["order:ORD-1"]["tenant_id"] == TENANT_A
    versions = await repo.record_versions(TENANT_A, "delivery", "order:ORD-1")
    assert {v["tenant_id"] for v in versions} == {TENANT_A}
    summary = [r async for page in repo.iter_summary_rows(
        TENANT_A, as_of_from=AS_OF - timedelta(days=1), as_of_to=AS_OF + timedelta(days=1)
    ) for r in page]
    assert summary == []  # A's only record is void

    entries = await repo.list_entries(TENANT_A, status="all")
    assert {e["tenant_id"] for e in entries.items} == {TENANT_A} and len(entries.items) == 2
    bols = await repo.active_entries_for_bols(TENANT_A, ["BOL-1"])
    assert bols["BOL-1"]["tenant_id"] == TENANT_A
    assert set((await repo.active_natural_keys(TENANT_A, ["nk-1", "nk-o"])).values()) <= {
        e["entry_id"] for e in entries.items
    }
    lots = await repo.active_purchase_lots(
        TENANT_A, product_code="DIESEL_2", terminal_id=None,
        window_start=T0 - timedelta(days=1), as_of=AS_OF,
    )
    assert lots == []  # the purchase references a BOL, so it is not a standalone lot
    overrides = await repo.active_effective_entries(
        TENANT_A, kind="override", product_code="DIESEL_2", terminal_id="TERM-1", as_of=AS_OF
    )
    assert [o["tenant_id"] for o in overrides] == [TENANT_A]
    assert (await repo.skipped_sources(TENANT_A))["count"] == 1
    assert (await repo.pending_records(TENANT_A)) == []
    assert [r["tenant_id"] for r in await repo.pending_records(TENANT_B)] == [TENANT_B]

    # Entries of B cannot be transitioned through A.
    b_entry = (await repo.list_entries(TENANT_B)).items[0]
    with pytest.raises(MarginEntryNotFoundError):
        await repo.transition_entry(TENANT_A, b_entry["entry_id"], action="void",
                                    reason="r", actor="a")
    assert await repo.get_entry(TENANT_A, b_entry["entry_id"]) is None


async def test_payload_tenant_mismatch_is_refused(repo):
    with pytest.raises(ValueError):
        await repo.insert_entry(TENANT_A, {**_entry(), "tenant_id": TENANT_B})
    with pytest.raises(ValueError):
        await repo.insert_report_if_absent(TENANT_A, {"tenant_id": TENANT_B, "iso_week": "x"})


async def test_queue_and_alerts_are_tenant_scoped(repo, make_candidate):
    a = (await repo.write_record(TENANT_A, make_candidate(flag_negative_margin=True), "live")).record
    b = (await repo.write_record(TENANT_B, make_candidate(flag_negative_margin=True), "live")).record
    # A cannot settle B's record or create alerts against it.
    assert await repo.record_alert_outcome(TENANT_A, b["record_id"], [_alert(b)]) == []
    assert await repo.mark_record_done(TENANT_A, b["record_id"]) is False
    assert (await repo.get_record(TENANT_B, b["record_id"]))["alert_state"] == "pending"
    await repo.record_alert_outcome(TENANT_A, a["record_id"], [_alert(a)])
    assert (await repo.list_alerts(TENANT_B)).total == 0
    alert_id = (await repo.list_alerts(TENANT_A)).items[0]["alert_id"]
    with pytest.raises(MarginAlertNotFoundError):
        await repo.transition_alert(TENANT_B, alert_id, action="acknowledge", actor="b")
    assert await repo.discovery.pending_work_tenants() == [TENANT_B]


# ---------------------------------------------------------------------------
# Cost entries
# ---------------------------------------------------------------------------


async def test_insert_and_duplicates(repo):
    first = await repo.insert_entry(TENANT_A, _entry(bol_id="BOL-1"))
    assert first["entry_id"].startswith("mce_") and first["status"] == "active"
    with pytest.raises(MarginDuplicateEntryError) as exc:
        await repo.insert_entry(TENANT_A, _entry())  # same natural key
    assert exc.value.existing_entry_id == first["entry_id"]
    with pytest.raises(MarginDuplicateEntryError) as exc:
        await repo.insert_entry(TENANT_A, _entry(natural_key="nk-2", bol_id="BOL-1"))
    assert exc.value.existing_entry_id == first["entry_id"]
    # A CHECK failure is not reported as a duplicate.
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await repo.insert_entry(TENANT_A, _entry(natural_key="nk-3", gallons_milli=None))


async def test_supersede_reuses_the_natural_key_and_links(repo):
    old = await repo.insert_entry(TENANT_A, _entry())
    change = await repo.transition_entry(
        TENANT_A, old["entry_id"], action="supersede", reason="typo", actor="admin",
        replacement=_entry(unit_cost_micros=2_600_000),
    )
    assert change.before["status"] == "active"
    assert change.after["status"] == "superseded"
    assert change.after["status_reason"] == "typo" and change.after["status_changed_by"] == "admin"
    assert change.new_entry["supersedes_id"] == old["entry_id"]
    assert change.after["superseded_by_id"] == change.new_entry["entry_id"]
    with pytest.raises(MarginEntryNotActiveError):
        await repo.transition_entry(TENANT_A, old["entry_id"], action="void", reason="r",
                                    actor="a")
    with pytest.raises(MarginEntryKindMismatchError):
        await repo.transition_entry(
            TENANT_A, change.new_entry["entry_id"], action="supersede", reason="r", actor="a",
            replacement=_entry(kind="override", gallons_milli=None),
        )
    voided = await repo.transition_entry(
        TENANT_A, change.new_entry["entry_id"], action="void", reason="wrong", actor="a"
    )
    assert voided.after["status"] == "voided" and voided.new_entry is None
    statuses = sorted(e["status"] for e in (await repo.list_entries(TENANT_A, status="all")).items)
    assert statuses == ["superseded", "voided"]
    assert (await repo.list_entries(TENANT_A)).items == []
    with pytest.raises(MarginEntryNotFoundError):
        await repo.transition_entry(TENANT_A, "mce_missing", action="void", reason="r", actor="a")


async def test_insert_entries_is_all_or_nothing(repo):
    await repo.insert_entry(TENANT_A, _entry(natural_key="dup"))
    with pytest.raises(MarginDuplicateEntryError):
        await repo.insert_entries(
            TENANT_A,
            [_entry(natural_key="fresh", import_batch_id="b1", source="csv_import"),
             _entry(natural_key="dup", import_batch_id="b1", source="csv_import")],
        )
    assert len((await repo.list_entries(TENANT_A)).items) == 1
    ids = await repo.insert_entries(
        TENANT_A, [_entry(natural_key=f"k{i}", import_batch_id="b2", source="csv_import")
                   for i in range(3)]
    )
    assert len(ids) == 3


async def test_list_entries_keyset_paging(repo):
    for i in range(5):
        await repo.insert_entry(
            TENANT_A, _entry(natural_key=f"k{i}", created_at=T0 + timedelta(minutes=i))
        )
    first = await repo.list_entries(TENANT_A, limit=2)
    assert [e["natural_key"] for e in first.items] == ["k4", "k3"]
    second = await repo.list_entries(TENANT_A, limit=2, after=first.next_key)
    third = await repo.list_entries(TENANT_A, limit=2, after=second.next_key)
    assert [e["natural_key"] for e in second.items + third.items] == ["k2", "k1", "k0"]
    assert third.next_key is None
    filtered = await repo.list_entries(TENANT_A, kind="override")
    assert filtered.items == []


async def test_active_effective_entries_scope_and_dates(repo):
    def override(nk, terminal, start, end=None):
        return _entry(kind="override", gallons_milli=None, natural_key=nk, terminal_id=terminal,
                      effective_at=start, effective_to=end)

    await repo.insert_entry(TENANT_A, override("wide", None, T0))
    await repo.insert_entry(TENANT_A, override("term", "TERM-1", T0))
    await repo.insert_entry(TENANT_A, override("other", "TERM-2", T0))
    await repo.insert_entry(TENANT_A, override("ended", "TERM-1", T0, T0 + timedelta(days=1)))
    await repo.insert_entry(TENANT_A, override("future", "TERM-1", T0 + timedelta(days=60)))

    at_t1 = await repo.active_effective_entries(
        TENANT_A, kind="override", product_code="DIESEL_2", terminal_id="TERM-1", as_of=AS_OF
    )
    assert sorted(e["natural_key"] for e in at_t1) == ["term", "wide"]
    unattributed = await repo.active_effective_entries(
        TENANT_A, kind="override", product_code="DIESEL_2", terminal_id=None, as_of=AS_OF
    )
    assert [e["natural_key"] for e in unattributed] == ["wide"]
    # effective_to is exclusive.
    at_end = await repo.active_effective_entries(
        TENANT_A, kind="override", product_code="DIESEL_2", terminal_id="TERM-1",
        as_of=T0 + timedelta(days=1),
    )
    assert "ended" not in {e["natural_key"] for e in at_end}
    just_before = await repo.active_effective_entries(
        TENANT_A, kind="override", product_code="DIESEL_2", terminal_id="TERM-1",
        as_of=T0 + timedelta(days=1) - timedelta(microseconds=1),
    )
    assert "ended" in {e["natural_key"] for e in just_before}
    with pytest.raises(ValueError):
        await repo.active_effective_entries(
            TENANT_A, kind="purchase", product_code="DIESEL_2", terminal_id=None, as_of=AS_OF
        )


async def test_active_purchase_lots_window_bounds(repo):
    window_start = AS_OF - timedelta(days=30)
    await repo.insert_entry(TENANT_A, _entry(natural_key="at-start", effective_at=window_start))
    await repo.insert_entry(TENANT_A, _entry(natural_key="inside",
                                             effective_at=window_start + timedelta(seconds=1)))
    await repo.insert_entry(TENANT_A, _entry(natural_key="at-as-of", effective_at=AS_OF))
    await repo.insert_entry(TENANT_A, _entry(natural_key="after",
                                             effective_at=AS_OF + timedelta(seconds=1)))
    await repo.insert_entry(TENANT_A, _entry(natural_key="other-term", terminal_id="TERM-2",
                                             effective_at=AS_OF))
    await repo.insert_entry(TENANT_A, _entry(natural_key="with-bol", bol_id="BOL-7",
                                             effective_at=AS_OF))

    lots = await repo.active_purchase_lots(
        TENANT_A, product_code="DIESEL_2", terminal_id="TERM-1",
        window_start=window_start, as_of=AS_OF,
    )
    assert [e["natural_key"] for e in lots] == ["inside", "at-as-of"]
    anywhere = await repo.active_purchase_lots(
        TENANT_A, product_code="DIESEL_2", terminal_id=None,
        window_start=window_start, as_of=AS_OF,
    )
    assert sorted(e["natural_key"] for e in anywhere) == ["at-as-of", "inside", "other-term"]


# ---------------------------------------------------------------------------
# Settings and activation
# ---------------------------------------------------------------------------


async def test_settings_defaults_put_and_watermark(repo):
    defaults = await repo.get_settings(TENANT_A)
    assert defaults["persisted"] is False
    assert (defaults["wac_window_days"], defaults["rack_staleness_days"],
            defaults["floor_micros"], defaults["timezone"]) == (30, 4, 100_000, "America/Chicago")

    first = await repo.ensure_activated(TENANT_A, now=AS_OF)
    later = await repo.ensure_activated(TENANT_A, now=AS_OF + timedelta(days=3))
    assert first == later == AS_OF
    row = await repo.get_settings(TENANT_A)
    assert row["persisted"] is True and row["updated_by"] is None
    assert row["wac_window_days"] == 30

    change = await repo.put_settings(
        TENANT_A, {"wac_window_days": 45, "product_floors": {"DIESEL_2": 50_000}},
        actor="admin", now=AS_OF + timedelta(days=4),
    )
    assert change.before["wac_window_days"] == 30
    assert change.after["wac_window_days"] == 45
    assert change.after["product_floors"] == {"DIESEL_2": 50_000}
    assert change.after["feed_activated_at"] == AS_OF  # PUT never moves the watermark
    assert change.after["updated_by"] == "admin"
    with pytest.raises(ValueError):
        await repo.put_settings(TENANT_A, {"feed_activated_at": None}, actor="admin")


async def test_put_settings_first_then_activation_fills_watermark(repo):
    change = await repo.put_settings(TENANT_B, {"floor_micros": 0}, actor="admin")
    assert change.before["persisted"] is False and change.after["feed_activated_at"] is None
    assert await repo.ensure_activated(TENANT_B, now=AS_OF) == AS_OF
    assert (await repo.get_settings(TENANT_B))["floor_micros"] == 0


# ---------------------------------------------------------------------------
# Record reads
# ---------------------------------------------------------------------------


async def test_list_records_filters_and_keyset(repo, make_candidate, make_missing_cost):
    for i in range(4):
        await repo.write_record(
            TENANT_A,
            make_candidate(source_key=f"order:O{i}", order_id=f"O{i}",
                           as_of=AS_OF + timedelta(hours=i), flag_below_floor=i % 2 == 0),
            "live",
        )
    await repo.write_record(TENANT_A, make_missing_cost(source_key="order:OX", order_id="OX",
                                                        customer_id="CUST-2"), "live")
    first = await repo.list_records(TENANT_A, limit=2)
    assert [r["order_id"] for r in first.items] == ["O3", "O2"]
    rest = await repo.list_records(TENANT_A, limit=10, after=first.next_key)
    # O0 and OX share as_of; record_id desc breaks the tie.
    assert [r["order_id"] for r in rest.items][0] == "O1"
    assert sorted(r["order_id"] for r in rest.items[1:]) == ["O0", "OX"]
    assert rest.next_key is None

    assert await repo.count_records(TENANT_A) == 5
    assert await repo.count_records(TENANT_A, RecordFilters(flag="below_floor")) == 2
    assert await repo.count_records(TENANT_A, RecordFilters(flag="missing_cost")) == 1
    assert await repo.count_records(TENANT_A, RecordFilters(customer_id="CUST-2")) == 1
    window = RecordFilters(as_of_from=AS_OF + timedelta(hours=1),
                           as_of_to=AS_OF + timedelta(hours=3))
    assert [r["order_id"] for r in (await repo.list_records(TENANT_A, window)).items] == [
        "O2", "O1"
    ]  # as_of_to is exclusive
    with pytest.raises(ValueError):
        await repo.count_records(TENANT_A, RecordFilters(flag="nope"))


async def test_iter_summary_rows_pages_active_integer_columns(repo, make_candidate):
    for i in range(5):
        await repo.write_record(
            TENANT_A, make_candidate(source_key=f"order:S{i}", order_id=f"S{i}",
                                     as_of=AS_OF + timedelta(minutes=i)), "live"
        )
    await repo.write_record(
        TENANT_A, make_candidate(source_key="order:S0", order_id="S0", input_hash="h2",
                                 as_of=AS_OF), "live"
    )  # supersedes S0 v1
    pages = [page async for page in repo.iter_summary_rows(
        TENANT_A, as_of_from=AS_OF, as_of_to=AS_OF + timedelta(hours=1), page_size=2
    )]
    rows = [r for page in pages for r in page]
    assert [len(p) for p in pages] == [2, 2, 1]
    assert sorted(r["order_id"] for r in rows) == ["S0", "S1", "S2", "S3", "S4"]
    assert "cost_snapshot" not in rows[0]
    assert sum(r["revenue_cents"] for r in rows) == 5 * 300_000


async def test_latest_for_keys_returns_active_or_void(repo, make_candidate):
    await repo.write_record(TENANT_A, make_candidate(source_key="order:L1"), "live")
    await repo.write_record(TENANT_A, make_candidate(source_key="order:L1", input_hash="h2"),
                            "live")
    await repo.write_record(TENANT_A, make_candidate(source_key="order:L2"), "void")
    found = await repo.latest_for_keys(TENANT_A, "delivery",
                                       ["order:L1", "order:L2", "order:L3"])
    assert {k: (v["version"], v["status"]) for k, v in found.items()} == {
        "order:L1": (2, "active"),
        "order:L2": (1, "void"),
    }
    assert await repo.latest_for_keys(TENANT_A, "invoice", ["order:L1"]) == {}
    history = await repo.record_versions(TENANT_A, "delivery", "order:L1")
    assert [(v["version"], v["status"]) for v in history] == [(1, "superseded"), (2, "active")]


# ---------------------------------------------------------------------------
# Queue, alerts and leakage
# ---------------------------------------------------------------------------


def _alert(record, alert_type="negative_margin", severity="high"):
    return {
        "alert_type": alert_type,
        "severity": severity,
        "dedupe_key": record["record_id"],
        "record_id": record["record_id"],
        "order_id": record["order_id"],
        "customer_id": record["customer_id"],
        "product_code": record["product_code"],
        "details": {"flags": ["negative_margin"]},
    }


async def test_pending_queue_order_expiry_and_outcome(repo, make_candidate):
    old = (await repo.write_record(
        TENANT_A, make_candidate(source_key="order:Q0", flag_negative_margin=True), "live",
        now=AS_OF - timedelta(days=8),
    )).record
    newer = (await repo.write_record(
        TENANT_A, make_candidate(source_key="order:Q1", flag_negative_margin=True), "live",
        now=AS_OF,
    )).record
    assert [r["record_id"] for r in await repo.pending_records(TENANT_A)] == [
        old["record_id"], newer["record_id"]
    ]
    assert await repo.pending_records(TENANT_A, limit=1) == [
        await repo.get_record(TENANT_A, old["record_id"])
    ]
    assert await repo.expire_stale_pending(TENANT_A, older_than=timedelta(days=7), now=AS_OF) == 1
    assert (await repo.get_record(TENANT_A, old["record_id"]))["alert_state"] == "expired"

    first = await repo.record_alert_outcome(TENANT_A, newer["record_id"], [_alert(newer)])
    again = await repo.record_alert_outcome(TENANT_A, newer["record_id"], [_alert(newer)])
    assert len(first) == 1 and again == []
    assert (await repo.get_record(TENANT_A, newer["record_id"]))["alert_state"] == "done"
    alerts = await repo.list_alerts(TENANT_A)
    assert alerts.total == 1 and alerts.items[0]["order_id"] == "ORD-1"
    assert await repo.discovery.pending_work_tenants() == []


async def test_one_failing_dedupe_does_not_drop_other_alerts(repo, make_candidate):
    record = (await repo.write_record(
        TENANT_A, make_candidate(flag_negative_margin=True), "live"
    )).record
    await repo.record_alert_outcome(TENANT_A, record["record_id"], [_alert(record)])
    inserted = await repo.record_alert_outcome(
        TENANT_A, record["record_id"],
        [_alert(record), _alert(record, alert_type="missing_cost", severity="medium")],
    )
    assert len(inserted) == 1
    assert (await repo.list_alerts(TENANT_A)).total == 2


async def test_digest_queue(repo):
    run = await repo.start_run(
        TENANT_A, requested_by="admin", start_date=date(2026, 9, 1), end_date=date(2026, 9, 7),
        stages=["invoice"], only_missing=True, reason="late BOLs",
    )
    finished = await repo.finish_run(
        TENANT_A, run["run_id"], status="completed",
        counts={"sources": 3, "by_flag": {"missing_cost": 2}},
    )
    assert finished["digest_state"] == "pending"
    assert [d["run_id"] for d in await repo.pending_digests(TENANT_A)] == [run["run_id"]]
    assert await repo.discovery.pending_work_tenants() == [TENANT_A]
    digest = {"alert_type": "recompute_digest", "severity": "info",
              "dedupe_key": run["run_id"], "run_id": run["run_id"], "details": {"written": 3}}
    assert len(await repo.mark_digest_done(TENANT_A, run["run_id"], alert=digest)) == 1
    assert await repo.mark_digest_done(TENANT_A, run["run_id"], alert=digest) == []
    assert await repo.pending_digests(TENANT_A) == []


async def test_leakage_window_groups_by_sale(repo, make_candidate):
    # Three invoices; the newest has two split lines, one of them below floor.
    for i, (lines, below) in enumerate([(1, [True]), (1, [True]), (2, [False, True])]):
        for line in range(lines):
            await repo.write_record(
                TENANT_A,
                make_candidate(stage="invoice", source_key=f"invoice:I{i}:line:{line}",
                               invoice_id=f"I{i}", line_index=line,
                               as_of=AS_OF + timedelta(days=i), flag_below_floor=below[line]),
                "live",
            )
    sales = await repo.leakage_window(
        TENANT_A, customer_id="CUST-1", product_code="DIESEL_2", stage="invoice", threshold=3
    )
    assert [s["sale_key"] for s in sales] == ["I2", "I1", "I0"]
    assert [len(s["record_ids"]) for s in sales] == [2, 1, 1]
    assert all(s["below_floor"] for s in sales)
    two = await repo.leakage_window(
        TENANT_A, customer_id="CUST-1", product_code="DIESEL_2", stage="invoice", threshold=2
    )
    assert [s["sale_key"] for s in two] == ["I2", "I1"]
    assert await repo.leakage_window(
        TENANT_A, customer_id="CUST-1", product_code="DIESEL_2", stage="delivery", threshold=3
    ) == []


async def test_alert_transitions(repo, make_candidate):
    record = (await repo.write_record(TENANT_A, make_candidate(flag_negative_margin=True),
                                      "live")).record
    await repo.record_alert_outcome(TENANT_A, record["record_id"], [
        _alert(record),
        {"alert_type": "leakage_proposal", "severity": "high", "status": "pending_review",
         "dedupe_key": f"CUST-1|DIESEL_2|{record['record_id']}", "customer_id": "CUST-1",
         "product_code": "DIESEL_2", "proposal_id": "prop-1"},
    ])
    assert await repo.has_pending_leakage(TENANT_A, customer_id="CUST-1",
                                          product_code="DIESEL_2")
    assert not await repo.has_pending_leakage(TENANT_B, customer_id="CUST-1",
                                              product_code="DIESEL_2")
    items = {a["alert_type"]: a for a in (await repo.list_alerts(TENANT_A)).items}
    neg, leak = items["negative_margin"], items["leakage_proposal"]

    with pytest.raises(MarginAlertStateError):
        await repo.transition_alert(TENANT_A, neg["alert_id"], action="approve", actor="a")
    done = await repo.transition_alert(TENANT_A, neg["alert_id"], action="acknowledge",
                                       actor="admin", note="seen")
    assert (done.from_status, done.alert["status"]) == ("open", "acknowledged")
    assert done.alert["resolved_by"] == "admin" and done.alert["resolution_note"] == "seen"
    with pytest.raises(MarginAlertStateError):
        await repo.transition_alert(TENANT_A, neg["alert_id"], action="acknowledge", actor="a")

    approved = await repo.transition_alert(TENANT_A, leak["alert_id"], action="approve",
                                           actor="admin")
    assert (approved.from_status, approved.alert["status"]) == ("pending_review", "approved")
    with pytest.raises(MarginAlertStateError):
        await repo.transition_alert(TENANT_A, leak["alert_id"], action="dismiss", actor="a")
    assert not await repo.has_pending_leakage(TENANT_A, customer_id="CUST-1",
                                              product_code="DIESEL_2")
    open_only = await repo.list_alerts(TENANT_A, statuses=["open", "pending_review"])
    assert open_only.total == 0
    with pytest.raises(MarginAlertNotFoundError):
        await repo.transition_alert(TENANT_A, "malert_x", action="dismiss", actor="a")


async def test_list_alerts_keyset(repo, make_candidate):
    record = (await repo.write_record(TENANT_A, make_candidate(), "live")).record
    for i in range(3):
        await repo.record_alert_outcome(
            TENANT_A, record["record_id"],
            [{**_alert(record), "dedupe_key": f"k{i}"}], now=AS_OF + timedelta(minutes=i),
        )
    first = await repo.list_alerts(TENANT_A, limit=2)
    assert first.total == 3 and [a["dedupe_key"] for a in first.items] == ["k2", "k1"]
    second = await repo.list_alerts(TENANT_A, limit=2, after=first.next_key)
    assert [a["dedupe_key"] for a in second.items] == ["k0"] and second.next_key is None
    assert (await repo.list_alerts(TENANT_A, alert_type="missing_cost")).total == 0


# ---------------------------------------------------------------------------
# Recompute runs
# ---------------------------------------------------------------------------


def _run_args(**overrides):
    values = dict(requested_by="admin", start_date=date(2026, 9, 1), end_date=date(2026, 9, 7),
                  stages=["invoice", "delivery"], only_missing=True, reason="r")
    values.update(overrides)
    return values


async def test_one_running_run_per_tenant_with_stale_takeover(repo):
    run = await repo.start_run(TENANT_A, now=AS_OF, **_run_args())
    with pytest.raises(MarginRecomputeRunningError) as exc:
        await repo.start_run(TENANT_A, now=AS_OF + timedelta(minutes=59), **_run_args())
    assert exc.value.run_id == run["run_id"]
    other_tenant = await repo.start_run(TENANT_B, now=AS_OF, **_run_args())
    assert other_tenant["status"] == "running"

    assert await repo.heartbeat(TENANT_A, run["run_id"], counts={"sources": 100},
                                now=AS_OF + timedelta(minutes=30))
    with pytest.raises(MarginRecomputeRunningError):
        await repo.start_run(TENANT_A, now=AS_OF + timedelta(minutes=89), **_run_args())
    taken = await repo.start_run(TENANT_A, now=AS_OF + timedelta(minutes=91), **_run_args())
    assert taken["run_id"] != run["run_id"]
    old = await repo.get_run(TENANT_A, run["run_id"])
    assert old["status"] == "failed" and old["counts"] == {"sources": 100}
    assert await repo.heartbeat(TENANT_A, run["run_id"]) is False  # not running any more
    assert await repo.get_run(TENANT_B, run["run_id"]) is None


async def test_finish_run_digest_state(repo):
    run = await repo.start_run(TENANT_A, **_run_args())
    done = await repo.finish_run(TENANT_A, run["run_id"], status="completed",
                                 counts={"sources": 2, "written": 2, "by_flag": {}})
    assert (done["status"], done["digest_state"]) == ("completed", "done")
    failed_run = await repo.start_run(TENANT_A, **_run_args())
    failed = await repo.finish_run(TENANT_A, failed_run["run_id"], status="failed", counts={})
    assert (failed["status"], failed["digest_state"]) == ("failed", "none")
    assert await repo.finish_run(TENANT_B, run["run_id"], status="failed", counts={}) is None
    with pytest.raises(ValueError):
        await repo.finish_run(TENANT_A, run["run_id"], status="running", counts={})


# ---------------------------------------------------------------------------
# Weekly reports and discovery
# ---------------------------------------------------------------------------


async def test_weekly_report_insert_if_absent(repo, make_candidate):
    report = dict(
        iso_week="2026-W40", period_start=AS_OF, period_end=AS_OF + timedelta(days=7),
        revenue_cents=100, cost_cents=80, margin_cents=20, revenue_cents_missing_cost=5,
        records_total=3, gallons_ugal_total=3_000_000, flag_counts={"missing_cost": {
            "records": 1, "gallons_ugal": 1_000_000}},
        missing_cost_share_bp=3_333, generated_at=AS_OF,
    )
    assert await repo.insert_report_if_absent(TENANT_A, report) is True
    assert await repo.insert_report_if_absent(TENANT_A, {**report, "revenue_cents": 1}) is False
    assert await repo.report_exists(TENANT_A, "2026-W40")
    assert not await repo.report_exists(TENANT_B, "2026-W40")
    listed = await repo.list_reports(TENANT_A)
    assert [(r["iso_week"], r["revenue_cents"]) for r in listed] == [("2026-W40", 100)]
    assert await repo.list_reports(TENANT_B) == []

    await repo.write_record(TENANT_B, make_candidate(), "live")
    assert await repo.discovery.tenants_with_records() == [TENANT_B]
