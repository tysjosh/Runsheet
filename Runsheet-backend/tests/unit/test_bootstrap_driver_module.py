"""
Unit tests for ``bootstrap/driver.py`` — the driver-domain bootstrap module.

Covers what task 2.3 puts in place: the module's position in ``_BOOT_ORDER``,
the ``initialize`` / ``shutdown`` contract every bootstrap module must satisfy,
the Mobile_Session router mount, and the authoritative re-pass over the two
same-named ``configure_driver_endpoints`` functions.

Validates: Requirements 4.1, 15.12
"""
import pytest
from fastapi import FastAPI

import bootstrap.driver as bootstrap_driver
from bootstrap import _BOOT_ORDER
from bootstrap.container import ServiceContainer


class _FakeDriverRepository:
    """Minimal ``drivers_current`` read surface."""

    async def get(self, tenant_id, driver_id):  # pragma: no cover - not called
        return None


class _FakeJobService:
    """Placeholder collaborator for the scheduling driver endpoints."""


@pytest.fixture
def app():
    return FastAPI()


@pytest.fixture
def container():
    cont = ServiceContainer()
    cont.es_service = object()
    return cont


class TestBootOrder:
    """The module's position in the boot sequence."""

    def test_driver_is_after_agents_and_before_integrations(self):
        """``redis_client`` arrives in ``agents``, so ``driver`` must follow it."""
        assert "driver" in _BOOT_ORDER
        assert _BOOT_ORDER.index("driver") > _BOOT_ORDER.index("agents")
        assert _BOOT_ORDER.index("driver") < _BOOT_ORDER.index("integrations")

    def test_module_exposes_the_bootstrap_contract(self):
        """``initialize_all`` / ``shutdown_all`` require these two coroutines."""
        import inspect

        assert inspect.iscoroutinefunction(bootstrap_driver.initialize)
        assert inspect.iscoroutinefunction(bootstrap_driver.shutdown)


class TestSessionRouterWiring:
    """Mobile_Session mount and collaborator wiring."""

    @pytest.mark.asyncio
    async def test_mounts_session_router_and_wires_repository(self, app, container):
        repository = _FakeDriverRepository()
        container.driver_repository = repository

        await bootstrap_driver.initialize(app, container)

        paths = {route.path for route in app.routes}
        assert "/auth/driver/session" in paths
        assert "/auth/driver/session/refresh" in paths

        from driver.api import session_endpoints

        assert session_endpoints._driver_repository is repository

    @pytest.mark.asyncio
    async def test_mount_is_idempotent(self, app, container):
        """Booting the same app twice must not duplicate the session routes."""
        container.driver_repository = _FakeDriverRepository()

        await bootstrap_driver.initialize(app, container)
        first = [r.path for r in app.routes].count("/auth/driver/session")
        await bootstrap_driver.initialize(app, container)
        second = [r.path for r in app.routes].count("/auth/driver/session")

        assert first == second

    @pytest.mark.asyncio
    async def test_initialize_survives_an_empty_container(self, app, container):
        """A missing collaborator degrades the surface; it does not raise."""
        await bootstrap_driver.initialize(app, container)

        assert "/auth/driver/session" in {route.path for route in app.routes}


class TestWorkRouterWiring:
    """Driver_Work_API mount and collaborator wiring."""

    @pytest.mark.asyncio
    async def test_mounts_work_router_and_wires_collaborators(self, app, container):
        order_repository = object()
        redis_client = object()
        container.order_repository = order_repository
        container.redis_client = redis_client

        await bootstrap_driver.initialize(app, container)

        paths = {route.path for route in app.routes}
        assert "/api/driver/work" in paths
        assert "/api/driver/work/{order_id}" in paths
        assert "/api/driver/me" in paths

        from driver.api import work_endpoints

        assert work_endpoints._order_repository is order_repository
        assert work_endpoints._redis_client is redis_client
        assert work_endpoints._work_service is not None

    @pytest.mark.asyncio
    async def test_absent_redis_is_a_cache_miss_not_a_boot_failure(
        self, app, container
    ):
        """No Redis means the bundle cache always misses; the surface still boots."""
        await bootstrap_driver.initialize(app, container)

        from driver.api import work_endpoints

        assert work_endpoints._redis_client is None
        assert "/api/driver/work" in {route.path for route in app.routes}


