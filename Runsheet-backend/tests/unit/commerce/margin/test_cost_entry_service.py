"""MarginCostEntryService: validation, create, supersede, void, list, settings.

Design "Cost entries (FR1)"; AC-21, AC-26, AC-34, AC-36. SQLite repository,
in-memory terminals and BOL store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from commerce.models.margin import (
    CostEntryCreate,
    CostEntrySupersede,
    CostEntryVoid,
    MarginSettingsUpdate,
)
from commerce.services.margin_cost_entry_service import (
    WARNING_GALLONS_IGNORED,
    WARNING_WAC_WINDOW,
    MarginCostEntryService,
    natural_key,
)
from errors.exceptions import AppException

from ._margin_fakes import CapturingTelemetry, FakeDocStore, FakeTerminals, tenant_of_query
from .conftest import TENANT_A, TENANT_B

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
TERM_A = "TERM-A1"
TERM_A2 = "TERM-A2"
TERM_B = "TERM-B1"


def _bol(bol_id: str, tenant: str, terminal: str, product: str = "DIESEL_2") -> Dict[str, Any]:
    return {
        "bol_id": bol_id,
        "tenant_id": tenant,
        "terminal_id": terminal,
        "product_code": product,
        "net_gallons": 7500.0,
        "timestamp": "2026-10-01T10:00:00Z",
    }


@pytest.fixture
def store() -> FakeDocStore:
    s = FakeDocStore()
    s.add(
        "terminal_bols",
        _bol("BOL-A1", TENANT_A, TERM_A),
        _bol("BOL-A2", TENANT_A, TERM_A2),
        _bol("BOL-AGAS", TENANT_A, TERM_A, product="GASOLINE_REG"),
        _bol("BOL-B1", TENANT_B, TERM_B),
    )
    return s


@pytest.fixture
def terminals() -> FakeTerminals:
    return FakeTerminals({TENANT_A: [TERM_A, TERM_A2], TENANT_B: [TERM_B]})


@pytest.fixture
def telemetry() -> CapturingTelemetry:
    return CapturingTelemetry()


@pytest.fixture
def service(repo, terminals, store, telemetry) -> MarginCostEntryService:
    return MarginCostEntryService(
        repo, terminals=terminals, es_service=store, telemetry=telemetry, clock=lambda: NOW
    )


def purchase(**overrides: Any) -> CostEntryCreate:
    values: Dict[str, Any] = dict(
        kind="purchase",
        product_code="DIESEL_2",
        terminal_id=TERM_A,
        effective_at="2026-10-01T10:00:00Z",
        unit_cost_usd="2.500000",
        gallons="1000",
        reference="INV-1",
    )
    values.update(overrides)
    values = {k: v for k, v in values.items() if v is not None}
    return CostEntryCreate(**values)


def override(**overrides: Any) -> CostEntryCreate:
    values: Dict[str, Any] = dict(
        kind="override",
        product_code="DIESEL_2",
        terminal_id=TERM_A,
        effective_at="2026-10-01T00:00:00Z",
        unit_cost_usd="2.400000",
    )
    values.update(overrides)
    values = {k: v for k, v in values.items() if v is not None}
    return CostEntryCreate(**values)


def errors_of(exc: AppException) -> list[tuple[tuple, str]]:
    assert exc.status_code == 422
    return [(tuple(e["loc"]), e["type"]) for e in exc.details["errors"]]


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_purchase_inserts_an_active_entry(service, repo):
    result = await service.create(TENANT_A, "admin-1", purchase())
    entry = result["entry"]
    assert entry["status"] == "active"
    assert entry["kind"] == "purchase"
    assert entry["unit_cost_micros"] == 2_500_000
    assert entry["gallons_milli"] == 1_000_000
    assert entry["source"] == "manual"
    assert entry["created_by"] == "admin-1"
    assert entry["created_at"] == NOW
    assert result["warnings"] == []
    stored = await repo.get_entry(TENANT_A, entry["entry_id"])
    assert stored["natural_key"] == entry["natural_key"]


async def test_create_canonicalizes_the_product_alias(service):
    result = await service.create(TENANT_A, "admin-1", override(product_code="ago"))
    assert result["entry"]["product_code"] == "DIESEL_2"


async def test_bol_purchase_reports_gallons_ignored_warning(service):
    result = await service.create(TENANT_A, "admin-1", purchase(bol_id="BOL-A1"))
    assert result["warnings"] == [WARNING_GALLONS_IGNORED]
    assert result["entry"]["bol_id"] == "BOL-A1"


async def test_date_only_effective_at_is_midnight_in_the_settings_timezone(service):
    result = await service.create(TENANT_A, "admin-1", override(effective_at="2026-10-01"))
    # America/Chicago is UTC-5 (CDT) on 2026-10-01.
    assert result["entry"]["effective_at"] == datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)


async def test_date_only_uses_a_custom_settings_timezone(service, repo):
    await repo.put_settings(TENANT_A, {"timezone": "UTC"}, actor="admin-1")
    result = await service.create(TENANT_A, "admin-1", override(effective_at="2026-10-01"))
    assert result["entry"]["effective_at"] == datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


async def test_explicit_zero_cost_is_allowed(service):
    result = await service.create(TENANT_A, "admin-1", override(unit_cost_usd="0"))
    assert result["entry"]["unit_cost_micros"] == 0


def test_natural_key_is_offset_independent():
    a = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    b = datetime(2026, 10, 1, 5, 0, tzinfo=timezone(timedelta(hours=-5)))
    common = dict(kind="override", product_code="DIESEL_2", terminal_id=None, bol_id=None, reference=None, adder_type=None)
    assert natural_key(effective_at=a, **common) == natural_key(effective_at=b, **common)
    assert len(natural_key(effective_at=a, **common)) == 64


async def test_duplicate_create_is_409_with_the_existing_id(service):
    first = await service.create(TENANT_A, "admin-1", override(effective_at="2026-10-01T10:00:00Z"))
    with pytest.raises(AppException) as info:
        await service.create(TENANT_A, "admin-1", override(effective_at="2026-10-01T05:00:00-05:00"))
    assert info.value.status_code == 409
    assert info.value.details["existing_entry_id"] == first["entry"]["entry_id"]


async def test_duplicate_bol_create_is_409(service):
    first = await service.create(TENANT_A, "admin-1", purchase(bol_id="BOL-A1", reference="X"))
    with pytest.raises(AppException) as info:
        await service.create(
            TENANT_A, "admin-1", purchase(bol_id="BOL-A1", effective_at="2026-10-02T10:00:00Z")
        )
    assert info.value.status_code == 409
    assert info.value.details["existing_entry_id"] == first["entry"]["entry_id"]


# ---------------------------------------------------------------------------
# Validation matrix
# ---------------------------------------------------------------------------

_FUTURE = (NOW + timedelta(days=1, minutes=1)).isoformat()


@pytest.mark.parametrize(
    "body, expected",
    [
        (override(product_code="NOT_A_FUEL"), (("product_code",), "unknown_product")),
        (purchase(terminal_id=None), (("terminal_id",), "required")),
        (override(terminal_id="TERM-NOPE"), (("terminal_id",), "unknown_terminal")),
        (override(effective_at="2026-10-01T10:00:00"), (("effective_at",), "invalid_datetime")),
        (override(effective_at="yesterday"), (("effective_at",), "invalid_datetime")),
        (override(effective_at="2026-02-30"), (("effective_at",), "invalid_datetime")),
        (override(effective_at=_FUTURE), (("effective_at",), "too_far_future")),
        (purchase(effective_to="2026-10-09T00:00:00Z"), (("effective_to",), "not_allowed")),
        (override(effective_to="2026-09-30T00:00:00Z"), (("effective_to",), "before_effective_at")),
        (override(effective_to="2026-10-01T00:00:00Z"), (("effective_to",), "before_effective_at")),
        (override(unit_cost_usd="2.0000001"), (("unit_cost_usd",), "too_many_decimals")),
        (override(unit_cost_usd="1e2"), (("unit_cost_usd",), "invalid_decimal")),
        (override(unit_cost_usd="-1"), (("unit_cost_usd",), "invalid_decimal")),
        (override(unit_cost_usd="100.000001"), (("unit_cost_usd",), "out_of_range")),
        (purchase(gallons=None), (("gallons",), "required")),
        (purchase(gallons="1.0001"), (("gallons",), "too_many_decimals")),
        (purchase(gallons="0"), (("gallons",), "out_of_range")),
        (override(gallons="10"), (("gallons",), "not_allowed")),
        (override(kind="adder"), (("adder_type",), "required")),
        (override(adder_type="freight"), (("adder_type",), "not_allowed")),
        (override(bol_id="BOL-A1"), (("bol_id",), "not_allowed")),
        (purchase(bol_id="BOL-NOPE"), (("bol_id",), "unknown_bol")),
        (purchase(bol_id="BOL-AGAS"), (("bol_id",), "bol_mismatch")),
        (purchase(bol_id="BOL-A2"), (("bol_id",), "bol_mismatch")),
        (override(notes="line one\nline two"), (("notes",), "control_characters")),
        (override(reference="ref\x00"), (("reference",), "control_characters")),
    ],
)
async def test_validation_matrix(service, repo, body, expected):
    with pytest.raises(AppException) as info:
        await service.create(TENANT_A, "admin-1", body)
    assert expected in errors_of(info.value)
    page = await repo.list_entries(TENANT_A, status="all")
    assert page.items == []


async def test_every_error_is_reported_at_once(service):
    body = purchase(
        product_code="NOT_A_FUEL",
        terminal_id="TERM-NOPE",
        effective_at="2026-10-01T10:00:00",
        unit_cost_usd="2.0000001",
        gallons=None,
    )
    with pytest.raises(AppException) as info:
        await service.create(TENANT_A, "admin-1", body)
    found = set(errors_of(info.value))
    assert {
        (("product_code",), "unknown_product"),
        (("terminal_id",), "unknown_terminal"),
        (("effective_at",), "invalid_datetime"),
        (("unit_cost_usd",), "too_many_decimals"),
        (("gallons",), "required"),
    } <= found
    assert info.value.error_code == "VALIDATION_ERROR"


def test_json_float_money_is_rejected_by_the_model():
    with pytest.raises(ValidationError) as info:
        CostEntryCreate(
            kind="override", product_code="DIESEL_2", effective_at="2026-10-01", unit_cost_usd=2.5
        )
    assert info.value.errors()[0]["type"] == "invalid_decimal"


def test_body_tenant_id_is_rejected_by_the_model():
    with pytest.raises(ValidationError):
        CostEntryCreate(
            kind="override",
            product_code="DIESEL_2",
            effective_at="2026-10-01",
            unit_cost_usd="2.5",
            tenant_id=TENANT_B,
        )


# ---------------------------------------------------------------------------
# Cross-tenant references (AC-26)
# ---------------------------------------------------------------------------


async def test_another_tenants_terminal_is_unknown(service, terminals):
    with pytest.raises(AppException) as info:
        await service.create(TENANT_A, "admin-1", override(terminal_id=TERM_B))
    assert (("terminal_id",), "unknown_terminal") in errors_of(info.value)
    assert terminals.calls == [(TENANT_A, TERM_B)]


async def test_another_tenants_bol_is_unknown(service, store):
    with pytest.raises(AppException) as info:
        await service.create(TENANT_A, "admin-1", purchase(bol_id="BOL-B1"))
    assert (("bol_id",), "unknown_bol") in errors_of(info.value)
    bol_queries = store.calls_to("terminal_bols")
    assert len(bol_queries) == 1
    assert tenant_of_query(bol_queries[0]) == TENANT_A


async def test_a_foreign_bol_returned_by_the_store_is_dropped(service, store, caplog):
    class LeakyStore(FakeDocStore):
        async def search_documents(self, index, body, size=100, request_timeout=10):
            self.calls.append((index, body))
            return {"hits": {"hits": [{"_source": _bol("BOL-B1", TENANT_B, TERM_A)}]}}

    leaky = LeakyStore()
    svc = MarginCostEntryService(
        service._repo, terminals=service._terminals, es_service=leaky, telemetry=CapturingTelemetry(), clock=lambda: NOW
    )
    with pytest.raises(AppException) as info:
        await svc.create(TENANT_A, "admin-1", purchase(bol_id="BOL-B1"))
    assert (("bol_id",), "unknown_bol") in errors_of(info.value)
    assert any(r.levelname == "ERROR" and "another tenant" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Supersede / void state machine
# ---------------------------------------------------------------------------


async def test_supersede_links_both_versions(service, repo):
    first = (await service.create(TENANT_A, "admin-1", override()))["entry"]
    body = CostEntrySupersede(
        kind="override",
        product_code="DIESEL_2",
        terminal_id=TERM_A,
        effective_at="2026-10-01T00:00:00Z",
        unit_cost_usd="2.450000",
        reason="supplier correction",
    )
    result = await service.supersede(TENANT_A, "admin-2", first["entry_id"], body)
    new = result["entry"]
    assert new["supersedes_id"] == first["entry_id"]
    assert new["unit_cost_micros"] == 2_450_000
    assert new["natural_key"] == first["natural_key"]  # same key, reused after the flush
    old = await repo.get_entry(TENANT_A, first["entry_id"])
    assert old["status"] == "superseded"
    assert old["superseded_by_id"] == new["entry_id"]
    assert old["status_reason"] == "supplier correction"
    assert old["status_changed_by"] == "admin-2"


def _supersede_body(**overrides: Any) -> CostEntrySupersede:
    values: Dict[str, Any] = dict(
        kind="override",
        product_code="DIESEL_2",
        terminal_id=TERM_A,
        effective_at="2026-10-01T00:00:00Z",
        unit_cost_usd="2.450000",
        reason="fix",
    )
    values.update(overrides)
    return CostEntrySupersede(**values)


async def test_supersede_in_another_tenant_is_404(service):
    first = (await service.create(TENANT_A, "admin-1", override()))["entry"]
    with pytest.raises(AppException) as info:
        await service.supersede(TENANT_B, "admin-b", first["entry_id"], _supersede_body())
    assert info.value.status_code == 404


async def test_supersede_unknown_entry_is_404(service):
    with pytest.raises(AppException) as info:
        await service.supersede(TENANT_A, "admin-1", "mce_missing", _supersede_body())
    assert info.value.status_code == 404


async def test_supersede_a_superseded_entry_is_409(service):
    first = (await service.create(TENANT_A, "admin-1", override()))["entry"]
    await service.supersede(TENANT_A, "admin-1", first["entry_id"], _supersede_body())
    with pytest.raises(AppException) as info:
        await service.supersede(TENANT_A, "admin-1", first["entry_id"], _supersede_body(unit_cost_usd="2.5"))
    assert info.value.status_code == 409


async def test_supersede_changing_kind_is_422(service, repo):
    first = (await service.create(TENANT_A, "admin-1", override()))["entry"]
    with pytest.raises(AppException) as info:
        await service.supersede(
            TENANT_A, "admin-1", first["entry_id"], _supersede_body(kind="adder", adder_type="freight")
        )
    assert (("kind",), "kind_mismatch") in errors_of(info.value)
    assert (await repo.get_entry(TENANT_A, first["entry_id"]))["status"] == "active"


async def test_supersede_with_invalid_fields_is_422_and_leaves_the_entry(service, repo):
    first = (await service.create(TENANT_A, "admin-1", override()))["entry"]
    with pytest.raises(AppException) as info:
        await service.supersede(TENANT_A, "admin-1", first["entry_id"], _supersede_body(unit_cost_usd="1e2"))
    assert (("unit_cost_usd",), "invalid_decimal") in errors_of(info.value)
    assert (await repo.get_entry(TENANT_A, first["entry_id"]))["status"] == "active"


async def test_supersede_onto_another_active_key_is_409(service):
    a = (await service.create(TENANT_A, "admin-1", override()))["entry"]
    b = (await service.create(TENANT_A, "admin-1", override(effective_at="2026-10-02T00:00:00Z")))["entry"]
    with pytest.raises(AppException) as info:
        await service.supersede(TENANT_A, "admin-1", a["entry_id"], _supersede_body(effective_at="2026-10-02T00:00:00Z"))
    assert info.value.status_code == 409
    assert info.value.details["existing_entry_id"] == b["entry_id"]


async def test_void_then_void_again(service, repo):
    first = (await service.create(TENANT_A, "admin-1", override()))["entry"]
    result = await service.void(TENANT_A, "admin-1", first["entry_id"], CostEntryVoid(reason="entered twice"))
    assert result["entry"]["status"] == "voided"
    with pytest.raises(AppException) as info:
        await service.void(TENANT_A, "admin-1", first["entry_id"], CostEntryVoid(reason="again"))
    assert info.value.status_code == 409


async def test_void_in_another_tenant_is_404(service, repo):
    first = (await service.create(TENANT_A, "admin-1", override()))["entry"]
    with pytest.raises(AppException) as info:
        await service.void(TENANT_B, "admin-b", first["entry_id"], CostEntryVoid(reason="x"))
    assert info.value.status_code == 404
    assert (await repo.get_entry(TENANT_A, first["entry_id"]))["status"] == "active"


async def test_void_reason_with_control_characters_is_422(service):
    first = (await service.create(TENANT_A, "admin-1", override()))["entry"]
    with pytest.raises(AppException) as info:
        await service.void(TENANT_A, "admin-1", first["entry_id"], CostEntryVoid(reason="bad\x07"))
    assert (("reason",), "control_characters") in errors_of(info.value)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_superseded_and_voided_entries_stay_listable(service):
    a = (await service.create(TENANT_A, "admin-1", override()))["entry"]
    b = (await service.create(TENANT_A, "admin-1", override(effective_at="2026-10-02T00:00:00Z")))["entry"]
    await service.supersede(TENANT_A, "admin-1", a["entry_id"], _supersede_body(unit_cost_usd="2.6"))
    await service.void(TENANT_A, "admin-1", b["entry_id"], CostEntryVoid(reason="x"))
    active = await service.list_entries(TENANT_A)
    assert [e["status"] for e in active.items] == ["active"]
    everything = await service.list_entries(TENANT_A, status="all")
    assert sorted(e["status"] for e in everything.items) == ["active", "superseded", "voided"]
    voided = await service.list_entries(TENANT_A, status="voided")
    assert [e["entry_id"] for e in voided.items] == [b["entry_id"]]
    assert (await service.list_entries(TENANT_B, status="all")).items == []


async def test_list_filters_canonicalize_and_validate(service):
    await service.create(TENANT_A, "admin-1", override())
    assert len((await service.list_entries(TENANT_A, product_code="AGO")).items) == 1
    for kwargs, loc in [
        ({"status": "deleted"}, "status"),
        ({"limit": 201}, "limit"),
        ({"limit": 0}, "limit"),
        ({"kind": "rebate"}, "kind"),
        ({"product_code": "NOT_A_FUEL"}, "product_code"),
    ]:
        with pytest.raises(AppException) as info:
            await service.list_entries(TENANT_A, **kwargs)
        assert info.value.status_code == 422
        assert info.value.details["errors"][0]["loc"] == [loc]


async def test_list_keyset_pages(service):
    for day in range(1, 4):
        await service.create(TENANT_A, "admin-1", override(effective_at=f"2026-10-0{day}T00:00:00Z"))
    first = await service.list_entries(TENANT_A, limit=2)
    assert len(first.items) == 2 and first.next_key is not None
    second = await service.list_entries(TENANT_A, limit=2, after=first.next_key)
    assert len(second.items) == 1 and second.next_key is None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def test_settings_update_converts_usd_and_canonicalizes(service, repo):
    result = await service.update_settings(
        TENANT_A,
        "admin-1",
        MarginSettingsUpdate(
            wac_window_days=14,
            rack_staleness_days=3,
            floor_usd_per_gallon="0.150000",
            product_floors={"AGO": "0.2"},
            timezone="America/New_York",
        ),
    )
    assert result["warnings"] == []
    settings = await repo.get_settings(TENANT_A)
    assert settings["floor_micros"] == 150_000
    assert settings["product_floors"] == {"DIESEL_2": 200_000}
    assert settings["wac_window_days"] == 14
    assert settings["timezone"] == "America/New_York"
    assert settings["updated_by"] == "admin-1"


async def test_wac_window_above_30_days_is_accepted_with_a_warning(service, repo):
    result = await service.update_settings(TENANT_A, "admin-1", MarginSettingsUpdate(wac_window_days=31))
    assert result["warnings"] == [WARNING_WAC_WINDOW]
    assert (await repo.get_settings(TENANT_A))["wac_window_days"] == 31


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"floor_usd_per_gallon": "5.000001"}, (("floor_usd_per_gallon",), "out_of_range")),
        ({"floor_usd_per_gallon": "0.1234567"}, (("floor_usd_per_gallon",), "too_many_decimals")),
        ({"product_floors": {"NOT_A_FUEL": "0.1"}}, (("product_floors", "NOT_A_FUEL"), "unknown_product")),
        ({"product_floors": {"AGO": "0.1", "DIESEL_2": "0.2"}}, (("product_floors", "DIESEL_2"), "duplicate_product")),
        ({"product_floors": {"AGO": "9"}}, (("product_floors", "AGO"), "out_of_range")),
        ({"timezone": "Mars/Olympus"}, (("timezone",), "invalid_timezone")),
    ],
)
async def test_settings_validation(service, repo, kwargs, expected):
    with pytest.raises(AppException) as info:
        await service.update_settings(TENANT_A, "admin-1", MarginSettingsUpdate(**kwargs))
    assert expected in errors_of(info.value)
    assert (await repo.get_settings(TENANT_A))["persisted"] is False


async def test_settings_update_never_touches_the_watermark(service, repo):
    activated = await repo.ensure_activated(TENANT_A, now=NOW - timedelta(days=3))
    await service.update_settings(TENANT_A, "admin-1", MarginSettingsUpdate(wac_window_days=10))
    assert (await repo.get_settings(TENANT_A))["feed_activated_at"] == activated
