"""The driver app-access grant refuses a customer identity (design §1.6; PRV-3)."""
from __future__ import annotations

import copy
import logging

import pytest

from tests.unit.test_driver_app_access_endpoints import (
    FakeAuthUsersDB,
    FakeDriverRepository,
    FakeSuperTokensAdmin,
    _build_client,
    _make_driver,
)


@pytest.mark.parametrize(
    "roles,customer_id",
    [(["customer"], "QA-CUST-1"), (["customer"], None), ([], "QA-CUST-1")],
)
def test_grant_on_customer_email_409_no_supertokens_write(roles, customer_id, caplog):
    repo = FakeDriverRepository()
    repo.seed(_make_driver())
    db = FakeAuthUsersDB()
    db.seed(
        email="buyer@example.com",
        tenant_id="tenant-A",
        roles=roles,
        has_pii_access=False,
        driver_id=None,
        st_user_id="st-buyer",
        customer_id=customer_id,
    )
    admin = FakeSuperTokensAdmin()
    admin.seed_user("buyer@example.com", "st-buyer", roles)
    before = copy.deepcopy(db.rows)
    client, _, db, admin, _ = _build_client(repo=repo, db=db, admin=admin)

    caplog.set_level(logging.INFO, logger="fuel.api.driver_endpoints")
    resp = client.post(
        "/api/ops/drivers/drv-001/app-access", json={"email": "buyer@example.com"}
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "APP_ACCESS_ALREADY_LINKED"
    assert admin.create_calls == []
    assert admin.roles["st-buyer"] == roles
    assert "st-buyer" not in admin.metadata
    assert db.rows == before
    outcomes = [
        getattr(r, "extra_data", {}).get("outcome")
        for r in caplog.records
        if getattr(r, "extra_data", {}).get("audit_event")
    ]
    assert outcomes == ["rejected:customer_identity"]
