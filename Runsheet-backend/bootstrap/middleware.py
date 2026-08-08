"""
Middleware bootstrap module.

Registers RequestID, rate limiting and SecurityHeaders on the FastAPI app.

---------------------------------------------------------------------------
Why this happens at IMPORT time and not in ``initialize``
---------------------------------------------------------------------------
Starlette builds its middleware stack when the application starts and refuses
``add_middleware`` afterwards. Every bootstrap module's ``initialize`` runs inside
the lifespan startup, which is already too late, so every call here raised::

    RuntimeError: Cannot add middleware after an application has started

and ``bootstrap/__init__.py`` caught it, logged
``Bootstrap module 'middleware' failed``, and carried on. The failure was therefore
both real and invisible: the module reported itself as the owner of four pieces of
middleware while installing none of them.

Only the CORS call was ever visibly compensated for — ``main.py`` registers CORS and
the auth gate at import time and says so. The other three were simply lost. Measured
against the deployed API before this change:

    HTTP/2 200        no x-request-id
                      no content-security-policy
                      no x-frame-options
                      no x-content-type-options

What was actually broken, in order of consequence:

* **Rate-limit rejections returned 500, not 429.** The limits themselves DO work —
  ``@limiter.limit`` closes over the module-level ``Limiter`` and never consults
  ``app.state.limiter`` — so this was latent rather than live. But
  ``setup_rate_limiting`` is what registers the ``RateLimitExceeded`` handler, and
  without it an over-limit request raises into the generic 500 path with no
  ``Retry-After`` and no ``X-RateLimit-*`` headers. Every rate-limited route sits
  behind auth, so it needed an authenticated client to surface.
* **No ``X-Request-ID`` response header, and no request id in logs.** Error bodies
  still carry one because ``errors/handlers.py`` generates its own, which is why this
  looked like it worked.
* **No security headers on API responses.** The Next.js UI sets its own via
  ``next.config.ts``; the API had none.

``register_at_import`` is called from ``main.py`` immediately before the CORS
registration, so the resulting stack is, outermost first::

    CORS -> SecurityHeaders -> RequestID -> auth gate -> route

CORS stays outermost so its headers are attached to the 401s the auth gate returns
and to the 429s the limiter returns. RequestID sits outside the auth gate so
rejected requests are still correlatable.

Requirements: 1.1, 1.2
"""
import logging

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)


def register_at_import(app, settings) -> None:
    """Register RequestID, rate limiting and SecurityHeaders on ``app``.

    Must be called at import time, before the application starts, and before the
    CORS registration in ``main.py`` so CORS remains the outermost layer.

    Synchronous and taking ``settings`` explicitly rather than reading the container,
    because at import time there is no container yet — that is the whole reason this
    cannot live in ``initialize``.
    """
    from middleware.request_id import RequestIDMiddleware
    from middleware.rate_limiter import setup_rate_limiting
    from middleware.security_headers import setup_security_headers

    # Added first, so it ends up INSIDE the security headers and CORS layers but
    # outside the auth gate: a 401 still comes back with a correlation id.
    app.add_middleware(RequestIDMiddleware)

    # Not middleware. It sets ``app.state.limiter`` and registers the
    # ``RateLimitExceeded`` handler that turns an over-limit request into a 429
    # instead of a 500.
    setup_rate_limiting(
        app,
        api_rate_limit=settings.rate_limit_requests_per_minute,
        ai_rate_limit=settings.rate_limit_ai_requests_per_minute,
    )

    setup_security_headers(app)

    logger.info(
        "Middleware registered at import time "
        "(RequestID, rate limiting %s/min, security headers)",
        settings.rate_limit_requests_per_minute,
    )


async def initialize(app, container: ServiceContainer) -> None:
    """No-op. Middleware is registered at import time by ``register_at_import``.

    This module stays in ``_BOOT_ORDER`` so the boot sequence keeps its documented
    shape, but it deliberately does nothing here. Anything it added would raise
    ``RuntimeError: Cannot add middleware after an application has started`` and be
    swallowed by the fail-open handler in ``bootstrap/__init__.py`` — which is exactly
    the failure this module used to produce on every single boot.
    """
    logger.debug(
        "middleware bootstrap is a no-op; registration happens at import time in "
        "main.py via register_at_import"
    )
