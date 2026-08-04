"""
Shared router-mounting helper for the bootstrap modules.

Every ``bootstrap`` module that mounts a router does so on the app object it is
handed, and that object is not always fresh:

* ``main.py`` mounts a fixed set of routers at **import** time, then boots the
  remaining wiring from its lifespan. Any bootstrap module that mounts a router
  ``main.py`` already included would register the same paths a second time.
* ``main.app`` is a module-level singleton cached in ``sys.modules``, so a test
  that boots the real chain against it leaves whatever it mounted behind for
  every later test in the session.

Both cases produce duplicate routes: FastAPI serves the first match, so the
duplicates are invisible at request time but show up as duplicated OpenAPI
operations and duplicated rows in ``docs/endpoint-registry.md``.

Mounting through :func:`mount_router` makes registration idempotent on the
router's own paths, so "already mounted" is a no-op regardless of who mounted
it or how many times boot runs.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def mount_router(app, router) -> None:
    """Include ``router`` on ``app`` unless one of its paths is already mounted.

    Idempotent on the router's own paths: if any path the router declares is
    already registered on ``app`` the router is treated as mounted and nothing
    is added.

    Args:
        app: The FastAPI application to mount onto.
        router: The ``APIRouter`` to include.
    """
    router_paths = {getattr(route, "path", None) for route in router.routes}
    router_paths.discard(None)
    existing = {getattr(route, "path", None) for route in app.routes}
    if router_paths & existing:
        logger.debug("Router already mounted (paths: %s)", sorted(router_paths))
        return
    app.include_router(router)
