"""
Unit tests for the User_Provisioner (``auth/provisioner.py``).

These exercise the provisioning logic through the injectable seams
(:class:`SuperTokensAdmin` / :class:`AuthUserStore`) with in-memory fakes — a
"fake managed core" and a fake source store — so no SuperTokens SDK or live
database is required.

Covers:
- ``provision_user`` creates a new SuperTokens user when none exists, assigns
  the row's roles, writes tenant_id/has_pii_access/driver_id metadata, and
  backfills st_user_id (Req 9.3, 9.6).
- ``provision_user`` is idempotent: a second run for the same email reuses the
  existing user instead of creating a duplicate (Req 9.4).
- ``provision_user`` validates that email/tenant_id are present.
- ``provision_all`` provisions every row and continues past a per-row failure,
  recording the error individually (Req 9.7).

Validates: Requirements 9.3, 9.4, 9.6, 9.7
"""

from __future__ import annotations

import pytest

from auth.provisioner import (
    AuthUserRow,
    ProvisionStatus,
    provision_all,
    provision_user,
)


# ---------------------------------------------------------------------------
# In-memory fakes (the "fake managed core" + source store)
# ---------------------------------------------------------------------------


class FakeSuperTokensAdmin:
    """In-memory stand-in for the SuperTokens managed core.

    Models users keyed by email -> user_id, plus per-user roles and metadata,
    so tests can assert the provisioner's effects without the SDK. An optional
    ``fail_emails`` set makes ``create_user`` raise to exercise the resilience
    path (Req 9.7).
    """

    def __init__(self, fail_emails: set[str] | None = None) -> None:
        self.users: dict[str, str] = {}  # email -> user_id
        self.roles: dict[str, set[str]] = {}  # user_id -> roles
        self.metadata: dict[str, dict] = {}  # user_id -> metadata
        self.create_calls: list[str] = []
        self._fail_emails = fail_emails or set()
        self._next_id = 0

    async def get_user_id_by_email(self, email: str):
        return self.users.get(email)

    async def create_user(self, email: str) -> str:
        if email in self._fail_emails:
            raise RuntimeError(f"simulated core failure for {email}")
        self.create_calls.append(email)
        self._next_id += 1
        user_id = f"st-user-{self._next_id}"
        self.users[email] = user_id
        self.roles[user_id] = set()
        self.metadata[user_id] = {}
        return user_id

    async def set_user_roles(self, user_id: str, roles) -> None:
        self.roles[user_id] = set(roles)

    async def set_user_metadata(self, user_id: str, metadata) -> None:
        self.metadata[user_id] = dict(metadata)


class FakeAuthUserStore:
    """In-memory stand-in for the ``auth_users`` write-back store."""

    def __init__(self) -> None:
        self.provisioned: dict[str, str] = {}  # email -> st_user_id
        self.errors: dict[str, str] = {}  # email -> provision_error

    async def mark_provisioned(self, *, email: str, st_user_id: str) -> None:
        self.provisioned[email] = st_user_id
        self.errors.pop(email, None)

    async def mark_failed(self, *, email: str, error: str) -> None:
        self.errors[email] = error


def _row(email="admin@runsheet.com", **kwargs) -> AuthUserRow:
    defaults = dict(
        tenant_id="demo-tenant",
        roles=("admin", "ops_manager"),
        has_pii_access=True,
    )
    defaults.update(kwargs)
    return AuthUserRow(email=email, **defaults)


# ---------------------------------------------------------------------------
# provision_user — create path (Req 9.3, 9.6)
# ---------------------------------------------------------------------------


async def test_provision_user_creates_user_with_roles_and_metadata():
    admin = FakeSuperTokensAdmin()
    store = FakeAuthUserStore()

    result = await provision_user(
        _row(driver_id=None), admin=admin, store=store
    )

    assert result.status is ProvisionStatus.CREATED
    assert result.ok
    st_user_id = result.st_user_id
    assert st_user_id is not None

    # Roles reflect the source row exactly (Req 9.3, 9.6).
    assert admin.roles[st_user_id] == {"admin", "ops_manager"}
    # Metadata carries tenant_id / has_pii_access / driver_id (Req 9.6).
    assert admin.metadata[st_user_id] == {
        "tenant_id": "demo-tenant",
        "has_pii_access": True,
        "driver_id": None,
    }
    # st_user_id is backfilled onto the source row.
    assert store.provisioned["admin@runsheet.com"] == st_user_id


