"""
Depot start-position resolver factory for the Route_Planning_Agent.

Task 9.7 / Req 5.4.6 introduced :func:`route_planning_agent.set_depot_resolver`
so the agent can resolve a real loading-site origin instead of the
``DEFAULT_DEPOT`` null-island sentinel. The agent ships the *seam* but the
production bootstrap never injected a resolver, so depot CRUD data never
reached the routing path — every plan silently routed from ``(0.0, 0.0)``.

This module supplies the missing resolver. It implements the documented
resolution chain:

    truck.assigned_depot_id  →  tenant_settings.default_depot_id  →
    tenant's `is_default` active depot  →  None (no depot configured)

The third step intentionally bridges a wiring gap: the dispatcher UI's
"set as default" action writes the depot document's ``is_default`` flag
(``DepotRepository.update(is_default=True)``), but the resolver chain the
agent documents reads ``tenant_settings.default_depot_id``. Those two were
never linked, so the UI control had no effect on routing. Honoring
``is_default`` here makes the UI affordance actually drive the route origin
even when ``tenant_settings.default_depot_id`` is unset.

Returning ``None`` is a deliberate, safe signal: the agent then raises
``no_depot_configured`` and skips the loading plan rather than producing a
geographically meaningless route from null-island.

Validates: Requirement 5.4.6 (depot fallback wiring).
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Tuple

from fuel.services.truck_start_position import DepotResolver

logger = logging.getLogger(__name__)


def make_depot_start_resolver(
    *,
    depot_repository: Any,
    tenant_settings_service: Any = None,
) -> DepotResolver:
    """Build a :data:`DepotResolver` backed by the depot repository.

    Args:
        depot_repository: A :class:`fuel.depot_models.DepotRepository`
            exposing ``await get(tenant_id, depot_id)`` and
            ``await list_for_tenant(tenant_id, status=...)``.
        tenant_settings_service: Optional
            :class:`services.tenant_settings.TenantSettingsService` exposing
            ``await get_default_depot_id(tenant_id)``. When ``None`` the
            resolver skips the tenant-default step and relies on the
            per-truck ``assigned_depot_id`` and the ``is_default`` bridge.

    Returns:
        An async ``(tenant_id, truck) -> Optional[(lat, lon)]`` callable
        matching the :data:`DepotResolver` contract.
    """
    if depot_repository is None:
        raise ValueError("depot_repository must not be None")

    async def _resolve(
        tenant_id: str, truck: Mapping[str, Any]
    ) -> Optional[Tuple[float, float]]:
        # 1. Per-truck assigned depot, when the caller threaded it through.
        depot_id: Optional[str] = None
        assigned = truck.get("assigned_depot_id") if isinstance(truck, Mapping) else None
        if isinstance(assigned, str) and assigned.strip():
            depot_id = assigned.strip()

        # 2. Tenant-level default depot id.
        if not depot_id and tenant_settings_service is not None:
            try:
                default_id = await tenant_settings_service.get_default_depot_id(
                    tenant_id
                )
                if isinstance(default_id, str) and default_id.strip():
                    depot_id = default_id.strip()
            except Exception as exc:  # noqa: BLE001 — degrade to is_default bridge
                logger.warning(
                    "depot_start_resolver: get_default_depot_id failed for "
                    "tenant=%s: %s",
                    tenant_id,
                    exc,
                )

        # Resolve a concrete depot id to coordinates.
        if depot_id:
            coords = await _coords_for_depot(depot_repository, tenant_id, depot_id)
            if coords is not None:
                return coords
            logger.info(
                "depot_start_resolver: depot_id=%s for tenant=%s did not "
                "resolve to an active depot; trying is_default fallback",
                depot_id,
                tenant_id,
            )

        # 3. is_default bridge — honor the dispatcher UI's "set as default"
        #    flag when no tenant_settings.default_depot_id is wired.
        try:
            depots = await depot_repository.list_for_tenant(
                tenant_id, status="active"
            )
        except Exception as exc:  # noqa: BLE001 — degrade to no-depot
            logger.warning(
                "depot_start_resolver: list_for_tenant failed for tenant=%s: %s",
                tenant_id,
                exc,
            )
            depots = []

        for depot in depots:
            if getattr(depot, "is_default", False):
                return (float(depot.location_lat), float(depot.location_lon))

        # 4. Nothing configured — signal no_depot_configured to the agent.
        logger.info(
            "depot_start_resolver: no assigned/default/is_default depot for "
            "tenant=%s — route planning will skip (no_depot_configured)",
            tenant_id,
        )
        return None

    return _resolve


async def _coords_for_depot(
    depot_repository: Any, tenant_id: str, depot_id: str
) -> Optional[Tuple[float, float]]:
    """Return active-depot coordinates for ``depot_id`` or ``None``."""
    try:
        depot = await depot_repository.get(tenant_id, depot_id)
    except Exception as exc:  # noqa: BLE001 — degrade to None
        logger.warning(
            "depot_start_resolver: depot_repository.get failed for "
            "tenant=%s depot=%s: %s",
            tenant_id,
            depot_id,
            exc,
        )
        return None
    if depot is None:
        return None
    # Only route from active depots; an inactive depot is treated as
    # unconfigured so the agent falls through rather than starting a route
    # from a decommissioned site.
    if getattr(depot, "status", "active") != "active":
        return None
    return (float(depot.location_lat), float(depot.location_lon))


__all__ = ["make_depot_start_resolver"]
