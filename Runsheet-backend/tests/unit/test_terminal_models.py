"""
Unit tests for :mod:`fuel.terminal_models`.

Covers Capability 8 / Requirements 8.1.1, 8.3.1, 8.4.1, 8.5.4 of the fuel-
ops hardening spec:

* :class:`Terminal`, :class:`SupplierContract`, :class:`TerminalWaitReport`
  and :class:`SourcingRecommendation` model validation — field shapes,
  coordinate bounds, cross-field invariants (branded/supplier_brand,
  open<close, effective_from<=effective_to, driver_report requires
  reporter_id), and product-code canonicalization on write.
* :class:`TerminalRepository`, :class:`SupplierContractRepository`,
  :class:`TerminalWaitReportRepository`, and
  :class:`SourcingRecommendationRepository` async CRUD, all tenant-scoped:
    - create → writes to ES with canonicalized product codes,
      stamps ``updated_at`` / ``created_at``, mints ids when omitted,
      rejects cross-tenant payloads.
    - get → returns the model, ``None`` when missing, ``None`` when owned
      by another tenant (no existence leak).
    - list_for_tenant → filters, drops mis-labelled records with a
      warning, never returns another tenant's data.
    - update → tenant-scoped, strips immutable fields, canonicalizes
      product codes, raises CrossTenantAccessError on cross-tenant
      writes, returns None for missing.
    - delete → returns True on success, False when missing, raises
      CrossTenantAccessError when owned by a different tenant.

The ElasticsearchService dependency is replaced with a recording async
mock so tests never touch a real cluster.

Validates: Requirements 8.1.1, 8.3.1, 8.4.1, 8.5.4.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from pydantic import ValidationError

from fuel.services.fuel_ops_es_mappings import (
    SOURCING_RECOMMENDATIONS_INDEX,
    SUPPLIER_CONTRACTS_INDEX,
    TERMINALS_INDEX,
    TERMINAL_WAIT_REPORTS_INDEX,
)
from fuel.services.fuel_product_catalog import UnknownFuelProductError
from fuel.terminal_models import (
    CrossTenantAccessError,
    OperatingHours,
    SourcingRecommendation,
    SourcingRecommendationRepository,
    SupplierContract,
    SupplierContractRepository,
    Terminal,
    TerminalCandidate,
    TerminalRepository,
    TerminalWaitReport,
    TerminalWaitReportRepository,
)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeESService:
    """In-memory recording mock for ElasticsearchService.

    Covers only the subset of async calls the terminal repositories make:

        * ``index_document``
        * ``update_document``
        * ``delete_document``
        * ``search_documents``

    Supports ``term`` clauses (and one ``range`` gte filter used by the
    wait-report repository) inside ``bool.must`` as well as the shorthand
    ``{"query": {"term": {...}}}`` used by ``_fetch_source``.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.search_calls: List[Dict[str, Any]] = []
        self.index_calls: List[Dict[str, Any]] = []
        self.update_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[str] = []

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.index_calls.append({"index": index, "id": doc_id, "doc": dict(document)})
        self.docs[doc_id] = dict(document)
        return {"_id": doc_id, "result": "created"}

    async def update_document(
        self, index: str, doc_id: str, partial_doc: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.update_calls.append(
            {"index": index, "id": doc_id, "partial": dict(partial_doc)}
        )
        existing = self.docs.get(doc_id, {})
        self.docs[doc_id] = {**existing, **partial_doc}
        return {"_id": doc_id, "result": "updated"}

    async def delete_document(self, index: str, doc_id: str) -> bool:
        self.delete_calls.append(doc_id)
        return self.docs.pop(doc_id, None) is not None

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int = 100
    ) -> Dict[str, Any]:
        self.search_calls.append({"index": index, "query": query, "size": size})
        matched = [doc for doc in self.docs.values() if _matches_query(doc, query)]
        return {"hits": {"hits": [{"_source": dict(d)} for d in matched[:size]]}}


def _matches_query(doc: Dict[str, Any], query: Dict[str, Any]) -> bool:
    """Minimal ES bool-filter matcher supporting ``term`` + ``range(gte)``."""

    inner = query.get("query", {})
    must: List[Dict[str, Any]] = inner.get("bool", {}).get("must", [])
    if not must and "term" in inner:
        must = [inner]

    for clause in must:
        if "term" in clause:
            for field, expected in clause["term"].items():
                actual = doc.get(field)
                # Terminals store supported_products / preferred_terminal_ids
                # as lists; match if the expected token is a member.
                if isinstance(actual, list):
                    if expected not in actual:
                        return False
                elif actual != expected:
                    return False
        elif "range" in clause:
            for field, predicates in clause["range"].items():
                actual = doc.get(field)
                if actual is None:
                    return False
                if "gte" in predicates and str(actual) < str(predicates["gte"]):
                    return False
    return True


# ---------------------------------------------------------------------------
# Terminal model
# ---------------------------------------------------------------------------


def _base_terminal_kwargs(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "terminal_id": "term_001",
        "tenant_id": "tenant-A",
        "name": "Newark Terminal",
        "operator": "Buckeye",
        "location_lat": 40.735,
        "location_lon": -74.172,
        "address": "100 Port St, Newark NJ",
        "timezone": "America/New_York",
        "operating_hours": [
            {"day_of_week": "mon", "open": "06:00", "close": "22:00"}
        ],
        "supported_products": ["DIESEL_2", "GASOLINE_REG"],
        "branded": False,
        "status": "active",
    }
    payload.update(overrides)
    return payload


