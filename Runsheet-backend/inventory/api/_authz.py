"""Who may reach the inventory surface.

Before this module every route under ``/api/inventory`` resolved a
``TenantContext`` and performed no role check. Verified against a running
server: the ``driver`` account could read ``GET /api/inventory/items``.

The gate is split by *what the call does* rather than applied to the whole
router, because a blanket admin-only rule would break this module's own design.
``inventory-pipeline-integration`` exists to make inventory "an active
participant in operational decision-making", and its Requirement 1 user story is
literally the dispatcher's:

    As a dispatcher, I want the system to verify that critical maintenance parts
    for a truck type are in stock before assigning it to a job.

A dispatcher who cannot read inventory cannot see the readiness indicator on
truck assignment or the alert badge on ``/ops/control``. So:

* **Reads** — the catalogue, alerts, summary, and per-item history. Operations
  roles, because that is who consumes readiness during a shift.
* **Stock adjustments** — operations roles too. Recording that a mechanic used
  two tires is shift work, not master-data administration. Note the *automatic*
  consumption path on maintenance-job completion does not come through here at
  all: ``scheduling/services/job_service.py`` writes a ``StockAdjustment``
  in-process, so this gate cannot starve it.
* **Catalogue mutations** — create, update, delete. ``admin`` only. Adding a part
  number or removing one is master data: it changes what every readiness check
  in the tenant evaluates against, and a wrong ``compatible_assets`` value
  silently changes dispatch decisions.
"""

from __future__ import annotations

from auth.router_guards import roles_dependency

#: Read and adjust. Matches the `fleet` and `control` nav items, both of which
#: require these two roles in ``runsheet/src/config/modules.ts``.
INVENTORY_OPS_ROLES: tuple[str, ...] = ("admin", "dispatcher")

#: Create / update / delete an inventory item. Master data, so `admin` only.
INVENTORY_ADMIN_ROLES: tuple[str, ...] = ("admin",)

#: Dependency for reads and stock adjustments.
inventory_ops_dependency = roles_dependency(*INVENTORY_OPS_ROLES)

#: Dependency for catalogue mutations.
inventory_admin_dependency = roles_dependency(*INVENTORY_ADMIN_ROLES)
