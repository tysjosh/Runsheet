"""Audit logging of cost-entry and settings mutations (FR1.7, AC-33).

Exactly one ``log_audit_event`` per create / supersede / void / import /
settings update, carrying tenant, user, action and ids; ``before``/``after``
on supersede and settings; counts only on import. A failing sink never fails
the request.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from commerce.models.margin import (
    CostEntryCreate,
    CostEntrySupersede,
    CostEntryVoid,
    MarginSettingsUpdate,
)
from commerce.services import margin_cost_entry_service as entry_module
from commerce.services.margin_cost_entry_service import MarginCostEntryService
from errors.exceptions import AppException

from ._margin_fakes import CapturingTelemetry, FakeDocStore, FakeTerminals
from .conftest import TENANT_A

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
TERM = "TERM-A1"


@pytest.fixture
def telemetry() -> CapturingTelemetry:
    return CapturingTelemetry()


def make_service(repo, telemetry) -> MarginCostEntryService:
    return MarginCostEntryService(
        repo,
        terminals=FakeTerminals({TENANT_A: [TERM]}),
        es_service=FakeDocStore(),
        telemetry=telemetry,
        clock=lambda: NOW,
    )


@pytest.fixture
def service(repo, telemetry) -> MarginCostEntryService:
    return make_service(repo, telemetry)


def override_body(**overrides) -> CostEntryCreate:
    values = dict(
        kind="override",
        product_code="DIESEL_2",
        terminal_id=TERM,
        effective_at="2026-10-01T00:00:00Z",
        unit_cost_usd="2.400000",
    )
    values.update(overrides)
    return CostEntryCreate(**values)


def supersede_body(**overrides) -> CostEntrySupersede:
    values = dict(override_body().model_dump(mode="json", exclude_none=True), reason="price fix")
    values["unit_cost_usd"] = "2.450000"
    values.update(overrides)
    return CostEntrySupersede(**values)


async def test_create_emits_one_event(service, telemetry):
    entry = (await service.create(TENANT_A, "admin-1", override_body()))["entry"]
    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert event["event_type"] == "margin_cost_entry_created"
    assert event["user_id"] == "admin-1"
    assert event["resource_type"] == "margin_cost_entry"
    assert event["resource_id"] == entry["entry_id"]
    assert event["action"] == "create"
    assert event["details"]["tenant_id"] == TENANT_A
    assert event["details"]["entry_id"] == entry["entry_id"]


async def test_supersede_emits_one_event_with_before_and_after(service, telemetry):
    old = (await service.create(TENANT_A, "admin-1", override_body()))["entry"]
    telemetry.events.clear()
    new = (await service.supersede(TENANT_A, "admin-2", old["entry_id"], supersede_body()))["entry"]
    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert event["event_type"] == "margin_cost_entry_superseded"
    assert event["action"] == "supersede"
    assert event["user_id"] == "admin-2"
    assert event["resource_id"] == old["entry_id"]
    details = event["details"]
    assert details["tenant_id"] == TENANT_A
    assert details["entry_id"] == old["entry_id"]
    assert details["new_entry_id"] == new["entry_id"]
    assert details["reason"] == "price fix"
    assert details["before"]["unit_cost_micros"] == 2_400_000
    assert details["before"]["status"] == "active"
    assert details["after"]["unit_cost_micros"] == 2_450_000
    assert details["after"]["entry_id"] == new["entry_id"]
    assert details["before"]["effective_at"] == "2026-10-01T00:00:00+00:00"  # JSON-safe


async def test_void_emits_one_event(service, telemetry):
    old = (await service.create(TENANT_A, "admin-1", override_body()))["entry"]
    telemetry.events.clear()
    await service.void(TENANT_A, "admin-3", old["entry_id"], CostEntryVoid(reason="duplicate"))
    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert (event["event_type"], event["action"], event["user_id"], event["resource_id"]) == (
        "margin_cost_entry_voided",
        "void",
        "admin-3",
        old["entry_id"],
    )
    assert event["details"]["tenant_id"] == TENANT_A
    assert event["details"]["before"] == {"status": "active"}
    assert event["details"]["after"] == {"status": "voided"}


def _csv(*rows: str) -> bytes:
    return ("kind,product_code,effective_at,unit_cost_usd\n" + "\n".join(rows) + "\n").encode()


async def test_import_emits_one_event_with_counts_only(service, telemetry):
    data = _csv("override,DIESEL_2,2026-10-01,2.4", "override,DIESEL_2,2026-10-02,2.5", "override,DIESEL_2,2026-10-02,2.5")
    report = await service.import_csv(TENANT_A, "admin-1", data, dry_run=False)
    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert event["event_type"] == "margin_cost_import"
    assert event["action"] == "import"
    assert event["resource_id"] == report["import_batch_id"]
    assert event["details"] == {
        "tenant_id": TENANT_A,
        "import_batch_id": report["import_batch_id"],
        "rows_total": 3,
        "rows_created": 2,
        "duplicates": 1,
    }


async def test_dry_run_and_failed_validation_emit_nothing(service, telemetry):
    await service.import_csv(TENANT_A, "admin-1", _csv("override,DIESEL_2,2026-10-01,2.4"), dry_run=True)
    with pytest.raises(AppException):
        await service.import_csv(TENANT_A, "admin-1", _csv("override,NOPE,2026-10-01,2.4"), dry_run=False)
    with pytest.raises(AppException):
        await service.create(TENANT_A, "admin-1", override_body(product_code="NOPE"))
    assert telemetry.events == []


async def test_settings_update_emits_one_event_with_before_and_after(service, telemetry):
    await service.update_settings(
        TENANT_A,
        "admin-1",
        MarginSettingsUpdate(floor_usd_per_gallon="0.15", product_floors={"DIESEL_2": "0.2"}),
    )
    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert event["event_type"] == "margin_settings_updated"
    assert event["action"] == "update"
    assert event["resource_type"] == "margin_settings"
    assert event["resource_id"] == TENANT_A
    assert event["details"]["tenant_id"] == TENANT_A
    assert event["details"]["before"]["floor_micros"] == 100_000
    assert event["details"]["before"]["product_floors"] == {}
    assert event["details"]["after"]["floor_micros"] == 150_000
    assert event["details"]["after"]["product_floors"] == {"DIESEL_2": 200_000}


async def test_a_failing_audit_sink_does_not_fail_the_request(repo, caplog):
    failing = CapturingTelemetry(fail=True)
    service = make_service(repo, failing)
    with caplog.at_level(logging.INFO, logger=entry_module.__name__):
        entry = (await service.create(TENANT_A, "admin-1", override_body()))["entry"]
        await service.supersede(TENANT_A, "admin-1", entry["entry_id"], supersede_body())
        await service.update_settings(TENANT_A, "admin-1", MarginSettingsUpdate())
    assert len(failing.events) == 3
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "audit sink failed" in r.getMessage()]
    assert len(warnings) == 3
    infos = [r for r in caplog.records if r.levelname == "INFO" and r.getMessage().startswith("margin margin_")]
    assert len(infos) == 3
    assert entry["entry_id"] in infos[0].getMessage()


async def test_default_sink_is_the_global_telemetry_service(repo, monkeypatch):
    captured = CapturingTelemetry()
    monkeypatch.setattr(entry_module, "get_telemetry_service", lambda: captured)
    service = MarginCostEntryService(
        repo,
        terminals=FakeTerminals({TENANT_A: [TERM]}),
        es_service=FakeDocStore(),
        clock=lambda: NOW,
    )
    await service.create(TENANT_A, "admin-1", override_body())
    assert [e["event_type"] for e in captured.events] == ["margin_cost_entry_created"]
