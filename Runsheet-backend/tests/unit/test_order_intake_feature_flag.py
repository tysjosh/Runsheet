"""
Unit tests for the Order Intake Pipeline feature flag behaviour.

Covers each flag state's intake behaviour and the admin rollback endpoint.

The dual-write / dual-broadcast coverage that used to live here went out
with the legacy mirror: no flag state reaches the legacy surface any more,
so the state tests now assert that the new path runs and the legacy
surface stays untouched.

Validates: Requirements 9.3, 10.2.1.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.feature_flag_admin_endpoints import (
    ORDER_INTAKE_PIPELINE_FLAG_KEY,
    VALID_STATES,
    configure_feature_flag_admin,
    router as admin_router,
)


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------


class FakeFeatureFlagService:
    """In-memory feature flag service for testing."""

    def __init__(self, initial_state: str = "disabled"):
        self._states: Dict[str, str] = {}
        self._default = initial_state

    async def get_overlay_state(self, flag_key: str, tenant_id: str) -> str:
        key = f"{flag_key}:{tenant_id}"
        return self._states.get(key, self._default)

    async def set_overlay_state(
        self, flag_key: str, tenant_id: str, state: str, user_id: str
    ) -> str:
        key = f"{flag_key}:{tenant_id}"
        previous = self._states.get(key, self._default)
        self._states[key] = state
        return previous


class FakeOrdersWSManager:
    """Fake WS manager that records broadcasts."""

    def __init__(self):
        self.broadcasts: list = []
        self.shipment_updates: list = []
        self.rider_updates: list = []

    async def broadcast(self, event_type: str, data: dict, tenant_id: str):
        self.broadcasts.append({
            "event_type": event_type,
            "data": data,
            "tenant_id": tenant_id,
        })

    async def broadcast_shipment_update(self, data: dict):
        self.shipment_updates.append(data)

    async def broadcast_rider_update(self, data: dict):
        self.rider_updates.append(data)


class FakeTenantContext:
    """Fake tenant context for dependency injection."""

    def __init__(self, tenant_id: str = "tenant-test", user_id: str = "admin-user", roles=None):
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.roles = roles or ["admin"]


class FakeChannel:
    """Fake intake channel."""

    def __init__(self, tenant_id: str = "tenant-test", channel_type: str = "dispatcher"):
        self.tenant_id = tenant_id
        self.channel_id = "ch-test"
        self.channel_type = channel_type
        self.enabled = True
        self.supported_schema_versions = ["1.0"]
        self.hmac_secret_ref = "ref-123"


class FakeIdempotencyService:
    """Fake idempotency service."""

    def __init__(self, is_dup: bool = False):
        self._is_dup = is_dup
        self.marked: list = []

    async def is_duplicate(self, event_id: str, tenant_id: str = "") -> bool:
        return self._is_dup

    async def mark_processed(self, event_id: str, tenant_id: str = "") -> None:
        self.marked.append((event_id, tenant_id))


class FakeAdapterResult:
    def __init__(self):
        self.order_doc = {
            "customer_id": "cust-1",
            "customer_name": "Test Customer",
            "ship_to_address": "123 Main St",
            "ship_to_lat": 30.0,
            "ship_to_lon": -90.0,
            "product_code": "DIESEL_2",
            "gallons_requested": 500.0,
            "fill_to_full": False,
            "call_type": "one_off",
            "delivery_window_start": "2026-01-01T08:00:00",
            "delivery_window_end": "2026-01-01T17:00:00",
            "intake_channel": "dispatcher",
            "intake_channel_id": "ch-test",
            "intake_metadata": {},
            "source_schema_version": "1.0",
        }
        self.event_docs = [{"event_type": "order_placed"}]


class FakeAdapter:
    def transform(self, payload, context):
        return FakeAdapterResult()


class FakeAdapterRegistry:
    def get(self, channel_type, schema_version):
        return FakeAdapter()


class FakePoisonQueueService:
    async def store_failed_event(self, **kwargs):
        pass


class FakeCustomerTankRepo:
    async def get(self, tenant_id, tank_id):
        return None


class FakeCredentialsVault:
    async def get(self, tenant_id, ref):
        return {"secret": "test-secret"}


class FakeEsService:
    async def index_document(self, *args, **kwargs):
        pass


class FakeOrderRepo:
    async def upsert_with_last_event_timestamp(self, tenant_id, doc):
        pass

    async def append_event(self, tenant_id, ev):
        pass


# ``FakeLegacyDualWriter`` lived here. It stood in for the retired
# LegacyDualWriter shim; the pipeline no longer takes that dependency.


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ff_service():
    return FakeFeatureFlagService(initial_state="disabled")


@pytest.fixture
def ws_manager():
    return FakeOrdersWSManager()


@pytest.fixture
def legacy_ws_manager():
    return FakeOrdersWSManager()


@pytest.fixture
def app(ff_service, ws_manager):
    """Create a test FastAPI app with the admin router."""
    from fastapi.responses import JSONResponse
    from errors.exceptions import AppException
    from ops.middleware.tenant_guard import get_tenant_context

    test_app = FastAPI()
    test_app.include_router(admin_router)

    configure_feature_flag_admin(
        feature_flag_service=ff_service,
        orders_ws_manager=ws_manager,
    )

    # Register the AppException handler so errors come back as JSON
    @test_app.exception_handler(AppException)
    async def _app_exception_handler(request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_dict(),
        )

    # Override the tenant context dependency
    test_app.dependency_overrides[get_tenant_context] = lambda: FakeTenantContext()

    return test_app


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests — Admin Rollback Endpoint
# ---------------------------------------------------------------------------


class TestAdminRollbackEndpoint:
    """Tests for POST /api/ops/admin/feature-flags/{tenant_id}/order-intake-pipeline/{new_state}."""

    def test_set_state_to_shadow(self, client, ff_service):
        """Admin can flip the flag to shadow."""
        resp = client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/shadow"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["previous_state"] == "disabled"
        assert body["data"]["new_state"] == "shadow"
        assert body["data"]["tenant_id"] == "tenant-test"

    def test_set_state_to_active_gated(self, client, ff_service):
        """Admin can flip the flag to active_gated."""
        resp = client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/active_gated"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["new_state"] == "active_gated"

    def test_set_state_to_active_auto(self, client, ff_service):
        """Admin can flip the flag to active_auto."""
        resp = client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/active_auto"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["new_state"] == "active_auto"

    def test_set_state_to_disabled(self, client, ff_service):
        """Admin can flip the flag back to disabled (rollback)."""
        # First set to active_gated
        client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/active_gated"
        )
        # Then rollback to disabled
        resp = client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/disabled"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["previous_state"] == "active_gated"
        assert body["data"]["new_state"] == "disabled"

    def test_invalid_state_returns_400(self, client):
        """Invalid state returns 400."""
        resp = client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/invalid_state"
        )
        assert resp.status_code == 400

    def test_non_admin_returns_403(self, app):
        """Non-admin role returns 403."""
        from ops.middleware.tenant_guard import get_tenant_context

        app.dependency_overrides[get_tenant_context] = lambda: FakeTenantContext(
            roles=["dispatcher"]
        )
        non_admin_client = TestClient(app)
        resp = non_admin_client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/shadow"
        )
        assert resp.status_code == 403
        # Restore the admin override for other tests
        app.dependency_overrides[get_tenant_context] = lambda: FakeTenantContext()

    def test_ws_broadcast_on_state_change(self, client, ws_manager):
        """Flag change broadcasts to WS clients."""
        client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/shadow"
        )
        assert len(ws_manager.broadcasts) == 1
        broadcast = ws_manager.broadcasts[0]
        assert broadcast["event_type"] == "feature_flag_changed"
        assert broadcast["data"]["flag_key"] == ORDER_INTAKE_PIPELINE_FLAG_KEY
        assert broadcast["data"]["new_state"] == "shadow"
        assert broadcast["tenant_id"] == "tenant-test"

    def test_response_includes_ws_broadcast_status(self, client, ws_manager):
        """Response indicates whether WS broadcast succeeded."""
        resp = client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/active_auto"
        )
        body = resp.json()
        assert body["data"]["ws_broadcast"] is True

    def test_all_valid_states_accepted(self, client):
        """All four valid states are accepted."""
        for state in VALID_STATES:
            resp = client.post(
                f"/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/{state}"
            )
            assert resp.status_code == 200, f"State {state} should be accepted"


# ---------------------------------------------------------------------------
# Tests — Feature Flag State Behaviour in Pipeline
# ---------------------------------------------------------------------------


class TestFeatureFlagStateBehaviour:
    """Tests for each flag state's effect on the intake pipeline."""

    @pytest.fixture
    def pipeline_deps(self, ff_service, ws_manager, legacy_ws_manager):
        """Common pipeline dependencies.

        ``legacy_dual_writer`` was dropped from this map with the pipeline
        parameter of the same name. ``legacy_ws_manager`` is still accepted
        by the constructor, so it stays here as the regression hook that
        proves nothing broadcasts to the legacy surface any more.
        """
        return {
            "es_service": FakeEsService(),
            "intake_channel_repo": MagicMock(),
            "adapter_registry": FakeAdapterRegistry(),
            "idempotency_service": FakeIdempotencyService(),
            "feature_flag_service": ff_service,
            "poison_queue_service": FakePoisonQueueService(),
            "ws_manager": ws_manager,
            "credentials_vault": FakeCredentialsVault(),
            "customer_tank_repo": FakeCustomerTankRepo(),
            "legacy_ws_manager": legacy_ws_manager,
        }

    @pytest.mark.asyncio
    async def test_disabled_state_returns_legacy_passthrough(self, pipeline_deps, ff_service):
        """When flag is disabled, pipeline returns legacy_passthrough."""
        from fuel.services.order_intake_pipeline import OrderIntakePipeline

        pipeline = OrderIntakePipeline(**pipeline_deps)
        channel = FakeChannel()

        result = await pipeline._ingest_common(
            channel=channel,
            payload={"schema_version": "1.0"},
            request_id="req-001",
            actor_user_id="user-1",
            client_event_id="evt-001",
        )

        assert result.status == "legacy_passthrough"

    # The six tests that used to follow asserted the legacy dual-write and
    # dual-broadcast behaviour per flag state:
    # ``test_shadow_state_dual_writes_and_compares``,
    # ``test_active_gated_writes_new_and_mirrors_legacy``,
    # ``test_active_auto_writes_only_new_path``,
    # ``test_active_auto_stops_legacy_broadcast``,
    # ``test_shadow_state_still_broadcasts_to_legacy_ws``, and
    # ``test_active_gated_still_broadcasts_to_legacy_ws``. With the mirror
    # retired, every enabled state behaves the same, so they collapse into
    # one parametrised check that the new path runs and the legacy surface
    # is never touched.

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "state", ["shadow", "active_gated", "active_auto"]
    )
    async def test_enabled_states_write_new_path_only(
        self, pipeline_deps, ff_service, legacy_ws_manager, state
    ):
        """Every enabled state writes the new path and nothing else."""
        from fuel.services.order_intake_pipeline import OrderIntakePipeline

        await ff_service.set_overlay_state(
            "order_intake_pipeline", "tenant-test", state, "admin"
        )

        pipeline = OrderIntakePipeline(**pipeline_deps)
        channel = FakeChannel()

        with patch("fuel.order_repository.FuelOrderRepository") as MockRepo:
            MockRepo.return_value = FakeOrderRepo()

            result = await pipeline._ingest_common(
                channel=channel,
                payload={"schema_version": "1.0"},
                request_id=f"req-{state}",
                actor_user_id="user-1",
                client_event_id=f"evt-{state}",
            )

        assert result.status == "processed"
        # No legacy shipment/rider broadcast in any state.
        assert legacy_ws_manager.shipment_updates == []
        assert legacy_ws_manager.rider_updates == []
        assert legacy_ws_manager.broadcasts == []


