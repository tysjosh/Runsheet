"""
Unit tests for the HOS advisory — service and router.

The router is wired to a **real**
:class:`~driver.services.hos_advisory_service.HOSAdvisoryService` over fakes, so
the assertions cover the whole path: the ``drivers_current`` read that resolves
the truck, the ``truck_telemetry`` query that resolves the reading, and the
freshness classification that turns the two into an advisory.

The sharp assertions are about *totality and conservatism*. Every combination of
"no assigned truck / no document / a document of some age" lands on exactly one
of ``fresh`` / ``stale`` / ``unknown``; ``unknown`` carries one of the two reason
codes; a ``stale`` or ``unknown`` reading reports every remaining-hours figure as
``unavailable`` and the compliance state as ``unknown``; and the resolution never
consults ``truck_telemetry.driver_id``, which carries the telematics vendor's
identifier rather than a Runsheet ``driver_id``.

Validates: Requirements 17.1, 17.3, 17.4, 17.5, 17.6, 17.7, 17.8, 17.9, 17.10,
17.11, 17.12, 17.13, 17.14, 17.32
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver.api.hos_endpoints import (
    configure_hos_endpoints,
    configured_hos_advisory_service,
    router as hos_router,
)
from driver.services.driver_es_mappings import (
    HOS_GATE_OVERRIDES_INDEX,
    HOS_GATE_OVERRIDES_MAPPING,
)
from driver.services.hos_advisory_service import (
    AUDIT_GATE_BLOCKED,
    AUDIT_GATE_OVERRIDDEN,
    AUDIT_GATE_PASSED,
    AUDIT_GATE_SKIPPED,
    DEFAULT_FRESHNESS_SECONDS,
    FRESHNESS_STATES,
    HOS_AT_LIMIT,
    HOS_FIGURES_UNAVAILABLE,
    HOS_GATING_DISABLED,
    HOS_GATING_FLAG_KEY,
    HOS_GPS_ELD_DISABLED,
    HOS_NO_READING,
    HOS_OVERRIDE_APPLIED,
    HOS_READING_STALE,
    HOS_TRUCK_UNASSIGNED,
    HOURS_UNIT,
    MAX_OVERRIDE_REASON_LENGTH,
    OVERRIDE_ID_PREFIX,
    OVERRIDE_ROLES,
    UNKNOWN_REASON_CODES,
    HOSAdvisoryService,
)
from errors.exceptions import AppException
from errors.handlers import register_exception_handlers
from fuel.services.fuel_ops_es_mappings import TRUCK_TELEMETRY_INDEX
from fuel.services.order_es_mappings import DRIVERS_CURRENT_INDEX
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "t1"
OTHER_TENANT = "t2"
DRIVER = "drv_1"
OTHER_DRIVER = "drv_2"
TRUCK = "truck_1"

NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)

#: The telematics vendor's driver identifier, which shares no id space with
#: ``drivers_current.driver_id`` — the whole reason R17.5 excludes the field.
VENDOR_DRIVER_ID = "geotab-b7"


def _clock() -> datetime:
    return NOW


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _reading(
    *,
    age_seconds: int = 30,
    hos_status: Optional[str] = "Driving",
    tenant_id: str = TENANT,
    truck_id: str = TRUCK,
    driver_id: Optional[str] = VENDOR_DRIVER_ID,
    recorded_at: Any = ...,
    **extra: Any,
) -> Dict[str, Any]:
    """Build a ``truck_telemetry`` ``_source`` dict matching the mapping."""
    doc: Dict[str, Any] = {
        "telemetry_id": "tel_1",
        "tenant_id": tenant_id,
        "truck_id": truck_id,
        "hos_status": hos_status,
        "recorded_at": (
            (NOW - timedelta(seconds=age_seconds)).isoformat()
            if recorded_at is ...
            else recorded_at
        ),
    }
    if driver_id is not None:
        doc["driver_id"] = driver_id
    doc.update(extra)
    return doc


class FakeES:
    """Answers the ``drivers_current`` and ``truck_telemetry`` searches."""

    def __init__(
        self,
        *,
        driver: Optional[dict] = None,
        readings: Optional[List[dict]] = None,
        overrides: Optional[List[dict]] = None,
        fail_on: tuple = (),
        fail_writes: bool = False,
    ) -> None:
        self._driver = driver
        self._readings = list(readings or [])
        self._overrides = list(overrides or [])
        self._fail_on = fail_on
        self._fail_writes = fail_writes
        self.searches: List[tuple] = []
        self.indexed: List[tuple] = []

    async def index_document(self, index, doc_id, document):
        if self._fail_writes:
            raise RuntimeError(f"{index} unavailable")
        self.indexed.append((index, doc_id, dict(document)))
        if index == HOS_GATE_OVERRIDES_INDEX:
            # Written overrides become readable, so the write side and the read
            # side of ``hos_gate_overrides`` are exercised against one store.
            self._overrides.append(dict(document))
        return {"result": "created"}

    async def search_documents(self, index, query, size=100):
        self.searches.append((index, query, size))
        if index in self._fail_on:
            raise RuntimeError(f"{index} unavailable")
        if index == DRIVERS_CURRENT_INDEX:
            docs = [self._driver] if self._driver else []
        elif index == TRUCK_TELEMETRY_INDEX:
            docs = self._readings
        elif index == HOS_GATE_OVERRIDES_INDEX:
            docs = self._overrides
        else:  # pragma: no cover - no other index is read
            docs = []
        return {"hits": {"hits": [{"_source": dict(d)} for d in docs]}}

    def query_for(self, index: str) -> dict:
        return next(q for i, q, _ in self.searches if i == index)

    def searched(self, index: str) -> bool:
        return any(i == index for i, _, _ in self.searches)


class FakeDriverRepository:
    """``drivers_current`` read."""

    def __init__(self, *, record: Optional[dict] = None) -> None:
        self.record = record

    async def get(self, tenant_id, driver_id):
        if self.record is None:
            return None
        if self.record.get("driver_id") != driver_id:
            return None
        if self.record.get("tenant_id") != tenant_id:
            return None
        return dict(self.record)


class FakeInstanceRepository:
    """``integration_instances`` read, filtered to the ``gps_eld`` category."""

    def __init__(self, *, instances: Optional[List[dict]] = None, fail=False) -> None:
        self._instances = list(instances or [])
        self._fail = fail
        self.calls: List[dict] = []

    async def list_for_tenant(self, tenant_id, *, category=None, **kwargs):
        self.calls.append({"tenant_id": tenant_id, "category": category})
        if self._fail:
            raise RuntimeError("integration_instances unavailable")
        return [
            dict(i)
            for i in self._instances
            if i.get("tenant_id") == tenant_id
            and (category is None or i.get("category") == category)
        ]


class FakeFlagService:
    """The overlay read behind ``driver.hos_gating``."""

    def __init__(self, *, state: str = "disabled", raises=None) -> None:
        self._state = state
        self._raises = raises
        self.reads: List[tuple] = []

    async def get_overlay_state(self, flag_key, tenant_id):
        self.reads.append((flag_key, tenant_id))
        if self._raises is not None:
            raise self._raises
        return self._state


def _driver_record(*, assigned_truck_id: Optional[str] = TRUCK) -> dict:
    return {
        "driver_id": DRIVER,
        "tenant_id": TENANT,
        "driver_name": "Ada Driver",
        "status": "active",
        "assigned_truck_id": assigned_truck_id,
    }


def _gps_eld_instance(*, enabled: bool = True, **config: Any) -> dict:
    return {
        "instance_id": "integration_1",
        "tenant_id": TENANT,
        "provider_name": "geotab",
        "category": "gps_eld",
        "enabled": enabled,
        "config": dict(config),
    }


def _figure_reading(*, drive_hours: float, age_seconds: int = 30) -> Dict[str, Any]:
    """A reading from a connector that *does* supply the three figures.

    No connector writes one today — ``truck_telemetry`` is ``dynamic: strict``
    and declares none of these fields — which is exactly why the gate cannot be
    armed for a Geotab tenant (R17.13). It exists here so the one blocking
    verdict is testable at all.
    """
    return _reading(
        age_seconds=age_seconds,
        available_drive_hours=drive_hours,
        available_window_hours=4.0,
        cumulative_cycle_hours=40.0,
    )


def _override(*, expires_in_seconds: int = 3600, override_id: str = "hgo_1") -> dict:
    return {
        "override_id": override_id,
        "tenant_id": TENANT,
        "driver_id": DRIVER,
        "actor_id": "user-9",
        "reason": "Dispatcher cleared the gate by phone",
        "expires_at": (NOW + timedelta(seconds=expires_in_seconds)).isoformat(),
        "created_at": NOW.isoformat(),
    }


def _service(
    *,
    es_service: Any = None,
    driver_repository: Any = None,
    integration_instance_repository: Any = None,
    feature_flag_service: Any = None,
) -> HOSAdvisoryService:
    return HOSAdvisoryService(
        es_service=es_service,
        driver_repository=driver_repository,
        integration_instance_repository=integration_instance_repository,
        feature_flag_service=feature_flag_service,
        clock=_clock,
    )


def _armed_service(
    *,
    readings: Optional[List[dict]] = None,
    overrides: Optional[List[dict]] = None,
    flag_state: str = "active_gated",
    instance_enabled: bool = True,
    es_service: Any = None,
    **instance_config: Any,
) -> HOSAdvisoryService:
    """A service with both gating switches on, unless a caller turns one off."""
    return _service(
        es_service=es_service
        if es_service is not None
        else FakeES(
            driver=_driver_record(),
            readings=list(readings or []),
            overrides=list(overrides or []),
        ),
        integration_instance_repository=FakeInstanceRepository(
            instances=[
                _gps_eld_instance(enabled=instance_enabled, **instance_config)
            ]
        ),
        feature_flag_service=FakeFlagService(state=flag_state),
    )


# ---------------------------------------------------------------------------
# Freshness classification (R17.3-R17.10)
# ---------------------------------------------------------------------------


class TestFreshnessClassification:
    """Every input lands on exactly one state, conservatively."""

    @pytest.mark.asyncio
    async def test_no_assigned_truck_is_unknown_truck_unassigned(self):
        """A driver with no ``assigned_truck_id`` is ``unknown`` (R17.6)."""
        es = FakeES(driver=_driver_record(assigned_truck_id=None))

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "unknown"
        assert advisory.reason_code == HOS_TRUCK_UNASSIGNED
        assert advisory.compliance_state == "unknown"
        assert advisory.truck_id is None
        # No truck means no telemetry read was even attempted.
        assert not es.searched(TRUCK_TELEMETRY_INDEX)

    @pytest.mark.asyncio
    async def test_missing_driver_record_is_unknown_truck_unassigned(self):
        """No ``drivers_current`` record is still "no truck" (R17.6)."""
        advisory = await _service(es_service=FakeES()).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "unknown"
        assert advisory.reason_code == HOS_TRUCK_UNASSIGNED

    @pytest.mark.asyncio
    async def test_no_telemetry_document_is_unknown_no_reading(self):
        """An unmapped device leaves no document for the truck (R17.7)."""
        es = FakeES(driver=_driver_record(), readings=[])

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "unknown"
        assert advisory.reason_code == HOS_NO_READING
        assert advisory.compliance_state == "unknown"
        assert advisory.truck_id == TRUCK

    @pytest.mark.asyncio
    async def test_unparseable_recorded_at_is_unknown_no_reading(self):
        """A reading whose age cannot be computed is unusable (R17.7)."""
        es = FakeES(
            driver=_driver_record(),
            readings=[_reading(recorded_at="last tuesday")],
        )

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "unknown"
        assert advisory.reason_code == HOS_NO_READING

    @pytest.mark.asyncio
    async def test_read_failure_is_unknown_not_an_error(self):
        """A failed ``truck_telemetry`` read resolves ``unknown`` (R17.7)."""
        es = FakeES(
            driver=_driver_record(), fail_on=(TRUCK_TELEMETRY_INDEX,)
        )

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "unknown"
        assert advisory.reason_code == HOS_NO_READING

    @pytest.mark.asyncio
    async def test_reading_inside_the_window_is_fresh(self):
        """A 30-second-old reading is ``fresh`` and reports its age (R17.11)."""
        es = FakeES(driver=_driver_record(), readings=[_reading(age_seconds=30)])

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "fresh"
        assert advisory.reason_code is None
        assert advisory.duty_status == "Driving"
        assert advisory.reading_age_seconds == 30
        assert advisory.recorded_at == (
            NOW - timedelta(seconds=30)
        ).isoformat()
        assert advisory.freshness_window_seconds == DEFAULT_FRESHNESS_SECONDS

    @pytest.mark.asyncio
    async def test_reading_at_the_window_boundary_is_fresh(self):
        """Exactly 300 seconds old is not yet stale (R17.8, R17.9)."""
        es = FakeES(
            driver=_driver_record(),
            readings=[_reading(age_seconds=DEFAULT_FRESHNESS_SECONDS)],
        )

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "fresh"

    @pytest.mark.asyncio
    async def test_reading_past_the_window_is_stale_with_its_age(self):
        """Older than the window is ``stale``, and the age is reported (R17.8)."""
        es = FakeES(
            driver=_driver_record(),
            readings=[_reading(age_seconds=DEFAULT_FRESHNESS_SECONDS + 1)],
        )

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "stale"
        assert advisory.reason_code == HOS_READING_STALE
        assert advisory.reading_age_seconds == DEFAULT_FRESHNESS_SECONDS + 1
        # Conservative: never within limits on a stale reading (R17.10).
        assert advisory.compliance_state == "unknown"
        assert advisory.remaining_drive_time.availability == "unavailable"
        assert advisory.remaining_on_duty_window.availability == "unavailable"
        assert advisory.cycle_hours.availability == "unavailable"

    @pytest.mark.asyncio
    async def test_future_dated_reading_is_fresh_with_zero_age(self):
        """Clock skew is not a negative age."""
        es = FakeES(driver=_driver_record(), readings=[_reading(age_seconds=-90)])

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "fresh"
        assert advisory.reading_age_seconds == 0

    @pytest.mark.asyncio
    async def test_state_and_reason_vocabularies_are_closed(self):
        """Whatever the input, the state is one of three and the reason is legal."""
        cases = [
            FakeES(driver=_driver_record(assigned_truck_id=None)),
            FakeES(driver=_driver_record(), readings=[]),
            FakeES(driver=_driver_record(), readings=[_reading(age_seconds=1)]),
            FakeES(
                driver=_driver_record(),
                readings=[_reading(age_seconds=100000)],
            ),
        ]
        for es in cases:
            advisory = await _service(es_service=es).resolve(TENANT, DRIVER)
            assert advisory.freshness_state in FRESHNESS_STATES
            if advisory.freshness_state == "unknown":
                assert advisory.reason_code in UNKNOWN_REASON_CODES
                assert advisory.compliance_state == "unknown"


# ---------------------------------------------------------------------------
# Resolution scope (R17.4, R17.5)
# ---------------------------------------------------------------------------


class TestResolutionScope:
    """The reading is resolved by tenant and truck, and by nothing else."""

    @pytest.mark.asyncio
    async def test_query_sorts_by_recorded_at_desc_and_excludes_vendor_driver_id(
        self,
    ):
        """Greatest ``recorded_at``, no ``driver_id`` clause (R17.4, R17.5)."""
        es = FakeES(driver=_driver_record(), readings=[_reading()])

        await _service(es_service=es).resolve(TENANT, DRIVER)

        query = es.query_for(TRUCK_TELEMETRY_INDEX)
        assert query["sort"] == [{"recorded_at": {"order": "desc"}}]
        assert query["size"] == 1
        assert query["_source"] == {"excludes": ["driver_id"]}
        # ``inject_tenant_filter`` nests the service's own clause under
        # ``must`` and adds the tenant term beside it.
        assert {"term": {"tenant_id": TENANT}} in query["query"]["bool"]["filter"]
        inner = query["query"]["bool"]["must"][0]["bool"]["filter"]
        assert inner == [{"term": {"truck_id": TRUCK}}]
        # Nowhere in the body is there a ``driver_id`` term (R17.5).
        assert '"driver_id":' not in json.dumps(query["query"])

    @pytest.mark.asyncio
    async def test_vendor_driver_id_does_not_reach_the_advisory(self):
        """A reading whose vendor ``driver_id`` differs still resolves (R17.5)."""
        es = FakeES(
            driver=_driver_record(),
            readings=[_reading(driver_id=VENDOR_DRIVER_ID)],
        )

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "fresh"
        assert advisory.driver_id == DRIVER
        assert VENDOR_DRIVER_ID not in advisory.model_dump_json()

    @pytest.mark.asyncio
    async def test_another_tenants_document_is_dropped(self):
        """A mis-labelled document never crosses into this tenant's advisory."""
        es = FakeES(
            driver=_driver_record(),
            readings=[_reading(tenant_id=OTHER_TENANT)],
        )

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "unknown"
        assert advisory.reason_code == HOS_NO_READING

    @pytest.mark.asyncio
    async def test_driver_repository_is_the_preferred_reader(self):
        """With a repository wired, ``drivers_current`` is not searched."""
        es = FakeES(readings=[_reading()])
        repo = FakeDriverRepository(record=_driver_record())

        advisory = await _service(
            es_service=es, driver_repository=repo
        ).resolve(TENANT, DRIVER)

        assert advisory.truck_id == TRUCK
        assert not es.searched(DRIVERS_CURRENT_INDEX)


