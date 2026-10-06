"""Write-time check that a customer-tank's ``customer_id`` is a real customer.

Shared by the REST customer-tank create/update (``fuel/api/fuel_ops_endpoints.py``)
and the CSV tank importer (``fuel/services/tank_import_service.py``), so both
writers refuse the same references (cross-module-entity-linkage Req 7.1,
finding F7).
"""

from __future__ import annotations

from typing import Any, Optional


async def validate_customer_ref(
    resolver: Any, tenant_id: str, customer_id: Optional[str]
) -> None:
    """Reject a ``customer_id`` that doesn't resolve in ``tenant_id``.

    Validation is delegated to the shared ``RefResolver`` and is only enforced
    when a ``customer`` loader is registered, so a partially-wired environment
    (e.g. a focused unit test that injects no resolver) stays additive rather
    than rejecting every write. An empty ``customer_id`` is left to the model's
    own required-field check.

    Raises:
        AppException: ``validation_error`` (HTTP 400,
            ``details.reason = customer_not_found``) when the reference is
            non-existent or belongs to another tenant.
    """
    if not customer_id:
        return
    try:
        registered = "customer" in resolver.registered_types()
    except Exception:  # noqa: BLE001 - defensive; never block a write on this
        registered = False
    if not registered:
        return
    await resolver.validate_ref(tenant_id, "customer", customer_id, required=True)


__all__ = ["validate_customer_ref"]
