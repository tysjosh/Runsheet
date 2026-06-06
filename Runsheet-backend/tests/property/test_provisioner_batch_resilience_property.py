"""
Property-based test for User_Provisioner batch resilience.

**Validates: Requirements 9.7**

Property 14: Batch provisioning is resilient to per-user failure — for any set
of source rows with an arbitrary subset designated to fail, ``provision_all``
provisions every non-failing row and records each failing row individually,
without one bad row aborting the batch.

This exercises ``auth.provisioner.provision_all`` through the injectable seams
(:class:`SuperTokensAdmin` / :class:`AuthUserStore`) using the same in-memory
fakes pattern as ``tests/unit/test_provisioner.py`` — a "fake managed core"
whose ``create_user`` raises for emails in ``fail_emails`` (Req 9.7) and a fake
source store that records ``provisioned`` / ``errors``. No SuperTokens SDK or
live database is involved.
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from auth.provisioner import AuthUserRow, ProvisionStatus, provision_all


# ---------------------------------------------------------------------------
# In-memory fakes (mirrors tests/unit/test_provisioner.py)
# ---------------------------------------------------------------------------


class FakeSuperTokensAdmin:
    """In-memory stand-in for the SuperTokens managed core.

    ``create_user`` raises for any email in ``fail_emails`` so the resilience
    path (Req 9.7) is exercised; everything else is recorded in memory.
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


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Distinct emails are the idempotency key, so we generate a set of unique local
# parts and build well-formed addresses from them. A set of size N gives N rows.
_emails = st.lists(
    st.text(
        alphabet=st.characters(min_codepoint=97, max_codepoint=122),  # a-z
        min_size=1,
        max_size=8,
    ),
    min_size=0,
    max_size=12,
    unique=True,
).map(lambda locals_: [f"{lp}@runsheet.com" for lp in locals_])

_roles = st.lists(
    st.sampled_from(["admin", "dispatcher", "ops_manager", "driver"]),
    min_size=0,
    max_size=4,
    unique=True,
).map(tuple)


# ---------------------------------------------------------------------------
# Property 14 — batch resilient to per-user failure
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 14: Batch provisioning is resilient to per-user failure
class TestBatchProvisioningResilience:
    """**Validates: Requirements 9.7**"""

    @given(
        data=st.data(),
        emails=_emails,
        roles=_roles,
        has_pii=st.booleans(),
    )
    @settings(max_examples=100)
    def test_non_failing_provisioned_and_each_failure_recorded(
        self, data, emails, roles, has_pii
    ):
        """All non-failing rows provision; each failing row is recorded.

        For a generated set of rows and a generated subset designated to fail:

        * every row appears exactly once in the report,
        * every non-failing row is provisioned (recorded in ``store.provisioned``)
          and lands in ``report.succeeded``,
        * every failing row is recorded individually (in ``store.errors``) and
          lands in ``report.failed``,
        * the report's succeeded/failed partition matches the designated split.
        """
        # Choose an arbitrary subset of the emails to fail.
        fail_emails: set[str] = set(
            data.draw(
                st.lists(st.sampled_from(emails), unique=True).map(set)
            )
            if emails
            else set()
        )

        rows = [
            AuthUserRow(
                email=email,
                tenant_id="demo-tenant",
                roles=roles,
                has_pii_access=has_pii,
            )
            for email in emails
        ]

        admin = FakeSuperTokensAdmin(fail_emails=fail_emails)
        store = FakeAuthUserStore()

        report = asyncio.run(provision_all(rows, admin=admin, store=store))

        expected_succeeded = {e for e in emails if e not in fail_emails}
        expected_failed = {e for e in emails if e in fail_emails}

        # Every row is accounted for exactly once; no row aborts the batch.
        assert len(report.results) == len(emails)
        assert {r.email for r in report.results} == set(emails)

        # Partition matches the designated success/failure split.
        assert {r.email for r in report.succeeded} == expected_succeeded
        assert {r.email for r in report.failed} == expected_failed

        # Every non-failing row was actually provisioned into the core/store.
        assert set(store.provisioned.keys()) == expected_succeeded
        for r in report.succeeded:
            assert r.status in (ProvisionStatus.CREATED, ProvisionStatus.UPDATED)
            assert r.ok
            assert r.st_user_id is not None

        # Every failing row is recorded individually with an error.
        assert set(store.errors.keys()) == expected_failed
        for r in report.failed:
            assert r.status is ProvisionStatus.FAILED
            assert not r.ok
            assert r.error is not None
            assert "simulated core failure" in store.errors[r.email]

        # all_succeeded is true iff nothing was designated to fail.
        assert report.all_succeeded == (len(expected_failed) == 0)