# ---------------------------------------------------------------------------
# The three figures (R17.12, R17.13, R17.14)
# ---------------------------------------------------------------------------


class TestFigures:
    """A duty-status-only connector supplies no remaining-hours figure."""

    @pytest.mark.asyncio
    async def test_duty_status_only_reading_reports_every_figure_unavailable(self):
        """The Geotab connector as built (R17.13)."""
        es = FakeES(driver=_driver_record(), readings=[_reading()])

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.freshness_state == "fresh"
        assert advisory.hos_status is None
        assert advisory.compliance_state == "unknown"
        for figure in (
            advisory.remaining_drive_time,
            advisory.remaining_on_duty_window,
            advisory.cycle_hours,
        ):
            assert figure.availability == "unavailable"
            assert figure.value is None
            assert figure.unit is None
            assert figure.advisory is True

    @pytest.mark.asyncio
    async def test_figure_supplying_reading_populates_the_existing_hos_status(self):
        """One HOS model, not a second one (R17.12, R17.14)."""
        es = FakeES(
            driver=_driver_record(),
            readings=[
                _reading(
                    available_drive_hours=4.5,
                    available_window_hours=6.0,
                    cumulative_cycle_hours=52.0,
                    cycle_type="8_day",
                )
            ],
        )

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.compliance_state == "within_limits"
        assert advisory.remaining_drive_time.availability == "available"
        assert advisory.remaining_drive_time.value == 4.5
        assert advisory.remaining_drive_time.unit == HOURS_UNIT
        assert advisory.cycle_hours.value == 52.0
        assert advisory.hos_status is not None
        # The Runsheet driver, never the vendor's identifier (R17.5).
        assert advisory.hos_status.driver_id == DRIVER
        assert advisory.hos_status.cycle_type == "8_day"
        assert advisory.hos_status.available_window_hours == 6.0

    @pytest.mark.asyncio
    async def test_zero_remaining_drive_time_is_at_limit(self):
        """Zero remaining drive hours is distinguishable from unknown (R17.16)."""
        es = FakeES(
            driver=_driver_record(),
            readings=[
                _reading(
                    available_drive_hours=0,
                    available_window_hours=0,
                    cumulative_cycle_hours=70.0,
                )
            ],
        )

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.compliance_state == "at_limit"
        assert advisory.remaining_drive_time.availability == "available"
        assert advisory.remaining_drive_time.value == 0.0

    @pytest.mark.asyncio
    async def test_partial_figures_are_no_figures(self):
        """Two of three figures would report a window nobody supplied."""
        es = FakeES(
            driver=_driver_record(),
            readings=[_reading(available_drive_hours=4.5)],
        )

        advisory = await _service(es_service=es).resolve(TENANT, DRIVER)

        assert advisory.hos_status is None
        assert advisory.remaining_drive_time.availability == "unavailable"


