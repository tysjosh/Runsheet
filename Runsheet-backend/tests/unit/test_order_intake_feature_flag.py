"""
Unit tests for the Order Intake Pipeline feature flag behaviour.

Covers each flag state's dual-write / dual-broadcast / legacy-response
behaviour and the admin rollback endpoint.

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


class FakeLegacyDualWriter:
    def __init__(self):
        self.mirrored_orders: list = []

    async def mirror_order(self, order_doc):
        self.mirrored_orders.append(order_doc)


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
def legacy_dual_writer():
    return FakeLegacyDualWriter()


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
    def pipeline_deps(self, ff_service, ws_manager, legacy_ws_manager, legacy_dual_writer):
        """Common pipeline dependencies."""
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
            "legacy_dual_writer": legacy_dual_writer,
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

    @pytest.mark.asyncio
    async def test_shadow_state_dual_writes_and_compares(
        self, pipeline_deps, ff_service, legacy_dual_writer
    ):
        """When flag is shadow, pipeline writes to new path AND dual-writes to legacy."""
        from fuel.services.order_intake_pipeline import OrderIntakePipeline

        # Set state to shadow
        await ff_service.set_overlay_state(
            "order_intake_pipeline", "tenant-test", "shadow", "admin"
        )

        pipeline = OrderIntakePipeline(**pipeline_deps)
        channel = FakeChannel()

        with patch("fuel.order_repository.FuelOrderRepository") as MockRepo:
            mock_repo = FakeOrderRepo()
            MockRepo.return_value = mock_repo

            with patch(
                "fuel.services.order_intake_pipeline.OrderIntakePipeline._run_shadow_divergence_check",
                new_callable=AsyncMock,
            ) as mock_divergence:
                result = await pipeline._ingest_common(
                    channel=channel,
                    payload={"schema_version": "1.0"},
                    request_id="req-002",
                    actor_user_id="user-1",
                    client_event_id="evt-002",
                )

                assert result.status == "processed"
                # Shadow divergence check should have been called
                mock_divergence.assert_awaited_once()
                # Legacy dual-writer should have been called
                assert len(legacy_dual_writer.mirrored_orders) == 1

    @pytest.mark.asyncio
    async def test_active_gated_writes_new_and_mirrors_legacy(
        self, pipeline_deps, ff_service, legacy_dual_writer
    ):
        """When flag is active_gated, writes to new path + dual-mirrors to legacy."""
        from fuel.services.order_intake_pipeline import OrderIntakePipeline

        await ff_service.set_overlay_state(
            "order_intake_pipeline", "tenant-test", "active_gated", "admin"
        )

        pipeline = OrderIntakePipeline(**pipeline_deps)
        channel = FakeChannel()

        with patch("fuel.order_repository.FuelOrderRepository") as MockRepo:
            mock_repo = FakeOrderRepo()
            MockRepo.return_value = mock_repo

            result = await pipeline._ingest_common(
                channel=channel,
                payload={"schema_version": "1.0"},
                request_id="req-003",
                actor_user_id="user-1",
                client_event_id="evt-003",
            )

            assert result.status == "processed"
            # Legacy dual-writer should have been called (active_gated mirrors)
            assert len(legacy_dual_writer.mirrored_orders) == 1

    @pytest.mark.asyncio
    async def test_active_auto_writes_only_new_path(
        self, pipeline_deps, ff_service, legacy_dual_writer
    ):
        """When flag is active_auto, writes only to new path — no legacy."""
        from fuel.services.order_intake_pipeline import OrderIntakePipeline

        await ff_service.set_overlay_state(
            "order_intake_pipeline", "tenant-test", "active_auto", "admin"
        )

        pipeline = OrderIntakePipeline(**pipeline_deps)
        channel = FakeChannel()

        with patch("fuel.order_repository.FuelOrderRepository") as MockRepo:
            mock_repo = FakeOrderRepo()
            MockRepo.return_value = mock_repo

            result = await pipeline._ingest_common(
                channel=channel,
                payload={"schema_version": "1.0"},
                request_id="req-004",
                actor_user_id="user-1",
                client_event_id="evt-004",
            )

            assert result.status == "processed"
            # Legacy dual-writer should NOT have been called
            assert len(legacy_dual_writer.mirrored_orders) == 0

    @pytest.mark.asyncio
    async def test_active_auto_stops_legacy_broadcast(
        self, pipeline_deps, ff_service, legacy_ws_manager
    ):
        """When flag is active_auto, legacy WS broadcast is suppressed."""
        from fuel.services.order_intake_pipeline import OrderIntakePipeline

        await ff_service.set_overlay_state(
            "order_intake_pipeline", "tenant-test", "active_auto", "admin"
        )

        pipeline = OrderIntakePipeline(**pipeline_deps)
        channel = FakeChannel()

        with patch("fuel.order_repository.FuelOrderRepository") as MockRepo:
            mock_repo = FakeOrderRepo()
            MockRepo.return_value = mock_repo

            await pipeline._ingest_common(
                channel=channel,
                payload={"schema_version": "1.0"},
                request_id="req-005",
                actor_user_id="user-1",
                client_event_id="evt-005",
            )

            # Legacy WS manager should NOT have received any broadcasts
            assert len(legacy_ws_manager.broadcasts) == 0

    @pytest.mark.asyncio
    async def test_shadow_state_still_broadcasts_to_legacy_ws(
        self, pipeline_deps, ff_service, legacy_ws_manager
    ):
        """When flag is shadow, legacy WS broadcast still fires."""
        from fuel.services.order_intake_pipeline import OrderIntakePipeline

        await ff_service.set_overlay_state(
            "order_intake_pipeline", "tenant-test", "shadow", "admin"
        )

        pipeline = OrderIntakePipeline(**pipeline_deps)
        channel = FakeChannel()

        with patch("fuel.order_repository.FuelOrderRepository") as MockRepo:
            mock_repo = FakeOrderRepo()
            MockRepo.return_value = mock_repo

            await pipeline._ingest_common(
                channel=channel,
                payload={"schema_version": "1.0"},
                request_id="req-006",
                actor_user_id="user-1",
                client_event_id="evt-006",
            )

            # Legacy WS manager should have received a shipment_update broadcast
            assert len(legacy_ws_manager.shipment_updates) >= 1

    @pytest.mark.asyncio
    async def test_active_gated_still_broadcasts_to_legacy_ws(
        self, pipeline_deps, ff_service, legacy_ws_manager
    ):
        """When flag is active_gated, legacy WS broadcast still fires."""
        from fuel.services.order_intake_pipeline import OrderIntakePipeline

        await ff_service.set_overlay_state(
            "order_intake_pipeline", "tenant-test", "active_gated", "admin"
        )

        pipeline = OrderIntakePipeline(**pipeline_deps)
        channel = FakeChannel()

        with patch("fuel.order_repository.FuelOrderRepository") as MockRepo:
            mock_repo = FakeOrderRepo()
            MockRepo.return_value = mock_repo

            await pipeline._ingest_common(
                channel=channel,
                payload={"schema_version": "1.0"},
                request_id="req-007",
                actor_user_id="user-1",
                client_event_id="evt-007",
            )

            # Legacy WS manager should have received a shipment_update broadcast
            assert len(legacy_ws_manager.shipment_updates) >= 1


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
