"""Who may change integration instances (C2).

Before this module every route under ``/api/integrations`` resolved a
``TenantContext`` and performed no role check, so any authenticated user,
including a ``driver``, could create, edit, delete, enable, disable or force a
sync of a tenant's ERP/telematics integrations.

Writes are ``admin`` only. That matches the UI: the integrations tab lives in
the AdminHub under the ``admin`` nav item (``requiredRoles: ["admin"]`` in
``runsheet/src/config/modules.ts``). Reads (the instance list, sync runs and
the provider catalogue) are unchanged.
"""

from __future__ import annotations

from auth.router_guards import roles_dependency

#: Create / update / delete / enable / disable / sync-now an instance.
INTEGRATION_ADMIN_ROLES: tuple[str, ...] = ("admin",)

#: Dependency for integration-instance writes.
integration_admin_dependency = roles_dependency(*INTEGRATION_ADMIN_ROLES)