# ---------------------------------------------------------------------------
# The freshness window and the provider name (R17.9, R17.11)
# ---------------------------------------------------------------------------


class TestFreshnessWindow:
    """300 seconds by default, per-tenant override, provider name reported."""

    @pytest.mark.asyncio
    async def test_default_window_matches_the_connector_constant(self):
        assert DEFAULT_FRESHNESS_SECONDS == 300

    @pytest.mark.asyncio
    async def test_tenant_override_widens_the_window(self):
        """A 900-second tenant keeps a 400-second-old reading fresh (R17.9)."""
        es = FakeES(driver=_driver_record(), readings=[_reading(age_seconds=400)])
        instances = FakeInstanceRepository(
            instances=[_gps_eld_instance(hos_freshness_seconds=900)]
        )

        advisory = await _service(
            es_service=es, integration_instance_repository=instances
        ).resolve(TENANT, DRIVER)

        assert advisory.freshness_window_seconds == 900
        assert advisory.freshness_state == "fresh"
        assert advisory.provider_name == "geotab"
        assert instances.calls == [{"tenant_id": TENANT, "category": "gps_eld"}]

    @pytest.mark.asyncio
    async def test_non_positive_override_falls_back_to_the_default(self):
        """"0 seconds" would make every reading stale, so it is ignored."""
        es = FakeES(driver=_driver_record(), readings=[_reading(age_seconds=30)])
        instances = FakeInstanceRepository(
            instances=[_gps_eld_instance(hos_freshness_seconds=0)]
        )

        advisory = await _service(
            es_service=es, integration_instance_repository=instances
        ).resolve(TENANT, DRIVER)

        assert advisory.freshness_window_seconds == DEFAULT_FRESHNESS_SECONDS
        assert advisory.freshness_state == "fresh"

    @pytest.mark.asyncio
    async def test_instance_read_failure_falls_back_to_the_default(self):
        es = FakeES(driver=_driver_record(), readings=[_reading(age_seconds=30)])
        instances = FakeInstanceRepository(fail=True)

        advisory = await _service(
            es_service=es, integration_instance_repository=instances
        ).resolve(TENANT, DRIVER)

        assert advisory.freshness_window_seconds == DEFAULT_FRESHNESS_SECONDS
        assert advisory.freshness_state == "fresh"


