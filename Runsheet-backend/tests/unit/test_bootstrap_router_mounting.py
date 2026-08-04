"""
Regression tests for idempotent router mounting during boot.

``main.py`` includes a fixed set of routers at **import** time and then boots the
rest of the wiring from its lifespan. ``bootstrap/agents.py`` used to call
``app.include_router`` directly for three routers ``main.py`` had already
included (``Agents.support.mvp_endpoints.router`` and both routers from
``fuel.api.fuel_ops_endpoints``), so a booted app carried every one of those
routes twice: 329 routes before boot, 390 after.

FastAPI serves the first match, so the duplicates were invisible at request time.
They were not invisible in the generated documentation — each duplicate produced
an extra row in ``docs/endpoint-registry.md``, which is what made
``tests/unit/test_endpoint_registry.py`` fail in the full suite (a test that
booted the chain against the shared ``main.app`` singleton left the duplicates
behind) while passing in isolation.

Mounting through ``bootstrap.routing.mount_router`` makes registration
idempotent on the router's own paths.
"""
import inspect

from fastapi import APIRouter, FastAPI

import bootstrap.agents as agents_bootstrap
import bootstrap.driver as driver_bootstrap
from bootstrap.routing import mount_router


def _paths(app) -> list:
    return [getattr(route, "path", None) for route in app.routes]


class TestMountRouter:
    """``mount_router`` adds a router once and only once."""

    def test_mounts_a_new_router(self):
        app = FastAPI()
        router = APIRouter()

        @router.get("/thing")
        async def _thing():  # pragma: no cover - never called
            return {}

        before = len(app.routes)
        mount_router(app, router)

        assert len(app.routes) == before + 1
        assert "/thing" in _paths(app)

    def test_second_mount_of_the_same_router_is_a_no_op(self):
        app = FastAPI()
        router = APIRouter()

        @router.get("/thing")
        async def _thing():  # pragma: no cover - never called
            return {}

        mount_router(app, router)
        after_first = _paths(app)
        mount_router(app, router)

        assert _paths(app) == after_first

    def test_skips_a_router_someone_else_already_included(self):
        """The production case: ``main.py`` included it, boot must not repeat it."""
        app = FastAPI()
        router = APIRouter()

        @router.get("/thing")
        async def _thing():  # pragma: no cover - never called
            return {}

        app.include_router(router)
        after_import_time = _paths(app)

        mount_router(app, router)

        assert _paths(app) == after_import_time
        assert _paths(app).count("/thing") == 1

    def test_mounts_a_router_with_disjoint_paths(self):
        """Idempotency keys off paths, so an unrelated router still mounts."""
        app = FastAPI()
        first = APIRouter()
        second = APIRouter()

        @first.get("/one")
        async def _one():  # pragma: no cover - never called
            return {}

        @second.get("/two")
        async def _two():  # pragma: no cover - never called
            return {}

        mount_router(app, first)
        mount_router(app, second)

        assert "/one" in _paths(app)
        assert "/two" in _paths(app)


class TestBootstrapModulesMountIdempotently:
    """No bootstrap module reaches for ``app.include_router`` directly.

    A direct call is the defect: it duplicates whatever ``main.py`` already
    mounted, and it duplicates itself on any app booted more than once.
    """

    def test_agents_bootstrap_uses_the_shared_helper(self):
        source = inspect.getsource(agents_bootstrap)

        assert "app.include_router(" not in source, (
            "bootstrap/agents.py includes a router directly — use "
            "bootstrap.routing.mount_router so a router main.py already "
            "mounted is not registered twice"
        )
        assert "mount_router(app," in source

    def test_driver_bootstrap_uses_the_shared_helper(self):
        source = inspect.getsource(driver_bootstrap)

        assert "app.include_router(" not in source
        assert "mount_router(app," in source