class TestTerminalModel:
    def test_valid_payload_round_trips(self):
        terminal = Terminal(**_base_terminal_kwargs())
        assert terminal.terminal_id == "term_001"
        assert terminal.supported_products == ["DIESEL_2", "GASOLINE_REG"]
        assert terminal.branded is False
        assert terminal.supplier_brand is None

    def test_rejects_latitude_out_of_range(self):
        with pytest.raises(ValidationError):
            Terminal(**_base_terminal_kwargs(location_lat=100.0))

    def test_rejects_longitude_out_of_range(self):
        with pytest.raises(ValidationError):
            Terminal(**_base_terminal_kwargs(location_lon=-200.0))

    def test_rejects_blank_required_strings(self):
        with pytest.raises(ValidationError):
            Terminal(**_base_terminal_kwargs(name="   "))
        with pytest.raises(ValidationError):
            Terminal(**_base_terminal_kwargs(tenant_id=""))

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            Terminal(**_base_terminal_kwargs(status="retired"))

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            Terminal(**_base_terminal_kwargs(mystery_field="boom"))

    def test_canonicalizes_legacy_product_aliases(self):
        terminal = Terminal(
            **_base_terminal_kwargs(supported_products=["AGO", "LPG"])
        )
        # AGO → DIESEL_2 and LPG → PROPANE.
        assert terminal.supported_products == ["DIESEL_2", "PROPANE"]

    def test_dedupes_supported_products_including_aliases(self):
        terminal = Terminal(
            **_base_terminal_kwargs(supported_products=["DIESEL_2", "AGO", "diesel_2"])
        )
        assert terminal.supported_products == ["DIESEL_2"]

    def test_rejects_unknown_supported_product(self):
        with pytest.raises((ValidationError, UnknownFuelProductError)):
            Terminal(**_base_terminal_kwargs(supported_products=["UNOBTAINIUM"]))

    def test_branded_requires_supplier_brand(self):
        with pytest.raises(ValidationError):
            Terminal(**_base_terminal_kwargs(branded=True))

    def test_unbranded_rejects_supplier_brand(self):
        with pytest.raises(ValidationError):
            Terminal(**_base_terminal_kwargs(branded=False, supplier_brand="Shell"))

    def test_branded_with_supplier_brand_ok(self):
        terminal = Terminal(
            **_base_terminal_kwargs(branded=True, supplier_brand="Shell")
        )
        assert terminal.branded is True
        assert terminal.supplier_brand == "Shell"

    def test_rejects_duplicate_operating_day(self):
        with pytest.raises(ValidationError):
            Terminal(
                **_base_terminal_kwargs(
                    operating_hours=[
                        {"day_of_week": "mon", "open": "06:00", "close": "12:00"},
                        {"day_of_week": "mon", "open": "14:00", "close": "20:00"},
                    ]
                )
            )

    def test_operating_hours_bad_time_format(self):
        with pytest.raises(ValidationError):
            Terminal(
                **_base_terminal_kwargs(
                    operating_hours=[
                        {"day_of_week": "mon", "open": "6am", "close": "10pm"}
                    ]
                )
            )

    def test_operating_hours_open_must_precede_close(self):
        with pytest.raises(ValidationError):
            Terminal(
                **_base_terminal_kwargs(
                    operating_hours=[
                        {"day_of_week": "mon", "open": "18:00", "close": "06:00"}
                    ]
                )
            )

    def test_operating_hours_rejects_bad_day(self):
        with pytest.raises(ValidationError):
            OperatingHours(day_of_week="funday", open="06:00", close="22:00")


# ---------------------------------------------------------------------------
# SupplierContract model
# ---------------------------------------------------------------------------


def _base_contract_kwargs(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "contract_id": "sc_001",
        "tenant_id": "tenant-A",
        "supplier_name": "BP North America",
        "product_code": "DIESEL_2",
        "preferred_terminal_ids": ["term_001"],
        "contract_price_per_gallon_usd": 3.25,
        "branded_required": True,
        "minimum_lift_gallons_per_month": 100000,
        "effective_from": date(2025, 1, 1),
        "effective_to": date(2025, 12, 31),
        "status": "active",
    }
    payload.update(overrides)
    return payload


class TestSupplierContractModel:
    def test_valid_payload_round_trips(self):
        contract = SupplierContract(**_base_contract_kwargs())
        assert contract.contract_id == "sc_001"
        assert contract.product_code == "DIESEL_2"

    def test_canonicalizes_legacy_alias(self):
        contract = SupplierContract(**_base_contract_kwargs(product_code="AGO"))
        assert contract.product_code == "DIESEL_2"

    def test_rejects_unknown_product_code(self):
        with pytest.raises((ValidationError, UnknownFuelProductError)):
            SupplierContract(**_base_contract_kwargs(product_code="UNOBTAINIUM"))

    def test_effective_to_before_effective_from_rejected(self):
        with pytest.raises(ValidationError):
            SupplierContract(
                **_base_contract_kwargs(
                    effective_from=date(2025, 6, 1),
                    effective_to=date(2025, 3, 1),
                )
            )

    def test_effective_to_equal_to_from_allowed(self):
        contract = SupplierContract(
            **_base_contract_kwargs(
                effective_from=date(2025, 1, 1),
                effective_to=date(2025, 1, 1),
            )
        )
        assert contract.effective_to == date(2025, 1, 1)

    def test_effective_to_nullable(self):
        contract = SupplierContract(
            **_base_contract_kwargs(effective_to=None)
        )
        assert contract.effective_to is None

    def test_rejects_negative_contract_price(self):
        with pytest.raises(ValidationError):
            SupplierContract(
                **_base_contract_kwargs(contract_price_per_gallon_usd=-1.0)
            )

    def test_rejects_negative_minimum_lift(self):
        with pytest.raises(ValidationError):
            SupplierContract(
                **_base_contract_kwargs(minimum_lift_gallons_per_month=-1)
            )

    def test_dedupes_preferred_terminal_ids(self):
        contract = SupplierContract(
            **_base_contract_kwargs(
                preferred_terminal_ids=["term_001", " term_001 ", "term_002", ""]
            )
        )
        assert contract.preferred_terminal_ids == ["term_001", "term_002"]

    def test_rejects_blank_required_strings(self):
        with pytest.raises(ValidationError):
            SupplierContract(**_base_contract_kwargs(supplier_name="   "))

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            SupplierContract(**_base_contract_kwargs(mystery="x"))