# ---------------------------------------------------------------------------
# GET /api/driver/hos (R17.1, R17.11, R17.32)
# ---------------------------------------------------------------------------


def _make_app(
    *,
    es_service: Any = None,
    driver_repository: Any = None,
    integration_instance_repository: Any = None,
    feature_flag_service: Any = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(hos_router)
    configure_hos_endpoints(
        es_service=es_service,
        driver_repository=driver_repository,
        integration_instance_repository=integration_instance_repository,
        feature_flag_service=feature_flag_service,
    )
    install_test_auth(app)
    return app


def _driver_headers(driver_id: str = DRIVER, **kwargs) -> dict:
    kwargs.setdefault("roles", ["driver"])
    return auth_headers(TENANT, sub="user-1", driver_id=driver_id, **kwargs)


def _live_reading(*, age_seconds: int = 30, **kwargs) -> Dict[str, Any]:
    """A reading aged against the wall clock.

    The router builds its own service, so there is no clock to inject: these
    readings are stamped relative to ``now`` rather than the frozen ``NOW`` the
    service-level tests use.
    """
    recorded_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return _reading(recorded_at=recorded_at.isoformat(), **kwargs)


class TestHOSEndpoint:
    """The read is the caller's own, and every figure is labelled advisory."""

    def test_returns_the_callers_own_advisory(self):
        """Validates: Requirements 17.1, 17.11"""
        app = _make_app(
            es_service=FakeES(driver=_driver_record(), readings=[_live_reading()])
        )
        client = TestClient(app)

        resp = client.get("/api/driver/hos", headers=_driver_headers())

        assert resp.status_code == 200
        body = resp.json()
        assert body["advisory"] is True
        assert body["authoritative_record"] == "carrier_eld"
        assert "ELD" in body["authoritative_record_statement"]
        data = body["data"]
        assert data["driver_id"] == DRIVER
        assert data["tenant_id"] == TENANT
        assert data["freshness_state"] == "fresh"
        assert data["duty_status"] == "Driving"
        assert data["recorded_at"]
        assert 30 <= data["reading_age_seconds"] <= 60
        assert data["provider_name"] == "geotab"
        assert data["advisory"] is True
        for key in (
            "remaining_drive_time",
            "remaining_on_duty_window",
            "cycle_hours",
        ):
            assert data[key]["availability"] == "unavailable"
            assert data[key]["advisory"] is True

    def test_own_driver_id_in_the_query_is_accepted(self):
        """Validates: Requirements 17.32"""
        app = _make_app(
            es_service=FakeES(driver=_driver_record(), readings=[_live_reading()])
        )
        client = TestClient(app)

        resp = client.get(
            f"/api/driver/hos?driver_id={DRIVER}", headers=_driver_headers()
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["driver_id"] == DRIVER

    def test_another_driver_id_is_403_forbidden(self):
        """Validates: Requirements 17.32"""
        app = _make_app(
            es_service=FakeES(driver=_driver_record(), readings=[_live_reading()])
        )
        client = TestClient(app)

        resp = client.get(
            f"/api/driver/hos?driver_id={OTHER_DRIVER}",
            headers=_driver_headers(),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "FORBIDDEN"
        # The rejection names the rule, not the other identity (R15.14).
        assert OTHER_DRIVER not in resp.text

    def test_a_caller_without_a_driver_identity_is_rejected(self):
        app = _make_app(
            es_service=FakeES(driver=_driver_record(), readings=[_live_reading()])
        )
        client = TestClient(app)

        resp = client.get(
            "/api/driver/hos", headers=auth_headers(TENANT, roles=["driver"])
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "DRIVER_IDENTITY_MISSING"

    def test_a_dispatcher_is_rejected_by_the_driver_gate(self):
        app = _make_app(
            es_service=FakeES(driver=_driver_record(), readings=[_live_reading()])
        )
        client = TestClient(app)

        resp = client.get(
            "/api/driver/hos",
            headers=auth_headers(TENANT, roles=["dispatcher"]),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"

    def test_unconfigured_surface_fails_closed(self):
        app = _make_app(es_service=None)
        client = TestClient(app)

        resp = client.get("/api/driver/hos", headers=_driver_headers())

        assert resp.status_code == 500
        assert resp.json()["error_code"] == "INTERNAL_ERROR"

    def test_configured_service_is_exposed_for_the_gate_seam(self):
        """The seam the HOS gate is armed through."""
        flags = FakeFlagService()
        _make_app(
            es_service=FakeES(driver=_driver_record()),
            feature_flag_service=flags,
        )

        service = configured_hos_advisory_service()

        assert isinstance(service, HOSAdvisoryService)
        assert callable(getattr(service, "gate_verdict"))
        assert service._feature_flag_service is flags

    def test_the_read_surface_consults_no_flag(self):
        """The advisory is served whether or not the gate is armed."""
        flags = FakeFlagService(state="active_gated")
        app = _make_app(
            es_service=FakeES(driver=_driver_record(), readings=[_live_reading()]),
            feature_flag_service=flags,
        )
        client = TestClient(app)

        resp = client.get("/api/driver/hos", headers=_driver_headers())

        assert resp.status_code == 200
        assert flags.reads == []


# ---------------------------------------------------------------------------
# The gate verdict (R17.17-R17.21, R17.25, R17.26)
# ---------------------------------------------------------------------------


class TestGateVerdictEnablement:
    """Both switches must be on, and both default to false (R17.19, R17.20)."""

    @pytest.mark.asyncio
    async def test_no_feature_flag_service_is_no_gate_at_all(self):
        """Validates: Requirements 17.19, 17.20"""
        service = _service(
            es_service=FakeES(
                driver=_driver_record(), readings=[_figure_reading(drive_hours=0.0)]
            ),
            integration_instance_repository=FakeInstanceRepository(
                instances=[_gps_eld_instance()]
            ),
        )

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.outcome == "skipped"
        assert verdict.blocked is False
        assert verdict.gating_enabled is False
        assert verdict.reason_code == HOS_GATING_DISABLED
        # No gate means no audit record either (R17.19).
        assert verdict.audit_outcome is None
        assert verdict.audit_record() == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["disabled", "shadow"])
    async def test_a_non_enforcing_overlay_state_is_no_gate_at_all(self, state):
        """``disabled`` is the default and ``shadow`` observes without blocking.

        Validates: Requirements 17.19, 17.20
        """
        es = FakeES(
            driver=_driver_record(), readings=[_figure_reading(drive_hours=0.0)]
        )
        service = _armed_service(flag_state=state, es_service=es)

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.reason_code == HOS_GATING_DISABLED
        # The cheapest check runs first: no reading was resolved at all.
        assert not es.searched(TRUCK_TELEMETRY_INDEX)

    @pytest.mark.asyncio
    async def test_an_unreadable_flag_is_treated_as_disabled(self):
        """Validates: Requirements 17.19"""
        service = _service(
            es_service=FakeES(driver=_driver_record()),
            integration_instance_repository=FakeInstanceRepository(
                instances=[_gps_eld_instance()]
            ),
            feature_flag_service=FakeFlagService(
                raises=RuntimeError("Redis client not connected")
            ),
        )

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.reason_code == HOS_GATING_DISABLED

    @pytest.mark.asyncio
    async def test_the_flag_key_read_is_the_documented_one(self):
        flags = FakeFlagService(state="disabled")
        service = _service(
            es_service=FakeES(driver=_driver_record()),
            feature_flag_service=flags,
        )

        await service.gate_verdict(TENANT, DRIVER)

        assert flags.reads == [(HOS_GATING_FLAG_KEY, TENANT)]
        assert HOS_GATING_FLAG_KEY == "driver.hos_gating"

    @pytest.mark.asyncio
    async def test_a_disabled_gps_eld_instance_is_a_recorded_skip(self):
        """The second switch, defaulting to false (R17.18, R17.20)."""
        es = FakeES(
            driver=_driver_record(), readings=[_figure_reading(drive_hours=0.0)]
        )
        service = _armed_service(instance_enabled=False, es_service=es)

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.outcome == "skipped"
        assert verdict.blocked is False
        assert verdict.gating_enabled is True
        assert verdict.reason_code == HOS_GPS_ELD_DISABLED
        assert verdict.audit_outcome == AUDIT_GATE_SKIPPED
        assert not es.searched(TRUCK_TELEMETRY_INDEX)

    @pytest.mark.asyncio
    async def test_no_gps_eld_instance_at_all_is_a_recorded_skip(self):
        service = _service(
            es_service=FakeES(driver=_driver_record()),
            integration_instance_repository=FakeInstanceRepository(instances=[]),
            feature_flag_service=FakeFlagService(state="active_gated"),
        )

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.reason_code == HOS_GPS_ELD_DISABLED

    @pytest.mark.asyncio
    async def test_an_unreadable_instance_repository_is_a_recorded_skip(self):
        service = _service(
            es_service=FakeES(driver=_driver_record()),
            integration_instance_repository=FakeInstanceRepository(fail=True),
            feature_flag_service=FakeFlagService(state="active_gated"),
        )

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.reason_code == HOS_GPS_ELD_DISABLED


class TestGateVerdictIsFailOpen:
    """Only one row of the table blocks (R17.17, R17.18)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "readings,expected_reason,expected_freshness",
        [
            ([], HOS_NO_READING, "unknown"),
            (
                [_reading(age_seconds=DEFAULT_FRESHNESS_SECONDS + 1)],
                HOS_READING_STALE,
                "stale",
            ),
        ],
    )
    async def test_an_unusable_reading_permits_the_transition(
        self, readings, expected_reason, expected_freshness
    ):
        """Validates: Requirements 17.18"""
        service = _armed_service(readings=readings)

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.outcome == "skipped"
        assert verdict.blocked is False
        assert verdict.reason_code == expected_reason
        assert verdict.freshness_state == expected_freshness
        assert verdict.audit_outcome == AUDIT_GATE_SKIPPED

    @pytest.mark.asyncio
    async def test_an_unassigned_truck_permits_the_transition(self):
        """Validates: Requirements 17.18"""
        service = _armed_service(
            es_service=FakeES(driver=_driver_record(assigned_truck_id=None))
        )

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.outcome == "skipped"
        assert verdict.reason_code == HOS_TRUCK_UNASSIGNED
        assert verdict.freshness_state == "unknown"

    @pytest.mark.asyncio
    async def test_a_geotab_reading_permits_the_transition(self):
        """The connector as built supplies no figure, so there is no limit.

        Validates: Requirements 17.13, 17.18
        """
        service = _armed_service(readings=[_reading(hos_status="Driving")])

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.outcome == "skipped"
        assert verdict.blocked is False
        assert verdict.reason_code == HOS_FIGURES_UNAVAILABLE
        assert verdict.freshness_state == "fresh"
        assert verdict.audit_outcome == AUDIT_GATE_SKIPPED

    @pytest.mark.asyncio
    async def test_a_fresh_reading_within_limits_passes(self):
        service = _armed_service(readings=[_figure_reading(drive_hours=6.5)])

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.outcome == "passed"
        assert verdict.blocked is False
        assert verdict.reason_code is None
        assert verdict.freshness_state == "fresh"
        assert verdict.audit_outcome == AUDIT_GATE_PASSED

    @pytest.mark.asyncio
    async def test_a_fresh_at_limit_reading_blocks(self):
        """The one blocking verdict.

        Validates: Requirements 17.17, 17.26
        """
        service = _armed_service(readings=[_figure_reading(drive_hours=0.0)])

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.outcome == "blocked"
        assert verdict.blocked is True
        assert verdict.reason_code == HOS_AT_LIMIT
        assert verdict.freshness_state == "fresh"
        assert verdict.recorded_at == (NOW - timedelta(seconds=30)).isoformat()
        assert verdict.override_id is None
        record = verdict.audit_record()
        assert record["driver_id"] == DRIVER
        assert record["outcome"] == AUDIT_GATE_BLOCKED
        assert record["gate_outcome"] == "blocked"
        assert record["reason_code"] == HOS_AT_LIMIT
        assert record["freshness_state"] == "fresh"
        assert record["recorded_at"]


class TestGateVerdictOverride:
    """An unexpired override permits the transition (R17.25)."""

    @pytest.mark.asyncio
    async def test_an_unexpired_override_permits_and_is_recorded(self):
        """Validates: Requirements 17.25, 17.26"""
        service = _armed_service(
            readings=[_figure_reading(drive_hours=0.0)],
            overrides=[_override(override_id="hgo_abc")],
        )

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.outcome == "passed"
        assert verdict.blocked is False
        assert verdict.reason_code == HOS_OVERRIDE_APPLIED
        assert verdict.override_id == "hgo_abc"
        assert verdict.audit_outcome == AUDIT_GATE_OVERRIDDEN
        assert verdict.audit_record()["override_id"] == "hgo_abc"

    @pytest.mark.asyncio
    async def test_an_expired_override_does_not_clear_the_gate(self):
        """Validates: Requirements 17.17, 17.25"""
        service = _armed_service(
            readings=[_figure_reading(drive_hours=0.0)],
            overrides=[_override(expires_in_seconds=-1)],
        )

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.outcome == "blocked"
        assert verdict.override_id is None

    @pytest.mark.asyncio
    async def test_another_drivers_override_does_not_clear_the_gate(self):
        override = _override()
        override["driver_id"] = OTHER_DRIVER
        service = _armed_service(
            readings=[_figure_reading(drive_hours=0.0)], overrides=[override]
        )

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.outcome == "blocked"

    @pytest.mark.asyncio
    async def test_another_tenants_override_does_not_clear_the_gate(self):
        """Per-document tenant re-validation, not just the injected filter."""
        override = _override()
        override["tenant_id"] = OTHER_TENANT
        service = _armed_service(
            readings=[_figure_reading(drive_hours=0.0)], overrides=[override]
        )

        verdict = await service.gate_verdict(TENANT, DRIVER)

        assert verdict.outcome == "blocked"

    @pytest.mark.asyncio
    async def test_the_override_query_is_tenant_filtered(self):
        es = FakeES(
            driver=_driver_record(),
            readings=[_figure_reading(drive_hours=0.0)],
            overrides=[_override()],
        )
        service = _armed_service(es_service=es)

        await service.gate_verdict(TENANT, DRIVER)

        query = es.query_for(HOS_GATE_OVERRIDES_INDEX)
        assert json.dumps(query).count(TENANT) >= 1
        assert {"term": {"tenant_id": TENANT}} in query["query"]["bool"]["filter"]
        own = query["query"]["bool"]["must"][0]["bool"]["filter"]
        assert {"term": {"driver_id": DRIVER}} in own
        assert any("range" in clause for clause in own)


class TestArmingTheGate:
    """R17.21 — a gate cannot be armed against data that does not exist."""

    @pytest.mark.asyncio
    async def test_a_geotab_tenant_cannot_enable_gating(self):
        """Validates: Requirements 17.21"""
        service = _armed_service(readings=[_reading(hos_status="Driving")])

        with pytest.raises(AppException) as exc_info:
            await service.assert_gating_can_be_enabled(TENANT)

        assert exc_info.value.error_code == "HOS_FIGURES_UNAVAILABLE"
        assert exc_info.value.status_code == 409
        assert exc_info.value.details["flag_key"] == HOS_GATING_FLAG_KEY

    @pytest.mark.asyncio
    async def test_a_tenant_with_no_readings_cannot_enable_gating(self):
        """Validates: Requirements 17.21"""
        service = _armed_service(readings=[])

        with pytest.raises(AppException) as exc_info:
            await service.assert_gating_can_be_enabled(TENANT)

        assert exc_info.value.error_code == "HOS_FIGURES_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_a_figure_supplying_reading_permits_enabling(self):
        service = _armed_service(readings=[_figure_reading(drive_hours=3.0)])

        await service.assert_gating_can_be_enabled(TENANT)

        assert await service.supplies_remaining_drive_time(TENANT) is True

    @pytest.mark.asyncio
    async def test_a_connector_may_declare_the_capability(self):
        """The seam for a capable connector whose first reading has not landed."""
        service = _armed_service(readings=[], hos_supplies_remaining_hours=True)

        await service.assert_gating_can_be_enabled(TENANT)

    @pytest.mark.asyncio
    async def test_an_unreadable_probe_refuses_to_enable(self):
        """An unverifiable capability is not a capability."""
        service = _armed_service(
            es_service=FakeES(
                driver=_driver_record(), fail_on=(TRUCK_TELEMETRY_INDEX,)
            )
        )

        with pytest.raises(AppException) as exc_info:
            await service.assert_gating_can_be_enabled(TENANT)

        assert exc_info.value.error_code == "HOS_FIGURES_UNAVAILABLE"


# ---------------------------------------------------------------------------
# The override write (R17.23, R17.24)
# ---------------------------------------------------------------------------

#: The fields ``hos_gate_overrides`` declares. The index is ``dynamic: strict``,
#: so a document carrying anything outside this set fails the write outright —
#: which makes "the written keys are a subset of these" the sharpest assertion
#: available without a live Elasticsearch.
_DECLARED_OVERRIDE_FIELDS = set(
    HOS_GATE_OVERRIDES_MAPPING["mappings"]["properties"]
)


class TestRecordOverride:
    """The write side of ``hos_gate_overrides`` (R17.23)."""

    @pytest.mark.asyncio
    async def test_persists_the_declared_document_shape(self):
        """Validates: Requirements 17.23"""
        es = FakeES(driver=_driver_record())
        service = _service(es_service=es)

        override = await service.record_override(
            TENANT,
            DRIVER,
            actor_id="user-9",
            reason="Dispatcher cleared the gate by phone",
            expires_at=NOW + timedelta(hours=2),
        )

        assert len(es.indexed) == 1
        index, doc_id, document = es.indexed[0]
        assert index == HOS_GATE_OVERRIDES_INDEX
        assert doc_id == override.override_id
        # dynamic: strict — an undeclared field would fail the real write.
        assert set(document) <= _DECLARED_OVERRIDE_FIELDS
        assert set(document) == {
            "override_id",
            "tenant_id",
            "driver_id",
            "actor_id",
            "reason",
            "expires_at",
            "created_at",
        }
        assert document["tenant_id"] == TENANT
        assert document["driver_id"] == DRIVER
        assert document["actor_id"] == "user-9"
        assert document["reason"] == "Dispatcher cleared the gate by phone"

    @pytest.mark.asyncio
    async def test_mints_the_override_id_server_side(self):
        """Validates: Requirements 17.23"""
        service = _service(es_service=FakeES(driver=_driver_record()))

        first = await service.record_override(
            TENANT,
            DRIVER,
            actor_id="user-9",
            reason="storm",
            expires_at=NOW + timedelta(hours=1),
        )
        second = await service.record_override(
            TENANT,
            DRIVER,
            actor_id="user-9",
            reason="storm",
            expires_at=NOW + timedelta(hours=1),
        )

        assert first.override_id.startswith(OVERRIDE_ID_PREFIX)
        assert first.override_id != second.override_id

    @pytest.mark.asyncio
    async def test_a_written_override_clears_the_gate(self):
        """The write side and the read side agree on the document shape.

        Validates: Requirements 17.23, 17.25
        """
        es = FakeES(
            driver=_driver_record(), readings=[_figure_reading(drive_hours=0.0)]
        )
        service = _armed_service(es_service=es)

        blocked = await service.gate_verdict(TENANT, DRIVER)
        assert blocked.outcome == "blocked"

        override = await service.record_override(
            TENANT,
            DRIVER,
            actor_id="user-9",
            reason="Dispatcher cleared the gate by phone",
            expires_at=NOW + timedelta(hours=2),
        )
        cleared = await service.gate_verdict(TENANT, DRIVER)

        assert cleared.outcome == "passed"
        assert cleared.override_id == override.override_id
        assert cleared.reason_code == HOS_OVERRIDE_APPLIED

    @pytest.mark.asyncio
    async def test_a_blank_reason_is_rejected(self):
        """Validates: Requirements 17.23"""
        es = FakeES(driver=_driver_record())
        service = _service(es_service=es)

        with pytest.raises(AppException) as exc_info:
            await service.record_override(
                TENANT,
                DRIVER,
                actor_id="user-9",
                reason="   ",
                expires_at=NOW + timedelta(hours=1),
            )

        assert exc_info.value.error_code == "INVALID_REQUEST"
        assert es.indexed == []

    @pytest.mark.asyncio
    async def test_an_expiry_at_or_before_now_is_rejected(self):
        """Validates: Requirements 17.23"""
        es = FakeES(driver=_driver_record())
        service = _service(es_service=es)

        for expiry in (NOW, NOW - timedelta(seconds=1)):
            with pytest.raises(AppException) as exc_info:
                await service.record_override(
                    TENANT,
                    DRIVER,
                    actor_id="user-9",
                    reason="storm",
                    expires_at=expiry,
                )
            assert exc_info.value.error_code == "INVALID_REQUEST"

        assert es.indexed == []

    @pytest.mark.asyncio
    async def test_a_naive_expiry_is_read_as_utc(self):
        service = _service(es_service=FakeES(driver=_driver_record()))

        override = await service.record_override(
            TENANT,
            DRIVER,
            actor_id="user-9",
            reason="storm",
            expires_at=(NOW + timedelta(hours=1)).replace(tzinfo=None),
        )

        assert override.expires_at == NOW + timedelta(hours=1)

    @pytest.mark.asyncio
    async def test_a_long_reason_is_truncated_rather_than_stored_whole(self):
        service = _service(es_service=FakeES(driver=_driver_record()))

        override = await service.record_override(
            TENANT,
            DRIVER,
            actor_id="user-9",
            reason="x" * (MAX_OVERRIDE_REASON_LENGTH + 50),
            expires_at=NOW + timedelta(hours=1),
        )

        assert len(override.reason) == MAX_OVERRIDE_REASON_LENGTH

    @pytest.mark.asyncio
    async def test_a_write_failure_is_not_reported_as_success(self):
        """A clearance that did not land must not read as one."""
        service = _service(es_service=FakeES(driver=_driver_record(), fail_writes=True))

        with pytest.raises(AppException) as exc_info:
            await service.record_override(
                TENANT,
                DRIVER,
                actor_id="user-9",
                reason="storm",
                expires_at=NOW + timedelta(hours=1),
            )

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_a_blank_actor_never_becomes_a_document(self):
        """Validates: Requirements 17.23"""
        es = FakeES(driver=_driver_record())
        service = _service(es_service=es)

        with pytest.raises(AppException) as exc_info:
            await service.record_override(
                TENANT,
                DRIVER,
                actor_id="  ",
                reason="storm",
                expires_at=NOW + timedelta(hours=1),
            )

        assert exc_info.value.error_code == "INVALID_REQUEST"
        assert es.indexed == []


# ---------------------------------------------------------------------------
# POST /api/driver/hos/override (R17.23, R17.24)
# ---------------------------------------------------------------------------


def _override_body(**overrides: Any) -> dict:
    body = {
        "driver_id": DRIVER,
        "reason": "Dispatcher cleared the gate by phone",
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat(),
    }
    body.update(overrides)
    return body


class TestHOSGateOverrideEndpoint:
    """Only a dispatcher or an admin may clear a gate, and never for itself."""

    def test_a_dispatcher_records_an_override(self):
        """Validates: Requirements 17.23"""
        es = FakeES(driver=_driver_record())
        app = _make_app(es_service=es)
        client = TestClient(app)

        resp = client.post(
            "/api/driver/hos/override",
            json=_override_body(),
            headers=auth_headers(TENANT, sub="user-9", roles=["dispatcher"]),
        )

        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["override_id"].startswith(OVERRIDE_ID_PREFIX)
        assert data["tenant_id"] == TENANT
        assert data["driver_id"] == DRIVER
        # The actor is the verified session's user, not a body value.
        assert data["actor_id"] == "user-9"
        assert es.indexed[0][0] == HOS_GATE_OVERRIDES_INDEX

    def test_an_admin_records_an_override(self):
        """Validates: Requirements 17.23"""
        app = _make_app(es_service=FakeES(driver=_driver_record()))
        client = TestClient(app)

        resp = client.post(
            "/api/driver/hos/override",
            json=_override_body(),
            headers=auth_headers(TENANT, sub="user-7", roles=["admin"]),
        )

        assert resp.status_code == 201
        assert resp.json()["data"]["actor_id"] == "user-7"

    def test_a_driver_only_caller_is_403_insufficient_role(self):
        """Validates: Requirements 17.24"""
        es = FakeES(driver=_driver_record())
        app = _make_app(es_service=es)
        client = TestClient(app)

        resp = client.post(
            "/api/driver/hos/override",
            json=_override_body(),
            headers=_driver_headers(),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"
        # R15.14 — the rejection names the requirement, never the held roles.
        assert "driver" not in json.dumps(resp.json()["details"])
        assert es.indexed == []

    def test_a_near_miss_role_does_not_pass(self):
        """The role match is exact (R17.23)."""
        app = _make_app(es_service=FakeES(driver=_driver_record()))
        client = TestClient(app)

        resp = client.post(
            "/api/driver/hos/override",
            json=_override_body(),
            headers=auth_headers(TENANT, sub="user-9", roles=["dispatcher_lead"]),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"
        assert resp.json()["details"]["required_roles"] == list(OVERRIDE_ROLES)

    def test_a_body_actor_id_is_refused_outright(self):
        """Validates: Requirements 17.23"""
        app = _make_app(es_service=FakeES(driver=_driver_record()))
        client = TestClient(app)

        resp = client.post(
            "/api/driver/hos/override",
            json=_override_body(actor_id="somebody-else"),
            headers=auth_headers(TENANT, sub="user-9", roles=["dispatcher"]),
        )

        assert resp.status_code == 422

    def test_a_blank_reason_is_rejected(self):
        """Validates: Requirements 17.23"""
        es = FakeES(driver=_driver_record())
        app = _make_app(es_service=es)
        client = TestClient(app)

        resp = client.post(
            "/api/driver/hos/override",
            json=_override_body(reason="   "),
            headers=auth_headers(TENANT, sub="user-9", roles=["dispatcher"]),
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_REQUEST"
        assert es.indexed == []

    def test_a_missing_expiry_is_rejected(self):
        """Validates: Requirements 17.23"""
        app = _make_app(es_service=FakeES(driver=_driver_record()))
        client = TestClient(app)

        body = _override_body()
        body.pop("expires_at")

        resp = client.post(
            "/api/driver/hos/override",
            json=body,
            headers=auth_headers(TENANT, sub="user-9", roles=["dispatcher"]),
        )

        assert resp.status_code == 422

    def test_a_past_expiry_is_rejected(self):
        """Validates: Requirements 17.23"""
        es = FakeES(driver=_driver_record())
        app = _make_app(es_service=es)
        client = TestClient(app)

        resp = client.post(
            "/api/driver/hos/override",
            json=_override_body(
                expires_at=(
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat()
            ),
            headers=auth_headers(TENANT, sub="user-9", roles=["dispatcher"]),
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_REQUEST"
        assert es.indexed == []

    def test_the_override_is_stamped_with_the_callers_tenant(self):
        """A dispatcher cannot write into another tenant's scope."""
        es = FakeES(driver=_driver_record())
        app = _make_app(es_service=es)
        client = TestClient(app)

        resp = client.post(
            "/api/driver/hos/override",
            json=_override_body(),
            headers=auth_headers(OTHER_TENANT, sub="user-9", roles=["dispatcher"]),
        )

        assert resp.status_code == 201
        assert resp.json()["data"]["tenant_id"] == OTHER_TENANT
        assert es.indexed[0][2]["tenant_id"] == OTHER_TENANT

    def test_unconfigured_surface_fails_closed(self):
        app = _make_app(es_service=None)
        client = TestClient(app)

        resp = client.post(
            "/api/driver/hos/override",
            json=_override_body(),
            headers=auth_headers(TENANT, sub="user-9", roles=["dispatcher"]),
        )

        assert resp.status_code == 500
        assert resp.json()["error_code"] == "INTERNAL_ERROR"
