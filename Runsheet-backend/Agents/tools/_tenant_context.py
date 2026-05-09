"""
Tenant ContextVar for Strands @tool-decorated AI agent tools.

Strands tools are discovered by inspecting their function signatures, so we
cannot simply thread a ``tenant_id`` kwarg through every tool without breaking
the LLM's understanding of the tool schema. Instead, specialist agents
(``FleetAgent``, ``FuelAgent``, ``SchedulingAgent``, ``ReportingAgent``,
``OpsIntelligenceAgent``) and the orchestrator entry points
(``LogisticsAgent.chat_streaming`` / ``chat_fallback``) set this ContextVar
for the duration of a request. Each ES-reading tool then reads the var at
entry and uses it to scope its Elasticsearch query via
``inject_tenant_filter``.

Helpers:
    * ``set_current_tenant(tenant_id)``: returns a context manager that sets
      the var on entry and restores the previous value on exit. Use from
      agent entry points.
    * ``get_current_tenant()``: fetch the currently bound tenant_id. Raises
      ``RuntimeError`` if nothing is set — callers should only call this
      from inside a tool invocation that's already running inside a tenant
      scope.
    * ``get_current_tenant_or_none()``: fetch the tenant without raising.
      Used in tests and when gracefully degrading.

Note: the default is ``None`` so importing this module in a test that never
opens a tenant scope does not raise. The loud ``RuntimeError`` only fires
when a tool tries to read the tenant without one being bound.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Iterator, Optional


# Default ``None`` keeps module import + tests-that-never-bind-it safe.
# Tools that read ES call ``get_current_tenant()`` which raises if unset.
current_tenant_id_var: ContextVar[Optional[str]] = ContextVar(
    "current_tenant_id_var", default=None
)


def get_current_tenant() -> str:
    """Return the currently-bound tenant_id, or raise ``RuntimeError``.

    Called at the entry of every ES-reading ``@tool``. The loud error
    surfaces forgotten tenant scopes as test failures instead of silently
    leaking data across tenants.
    """
    value = current_tenant_id_var.get()
    if not value:
        raise RuntimeError(
            "AI tool invoked without a tenant scope. "
            "Ensure the specialist agent or chat entry point calls "
            "set_current_tenant(tenant_id) before dispatching tools."
        )
    return value


def get_current_tenant_or_none() -> Optional[str]:
    """Return the currently-bound tenant_id or ``None`` if unset.

    Use in call sites that should gracefully degrade (e.g. fallback paths
    in legacy code that existed before tenant scoping landed).
    """
    return current_tenant_id_var.get()


@contextlib.contextmanager
def set_current_tenant(tenant_id: Optional[str]) -> Iterator[None]:
    """Bind ``tenant_id`` to the current task/thread for the block's duration.

    The ContextVar is reset to its prior value on exit, which makes the
    helper safe to nest and safe to use across ``asyncio`` tasks that share
    the same event loop.
    """
    token = current_tenant_id_var.set(tenant_id)
    try:
        yield
    finally:
        current_tenant_id_var.reset(token)