# ---------------------------------------------------------------------------
# TerminalWaitReport model
# ---------------------------------------------------------------------------


def _base_wait_report_kwargs(**overrides: Any) -> Dict[str, Any]:
    observed = datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
    payload: Dict[str, Any] = {
        "report_id": "twr_001",
        "tenant_id": "tenant-A",
        "terminal_id": "term_001",
        "wait_minutes": 45.0,
        "source": "driver_report",
        "reporter_id": "driver_017",
        "observed_at": observed,
        "retrieved_at": observed + timedelta(seconds=30),
    }
    payload.update(overrides)
    return payload


class TestTerminalWaitReportModel:
    def test_valid_driver_report_round_trips(self):
        report = TerminalWaitReport(**_base_wait_report_kwargs())
        assert report.source == "driver_report"
        assert report.reporter_id == "driver_017"

    def test_driver_report_requires_reporter(self):
        with pytest.raises(ValidationError):
            TerminalWaitReport(**_base_wait_report_kwargs(reporter_id=None))

    def test_eld_geofence_report_no_reporter_required(self):
        report = TerminalWaitReport(
            **_base_wait_report_kwargs(
                source="eld_geofence", reporter_id=None, truck_id="truck_17"
            )
        )
        assert report.source == "eld_geofence"
        assert report.reporter_id is None

    def test_rejects_negative_wait_minutes(self):
        with pytest.raises(ValidationError):
            TerminalWaitReport(**_base_wait_report_kwargs(wait_minutes=-1))

    def test_rejects_unknown_source(self):
        with pytest.raises(ValidationError):
            TerminalWaitReport(**_base_wait_report_kwargs(source="tweet"))

    def test_retrieved_at_before_observed_rejected(self):
        observed = datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
        with pytest.raises(ValidationError):
            TerminalWaitReport(
                **_base_wait_report_kwargs(
                    observed_at=observed,
                    retrieved_at=observed - timedelta(seconds=10),
                )
            )

    def test_rejects_blank_required_strings(self):
        with pytest.raises(ValidationError):
            TerminalWaitReport(**_base_wait_report_kwargs(terminal_id="   "))

    def test_notes_field_round_trips(self):
        """Escalation #2: the optional dispatcher / driver ``notes``
        field persists the value verbatim after the
        ``_strip_optional_strings`` validator trims surrounding
        whitespace."""

        report = TerminalWaitReport(
            **_base_wait_report_kwargs(notes="  Rack outage delayed load  ")
        )
        assert report.notes == "Rack outage delayed load"

    def test_notes_empty_string_coerced_to_none(self):
        """Empty strings (and whitespace-only values) are normalized
        to ``None`` just like ``reporter_id`` / ``truck_id`` so the
        dispatcher textarea never persists an empty string."""

        for blank in ("", "   ", "\n\t"):
            report = TerminalWaitReport(
                **_base_wait_report_kwargs(notes=blank)
            )
            assert report.notes is None, blank

    def test_notes_defaults_to_none(self):
        """Omitting ``notes`` leaves it ``None`` — backwards compatible
        with callers written before the field existed."""

        report = TerminalWaitReport(**_base_wait_report_kwargs())
        assert report.notes is None

    def test_notes_rejects_over_1000_chars(self):
        """Pydantic ``max_length=1000`` trips a ValidationError on
        over-long notes so downstream persistence never sees oversized
        payloads."""

        with pytest.raises(ValidationError):
            TerminalWaitReport(
                **_base_wait_report_kwargs(notes="x" * 1001)
            )


# ---------------------------------------------------------------------------
# SourcingRecommendation model
# ---------------------------------------------------------------------------


def _base_candidate_kwargs(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "terminal_id": "term_001",
        "price_per_gallon_usd": 3.25,
        "branded_flag": False,
        "contract_id": None,
        "avg_wait_minutes": 10.0,
        "distance_km_from_start": 12.5,
        "score": 0.78,
        "reasons": ["lowest_price", "short_distance"],
        "wait_warning": False,
    }
    payload.update(overrides)
    return payload