# ---------------------------------------------------------------------------
# Tests — Shadow Divergence Checker
# ---------------------------------------------------------------------------


class TestShadowDivergenceChecker:
    """Tests for the shadow divergence comparison logic."""

    @pytest.mark.asyncio
    async def test_diff_detects_field_mismatch(self):
        """Field-by-field diff detects mismatched values."""
        from fuel.services.shadow_divergence_checker import ShadowDivergenceChecker

        checker = ShadowDivergenceChecker(sample_rate=1.0)
        divergences = checker._diff_outputs(
            {"status": "placed", "customer_name": "Alice"},
            {"status": "placed", "customer_name": "Bob"},
        )
        assert "customer_name" in divergences
        assert divergences["customer_name"]["new"] == "Alice"
        assert divergences["customer_name"]["legacy"] == "Bob"

    @pytest.mark.asyncio
    async def test_diff_skips_timestamp_fields(self):
        """Timestamp and ID fields are skipped during comparison."""
        from fuel.services.shadow_divergence_checker import ShadowDivergenceChecker

        checker = ShadowDivergenceChecker(sample_rate=1.0)
        divergences = checker._diff_outputs(
            {"updated_at": "2026-01-01", "created_at": "2026-01-01", "status": "placed"},
            {"updated_at": "2025-12-31", "created_at": "2025-12-31", "status": "placed"},
        )
        assert "updated_at" not in divergences
        assert "created_at" not in divergences

    @pytest.mark.asyncio
    async def test_diff_skips_id_fields(self):
        """_id, order_id, event_id, trace_id are skipped."""
        from fuel.services.shadow_divergence_checker import ShadowDivergenceChecker

        checker = ShadowDivergenceChecker(sample_rate=1.0)
        divergences = checker._diff_outputs(
            {"_id": "a", "order_id": "b", "event_id": "c", "trace_id": "d", "status": "placed"},
            {"_id": "x", "order_id": "y", "event_id": "z", "trace_id": "w", "status": "placed"},
        )
        assert "_id" not in divergences
        assert "order_id" not in divergences
        assert "event_id" not in divergences
        assert "trace_id" not in divergences

    @pytest.mark.asyncio
    async def test_no_divergence_returns_empty(self):
        """Identical outputs produce no divergences."""
        from fuel.services.shadow_divergence_checker import ShadowDivergenceChecker

        checker = ShadowDivergenceChecker(sample_rate=1.0)
        divergences = checker._diff_outputs(
            {"status": "placed", "customer_name": "Alice"},
            {"status": "placed", "customer_name": "Alice"},
        )
        assert divergences == {}

    @pytest.mark.asyncio
    async def test_sample_rate_zero_skips_comparison(self):
        """Sample rate 0.0 skips comparison entirely."""
        from fuel.services.shadow_divergence_checker import ShadowDivergenceChecker

        checker = ShadowDivergenceChecker(sample_rate=0.0)
        result = await checker.compare(
            new_output={"status": "placed"},
            original_payload={},
            channel=FakeChannel(),
            tenant_id="tenant-test",
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_sample_rate_one_always_compares(self):
        """Sample rate 1.0 always compares."""
        from fuel.services.shadow_divergence_checker import ShadowDivergenceChecker

        checker = ShadowDivergenceChecker(sample_rate=1.0)
        assert checker._should_sample() is True

    @pytest.mark.asyncio
    async def test_missing_field_in_one_output_detected(self):
        """A field present in one output but not the other is detected."""
        from fuel.services.shadow_divergence_checker import ShadowDivergenceChecker

        checker = ShadowDivergenceChecker(sample_rate=1.0)
        divergences = checker._diff_outputs(
            {"status": "placed", "po_number": "PO-123"},
            {"status": "placed"},
        )
        assert "po_number" in divergences
        assert divergences["po_number"]["new"] == "PO-123"
        assert divergences["po_number"]["legacy"] is None


# ---------------------------------------------------------------------------
# Tests — Legacy Route 410 Gone Behaviour
# ---------------------------------------------------------------------------


class TestLegacyRoute410:
    """Tests that legacy routes return 410 Gone when active_auto."""

    def test_active_auto_legacy_passthrough_not_returned(self):
        """When active_auto, the pipeline does NOT return legacy_passthrough.

        The pipeline processes normally in active_auto — it's the legacy
        webhook receiver that should return 410 Gone based on the flag state.
        """
        # This is a design verification: active_auto means the pipeline
        # processes the order normally (no legacy writes). The 410 Gone
        # response is the responsibility of the legacy route handler
        # (ops/webhooks/receiver.py) which checks the flag state.
        assert "active_auto" in VALID_STATES
        assert "disabled" in VALID_STATES


# ---------------------------------------------------------------------------
# Tests — Flag State Transitions
# ---------------------------------------------------------------------------


class TestFlagStateTransitions:
    """Tests for valid flag state transitions via the admin endpoint."""

    def test_disabled_to_shadow(self, client, ff_service):
        """Can transition from disabled to shadow."""
        resp = client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/shadow"
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["previous_state"] == "disabled"
        assert resp.json()["data"]["new_state"] == "shadow"

    def test_shadow_to_active_gated(self, client, ff_service):
        """Can transition from shadow to active_gated."""
        client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/shadow"
        )
        resp = client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/active_gated"
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["previous_state"] == "shadow"
        assert resp.json()["data"]["new_state"] == "active_gated"

    def test_active_gated_to_active_auto(self, client, ff_service):
        """Can transition from active_gated to active_auto."""
        client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/active_gated"
        )
        resp = client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/active_auto"
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["previous_state"] == "active_gated"
        assert resp.json()["data"]["new_state"] == "active_auto"

    def test_rollback_from_active_auto_to_disabled(self, client, ff_service):
        """Can rollback from active_auto to disabled within 60 seconds."""
        client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/active_auto"
        )
        resp = client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/disabled"
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["previous_state"] == "active_auto"
        assert resp.json()["data"]["new_state"] == "disabled"

    def test_rollback_from_active_gated_to_shadow(self, client, ff_service):
        """Can rollback from active_gated to shadow."""
        client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/active_gated"
        )
        resp = client.post(
            "/api/ops/admin/feature-flags/tenant-test/order-intake-pipeline/shadow"
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["previous_state"] == "active_gated"
        assert resp.json()["data"]["new_state"] == "shadow"


# ---------------------------------------------------------------------------
# Tests — the order endpoints over the real pipeline (findings F1, F2)
# ---------------------------------------------------------------------------


class _RecordingOrderRepo:
    """Stands in for ``FuelOrderRepository`` inside the pipeline and records writes."""

    def __init__(self, upserts: list, events: list):
        self._upserts = upserts
        self._events = events

    async def upsert_with_last_event_timestamp(self, tenant_id, doc):
        self._upserts.append((tenant_id, doc))

    async def append_event(self, tenant_id, ev):
        self._events.append((tenant_id, ev))


class _RecordingHook:
    def __init__(self):
        self.before = 0
        self.after = 0

    async def before_accept(self, order_doc):
        self.before += 1
        return order_doc

    async def after_accept(self, order_doc):
        self.after += 1


_ORDER_BODY = {
    "customer_id": "cust-1",
    "customer_name": "Test Customer",
    "ship_to_address": "123 Main St",
    "ship_to_lat": 30.0,
    "ship_to_lon": -90.0,
    "product_code": "DIESEL_2",
    "gallons_requested": 500,
    "call_type": "one_off",
    "delivery_window_start": "2026-01-16T08:00:00+00:00",
    "delivery_window_end": "2026-01-16T12:00:00+00:00",
}


class TestOrderIntakeDisabledAndDryRun:
    """POST /api/orders and /bulk through the real ``OrderIntakePipeline``.

    F1: a disabled intake flag is a 409 ``ORDER_INTAKE_DISABLED`` in the
    standard envelope, never a 201 that stored nothing. F2: a bulk dry run
    runs the pipeline's value validation, so a row the real run would refuse
    is reported as an error rather than ``dry_run_valid``.
    """

    @pytest.fixture
    def harness(self):
        from errors.handlers import register_exception_handlers
        from fuel.api.order_endpoints import configure_order_endpoints
        from fuel.api.order_endpoints import router as order_router
        from fuel.intake.adapter_base import IntakeAdapterRegistry
        from fuel.intake.dispatcher_adapter import DispatcherIntakeAdapter
        from fuel.services.order_intake_pipeline import OrderIntakePipeline
        from middleware.request_id import RequestIDMiddleware
        from ops.middleware.tenant_guard import TenantContext, get_tenant_context

        ff = FakeFeatureFlagService(initial_state="disabled")
        registry = IntakeAdapterRegistry()
        registry.register(
            DispatcherIntakeAdapter(), channel_type="dispatcher", schema_version="1.0"
        )
        channel_repo = MagicMock()
        channel_repo.ensure_dispatcher_channel = AsyncMock(return_value=FakeChannel())
        idempotency = FakeIdempotencyService()
        ws = FakeOrdersWSManager()
        pipeline = OrderIntakePipeline(
            es_service=FakeEsService(),
            intake_channel_repo=channel_repo,
            adapter_registry=registry,
            idempotency_service=idempotency,
            feature_flag_service=ff,
            poison_queue_service=FakePoisonQueueService(),
            ws_manager=ws,
            credentials_vault=FakeCredentialsVault(),
            customer_tank_repo=FakeCustomerTankRepo(),
        )
        hook = _RecordingHook()
        pipeline.register_hook(hook)
        configure_order_endpoints(
            order_intake_pipeline=pipeline, order_repository=MagicMock()
        )

        app = FastAPI()
        register_exception_handlers(app)
        app.add_middleware(RequestIDMiddleware)
        app.include_router(order_router)
        app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
            tenant_id="tenant-test",
            user_id="dispatcher-1",
            has_pii_access=False,
            roles=["dispatcher"],
        )

        upserts: list = []
        events: list = []
        with patch(
            "fuel.order_repository.FuelOrderRepository",
            return_value=_RecordingOrderRepo(upserts, events),
        ):
            yield {
                "client": TestClient(app),
                "ff": ff,
                "upserts": upserts,
                "events": events,
                "idempotency": idempotency,
                "ws": ws,
                "hook": hook,
            }

    async def _enable(self, ff):
        await ff.set_overlay_state(
            "order_intake_pipeline", "tenant-test", "active_auto", "admin"
        )

    def test_create_with_flag_disabled_is_409_standard_envelope(self, harness):
        resp = harness["client"].post(
            "/api/orders",
            json={"client_event_id": "evt-f1-1", **_ORDER_BODY},
            headers={"X-Request-ID": "req-f1-409"},
        )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["error_code"] == "ORDER_INTAKE_DISABLED"
        assert body["message"] == "Order intake isn't enabled for this account"
        assert body["request_id"] == "req-f1-409"
        assert "detail" not in body
        assert harness["upserts"] == []
        assert harness["events"] == []

    @pytest.mark.asyncio
    async def test_create_with_flag_enabled_is_201(self, harness):
        await self._enable(harness["ff"])
        resp = harness["client"].post(
            "/api/orders", json={"client_event_id": "evt-f1-2", **_ORDER_BODY}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "processed"
        assert len(harness["upserts"]) == 1

    def test_bulk_row_on_disabled_flag_is_an_error(self, harness):
        resp = harness["client"].post(
            "/api/orders/bulk", json={"orders": [dict(_ORDER_BODY)]}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["processed"] == 0
        assert body["errors"] == 1
        row = body["results"][0]
        assert row["status"] == "error"
        assert row["error"] == (
            "ORDER_INTAKE_DISABLED: Order intake isn't enabled for this account"
        )
        assert harness["upserts"] == []

    def test_bulk_dry_run_on_disabled_flag_is_an_error(self, harness):
        resp = harness["client"].post(
            "/api/orders/bulk",
            json={"orders": [dict(_ORDER_BODY)], "dry_run": True},
        )
        assert resp.status_code == 200, resp.text
        row = resp.json()["results"][0]
        assert row["status"] == "error"
        assert row["error"].startswith("ORDER_INTAKE_DISABLED: ")

    @pytest.mark.asyncio
    async def test_bulk_dry_run_runs_pipeline_value_validation(self, harness):
        await self._enable(harness["ff"])
        inverted = {
            **_ORDER_BODY,
            "delivery_window_start": "2026-01-16T12:00:00+00:00",
            "delivery_window_end": "2026-01-16T08:00:00+00:00",
        }
        unknown_tank = {**_ORDER_BODY, "customer_tank_id": "tank-nope"}
        resp = harness["client"].post(
            "/api/orders/bulk",
            json={"orders": [dict(_ORDER_BODY), inverted, unknown_tank], "dry_run": True},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        statuses = [r["status"] for r in body["results"]]
        assert statuses == ["dry_run_valid", "error", "error"]
        assert body["processed"] == 1
        assert body["errors"] == 2
        assert body["results"][1]["error"].startswith("ORDER_PAYLOAD_INVALID: ")
        assert body["results"][2]["error"].startswith("INVALID_CUSTOMER_TANK_REF: ")
        # A dry run writes, publishes and consumes nothing.
        assert harness["upserts"] == []
        assert harness["events"] == []
        assert harness["idempotency"].marked == []
        assert harness["ws"].broadcasts == []
        assert harness["hook"].before == 0
        assert harness["hook"].after == 0
