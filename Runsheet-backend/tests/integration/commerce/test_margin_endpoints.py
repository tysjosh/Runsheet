"""Margin admin API behaviour (AC-24, AC-25, AC-29, AC-42, Simplifications 3 and 12).

Admin is served on every route; ``tenant_id`` comes only from the session; two
tenants with identical terminal and product ids never see each other's data;
preview validates and persists nothing; the summary splits revenue into costed
and uncosted parts and caps the range at 92 days.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import pytest

from commerce.services.margin_repository import RecordFilters
from tests.unit.commerce.margin._cost_basis_support import add_entry
from tests.unit.commerce.margin._service_support import invoice_doc, seed_invoice
from tests.unit.commerce.margin.conftest import AS_OF, TENANT_A, TENANT_B, _candidate, _missing_cost

from ._margin_api import BASE, TERMINAL, TERMINAL_B_ONLY, error_code

UTC = timezone.utc
RANGE = "start_date=2026-09-25&end_date=2026-10-05"


async def seed_records(repo, tenant: str = TENANT_A) -> Dict[str, Dict[str, Any]]:
    """A costed record, a missing-cost record and a negative-margin record."""
    costed = await repo.write_record(tenant, _candidate(source_key="order:ORD-1", order_id="ORD-1"), "live")
    missing = await repo.write_record(
        tenant,
        _missing_cost(source_key="order:ORD-2", order_id="ORD-2", revenue_cents=120_000, input_hash="h2"),
        "live",
    )
    negative = await repo.write_record(
        tenant,
        _candidate(
            source_key="order:ORD-3", order_id="ORD-3", landed_cost_micros=3_200_000,
            product_cost_micros=3_200_000, cost_cents=320_000, margin_cents=-20_000,
            margin_per_gallon_micros=-200_000, margin_bp=-667, flag_negative_margin=True,
            input_hash="h3",
        ),
        "live",
    )
    return {"costed": costed.record, "missing": missing.record, "negative": negative.record}


def alert(record: Dict[str, Any], *, alert_type: str = "negative_margin", status: str = "open", key: str = "") -> Dict[str, Any]:
    return {
        "alert_type": alert_type,
        "severity": "high",
        "status": status,
        "dedupe_key": key or record["record_id"],
        "record_id": record["record_id"],
        "order_id": record["order_id"],
        "customer_id": record["customer_id"],
        "product_code": record["product_code"],
        "details": {"stage": record["stage"]},
    }


async def override(repo, tenant: str, micros: int) -> None:
    await add_entry(
        repo, tenant, "override", unit_cost_micros=micros,
        effective_at=datetime(2026, 1, 1, tzinfo=UTC), terminal=TERMINAL,
    )


def entry_body(**overrides: Any) -> Dict[str, Any]:
    body = {
        "kind": "override",
        "product_code": "DIESEL_2",
        "terminal_id": TERMINAL,
        "effective_at": "2026-01-01T00:00:00Z",
        "unit_cost_usd": "2.500000",
    }
    body.update(overrides)
    return body


def preview_body(**overrides: Any) -> Dict[str, Any]:
    body = {
        "product_code": "DIESEL_2",
        "gallons": "1000",
        "terminal_id": TERMINAL,
        "as_of": "2026-10-01T15:00:00Z",
        "unit_price_usd": "3.000000",
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


def recompute_body(**overrides: Any) -> Dict[str, Any]:
    body = {"start_date": "2026-09-01", "end_date": "2026-09-30", "reason": "late BOLs"}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Admin is served on every route (AC-29)
# ---------------------------------------------------------------------------


async def test_admin_is_served_on_every_route(margin_api, repo):
    api = margin_api.as_("admin")
    c = api.client
    records = await seed_records(repo)
    await repo.record_alert_outcome(TENANT_A, records["negative"]["record_id"], [alert(records["negative"])])
    await repo.record_alert_outcome(TENANT_A, records["missing"]["record_id"], [
        alert(records["missing"], alert_type="leakage_proposal", status="pending_review", key="p1"),
        alert(records["missing"], alert_type="leakage_proposal", status="pending_review", key="p2"),
    ])
    alerts = (await c.get(f"{BASE}/alerts")).json()["data"]["items"]
    open_alert = next(a for a in alerts if a["alert_type"] == "negative_margin")
    proposals = [a for a in alerts if a["alert_type"] == "leakage_proposal"]

    created = await c.post(f"{BASE}/cost-entries", json=entry_body())
    assert created.status_code == 201, created.text
    entry_id = created.json()["data"]["entry"]["entry_id"]
    second = await c.post(f"{BASE}/cost-entries", json=entry_body(terminal_id=None, unit_cost_usd="2.600000"))
    assert second.status_code == 201, second.text

    responses = {
        "list entries": await c.get(f"{BASE}/cost-entries"),
        "supersede": await c.post(
            f"{BASE}/cost-entries/{entry_id}/supersede",
            json={**entry_body(unit_cost_usd="2.400000"), "reason": "corrected invoice"},
        ),
        "void": await c.post(
            f"{BASE}/cost-entries/{second.json()['data']['entry']['entry_id']}/void",
            json={"reason": "duplicate"},
        ),
        "import": await c.post(
            f"{BASE}/cost-entries/import",
            files={"file": ("costs.csv", (
                "kind,product_code,terminal_id,effective_at,unit_cost_usd,gallons,reference\n"
                f"purchase,DIESEL_2,{TERMINAL},2026-09-01T08:00:00Z,2.450000,1000,INV-9\n"
            ).encode(), "text/csv")},
            data={"dry_run": "true"},
        ),
        "cost basis": await c.get(f"{BASE}/cost-basis?product_code=DIESEL_2&terminal_id={TERMINAL}"),
        "records": await c.get(f"{BASE}/records?{RANGE}"),
        "record": await c.get(f"{BASE}/records/{records['costed']['record_id']}"),
        "export": await c.get(f"{BASE}/records/export?{RANGE}"),
        "summary": await c.get(f"{BASE}/summary?{RANGE}"),
        "preview": await c.post(f"{BASE}/preview", json=preview_body()),
        "recompute": await c.post(f"{BASE}/recompute", json=recompute_body()),
        "acknowledge": await c.post(f"{BASE}/alerts/{open_alert['alert_id']}/acknowledge", json={"note": "seen"}),
        "approve": await c.post(f"{BASE}/alerts/{proposals[0]['alert_id']}/approve"),
        "dismiss": await c.post(f"{BASE}/alerts/{proposals[1]['alert_id']}/dismiss", json={}),
        "reports": await c.get(f"{BASE}/reports"),
        "settings": await c.get(f"{BASE}/settings"),
        "put settings": await c.put(f"{BASE}/settings", json={"wac_window_days": 30}),
    }
    expected = {"supersede": 201, "recompute": 202}
    for name, resp in responses.items():
        assert resp.status_code == expected.get(name, 200), (name, resp.text)
    run_id = responses["recompute"].json()["data"]["run_id"]
    await api.drain_runs()
    run = await c.get(f"{BASE}/recompute/{run_id}")
    assert run.status_code == 200, run.text
    assert run.json()["data"]["status"] == "completed"
    assert responses["import"].json()["data"]["rows_valid"] == 1
    assert responses["import"].json()["data"]["dry_run"] is True
    assert responses["acknowledge"].json()["data"]["status"] == "acknowledged"
    assert responses["approve"].json()["data"]["status"] == "approved"
    assert responses["dismiss"].json()["data"]["status"] == "dismissed"
    # Every success uses the {"data", "request_id"} envelope.
    for name, resp in responses.items():
        if name != "export":
            assert set(resp.json()) == {"data", "request_id"}, name
    # Cost edits and alert resolutions are audit-logged.
    events = {e["event_type"] for e in api.telemetry.events}
    assert {
        "margin_cost_entry_created", "margin_cost_entry_superseded", "margin_cost_entry_voided",
        "margin_settings_updated", "margin_recompute_started", "margin_alert_resolved",
    } <= events


async def test_record_detail_has_snapshot_and_versions(margin_api, repo):
    records = await seed_records(repo)
    newer = _candidate(source_key="order:ORD-1", order_id="ORD-1", input_hash="h1b", revenue_cents=310_000,
                       margin_cents=60_000)
    await repo.write_record(TENANT_A, newer, "live")
    resp = await margin_api.as_("admin").client.get(f"{BASE}/records/{records['costed']['record_id']}")
    data = resp.json()["data"]
    assert data["cost_snapshot"] == {"method": "wac"}
    assert [v["version"] for v in data["versions"]] == [1, 2]
    assert "tenant_id" not in data


async def test_unknown_or_other_tenant_record_is_404(margin_api, repo):
    b = await seed_records(repo, TENANT_B)
    c = margin_api.as_("admin").client
    assert (await c.get(f"{BASE}/records/nope")).status_code == 404
    resp = await c.get(f"{BASE}/records/{b['costed']['record_id']}")
    assert resp.status_code == 404
    assert error_code(resp) == "RESOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Missing cost stays null through the API (Simplification 12)
# ---------------------------------------------------------------------------


async def test_missing_cost_record_has_null_cost_fields_never_zero(margin_api, repo):
    records = await seed_records(repo)
    resp = await margin_api.as_("admin").client.get(f"{BASE}/records/{records['missing']['record_id']}")
    data = resp.json()["data"]
    assert data["method"] == "none"
    assert data["no_cost_reason"] == "no_lots_no_rack"
    for key in ("product_cost_micros", "landed_cost_micros", "cost_cents", "margin_cents",
                "margin_per_gallon_micros", "margin_bp", "margin_pct"):
        assert data[key] is None, key
    assert "missing_cost" in data["flags"]


# ---------------------------------------------------------------------------
# Lists, filters and cursors
# ---------------------------------------------------------------------------


async def test_records_list_filters_and_keyset_cursor(margin_api, repo):
    await seed_records(repo)
    c = margin_api.as_("admin").client
    seen: List[str] = []
    cursor = None
    for _ in range(4):
        url = f"{BASE}/records?{RANGE}&limit=1" + (f"&cursor={cursor}" if cursor else "")
        body = (await c.get(url)).json()["data"]
        seen += [r["order_id"] for r in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == ["ORD-1", "ORD-2", "ORD-3"]
    flagged = (await c.get(f"{BASE}/records?{RANGE}&flag=missing_cost")).json()["data"]["items"]
    assert [r["order_id"] for r in flagged] == ["ORD-2"]
    none_in_range = (await c.get(f"{BASE}/records?start_date=2026-10-02&end_date=2026-10-03")).json()
    assert none_in_range["data"]["items"] == []
    by_alias = (await c.get(f"{BASE}/records?{RANGE}&product_code=AGO")).json()["data"]["items"]
    assert len(by_alias) == 3  # AGO canonicalizes to DIESEL_2


@pytest.mark.parametrize("query", [
    "cursor=not-base64!!", "stage=quote", "flag=rich", "status=deleted", "limit=0", "limit=201",
    "start_date=2026-13-01",
])
async def test_bad_record_queries_are_422(margin_api, query):
    resp = await margin_api.as_("admin").client.get(f"{BASE}/records?{query}")
    assert resp.status_code == 422, resp.text
    assert error_code(resp) == "VALIDATION_ERROR"


async def test_alert_list_filters_and_bad_status(margin_api, repo):
    records = await seed_records(repo)
    await repo.record_alert_outcome(TENANT_A, records["negative"]["record_id"], [alert(records["negative"])])
    c = margin_api.as_("admin").client
    body = (await c.get(f"{BASE}/alerts?status=open,acknowledged")).json()["data"]
    assert body["total"] == 1 and body["items"][0]["order_id"] == "ORD-3"
    assert (await c.get(f"{BASE}/alerts?status=closed")).status_code == 422
    assert (await c.get(f"{BASE}/alerts?alert_type=other")).status_code == 422


async def test_alert_state_errors(margin_api, repo):
    records = await seed_records(repo)
    await repo.record_alert_outcome(TENANT_A, records["negative"]["record_id"], [alert(records["negative"])])
    c = margin_api.as_("admin").client
    alert_id = (await c.get(f"{BASE}/alerts")).json()["data"]["items"][0]["alert_id"]
    approve = await c.post(f"{BASE}/alerts/{alert_id}/approve")
    assert approve.status_code == 409  # only a leakage proposal can be approved
    assert (await c.post(f"{BASE}/alerts/missing/acknowledge")).status_code == 404


async def test_recompute_conflict_and_validation(margin_api, repo):
    c = margin_api.as_("admin").client
    run = await repo.start_run(
        TENANT_A, requested_by="u-admin", start_date=datetime(2026, 9, 1).date(),
        end_date=datetime(2026, 9, 2).date(), stages=["invoice"], only_missing=True, reason="x",
    )
    resp = await c.post(f"{BASE}/recompute", json=recompute_body())
    assert resp.status_code == 409
    assert error_code(resp) == "MARGIN_RECOMPUTE_RUNNING"
    assert resp.json()["details"]["run_id"] == run["run_id"]
    for body in (recompute_body(reason=""), recompute_body(end_date="2026-12-15"),
                 recompute_body(end_date="2026-08-01")):
        assert (await c.post(f"{BASE}/recompute", json=body)).status_code == 422, body
    assert (await c.get(f"{BASE}/recompute/unknown")).status_code == 404


async def test_settings_put_warns_above_30_days(margin_api):
    resp = await margin_api.as_("admin").client.put(
        f"{BASE}/settings", json={"wac_window_days": 31, "floor_usd_per_gallon": "0.150000"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["warnings"] == ["wac_window_may_exceed_bol_scan_cap"]
    assert data["settings"]["floor_micros"] == 150_000
    got = (await margin_api.client.get(f"{BASE}/settings")).json()["data"]
    assert got["wac_window_days"] == 31


# ---------------------------------------------------------------------------
# tenant_id only from the session (AC-25)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/cost-entries", entry_body()),
    ("POST", "/cost-entries/x/supersede", {**entry_body(), "reason": "r"}),
    ("POST", "/cost-entries/x/void", {"reason": "r"}),
    ("POST", "/preview", preview_body()),
    ("POST", "/recompute", recompute_body()),
    ("POST", "/alerts/x/acknowledge", {"note": "n"}),
    ("PUT", "/settings", {"wac_window_days": 30}),
])
async def test_body_tenant_id_is_422(margin_api, method, path, body):
    resp = await margin_api.as_("admin").client.request(
        method, f"{BASE}{path}", json={**body, "tenant_id": TENANT_B}
    )
    assert resp.status_code == 422, resp.text
    assert error_code(resp) == "VALIDATION_ERROR"


async def test_query_tenant_id_is_ignored(margin_api, repo):
    await seed_records(repo, TENANT_A)
    await seed_records(repo, TENANT_B)
    c = margin_api.as_("admin").client
    items = (await c.get(f"{BASE}/records?{RANGE}&tenant_id={TENANT_B}")).json()["data"]["items"]
    ids_a = {r["record_id"] for r in (await repo.list_records(TENANT_A, RecordFilters(), limit=50)).items}
    assert {r["record_id"] for r in items} == ids_a


# ---------------------------------------------------------------------------
# Two tenants with identical terminal and product ids (AC-24)
# ---------------------------------------------------------------------------


async def test_two_tenants_stay_isolated_on_every_read(margin_api, repo):
    a = await seed_records(repo, TENANT_A)
    b = await seed_records(repo, TENANT_B)
    # B gets an extra record and an alert; A gets an override, B a different one.
    await repo.write_record(TENANT_B, _candidate(source_key="order:ORD-9", order_id="ORD-9", input_hash="h9"), "live")
    await repo.record_alert_outcome(TENANT_B, b["negative"]["record_id"], [alert(b["negative"])])
    await override(repo, TENANT_A, 2_500_000)
    await override(repo, TENANT_B, 2_900_000)
    c = margin_api.as_("admin", tenant=TENANT_A).client

    listed = (await c.get(f"{BASE}/records?{RANGE}&status=all")).json()["data"]["items"]
    assert {r["record_id"] for r in listed} == {r["record_id"] for r in a.values()}

    summary = (await c.get(f"{BASE}/summary?{RANGE}")).json()["data"]
    assert summary["totals"]["records"] == 3

    export = await c.get(f"{BASE}/records/export?{RANGE}")
    rows = list(csv.reader(io.StringIO(export.content[3:].decode("utf-8"))))
    assert {r[0] for r in rows[1:]} == {r["record_id"] for r in a.values()}

    assert (await c.get(f"{BASE}/alerts")).json()["data"] == {"items": [], "next_cursor": None, "total": 0}

    basis = (await c.get(f"{BASE}/cost-basis?product_code=DIESEL_2&terminal_id={TERMINAL}"
                         "&as_of=2026-10-01T15:00:00Z")).json()["data"]
    assert basis["method"] == "override" and basis["landed_cost_micros"] == 2_500_000

    preview = (await c.post(f"{BASE}/preview", json=preview_body())).json()["data"]
    assert preview["landed_cost_micros"] == 2_500_000
    assert preview["cost_cents"] == 250_000 and preview["margin_cents"] == 50_000

    # B's terminal-only id is unknown in A, for preview and cost-basis alike.
    for resp in (
        await c.post(f"{BASE}/preview", json=preview_body(terminal_id=TERMINAL_B_ONLY)),
        await c.get(f"{BASE}/cost-basis?product_code=DIESEL_2&terminal_id={TERMINAL_B_ONLY}"),
    ):
        assert resp.status_code == 422
        assert resp.json()["details"]["errors"][0]["type"] == "unknown_terminal"

    started = await c.post(f"{BASE}/recompute", json=recompute_body())
    run_id = started.json()["data"]["run_id"]
    await margin_api.drain_runs()
    assert (await c.get(f"{BASE}/recompute/{run_id}")).status_code == 200
    b_client = margin_api.as_("admin", tenant=TENANT_B).client
    assert (await b_client.get(f"{BASE}/recompute/{run_id}")).status_code == 404
    # B's own recompute is not blocked by A's run, and its records were not touched.
    assert (await repo.get_record(TENANT_B, b["costed"]["record_id"]))["version"] == 1
    b_basis = (await b_client.get(f"{BASE}/cost-basis?product_code=DIESEL_2&terminal_id={TERMINAL}"
                                  "&as_of=2026-10-01T15:00:00Z")).json()["data"]
    assert b_basis["landed_cost_micros"] == 2_900_000
    # A cannot act on B's alert or entry.
    b_alert = (await b_client.get(f"{BASE}/alerts")).json()["data"]["items"][0]["alert_id"]
    a_client = margin_api.as_("admin", tenant=TENANT_A).client
    assert (await a_client.post(f"{BASE}/alerts/{b_alert}/acknowledge")).status_code == 404


# ---------------------------------------------------------------------------
# Preview validation (AC-42)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body,loc", [
    (preview_body(customer_id="CUST-1"), None),             # both price sources
    (preview_body(unit_price_usd=None), None),               # neither
    (preview_body(unit_price_usd="100.000001"), "unit_price_usd"),
    (preview_body(unit_price_usd="2.0000001"), "unit_price_usd"),
    (preview_body(unit_price_usd=2.5), None),                # JSON float
    (preview_body(gallons="abc"), "gallons"),
    (preview_body(terminal_id="TERM-NOPE"), "terminal_id"),
])
async def test_preview_validation_is_422_and_persists_nothing(margin_api, repo, body, loc):
    resp = await margin_api.as_("admin").client.post(f"{BASE}/preview", json=body)
    assert resp.status_code == 422, resp.text
    assert error_code(resp) == "VALIDATION_ERROR"
    if loc is not None:
        errors = resp.json()["details"]["errors"]
        assert any(loc in [str(p) for p in e["loc"]] for e in errors), errors
    assert await repo.count_records(TENANT_A, RecordFilters(status="all")) == 0


async def test_preview_accepts_the_100_dollar_bound_and_writes_nothing(margin_api, repo):
    resp = await margin_api.as_("admin").client.post(
        f"{BASE}/preview", json=preview_body(unit_price_usd="100.000000")
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["unit_price_micros"] == 100_000_000
    assert data["method"] == "none" and data["cost_cents"] is None  # no cost data in A
    assert await repo.count_records(TENANT_A, RecordFilters(status="all")) == 0
    assert (await repo.list_alerts(TENANT_A)).total == 0


# ---------------------------------------------------------------------------
# Summary (Simplification 3, freeze 12)
# ---------------------------------------------------------------------------


async def test_summary_revenue_split_and_totals_equal_record_sums(margin_api, repo):
    records = await seed_records(repo)
    await repo.record_skip(
        TENANT_A, stage="invoice", source_key="invoice:INV-X:line:0", error_type="invalid_gallons",
        order_id="ORD-X", invoice_id="INV-X", line_index=0, now=AS_OF,
    )
    c = margin_api.as_("admin").client
    for group_by in ("day", "customer", "product", "terminal"):
        data = (await c.get(f"{BASE}/summary?{RANGE}&group_by={group_by}")).json()["data"]
        for block in [*data["groups"], data["totals"]]:
            assert block["revenue_cents_with_cost"] + block["revenue_cents_missing_cost"] == block["revenue_cents"]
        totals = data["totals"]
        rows = list(records.values())
        assert totals["records"] == 3
        assert totals["revenue_cents"] == sum(r["revenue_cents"] for r in rows)
        assert totals["revenue_cents_missing_cost"] == records["missing"]["revenue_cents"]
        assert totals["cost_cents"] == sum(r["cost_cents"] for r in rows if r["cost_cents"] is not None)
        assert totals["margin_cents"] == sum(r["margin_cents"] for r in rows if r["margin_cents"] is not None)
        assert sum(g["records"] for g in data["groups"]) == 3
        assert data["skipped_sources"]["count"] == 1


@pytest.mark.parametrize("query,expected", [
    ("start_date=2026-07-01&end_date=2026-09-30", 200),   # 92 days
    ("start_date=2026-07-01&end_date=2026-10-01", 422),   # 93 days
    ("start_date=2026-10-02&end_date=2026-10-01", 422),
    ("group_by=week", 422),
    ("start_date=bogus", 422),
])
async def test_summary_range_and_group_validation(margin_api, query, expected):
    resp = await margin_api.as_("admin").client.get(f"{BASE}/summary?{query}")
    assert resp.status_code == expected, resp.text


async def test_summary_defaults_to_the_last_30_days(margin_api):
    data = (await margin_api.as_("admin").client.get(f"{BASE}/summary")).json()["data"]
    start = datetime.fromisoformat(data["start_date"]).date()
    end = datetime.fromisoformat(data["end_date"]).date()
    assert (end - start).days == 29


async def test_summary_default_end_is_today_in_the_settings_timezone(margin_api):
    data = (await margin_api.as_("admin").client.get(f"{BASE}/summary")).json()["data"]
    assert data["timezone"] == "America/Chicago"
    assert data["end_date"] == datetime.now(ZoneInfo("America/Chicago")).date().isoformat()


# ---------------------------------------------------------------------------
# One date axis for every admin surface (Simplification 13, review R1)
# ---------------------------------------------------------------------------

#: 03:00Z on Oct 5 is 22:00 on Oct 4 in America/Chicago (the default zone).
LATE_SALE = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
DAY_BEFORE, DAY_OF_UTC = "2026-10-04", "2026-10-05"


def _one_day(day: str) -> str:
    return f"start_date={day}&end_date={day}"


def _export_invoice_ids(resp) -> List[str]:
    assert resp.status_code == 200, resp.text
    assert resp.content[:3] == b"\xef\xbb\xbf"
    return [row["invoice_id"] for row in csv.DictReader(io.StringIO(resp.content[3:].decode("utf-8")))]


async def test_records_export_summary_and_recompute_share_the_settings_timezone_day(margin_api, repo):
    api = margin_api.as_("admin")
    c = api.client
    await seed_invoice(api.store, invoice_doc(
        "I-LATE", created_at=LATE_SALE, delivered_at=LATE_SALE, status="open", finalized_at=LATE_SALE,
    ))
    key = "invoice:I-LATE:line:0"
    await repo.write_record(TENANT_A, _missing_cost(
        stage="invoice", source_key=key, invoice_id="I-LATE", line_index=0, as_of=LATE_SALE,
    ), "finalize")

    # Records: listed under D-1 (local), not under D (its UTC date).
    listed = (await c.get(f"{BASE}/records?{_one_day(DAY_BEFORE)}")).json()["data"]
    assert listed["timezone"] == "America/Chicago"
    assert [r["invoice_id"] for r in listed["items"]] == ["I-LATE"]
    assert (await c.get(f"{BASE}/records?{_one_day(DAY_OF_UTC)}")).json()["data"]["items"] == []

    # The export uses the same filters.
    assert _export_invoice_ids(await c.get(f"{BASE}/records/export?{_one_day(DAY_BEFORE)}")) == ["I-LATE"]
    assert _export_invoice_ids(await c.get(f"{BASE}/records/export?{_one_day(DAY_OF_UTC)}")) == []

    # Summary counts it on D-1.
    summary = (await c.get(
        f"{BASE}/summary?start_date={DAY_BEFORE}&end_date={DAY_OF_UTC}&group_by=day"
    )).json()["data"]
    assert [g["key"] for g in summary["groups"]] == [DAY_BEFORE]

    # A late cost arrives. A recompute over D alone does not reach the record...
    await add_entry(repo, TENANT_A, "override", unit_cost_micros=2_500_000,
                    effective_at=datetime(2026, 1, 1, tzinfo=UTC))
    body = {"stages": ["invoice"], "only_missing": True, "reason": "late BOLs"}
    miss = await c.post(f"{BASE}/recompute", json={**body, "start_date": DAY_OF_UTC, "end_date": DAY_OF_UTC})
    assert miss.status_code == 202, miss.text
    await api.drain_runs()
    miss_run = (await c.get(f"{BASE}/recompute/{miss.json()['data']['run_id']}")).json()["data"]
    assert miss_run["status"] == "completed"
    assert miss_run["counts"]["written"] == 0 and miss_run["counts"]["out_of_range"] == 1
    assert [v["version"] for v in await repo.record_versions(TENANT_A, "invoice", key)] == [1]

    # ...and a recompute over D-1 alone does.
    hit = await c.post(f"{BASE}/recompute", json={**body, "start_date": DAY_BEFORE, "end_date": DAY_BEFORE})
    assert hit.status_code == 202, hit.text
    await api.drain_runs()
    hit_run = (await c.get(f"{BASE}/recompute/{hit.json()['data']['run_id']}")).json()["data"]
    assert hit_run["status"] == "completed" and hit_run["counts"]["written"] == 1
    versions = await repo.record_versions(TENANT_A, "invoice", key)
    assert [(v["version"], v["status"]) for v in versions] == [(1, "superseded"), (2, "active")]
    assert versions[-1]["method"] == "override"


@pytest.mark.parametrize("query,expected", [
    # ISO datetimes keep their exact instant.
    ("start_date=2026-10-05T03:00:00Z&end_date=2026-10-05T03:00:00Z", ["I-LATE"]),
    ("start_date=2026-10-05T03:00:01Z", []),
    # A bare start is local midnight: 2026-10-05 00:00 Chicago is 05:00Z.
    ("start_date=2026-10-05", []),
    ("end_date=2026-10-04", ["I-LATE"]),
])
async def test_record_date_bounds(margin_api, repo, query, expected):
    await repo.write_record(TENANT_A, _missing_cost(
        stage="invoice", source_key="invoice:I-LATE:line:0", invoice_id="I-LATE", line_index=0, as_of=LATE_SALE,
    ), "finalize")
    resp = await margin_api.as_("admin").client.get(f"{BASE}/records?{query}")
    assert resp.status_code == 200, resp.text
    assert [r["invoice_id"] for r in resp.json()["data"]["items"]] == expected


async def test_record_range_order_is_checked_on_local_days(margin_api):
    # 2026-10-05 00:00 Chicago (05:00Z) is after 04:00Z: 422.
    resp = await margin_api.as_("admin").client.get(
        f"{BASE}/records?start_date=2026-10-05&end_date=2026-10-05T04:00:00Z"
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["details"]["reason"] == "after_end_date"


# ---------------------------------------------------------------------------
# Cost entries and cost basis
# ---------------------------------------------------------------------------


async def test_cost_entry_errors_map_to_the_envelope(margin_api):
    c = margin_api.as_("admin").client
    assert (await c.post(f"{BASE}/cost-entries", json=entry_body(unit_cost_usd=2.5))).status_code == 422
    assert (await c.post(f"{BASE}/cost-entries", json=entry_body(terminal_id=TERMINAL_B_ONLY))).status_code == 422
    first = await c.post(f"{BASE}/cost-entries", json=entry_body())
    dup = await c.post(f"{BASE}/cost-entries", json=entry_body())
    assert dup.status_code == 409
    assert dup.json()["details"]["existing_entry_id"] == first.json()["data"]["entry"]["entry_id"]
    assert (await c.post(f"{BASE}/cost-entries/none/void", json={"reason": "r"})).status_code == 404
    listed = (await c.get(f"{BASE}/cost-entries?status=all")).json()["data"]
    assert len(listed["items"]) == 1 and listed["next_cursor"] is None
    assert (await c.get(f"{BASE}/cost-entries?status=bogus")).status_code == 422


async def test_import_commit_writes_rows(margin_api, repo):
    c = margin_api.as_("admin").client
    content = (
        "kind,product_code,terminal_id,effective_at,unit_cost_usd,gallons,reference\n"
        f"purchase,DIESEL_2,{TERMINAL},2026-09-01T08:00:00Z,2.450000,1000,INV-1\n"
        f"purchase,DIESEL_2,{TERMINAL},2026-09-02T08:00:00Z,2.550000,500,INV-2\n"
    ).encode()
    dry = await c.post(f"{BASE}/cost-entries/import", files={"file": ("c.csv", content, "text/csv")})
    assert dry.json()["data"]["dry_run"] is True  # dry run is the default
    assert (await repo.list_entries(TENANT_A, status="all")).items == []
    done = await c.post(
        f"{BASE}/cost-entries/import", files={"file": ("c.csv", content, "text/csv")}, data={"dry_run": "false"}
    )
    assert done.status_code == 200, done.text
    assert len(done.json()["data"]["created_entry_ids"]) == 2
    bad = await c.post(
        f"{BASE}/cost-entries/import",
        files={"file": ("c.csv", b"kind,product_code\npurchase,DIESEL_2\n", "text/csv")},
        data={"dry_run": "false"},
    )
    assert bad.status_code == 422


@pytest.mark.parametrize("query", ["product_code=NOPE", "product_code=DIESEL_2&terminal_id=TERM-NOPE", ""])
async def test_cost_basis_validation(margin_api, query):
    resp = await margin_api.as_("admin").client.get(f"{BASE}/cost-basis?{query}")
    assert resp.status_code == 422, resp.text


async def test_cost_basis_reader_failure_is_503(margin_api):
    margin_api.store.raise_on.add("terminal_bols")
    resp = await margin_api.as_("admin").client.get(
        f"{BASE}/cost-basis?product_code=DIESEL_2&terminal_id={TERMINAL}"
    )
    assert resp.status_code == 503, resp.text
    assert error_code(resp) == "ELASTICSEARCH_UNAVAILABLE"