class TestAuthoritativeRePass:
    """The two same-named ``configure_driver_endpoints`` functions."""

    @pytest.mark.asyncio
    async def test_rewires_both_driver_endpoint_modules(self, app, container):
        repository = _FakeDriverRepository()
        job_service = _FakeJobService()
        qualification_service = object()
        container.driver_repository = repository
        container.job_service = job_service
        container.driver_qualification_service = qualification_service

        await bootstrap_driver.initialize(app, container)

        from fuel.api import driver_endpoints as fuel_driver_endpoints
        from scheduling.api import driver_endpoints as scheduling_driver_endpoints

        # fuel: the ops driver surface, plus the compliance service that does
        # not exist yet when bootstrap/fuel.py runs.
        assert fuel_driver_endpoints._driver_repository is repository
        assert (
            fuel_driver_endpoints._driver_qualification_service
            is qualification_service
        )
        # scheduling: a different function with the same name.
        assert scheduling_driver_endpoints._job_service is job_service


class TestTransitionGateStackWiring:
    """The driver transition gate stack (task 9.1).

    It is wired here and not in ``bootstrap/fuel.py`` because
    ``Dispatch_Eligibility`` comes from ``bootstrap/compliance.py``, which runs
    earlier in ``_BOOT_ORDER`` than ``driver`` but later than ``fuel``.
    """

    def test_compliance_precedes_driver_in_boot_order(self):
        assert _BOOT_ORDER.index("compliance") < _BOOT_ORDER.index("driver")

    @pytest.mark.asyncio
    async def test_composes_the_stack_with_the_compliance_service(
        self, app, container
    ):
        from driver.services import order_transition_service

        qualification_service = object()
        order_service = object()
        order_repository = object()
        container.driver_qualification_service = qualification_service
        container.order_service = order_service
        container.order_repository = order_repository

        await bootstrap_driver.initialize(app, container)

        stack = order_transition_service.get_gate_stack()
        assert stack is not None
        assert stack._driver_qualification_service is qualification_service
        assert order_transition_service.get_order_service() is order_service
        assert order_transition_service.get_order_repository() is order_repository

    @pytest.mark.asyncio
    async def test_hos_gate_stays_dormant_in_phase_1(self, app, container):
        """``hos_advisory_service`` is ``None`` until Phase 2 arms the gate."""
        from driver.services import order_transition_service

        await bootstrap_driver.initialize(app, container)

        stack = order_transition_service.get_gate_stack()
        assert stack._hos_advisory_service is None

    @pytest.mark.asyncio
    async def test_mounts_the_transition_router_once(self, app, container):
        """The driver status endpoint is mounted here, idempotently (task 9.2)."""
        container.order_service = object()
        container.order_repository = object()

        await bootstrap_driver.initialize(app, container)
        await bootstrap_driver.initialize(app, container)

        paths = [route.path for route in app.routes]
        assert paths.count("/api/driver/orders/{order_id}/status") == 1

    @pytest.mark.asyncio
    async def test_wires_without_an_inspection_service(self, app, container):
        """Inspection intake lands in a later task; boot must not depend on it."""
        from driver.services import order_transition_service

        await bootstrap_driver.initialize(app, container)

        assert order_transition_service.get_gate_stack() is not None


class TestShutdown:
    """Shutdown tolerates a boot that started no background task."""

    @pytest.mark.asyncio
    async def test_shutdown_without_tasks_is_a_no_op(self, app, container):
        await bootstrap_driver.shutdown(app, container)


class TestPODOTPNotificationWiring:
    """Setter injection of Notification_Pipeline into PODOTPService (task 10.3).

    ``bootstrap/fuel.py`` constructs the service and subscribes it, but
    ``notifications`` boots after ``fuel``, so the pipeline can only be wired
    here.

    Validates: Requirement 5.27
    """

    def test_notifications_precedes_driver_in_boot_order(self):
        assert _BOOT_ORDER.index("notifications") < _BOOT_ORDER.index("driver")

    @pytest.mark.asyncio
    async def test_injects_the_notification_service(self, app, container):
        from driver.services.pod_otp_service import PODOTPService

        pod_otp_service = PODOTPService(es_service=object())
        notification_service = object()
        container.pod_otp_service = pod_otp_service
        container.notification_service = notification_service

        await bootstrap_driver.initialize(app, container)

        assert pod_otp_service._notification_service is notification_service

    @pytest.mark.asyncio
    async def test_absent_notification_service_degrades_without_raising(
        self, app, container
    ):
        from driver.services.pod_otp_service import PODOTPService

        pod_otp_service = PODOTPService(es_service=object())
        container.pod_otp_service = pod_otp_service

        await bootstrap_driver.initialize(app, container)

        assert pod_otp_service._notification_service is None
