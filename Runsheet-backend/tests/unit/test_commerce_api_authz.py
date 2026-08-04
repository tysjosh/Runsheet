"""Every commerce router enforces a role.

The regression these tests exist for: all nine routers under ``commerce/api/``
resolved a ``TenantContext`` and went straight to their service with no role
check, so any authenticated member of a tenant — a ``driver`` included — could
read the tenant's receivables aging, account roster, price books, and payment
records. Tenant isolation held; role isolation did not exist.

Two properties are asserted, and the first is the one that matters:

1. **No router is ungated.** Derived from the router objects themselves rather
   than from a hand-maintained list, so a router added later without a gate fails
   here instead of shipping open.
2. **The right audience** for each surface: staff-only for the pricing/billing
   set the ERP owns, operations roles for customers and invoices.

The feature-flag check must still precede the role check, so a tenant without the
commerce backbone gets 404 rather than 403 — a disabled feature should stay
invisible rather than advertise that it exists and is merely forbidden.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import commerce.api
from commerce.api._authz import (
    require_commerce_ops,
    require_commerce_staff,
)
from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext


def _ctx(*roles: str) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-A",
        user_id="user-1",
        has_pii_access=False,
        roles=list(roles),
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )


# ---------------------------------------------------------------------------
# The two policies
# ---------------------------------------------------------------------------


class TestStaffPolicy:
    """Tier 4 — the pricing and billing surfaces the customer's ERP owns."""

    def test_platform_admin_is_allowed(self) -> None:
        require_commerce_staff(_ctx("platform_admin"))

    def test_staff_bundle_is_allowed(self) -> None:
        # The shape real staff hold: platform_admin alongside an operations role.
        require_commerce_staff(_ctx("admin", "platform_admin"))

    def test_tenant_admin_is_refused(self) -> None:
        # Deliberate: the ERP holds the authoritative price and invoice, so a
        # customer's own admin gets no second editable copy.
        with pytest.raises(AppException) as exc:
            require_commerce_staff(_ctx("admin"))
        assert exc.value.status_code == 403

    def test_dispatcher_is_refused(self) -> None:
        with pytest.raises(AppException) as exc:
            require_commerce_staff(_ctx("dispatcher"))
        assert exc.value.status_code == 403

    def test_driver_is_refused(self) -> None:
        # The account that could read receivables aging before this gate existed.
        with pytest.raises(AppException) as exc:
            require_commerce_staff(_ctx("driver"))
        assert exc.value.status_code == 403

    def test_no_roles_is_refused(self) -> None:
        with pytest.raises(AppException):
            require_commerce_staff(_ctx())

    def test_rejection_does_not_echo_held_roles(self) -> None:
        # A probing caller must not learn the tenant's role lexicon from a 403.
        with pytest.raises(AppException) as exc:
            require_commerce_staff(_ctx("some-internal-role-name"))
        assert "some-internal-role-name" not in str(exc.value.details)


class TestOpsPolicy:
    """Customers and invoices — capabilities of the delivery pipeline."""

    @pytest.mark.parametrize("role", ["admin", "dispatcher"])
    def test_operations_roles_allowed(self, role: str) -> None:
        require_commerce_ops(_ctx(role))

    def test_driver_is_refused(self) -> None:
        with pytest.raises(AppException) as exc:
            require_commerce_ops(_ctx("driver"))
        assert exc.value.status_code == 403

    def test_platform_admin_alone_is_refused(self) -> None:
        # platform_admin implies nothing, exactly as in require_role. Staff hold
        # an operations role alongside it.
        with pytest.raises(AppException) as exc:
            require_commerce_ops(_ctx("platform_admin"))
        assert exc.value.status_code == 403


class TestRolesAreMatchedExactly:
    """Mirrors the backend's Req 4.2 — never substring matching."""

    @pytest.mark.parametrize(
        "held",
        ["platform_admin_readonly", "not_platform_admin", "platformadmin"],
    )
    def test_substring_neighbours_are_refused(self, held: str) -> None:
        with pytest.raises(AppException):
            require_commerce_staff(_ctx(held))

    @pytest.mark.parametrize("held", ["admin_ops", "lead-dispatcher"])
    def test_ops_substring_neighbours_are_refused(self, held: str) -> None:
        with pytest.raises(AppException):
            require_commerce_ops(_ctx(held))


# ---------------------------------------------------------------------------
# Drift guard — the property that actually protects future routers
# ---------------------------------------------------------------------------

#: Every symbol that constitutes "this module applies a role gate".
GATE_SYMBOLS = (
    "require_commerce_staff",
    "require_commerce_ops",
    "commerce_staff_dependency",
)


class TestNoRouterIsUngated:
    """Discovered from the package, not from a hand-maintained list.

    The original defect was not a wrong role — it was nine routers with *no*
    role check, which nothing detected because nothing was looking. A list of
    modules to check would reproduce that failure the first time someone adds a
    tenth router and forgets to extend the list, so the module set is globbed.
    """

    @staticmethod
    def _endpoint_modules() -> list[Path]:
        package_dir = Path(commerce.api.__file__).parent
        return sorted(package_dir.glob("*_endpoints.py"))

    def test_discovers_the_endpoint_modules(self) -> None:
        # Guards the guard: a glob that silently matches nothing would make the
        # assertion below vacuously true.
        modules = self._endpoint_modules()
        assert len(modules) >= 8, [m.name for m in modules]

    def test_every_endpoint_module_applies_a_gate(self) -> None:
        ungated = [
            module.name
            for module in self._endpoint_modules()
            if not any(
                symbol in module.read_text(encoding="utf-8")
                for symbol in GATE_SYMBOLS
            )
        ]
        assert ungated == [], (
            "commerce endpoint modules with no role gate: "
            f"{ungated}. Add require_commerce_ops / require_commerce_staff, or "
            "attach commerce_staff_dependency to the router."
        )

    def test_the_flag_check_still_precedes_the_role_check(self) -> None:
        # A disabled feature must answer 404, not 403: answering 403 would tell a
        # caller the surface exists and is merely forbidden. Where the gate lives
        # inside a require_*_enabled dependency, it has to come after the raise.
        package_dir = Path(commerce.api.__file__).parent
        for name in ("ar_aging_endpoints.py", "account_endpoints.py",
                     "customer_endpoints.py", "invoice_endpoints.py",
                     "payment_endpoints.py", "price_book_endpoints.py"):
            source = (package_dir / name).read_text(encoding="utf-8")
            # The *call*, not the import — matching the bare symbol would find
            # the import line and pass regardless of where the gate runs.
            calls = [f"{sym}(tenant)" for sym in GATE_SYMBOLS]
            gate_at = min(
                (source.index(call) for call in calls if call in source),
                default=-1,
            )
            assert gate_at != -1, f"{name}: no gate call site found"
            # The 404 the feature-flag guard raises.
            flag_at = source.index("status_code=404")
            assert gate_at > flag_at, (
                f"{name}: the role gate runs before the feature-flag 404, so a "
                "tenant without the module would get 403 and learn it exists."
            )
