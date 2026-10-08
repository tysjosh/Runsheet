"""CSV import of cost entries (design "Cost entries (FR1)", AC-34, AC-35, AC-36).

All or nothing, dry run writes nothing, 5 MB / 10,000-row limits, duplicate
reporting, and the batching budget (terminal memoization, one BOL ``terms``
query per 1,000 ids).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

import pytest

from commerce.services.margin_cost_entry_service import (
    IMPORT_CHUNK_BYTES,
    IMPORT_MAX_BYTES,
    IMPORT_MAX_ROWS,
    WARNING_GALLONS_IGNORED,
    MarginCostEntryService,
)
from commerce.services.margin_repository import MarginDuplicateEntryError
from errors.exceptions import AppException

from ._margin_fakes import CapturingTelemetry, FakeDocStore, FakeTerminals, tenant_of_query
from .conftest import TENANT_A, TENANT_B

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
HEADER = "kind,product_code,terminal_id,effective_at,unit_cost_usd,gallons,adder_type,bol_id,reference,notes"
TERMINALS_A = [f"TERM-{i}" for i in range(5)]


def csv_text(rows: Iterable[str], header: str = HEADER) -> bytes:
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def purchase_row(i: int, *, terminal: str = "TERM-0", bol: str = "", ref: str | None = None) -> str:
    minute = i % 60
    hour = (i // 60) % 24
    day = 1 + (i // 1440) % 4
    reference = ref if ref is not None else f"INV-{i}"
    return (
        f"purchase,DIESEL_2,{terminal},2026-10-0{day}T{hour:02d}:{minute:02d}:00Z,"
        f"2.500000,1000,,{bol},{reference},"
    )


@pytest.fixture
def store() -> FakeDocStore:
    s = FakeDocStore()
    s.add(
        "terminal_bols",
        {"bol_id": "BOL-1", "tenant_id": TENANT_A, "terminal_id": "TERM-0", "product_code": "AGO"},
        {"bol_id": "BOL-B", "tenant_id": TENANT_B, "terminal_id": "TERM-0", "product_code": "DIESEL_2"},
    )
    return s


@pytest.fixture
def terminals() -> FakeTerminals:
    return FakeTerminals({TENANT_A: TERMINALS_A, TENANT_B: ["TERM-0"]})


@pytest.fixture
def telemetry() -> CapturingTelemetry:
    return CapturingTelemetry()


@pytest.fixture
def service(repo, terminals, store, telemetry) -> MarginCostEntryService:
    return MarginCostEntryService(
        repo, terminals=terminals, es_service=store, telemetry=telemetry, clock=lambda: NOW
    )


async def all_entries(repo, tenant: str = TENANT_A) -> List[Dict[str, Any]]:
    return (await repo.list_entries(tenant, status="all", limit=10_000)).items


# ---------------------------------------------------------------------------
# Happy path, dry run, commit
# ---------------------------------------------------------------------------


async def test_dry_run_reports_and_writes_nothing(service, repo):
    report = await service.import_csv(
        TENANT_A, "admin-1", csv_text([purchase_row(1), purchase_row(2)]), dry_run=True
    )
    assert report == {
        "dry_run": True,
        "rows_total": 2,
        "rows_valid": 2,
        "duplicates": [],
        "created_entry_ids": [],
        "warnings": [],
    }
    assert await all_entries(repo) == []


async def test_commit_inserts_all_rows_with_one_batch_id(service, repo):
    report = await service.import_csv(
        TENANT_A,
        "admin-1",
        csv_text(
            [
                purchase_row(1),
                "override,AGO,,2026-10-01,2.400000,,,,,",
                "adder,DIESEL_2,TERM-1,2026-10-01T00:00:00Z,0.050000,,freight,,,",
            ]
        ),
        dry_run=False,
    )
    assert len(report["created_entry_ids"]) == 3
    entries = await all_entries(repo)
    assert {e["entry_id"] for e in entries} == set(report["created_entry_ids"])
    assert {e["import_batch_id"] for e in entries} == {report["import_batch_id"]}
    assert {e["source"] for e in entries} == {"csv_import"}
    assert {e["kind"] for e in entries} == {"purchase", "override", "adder"}
    override = next(e for e in entries if e["kind"] == "override")
    assert override["product_code"] == "DIESEL_2"
    assert override["terminal_id"] is None


async def test_bom_and_blank_lines_are_accepted(service):
    data = "\ufeff".encode("utf-8") + csv_text(["", purchase_row(1), "  ,  ", ""])
    report = await service.import_csv(TENANT_A, "admin-1", data)
    assert report["rows_total"] == 1


async def test_bol_rows_report_the_gallons_warning(service):
    report = await service.import_csv(TENANT_A, "admin-1", csv_text([purchase_row(1, bol="BOL-1")]))
    assert report["warnings"] == [WARNING_GALLONS_IGNORED]


async def test_columns_may_be_in_any_order_and_optional_ones_omitted(service):
    data = csv_text(
        ["2.4,2026-10-01,DIESEL_2,override"], header="unit_cost_usd,effective_at,product_code,kind"
    )
    report = await service.import_csv(TENANT_A, "admin-1", data)
    assert report["rows_valid"] == 1


# ---------------------------------------------------------------------------
# All or nothing (AC-35)
# ---------------------------------------------------------------------------


async def test_one_invalid_row_rejects_the_file_and_writes_nothing(service, repo):
    rows = [purchase_row(1), "purchase,NOT_A_FUEL,TERM-0,2026-10-01,2.5,10,,,R,", purchase_row(3)]
    with pytest.raises(AppException) as info:
        await service.import_csv(TENANT_A, "admin-1", csv_text(rows), dry_run=False)
    exc = info.value
    assert exc.status_code == 422
    assert exc.details["errors"] == [
        {"row": 2, "loc": ["product_code"], "msg": "unknown product", "type": "unknown_product"}
    ]
    assert exc.details["rows_total"] == 3
    assert await all_entries(repo) == []


async def test_row_errors_are_collected_across_rows(service):
    rows = [
        "purchase,DIESEL_2,TERM-0,2026-10-01,2.0000001,10,,,A,",
        "purchase,DIESEL_2,,2026-10-01,2.5,10,,,B,",
        "rebate,DIESEL_2,TERM-0,2026-10-01,2.5,,,,C,",
        "override,DIESEL_2,TERM-0,2026-10-01,1e2,,,,D,",
        "purchase,DIESEL_2,TERM-9,2026-10-01,2.5,10,,,E,",
        "purchase,DIESEL_2,TERM-0,2026-10-01,2.5,10,,BOL-B,F,",
    ]
    with pytest.raises(AppException) as info:
        await service.import_csv(TENANT_A, "admin-1", csv_text(rows))
    got = {(e["row"], tuple(e["loc"]), e["type"]) for e in info.value.details["errors"]}
    assert (1, ("unit_cost_usd",), "too_many_decimals") in got
    assert (2, ("terminal_id",), "required") in got
    assert (3, ("kind",), "enum") in got
    assert (4, ("unit_cost_usd",), "invalid_decimal") in got
    assert (5, ("terminal_id",), "unknown_terminal") in got
    assert (6, ("bol_id",), "unknown_bol") in got  # another tenant's BOL (AC-26)


async def test_extra_cells_are_a_row_error(service):
    with pytest.raises(AppException) as info:
        await service.import_csv(TENANT_A, "admin-1", csv_text([purchase_row(1) + ",surplus"]))
    assert info.value.details["errors"][0]["type"] == "extra_cells"


@pytest.mark.parametrize(
    "header, expected",
    [
        (HEADER + ",tenant_id", ("unknown_header", "tenant_id")),
        ("kind,product_code,effective_at", ("missing_header", "unit_cost_usd")),
        ("kind,kind,product_code,effective_at,unit_cost_usd", ("duplicate_header", "kind")),
    ],
)
async def test_header_rules(service, header, expected):
    with pytest.raises(AppException) as info:
        await service.import_csv(TENANT_A, "admin-1", csv_text([], header=header))
    assert info.value.status_code == 422
    got = {(e["type"], e["loc"][-1]) for e in info.value.details["errors"]}
    assert expected in got


async def test_empty_file_is_a_header_error(service):
    with pytest.raises(AppException) as info:
        await service.import_csv(TENANT_A, "admin-1", b"")
    assert {e["type"] for e in info.value.details["errors"]} == {"missing_header"}


async def test_non_utf8_is_422(service):
    with pytest.raises(AppException) as info:
        await service.import_csv(TENANT_A, "admin-1", HEADER.encode() + b"\n\xff\xfe,bad\n")
    assert info.value.status_code == 422
    assert info.value.details["errors"][0]["type"] == "invalid_encoding"


async def test_nul_byte_is_invalid_csv(service):
    with pytest.raises(AppException) as info:
        await service.import_csv(
            TENANT_A, "admin-1", csv_text(["purchase,DIESEL_2\x00,TERM-0,2026-10-01,2.5,10,,,R,"])
        )
    assert info.value.status_code == 422
    types = {e["type"] for e in info.value.details["errors"]}
    assert types & {"invalid_csv", "control_characters"}


# ---------------------------------------------------------------------------
# Limits (413)
# ---------------------------------------------------------------------------


class ChunkedUpload:
    """A Starlette-like ``UploadFile`` with an async ``read`` that counts calls."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)
        self.reads: List[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        return self._buffer.read(size)


async def test_over_5_mb_is_413_before_parsing(service, terminals):
    data = b"x" * (IMPORT_MAX_BYTES + 1)
    upload = ChunkedUpload(data)
    with pytest.raises(AppException) as info:
        await service.import_csv(TENANT_A, "admin-1", upload)
    assert info.value.status_code == 413
    assert info.value.error_code == "MARGIN_IMPORT_TOO_LARGE"
    assert set(upload.reads) == {IMPORT_CHUNK_BYTES}
    assert len(upload.reads) == IMPORT_MAX_BYTES // IMPORT_CHUNK_BYTES + 1
    assert terminals.calls == []


async def test_over_5_mb_bytes_is_413(service):
    with pytest.raises(AppException) as info:
        await service.import_csv(TENANT_A, "admin-1", b"x" * (IMPORT_MAX_BYTES + 1))
    assert info.value.status_code == 413


async def test_exactly_5_mb_is_read(service):
    data = csv_text([purchase_row(1)])
    padding = IMPORT_MAX_BYTES - len(data)
    blank_line = b" " * 999 + b"\n"  # blank lines are skipped; short enough for csv's field limit
    data = data + blank_line * (padding // len(blank_line)) + b" " * (padding % len(blank_line))
    assert len(data) == IMPORT_MAX_BYTES
    report = await service.import_csv(TENANT_A, "admin-1", ChunkedUpload(data))
    assert report["rows_total"] == 1


async def test_more_than_10000_rows_is_413(service, terminals):
    rows = [purchase_row(i) for i in range(IMPORT_MAX_ROWS + 1)]
    with pytest.raises(AppException) as info:
        await service.import_csv(TENANT_A, "admin-1", csv_text(rows))
    assert info.value.status_code == 413
    assert info.value.details == {"max_rows": IMPORT_MAX_ROWS}
    assert terminals.calls == []


# ---------------------------------------------------------------------------
# Duplicates (FR1.5)
# ---------------------------------------------------------------------------


async def test_duplicates_in_file_and_against_active_are_reported_and_skipped(service, repo):
    first = await service.import_csv(TENANT_A, "admin-1", csv_text([purchase_row(1)]), dry_run=False)
    existing_id = first["created_entry_ids"][0]
    rows = [
        purchase_row(1),  # duplicates the active entry
        purchase_row(2),
        purchase_row(2),  # duplicates row 2 of this file
        purchase_row(3),
    ]
    dry = await service.import_csv(TENANT_A, "admin-1", csv_text(rows))
    assert dry["rows_total"] == 4
    assert dry["rows_valid"] == 2
    assert [(d["row"], d.get("existing_entry_id"), d.get("duplicate_of_row")) for d in dry["duplicates"]] == [
        (1, existing_id, None),
        (3, None, 2),
    ]
    committed = await service.import_csv(TENANT_A, "admin-1", csv_text(rows), dry_run=False)
    assert len(committed["created_entry_ids"]) == 2
    assert len(await all_entries(repo)) == 3


async def test_same_instant_with_another_offset_is_a_duplicate(service):
    await service.import_csv(
        TENANT_A, "admin-1", csv_text(["override,DIESEL_2,,2026-10-01T10:00:00Z,2.4,,,,,"]), dry_run=False
    )
    report = await service.import_csv(
        TENANT_A, "admin-1", csv_text(["override,DIESEL_2,,2026-10-01T05:00:00-05:00,2.5,,,,,"])
    )
    assert len(report["duplicates"]) == 1


async def test_a_bol_already_priced_is_a_row_error(service, repo):
    await service.import_csv(TENANT_A, "admin-1", csv_text([purchase_row(1, bol="BOL-1")]), dry_run=False)
    with pytest.raises(AppException) as info:
        await service.import_csv(
            TENANT_A, "admin-1", csv_text([purchase_row(5, bol="BOL-1", ref="other")]), dry_run=False
        )
    assert info.value.details["errors"][0]["type"] == "bol_already_priced"
    assert len(await all_entries(repo)) == 1


async def test_one_bol_on_two_rows_is_a_row_error(service):
    rows = [purchase_row(1, bol="BOL-1"), purchase_row(2, bol="BOL-1")]
    with pytest.raises(AppException) as info:
        await service.import_csv(TENANT_A, "admin-1", csv_text(rows))
    assert [(e["row"], e["type"]) for e in info.value.details["errors"]] == [(2, "duplicate_bol")]


async def test_a_race_on_insert_is_409(service, repo, monkeypatch):
    async def racing_insert(tenant_id, rows):
        raise MarginDuplicateEntryError(None)

    monkeypatch.setattr(repo, "insert_entries", racing_insert)
    with pytest.raises(AppException) as info:
        await service.import_csv(TENANT_A, "admin-1", csv_text([purchase_row(1)]), dry_run=False)
    assert info.value.status_code == 409
    assert "dry run" in info.value.message


# ---------------------------------------------------------------------------
# Batching budget
# ---------------------------------------------------------------------------


async def test_10000_rows_with_5_terminals_and_3000_bols_issue_5_terminal_reads_and_3_bol_queries(
    repo, telemetry
):
    store = FakeDocStore()
    rows: List[str] = []
    for i in range(IMPORT_MAX_ROWS):
        terminal = TERMINALS_A[i % 5]
        if i < 3_000:
            bol_id = f"BOL-{i:05d}"
            store.add(
                "terminal_bols",
                {"bol_id": bol_id, "tenant_id": TENANT_A, "terminal_id": terminal, "product_code": "DIESEL_2"},
            )
            rows.append(purchase_row(i, terminal=terminal, bol=bol_id))
        else:
            rows.append(purchase_row(i, terminal=terminal))
    terminals = FakeTerminals({TENANT_A: TERMINALS_A})
    service = MarginCostEntryService(
        repo, terminals=terminals, es_service=store, telemetry=telemetry, clock=lambda: NOW
    )
    report = await service.import_csv(TENANT_A, "admin-1", csv_text(rows))
    assert report["rows_total"] == IMPORT_MAX_ROWS
    assert report["rows_valid"] == IMPORT_MAX_ROWS
    assert len(terminals.calls) == 5
    bol_queries = store.calls_to("terminal_bols")
    assert len(bol_queries) == 3
    assert all(tenant_of_query(q) == TENANT_A for q in bol_queries)
    assert all(len(q["query"]["bool"]["must"][0]["terms"]["bol_id"]) <= 1_000 for q in bol_queries)