def _base_recommendation_kwargs(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "recommendation_id": "srec_001",
        "request_id": "req_1",
        "tenant_id": "tenant-A",
        "truck_id": "truck_17",
        "run_id": "run_42",
        "product_code": "DIESEL_2",
        "volume_gallons": 8800.0,
        "origin_lat": 40.73,
        "origin_lon": -74.17,
        "candidates": [_base_candidate_kwargs()],
        "rack_price_fallback": False,
        "generated_at": datetime(2025, 3, 15, 15, 0, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return payload


class TestSourcingRecommendationModel:
    def test_valid_payload_round_trips(self):
        rec = SourcingRecommendation(**_base_recommendation_kwargs())
        assert rec.product_code == "DIESEL_2"
        assert len(rec.candidates) == 1
        assert isinstance(rec.candidates[0], TerminalCandidate)

    def test_canonicalizes_legacy_product_code(self):
        rec = SourcingRecommendation(
            **_base_recommendation_kwargs(product_code="AGO")
        )
        assert rec.product_code == "DIESEL_2"

    def test_rejects_zero_volume(self):
        with pytest.raises(ValidationError):
            SourcingRecommendation(
                **_base_recommendation_kwargs(volume_gallons=0)
            )

    def test_rejects_out_of_range_origin(self):
        with pytest.raises(ValidationError):
            SourcingRecommendation(
                **_base_recommendation_kwargs(origin_lat=91.0)
            )

    def test_candidate_score_clamped_to_0_1(self):
        with pytest.raises(ValidationError):
            TerminalCandidate(**_base_candidate_kwargs(score=1.5))
        with pytest.raises(ValidationError):
            TerminalCandidate(**_base_candidate_kwargs(score=-0.1))

    def test_candidate_negative_distance_rejected(self):
        with pytest.raises(ValidationError):
            TerminalCandidate(**_base_candidate_kwargs(distance_km_from_start=-1))

    def test_allows_empty_candidates(self):
        rec = SourcingRecommendation(
            **_base_recommendation_kwargs(candidates=[])
        )
        assert rec.candidates == []

    def test_wait_warning_terminal_ids_derived_from_candidates(self):
        """Task 7.11 — top-level list is computed from candidate flags."""

        candidates = [
            _base_candidate_kwargs(terminal_id="term_001", wait_warning=False),
            _base_candidate_kwargs(terminal_id="term_002", wait_warning=True),
            _base_candidate_kwargs(terminal_id="term_003", wait_warning=True),
        ]
        rec = SourcingRecommendation(
            **_base_recommendation_kwargs(candidates=candidates)
        )
        # Preserves candidate order so the highest-ranked offender appears first.
        assert rec.wait_warning_terminal_ids == ["term_002", "term_003"]

    def test_wait_warning_terminal_ids_ignores_caller_input(self):
        """Derived field — callers cannot inject arbitrary ids."""

        rec = SourcingRecommendation(
            **_base_recommendation_kwargs(
                candidates=[
                    _base_candidate_kwargs(
                        terminal_id="term_001", wait_warning=True
                    )
                ],
                wait_warning_terminal_ids=["term_bogus"],
            )
        )
        assert rec.wait_warning_terminal_ids == ["term_001"]

    def test_wait_warning_terminal_ids_empty_when_all_clear(self):
        rec = SourcingRecommendation(**_base_recommendation_kwargs())
        assert rec.wait_warning_terminal_ids == []


# ---------------------------------------------------------------------------
# Repository — construction + Terminal CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
def es() -> _FakeESService:
    return _FakeESService()


@pytest.fixture
def terminal_repo(es: _FakeESService) -> TerminalRepository:
    return TerminalRepository(es_service=es)


@pytest.fixture
def contract_repo(es: _FakeESService) -> SupplierContractRepository:
    return SupplierContractRepository(es_service=es)


@pytest.fixture
def wait_repo(es: _FakeESService) -> TerminalWaitReportRepository:
    return TerminalWaitReportRepository(es_service=es)


@pytest.fixture
def sourcing_repo(es: _FakeESService) -> SourcingRecommendationRepository:
    return SourcingRecommendationRepository(es_service=es)


class TestRepositoryConstruction:
    def test_rejects_none_es_service(self):
        with pytest.raises(ValueError):
            TerminalRepository(es_service=None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            SupplierContractRepository(es_service=None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            TerminalWaitReportRepository(es_service=None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            SourcingRecommendationRepository(es_service=None)  # type: ignore[arg-type]

    def test_rejects_empty_index_name(self, es: _FakeESService):
        with pytest.raises(ValueError):
            TerminalRepository(es_service=es, index_name="")

    async def test_defaults_to_canonical_indices(self, es: _FakeESService):
        await TerminalRepository(es_service=es).list_for_tenant("tenant-A")
        await SupplierContractRepository(es_service=es).list_for_tenant("tenant-A")
        await TerminalWaitReportRepository(es_service=es).list_for_tenant("tenant-A")
        await SourcingRecommendationRepository(es_service=es).list_for_tenant(
            "tenant-A"
        )
        used = [call["index"] for call in es.search_calls]
        assert TERMINALS_INDEX in used
        assert SUPPLIER_CONTRACTS_INDEX in used
        assert TERMINAL_WAIT_REPORTS_INDEX in used
        assert SOURCING_RECOMMENDATIONS_INDEX in used


class TestTerminalRepository:
    async def test_create_persists_canonical_payload(
        self, terminal_repo: TerminalRepository, es: _FakeESService
    ):
        terminal = Terminal(**_base_terminal_kwargs())
        result = await terminal_repo.create("tenant-A", terminal)
        assert result.terminal_id == "term_001"
        assert len(es.index_calls) == 1
        call = es.index_calls[0]
        assert call["index"] == TERMINALS_INDEX
        assert call["id"] == "term_001"
        assert call["doc"]["tenant_id"] == "tenant-A"
        assert call["doc"]["supported_products"] == ["DIESEL_2", "GASOLINE_REG"]
        assert call["doc"]["created_at"]
        assert call["doc"]["updated_at"]

    async def test_create_canonicalizes_dict_aliases(
        self, terminal_repo: TerminalRepository, es: _FakeESService
    ):
        payload = _base_terminal_kwargs(supported_products=["AGO", "LPG"])
        result = await terminal_repo.create("tenant-A", payload)
        assert result.supported_products == ["DIESEL_2", "PROPANE"]
        assert es.index_calls[0]["doc"]["supported_products"] == [
            "DIESEL_2",
            "PROPANE",
        ]

    async def test_create_mints_id_when_missing(
        self, terminal_repo: TerminalRepository, es: _FakeESService
    ):
        payload = _base_terminal_kwargs()
        payload.pop("terminal_id")
        result = await terminal_repo.create("tenant-A", payload)
        assert result.terminal_id.startswith("term_")
        assert es.index_calls[0]["id"] == result.terminal_id

    async def test_create_rejects_cross_tenant_payload(
        self, terminal_repo: TerminalRepository
    ):
        payload = _base_terminal_kwargs(tenant_id="tenant-B")
        with pytest.raises(CrossTenantAccessError):
            await terminal_repo.create("tenant-A", payload)

    async def test_create_requires_tenant_id(
        self, terminal_repo: TerminalRepository
    ):
        with pytest.raises(ValueError):
            await terminal_repo.create("", _base_terminal_kwargs())

    async def test_get_returns_model(
        self, terminal_repo: TerminalRepository
    ):
        await terminal_repo.create("tenant-A", _base_terminal_kwargs())
        result = await terminal_repo.get("tenant-A", "term_001")
        assert result is not None
        assert result.terminal_id == "term_001"

    async def test_get_suppresses_cross_tenant_read(
        self, terminal_repo: TerminalRepository
    ):
        await terminal_repo.create("tenant-A", _base_terminal_kwargs())
        result = await terminal_repo.get("tenant-B", "term_001")
        assert result is None

    async def test_get_returns_none_for_missing(
        self, terminal_repo: TerminalRepository
    ):
        result = await terminal_repo.get("tenant-A", "does_not_exist")
        assert result is None

    async def test_list_for_tenant_isolates_data(
        self, terminal_repo: TerminalRepository
    ):
        await terminal_repo.create("tenant-A", _base_terminal_kwargs())
        await terminal_repo.create(
            "tenant-B",
            _base_terminal_kwargs(terminal_id="term_002", tenant_id="tenant-B"),
        )
        a = await terminal_repo.list_for_tenant("tenant-A")
        b = await terminal_repo.list_for_tenant("tenant-B")
        assert [t.terminal_id for t in a] == ["term_001"]
        assert [t.terminal_id for t in b] == ["term_002"]

    async def test_list_filters_by_status_and_operator(
        self, terminal_repo: TerminalRepository
    ):
        await terminal_repo.create(
            "tenant-A",
            _base_terminal_kwargs(
                terminal_id="term_001", operator="Buckeye", status="active"
            ),
        )
        await terminal_repo.create(
            "tenant-A",
            _base_terminal_kwargs(
                terminal_id="term_002", operator="Magellan", status="inactive"
            ),
        )
        actives = await terminal_repo.list_for_tenant("tenant-A", status="active")
        buckeyes = await terminal_repo.list_for_tenant(
            "tenant-A", operator="Buckeye"
        )
        assert [t.terminal_id for t in actives] == ["term_001"]
        assert [t.terminal_id for t in buckeyes] == ["term_001"]

    async def test_list_filters_by_supported_product_alias(
        self, terminal_repo: TerminalRepository
    ):
        await terminal_repo.create(
            "tenant-A",
            _base_terminal_kwargs(
                terminal_id="term_001", supported_products=["DIESEL_2"]
            ),
        )
        await terminal_repo.create(
            "tenant-A",
            _base_terminal_kwargs(
                terminal_id="term_002", supported_products=["GASOLINE_REG"]
            ),
        )
        # AGO canonicalizes to DIESEL_2 → finds term_001 only.
        result = await terminal_repo.list_for_tenant(
            "tenant-A", supported_product="AGO"
        )
        assert [t.terminal_id for t in result] == ["term_001"]

    async def test_update_applies_patch_and_strips_immutables(
        self,
        terminal_repo: TerminalRepository,
        es: _FakeESService,
    ):
        await terminal_repo.create("tenant-A", _base_terminal_kwargs())
        result = await terminal_repo.update(
            "tenant-A",
            "term_001",
            {"name": "Renamed Terminal", "tenant_id": "other", "created_at": "1999-01-01"},
        )
        assert result is not None
        assert result.name == "Renamed Terminal"
        partial = es.update_calls[-1]["partial"]
        assert "tenant_id" not in partial
        assert "created_at" not in partial
        assert partial["name"] == "Renamed Terminal"

    async def test_update_cross_tenant_raises(
        self, terminal_repo: TerminalRepository
    ):
        await terminal_repo.create("tenant-A", _base_terminal_kwargs())
        with pytest.raises(CrossTenantAccessError):
            await terminal_repo.update("tenant-B", "term_001", {"name": "Rename"})

    async def test_update_returns_none_for_missing(
        self, terminal_repo: TerminalRepository
    ):
        result = await terminal_repo.update(
            "tenant-A", "does_not_exist", {"name": "nope"}
        )
        assert result is None

    async def test_delete_returns_true_on_success(
        self, terminal_repo: TerminalRepository
    ):
        await terminal_repo.create("tenant-A", _base_terminal_kwargs())
        assert await terminal_repo.delete("tenant-A", "term_001") is True
        assert await terminal_repo.get("tenant-A", "term_001") is None

    async def test_delete_returns_false_for_missing(
        self, terminal_repo: TerminalRepository
    ):
        assert (
            await terminal_repo.delete("tenant-A", "does_not_exist") is False
        )

    async def test_delete_cross_tenant_raises(
        self, terminal_repo: TerminalRepository
    ):
        await terminal_repo.create("tenant-A", _base_terminal_kwargs())
        with pytest.raises(CrossTenantAccessError):
            await terminal_repo.delete("tenant-B", "term_001")


# ---------------------------------------------------------------------------
# Repository — SupplierContract CRUD
# ---------------------------------------------------------------------------


class TestSupplierContractRepository:
    async def test_create_persists_canonical_product_code(
        self, contract_repo: SupplierContractRepository, es: _FakeESService
    ):
        payload = _base_contract_kwargs(product_code="AGO")
        result = await contract_repo.create("tenant-A", payload)
        assert result.product_code == "DIESEL_2"
        assert es.index_calls[0]["doc"]["product_code"] == "DIESEL_2"

    async def test_create_rejects_cross_tenant_payload(
        self, contract_repo: SupplierContractRepository
    ):
        payload = _base_contract_kwargs(tenant_id="tenant-B")
        with pytest.raises(CrossTenantAccessError):
            await contract_repo.create("tenant-A", payload)

    async def test_get_isolated_per_tenant(
        self, contract_repo: SupplierContractRepository
    ):
        await contract_repo.create("tenant-A", _base_contract_kwargs())
        assert await contract_repo.get("tenant-A", "sc_001") is not None
        assert await contract_repo.get("tenant-B", "sc_001") is None

    async def test_list_filters_by_product_and_preferred_terminal(
        self, contract_repo: SupplierContractRepository
    ):
        await contract_repo.create(
            "tenant-A",
            _base_contract_kwargs(
                contract_id="sc_001",
                product_code="DIESEL_2",
                preferred_terminal_ids=["term_001"],
            ),
        )
        await contract_repo.create(
            "tenant-A",
            _base_contract_kwargs(
                contract_id="sc_002",
                product_code="GASOLINE_REG",
                preferred_terminal_ids=["term_002"],
            ),
        )
        diesels = await contract_repo.list_for_tenant(
            "tenant-A", product_code="AGO"
        )
        at_term_001 = await contract_repo.list_for_tenant(
            "tenant-A", preferred_terminal_id="term_001"
        )
        assert [c.contract_id for c in diesels] == ["sc_001"]
        assert [c.contract_id for c in at_term_001] == ["sc_001"]

    async def test_update_canonicalizes_patch_product_code(
        self,
        contract_repo: SupplierContractRepository,
        es: _FakeESService,
    ):
        await contract_repo.create("tenant-A", _base_contract_kwargs())
        result = await contract_repo.update(
            "tenant-A", "sc_001", {"product_code": "LPG"}
        )
        assert result is not None
        assert result.product_code == "PROPANE"
        assert es.update_calls[-1]["partial"]["product_code"] == "PROPANE"

    async def test_update_cross_tenant_raises(
        self, contract_repo: SupplierContractRepository
    ):
        await contract_repo.create("tenant-A", _base_contract_kwargs())
        with pytest.raises(CrossTenantAccessError):
            await contract_repo.update(
                "tenant-B", "sc_001", {"supplier_name": "Other"}
            )

    async def test_delete_cross_tenant_raises(
        self, contract_repo: SupplierContractRepository
    ):
        await contract_repo.create("tenant-A", _base_contract_kwargs())
        with pytest.raises(CrossTenantAccessError):
            await contract_repo.delete("tenant-B", "sc_001")


# ---------------------------------------------------------------------------
# Repository — TerminalWaitReport CRUD
# ---------------------------------------------------------------------------


class TestTerminalWaitReportRepository:
    async def test_create_defaults_retrieved_at(
        self,
        wait_repo: TerminalWaitReportRepository,
        es: _FakeESService,
    ):
        payload = _base_wait_report_kwargs()
        payload.pop("retrieved_at")
        # Nudge observed_at to the past so the auto-stamped retrieved_at is later.
        payload["observed_at"] = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = await wait_repo.create("tenant-A", payload)
        assert result.retrieved_at >= result.observed_at
        assert "retrieved_at" in es.index_calls[0]["doc"]

    async def test_create_driver_report_requires_reporter(
        self, wait_repo: TerminalWaitReportRepository
    ):
        payload = _base_wait_report_kwargs()
        payload["reporter_id"] = None
        with pytest.raises(ValidationError):
            await wait_repo.create("tenant-A", payload)

    async def test_update_cannot_rewrite_observed_at(
        self,
        wait_repo: TerminalWaitReportRepository,
        es: _FakeESService,
    ):
        original = _base_wait_report_kwargs()
        await wait_repo.create("tenant-A", original)
        new_observed = original["observed_at"] + timedelta(hours=1)
        new_retrieved = new_observed + timedelta(minutes=1)
        # observed_at is in the immutable_fields set so it must be stripped.
        result = await wait_repo.update(
            "tenant-A",
            "twr_001",
            {
                "observed_at": new_observed,
                "retrieved_at": new_retrieved,
                "wait_minutes": 60.0,
            },
        )
        assert result is not None
        assert result.observed_at == original["observed_at"]
        assert result.wait_minutes == 60.0

    async def test_list_filters_by_terminal_and_source(
        self, wait_repo: TerminalWaitReportRepository
    ):
        base = _base_wait_report_kwargs()
        await wait_repo.create(
            "tenant-A",
            {**base, "report_id": "twr_001", "terminal_id": "term_001"},
        )
        await wait_repo.create(
            "tenant-A",
            {
                **base,
                "report_id": "twr_002",
                "terminal_id": "term_002",
                "source": "eld_geofence",
                "reporter_id": None,
                "truck_id": "truck_17",
            },
        )
        by_terminal = await wait_repo.list_for_tenant(
            "tenant-A", terminal_id="term_001"
        )
        by_source = await wait_repo.list_for_tenant(
            "tenant-A", source="eld_geofence"
        )
        assert [r.report_id for r in by_terminal] == ["twr_001"]
        assert [r.report_id for r in by_source] == ["twr_002"]

    async def test_list_is_tenant_isolated(
        self, wait_repo: TerminalWaitReportRepository
    ):
        await wait_repo.create("tenant-A", _base_wait_report_kwargs())
        await wait_repo.create(
            "tenant-B",
            _base_wait_report_kwargs(report_id="twr_999", tenant_id="tenant-B"),
        )
        a = await wait_repo.list_for_tenant("tenant-A")
        b = await wait_repo.list_for_tenant("tenant-B")
        assert [r.report_id for r in a] == ["twr_001"]
        assert [r.report_id for r in b] == ["twr_999"]

    async def test_delete_cross_tenant_raises(
        self, wait_repo: TerminalWaitReportRepository
    ):
        await wait_repo.create("tenant-A", _base_wait_report_kwargs())
        with pytest.raises(CrossTenantAccessError):
            await wait_repo.delete("tenant-B", "twr_001")


# ---------------------------------------------------------------------------
# Repository — SourcingRecommendation CRUD
# ---------------------------------------------------------------------------


class TestSourcingRecommendationRepository:
    async def test_create_canonicalizes_product_code(
        self,
        sourcing_repo: SourcingRecommendationRepository,
        es: _FakeESService,
    ):
        payload = _base_recommendation_kwargs(product_code="PMS")
        result = await sourcing_repo.create("tenant-A", payload)
        assert result.product_code == "GASOLINE_REG"
        assert es.index_calls[0]["doc"]["product_code"] == "GASOLINE_REG"

    async def test_create_fills_defaults(
        self,
        sourcing_repo: SourcingRecommendationRepository,
        es: _FakeESService,
    ):
        payload = _base_recommendation_kwargs()
        payload.pop("request_id")
        result = await sourcing_repo.create("tenant-A", payload)
        assert result.request_id.startswith("req_")
        assert es.index_calls[0]["doc"]["generated_at"]

    async def test_create_cross_tenant_rejected(
        self, sourcing_repo: SourcingRecommendationRepository
    ):
        payload = _base_recommendation_kwargs(tenant_id="tenant-B")
        with pytest.raises(CrossTenantAccessError):
            await sourcing_repo.create("tenant-A", payload)

    async def test_get_isolated_per_tenant(
        self, sourcing_repo: SourcingRecommendationRepository
    ):
        await sourcing_repo.create("tenant-A", _base_recommendation_kwargs())
        assert await sourcing_repo.get("tenant-A", "srec_001") is not None
        assert await sourcing_repo.get("tenant-B", "srec_001") is None

    async def test_list_filters_and_isolation(
        self, sourcing_repo: SourcingRecommendationRepository
    ):
        await sourcing_repo.create(
            "tenant-A",
            _base_recommendation_kwargs(
                recommendation_id="srec_001", truck_id="truck_1", run_id="run_1"
            ),
        )
        await sourcing_repo.create(
            "tenant-A",
            _base_recommendation_kwargs(
                recommendation_id="srec_002", truck_id="truck_2", run_id="run_2"
            ),
        )
        await sourcing_repo.create(
            "tenant-B",
            _base_recommendation_kwargs(
                recommendation_id="srec_999",
                truck_id="truck_1",
                run_id="run_1",
                tenant_id="tenant-B",
            ),
        )
        truck_1 = await sourcing_repo.list_for_tenant(
            "tenant-A", truck_id="truck_1"
        )
        run_2 = await sourcing_repo.list_for_tenant(
            "tenant-A", run_id="run_2"
        )
        all_a = await sourcing_repo.list_for_tenant("tenant-A")
        assert [r.recommendation_id for r in truck_1] == ["srec_001"]
        assert [r.recommendation_id for r in run_2] == ["srec_002"]
        assert sorted(r.recommendation_id for r in all_a) == [
            "srec_001",
            "srec_002",
        ]

    async def test_update_immutable_fields_stripped(
        self,
        sourcing_repo: SourcingRecommendationRepository,
        es: _FakeESService,
    ):
        await sourcing_repo.create("tenant-A", _base_recommendation_kwargs())
        # Attempt to mutate volume_gallons (immutable) — should be silently
        # dropped; rack_price_fallback (mutable) should apply.
        result = await sourcing_repo.update(
            "tenant-A",
            "srec_001",
            {"volume_gallons": 100.0, "rack_price_fallback": True},
        )
        assert result is not None
        assert result.volume_gallons == 8800.0
        assert result.rack_price_fallback is True
        partial = es.update_calls[-1]["partial"]
        assert "volume_gallons" not in partial
        assert partial["rack_price_fallback"] is True

    async def test_update_cross_tenant_raises(
        self, sourcing_repo: SourcingRecommendationRepository
    ):
        await sourcing_repo.create("tenant-A", _base_recommendation_kwargs())
        with pytest.raises(CrossTenantAccessError):
            await sourcing_repo.update(
                "tenant-B", "srec_001", {"rack_price_fallback": True}
            )

    async def test_delete_cross_tenant_raises(
        self, sourcing_repo: SourcingRecommendationRepository
    ):
        await sourcing_repo.create("tenant-A", _base_recommendation_kwargs())
        with pytest.raises(CrossTenantAccessError):
            await sourcing_repo.delete("tenant-B", "srec_001")

    async def test_delete_returns_true_on_success(
        self, sourcing_repo: SourcingRecommendationRepository
    ):
        await sourcing_repo.create("tenant-A", _base_recommendation_kwargs())
        assert (
            await sourcing_repo.delete("tenant-A", "srec_001") is True
        )
        assert await sourcing_repo.get("tenant-A", "srec_001") is None


# ---------------------------------------------------------------------------
# Terminal.is_open_at (Task 7.2 — Req 8.1.4)
# ---------------------------------------------------------------------------
#
# :meth:`Terminal.is_open_at` is consumed by:
#
# * the Sourcing_Recommender disqualification step (``terminal_closed``
#   reason), which used to carry a module-private ``_is_open_at`` that
#   has been promoted onto the model, and
# * the Req 8.1.4 ``POST /api/fuel/terminals/{id}/proposed-load``
#   validator which surfaces HTTP 400 with the same reason code plus a
#   next-open-window suggestion.
#
# These tests cover the four documented behaviors: empty operating_hours
# ≡ 24/7, weekday-scoped windows, IANA timezone handling (including the
# known-local-time-during-DST edge), the unknown-timezone degrade-to-
# open contract, and naive-UTC coercion.


class TestTerminalIsOpenAt:
    """Behaviour contract for :meth:`Terminal.is_open_at` (Req 8.1.4)."""

    def _at(self, **kw: Any) -> datetime:
        """Shorthand for a UTC datetime literal used in the checks."""

        kw.setdefault("tzinfo", timezone.utc)
        return datetime(**kw)

    def test_empty_operating_hours_is_24_7(self):
        """An unconstrained schedule always reports open (newly-
        provisioned terminal posture)."""

        terminal = Terminal(**_base_terminal_kwargs(operating_hours=[]))
        # Sunday 03:00 UTC — outside any "normal" business window.
        assert terminal.is_open_at(self._at(year=2025, month=3, day=16, hour=3))
        # Wed 14:30 local.
        assert terminal.is_open_at(
            self._at(year=2025, month=3, day=12, hour=14, minute=30)
        )

    def test_closed_day_returns_false(self):
        """Omitted day-of-week entries mean closed — not an implicit
        fallback to the nearest adjacent window."""

        terminal = Terminal(
            **_base_terminal_kwargs(
                timezone="UTC",
                operating_hours=[
                    {"day_of_week": "mon", "open": "06:00", "close": "22:00"},
                ],
            )
        )
        # 2025-03-11 is a Tuesday — no window, must return False.
        assert not terminal.is_open_at(
            self._at(year=2025, month=3, day=11, hour=12)
        )
        # 2025-03-10 is the matching Monday at 12:00 — within window.
        assert terminal.is_open_at(
            self._at(year=2025, month=3, day=10, hour=12)
        )

    def test_open_boundary_inclusive_close_exclusive(self):
        """``open`` is inclusive; ``close`` is exclusive so the closed
        edge matches the Sourcing_Recommender contract."""

        terminal = Terminal(
            **_base_terminal_kwargs(
                timezone="UTC",
                operating_hours=[
                    {"day_of_week": "mon", "open": "06:00", "close": "22:00"},
                ],
            )
        )
        # 06:00 exact → open.
        assert terminal.is_open_at(
            self._at(year=2025, month=3, day=10, hour=6, minute=0)
        )
        # 22:00 exact → closed (exclusive upper bound).
        assert not terminal.is_open_at(
            self._at(year=2025, month=3, day=10, hour=22, minute=0)
        )
        # 21:59 → still open.
        assert terminal.is_open_at(
            self._at(year=2025, month=3, day=10, hour=21, minute=59)
        )

    def test_local_timezone_conversion(self):
        """Operating_hours are declared in the terminal's local zone, so
        a UTC ``as_of`` must be converted before the window lookup."""

        # Newark: UTC-5 in March (EST; DST begins 2025-03-09). Monday
        # 11:00 local == 15:00 UTC on 2025-03-10 (post-DST). The window
        # is 08:00-18:00 local.
        terminal = Terminal(
            **_base_terminal_kwargs(
                timezone="America/New_York",
                operating_hours=[
                    {"day_of_week": "mon", "open": "08:00", "close": "18:00"},
                ],
            )
        )
        # 15:00 UTC on Monday 2025-03-10 → 11:00 EDT (DST active) → open.
        assert terminal.is_open_at(
            self._at(year=2025, month=3, day=10, hour=15)
        )
        # 03:00 UTC on Monday 2025-03-10 → 23:00 Sunday 2025-03-09 local
        # → Sunday is unscheduled → closed.
        assert not terminal.is_open_at(
            self._at(year=2025, month=3, day=10, hour=3)
        )

    def test_unknown_timezone_degrades_to_open_with_warning(self, caplog):
        """Typo'd IANA names must not block sourcing — the method
        degrades to True and emits a warning the ops team can action."""

        terminal = Terminal(
            **_base_terminal_kwargs(
                timezone="Not/A_Real_TZ",
                operating_hours=[
                    {"day_of_week": "mon", "open": "06:00", "close": "22:00"},
                ],
            )
        )
        with caplog.at_level("WARNING"):
            assert terminal.is_open_at(
                self._at(year=2025, month=3, day=10, hour=12)
            )
        assert any(
            "unknown timezone" in record.getMessage().lower()
            for record in caplog.records
        )

    def test_naive_datetime_treated_as_utc(self):
        """A naive ``as_of`` is coerced to UTC so a caller that drops
        in ``datetime.utcnow()`` does not silently mis-evaluate."""

        terminal = Terminal(
            **_base_terminal_kwargs(
                timezone="UTC",
                operating_hours=[
                    {"day_of_week": "mon", "open": "06:00", "close": "22:00"},
                ],
            )
        )
        # Naive datetime — must be treated as UTC (same wall clock).
        naive_mon_noon = datetime(2025, 3, 10, 12, 0)
        assert terminal.is_open_at(naive_mon_noon)
        # Naive datetime on the closed day.
        naive_tue_noon = datetime(2025, 3, 11, 12, 0)
        assert not terminal.is_open_at(naive_tue_noon)
