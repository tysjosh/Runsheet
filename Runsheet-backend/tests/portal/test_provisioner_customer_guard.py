"""Provisioner guard for the exclusive customer role (design §1.5)."""
from __future__ import annotations

import pytest

from auth.provisioner import AuthUserRow, provision_user


class _Admin:
    def __init__(self) -> None:
        self.calls = []
        self.metadata = {}

    async def get_user_id_by_email(self, email):
        self.calls.append("get_user_id_by_email")
        return None

    async def create_user(self, email):
        self.calls.append("create_user")
        return "st-new"

    async def set_user_roles(self, user_id, roles):
        self.calls.append("set_user_roles")

    async def set_user_metadata(self, user_id, metadata):
        self.calls.append("set_user_metadata")
        self.metadata[user_id] = dict(metadata)


class _Store:
    async def mark_provisioned(self, *, email, st_user_id):
        pass

    async def mark_failed(self, *, email, error):
        pass


def _row(**kw):
    base = {"email": "buyer@example.com", "tenant_id": "demo-tenant"}
    base.update(kw)
    return AuthUserRow(**base)


@pytest.mark.parametrize(
    "row",
    [
        _row(roles=("customer",)),  # no binding
        _row(roles=("customer", "admin"), customer_id="C1"),
        _row(roles=("customer",), customer_id="C1", driver_id="D1"),
        _row(roles=("customer",), customer_id="C1", has_pii_access=True),
        _row(roles=("dispatcher",), customer_id="C1"),
        _row(roles=(), customer_id="C1"),
        _row(roles=("customer",), customer_id="  "),
    ],
)
async def test_invalid_binding_refused_before_any_write(row):
    admin = _Admin()
    with pytest.raises(ValueError, match="invalid_customer_binding"):
        await provision_user(row, admin=admin, store=_Store())
    assert admin.calls == []


async def test_valid_customer_row_provisions_with_customer_id_metadata():
    admin = _Admin()
    result = await provision_user(
        _row(roles=("customer",), customer_id="C1"), admin=admin, store=_Store()
    )
    assert result.ok
    assert admin.metadata["st-new"]["customer_id"] == "C1"


async def test_staff_row_metadata_unchanged():
    admin = _Admin()
    await provision_user(_row(roles=("admin",)), admin=admin, store=_Store())
    assert admin.metadata["st-new"] == {
        "tenant_id": "demo-tenant",
        "has_pii_access": False,
        "driver_id": None,
    }
