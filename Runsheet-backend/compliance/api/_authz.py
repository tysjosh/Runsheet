"""Who may reach the compliance surface.

Before this module every router under ``compliance/api/`` resolved a
``TenantContext`` per handler and performed **no** role check — zero of the eight
endpoint modules had one. Verified against a running server: the ``driver``
account could read ``GET /api/compliance/meters``. Tenant isolation held; role
isolation did not exist.

These are DOT / FMCSA / IRS / IFTA records: driver qualification, asset
certifications, meter audit trail, terminal bills of lading, IFTA returns,
k-factor calibration, tax jurisdictions and exemptions. Unlike pricing — which is
staff-only because the customer's ERP owns the authoritative price — compliance is
the customer's *own* regulatory obligation, and a dispatcher legitimately works it
during a shift: they check certification expiry before assigning a driver.

So the audience is the operations roles, matching every caller found in the web
app (``FleetDashboard``, ``FleetTracking``, ``DispatchCockpit``, ``ComplianceHub``)
and matching the ``compliance`` and ``fleet`` nav items, which both require
``admin`` or ``dispatcher`` in ``runsheet/src/config/modules.ts``.

Nothing driver-facing depends on this surface: neither ``driver-app`` nor
``driver/api`` calls ``/api/compliance/*``. The compliance logic the delivery
pipeline needs — ``DyedDieselEnforcer``, ``delivery_filter``, ``VCFCalculator``,
``HOSStatus`` — is invoked in-process against the services, never over HTTP, so
gating the REST surface does not touch load building, routing, reconciliation, or
driver hours of service.
"""

from __future__ import annotations

from auth.router_guards import roles_dependency

#: Operations roles. Compliance is the customer's own regulatory obligation, so
#: their `admin` and the dispatcher working the shift both need it. Deliberately
#: NOT `platform_admin`-only: that would lock a customer out of their own DOT and
#: IRS records.
COMPLIANCE_OPS_ROLES: tuple[str, ...] = ("admin", "dispatcher")

#: Router-level dependency for every compliance router.
#:
#: Also used by ``asset_compliance_endpoints``, which serves ``/api/fleet/assets``
#: rather than ``/api/compliance/*`` — it lives in this package but is a Fleet
#: surface, and the ``fleet`` nav item carries the same two roles.
compliance_ops_dependency = roles_dependency(*COMPLIANCE_OPS_ROLES)