async def test_provision_user_sets_driver_id_for_driver_users():
    admin = FakeSuperTokensAdmin()
    store = FakeAuthUserStore()

    result = await provision_user(
        _row(email="driver@runsheet.com", roles=("driver",), driver_id="drv_123"),
        admin=admin,
        store=store,
    )

    assert admin.metadata[result.st_user_id]["driver_id"] == "drv_123"
    assert admin.roles[result.st_user_id] == {"driver"}


# ---------------------------------------------------------------------------
# provision_user — idempotency (Req 9.4)
# ---------------------------------------------------------------------------


async def test_provision_user_is_idempotent_no_duplicate_created():
    admin = FakeSuperTokensAdmin()
    store = FakeAuthUserStore()
    row = _row()

    first = await provision_user(row, admin=admin, store=store)
    second = await provision_user(row, admin=admin, store=store)

    # Exactly one user created across two runs (Req 9.4).
    assert admin.create_calls == ["admin@runsheet.com"]
    assert first.status is ProvisionStatus.CREATED
    assert second.status is ProvisionStatus.UPDATED
    assert first.st_user_id == second.st_user_id
    assert len(admin.users) == 1


async def test_provision_user_reconciles_changed_roles_on_rerun():
    admin = FakeSuperTokensAdmin()
    store = FakeAuthUserStore()

    await provision_user(_row(roles=("admin", "ops_manager")), admin=admin, store=store)
    # Re-provision the same email with a reduced role set.
    result = await provision_user(_row(roles=("dispatcher",)), admin=admin, store=store)

    # Roles are made to exactly match the new source row (added + removed).
    assert admin.roles[result.st_user_id] == {"dispatcher"}


# ---------------------------------------------------------------------------
# provision_user — validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_email", ["", "   "])
async def test_provision_user_rejects_missing_email(bad_email):
    admin = FakeSuperTokensAdmin()
    store = FakeAuthUserStore()
    with pytest.raises(ValueError):
        await provision_user(
            AuthUserRow(email=bad_email, tenant_id="t"), admin=admin, store=store
        )


async def test_provision_user_rejects_missing_tenant_id():
    admin = FakeSuperTokensAdmin()
    store = FakeAuthUserStore()
    with pytest.raises(ValueError):
        await provision_user(
            AuthUserRow(email="x@y.com", tenant_id="  "), admin=admin, store=store
        )


# ---------------------------------------------------------------------------
# provision_all — batch resilience (Req 9.7)
# ---------------------------------------------------------------------------


async def test_provision_all_continues_past_per_row_failure():
    admin = FakeSuperTokensAdmin(fail_emails={"bad@runsheet.com"})
    store = FakeAuthUserStore()

    rows = [
        _row(email="a@runsheet.com"),
        _row(email="bad@runsheet.com"),
        _row(email="c@runsheet.com"),
    ]

    report = await provision_all(rows, admin=admin, store=store)

    # All three rows are accounted for; the bad one did not abort the batch.
    assert len(report.results) == 3
    assert {r.email for r in report.succeeded} == {
        "a@runsheet.com",
        "c@runsheet.com",
    }
    assert [r.email for r in report.failed] == ["bad@runsheet.com"]
    assert not report.all_succeeded

    # The failure is individually recorded on the source store (Req 9.7).
    assert "bad@runsheet.com" in store.errors
    assert "simulated core failure" in store.errors["bad@runsheet.com"]
    # The good rows still provisioned successfully.
    assert store.provisioned.keys() == {"a@runsheet.com", "c@runsheet.com"}


async def test_provision_all_empty_batch_reports_success():
    admin = FakeSuperTokensAdmin()
    store = FakeAuthUserStore()
    report = await provision_all([], admin=admin, store=store)
    assert report.results == []
    assert report.all_succeeded


async def test_provision_report_summary_counts():
    admin = FakeSuperTokensAdmin(fail_emails={"bad@runsheet.com"})
    store = FakeAuthUserStore()
    rows = [
        _row(email="a@runsheet.com"),
        _row(email="bad@runsheet.com"),
    ]
    report = await provision_all(rows, admin=admin, store=store)
    summary = report.summary()
    assert "1 created" in summary
    assert "1 failed" in summary
