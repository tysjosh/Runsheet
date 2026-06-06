"""
Property-based test for idempotent User_Provisioner behaviour.

**Validates: Requirements 9.4**

Property 12: Provisioning is idempotent — provisioning a source ``auth_users``
row once or N times yields exactly one SuperTokens user.

``auth.provisioner.provision_user`` looks the SuperTokens user up by email
before creating one, so re-running it for the same source row must reconcile the
existing user rather than create a duplicate. This test drives that logic
through the in-memory ``FakeSuperTokensAdmin`` / ``FakeAuthUserStore`` seams (the
"fake managed core" pattern from ``tests/unit/test_provisioner.py``), so there
are no network calls to the managed SaaS core and no live database.

For a generated :class:`AuthUserRow` and a generated ``N >= 1``, provisioning the
row ``N`` times must:
  * create exactly one SuperTokens user (``admin.create_calls`` has length 1),
  * leave exactly one user registered for that email, and
  * return the same ``st_user_id`` on every run (only the first is ``CREATED``).
"""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from auth.provisioner import (
    AuthUserRow,
    ProvisionStatus,
    provision_user,
)

# Reuse the in-memory fakes that back the provisioner unit tests so this
# property exercises the same "fake managed core" seam.
from tests.unit.test_provisioner import FakeAuthUserStore, FakeSuperTokensAdmin


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# A non-empty email-like idempotency key. Exact format is irrelevant to the
# provisioner (it keys verbatim on the trimmed string); we only need a stable,
# non-blank value, so we build "<local>@<domain>" from simple tokens.
_tokens = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122),
    min_size=1,
    max_size=12,
)
_emails = st.builds(lambda local, domain: f"{local}@{domain}.com", _tokens, _tokens)

# Canonical role names (the only ones the source ever carries); any subset,
# including the empty set, is valid.
_role_sets = st.lists(
    st.sampled_from(["admin", "dispatcher", "ops_manager", "driver"]),
    min_size=0,
    max_size=4,
    unique=True,
).map(tuple)

_tenant_ids = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122),
    min_size=1,
    max_size=16,
)


@st.composite
def _auth_user_rows(draw) -> AuthUserRow:
    """Generate a valid (non-blank email + tenant_id) source row."""
    roles = draw(_role_sets)
    driver_id = "drv_1" if "driver" in roles else None
    return AuthUserRow(
        email=draw(_emails),
        tenant_id=draw(_tenant_ids),
        roles=roles,
        has_pii_access=draw(st.booleans()),
        driver_id=driver_id,
    )


# Number of times to provision the same row — at least once.
_repeat_counts = st.integers(min_value=1, max_value=8)


# ---------------------------------------------------------------------------
# Property 12 — provisioning N times yields exactly one SuperTokens user
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 12: Provisioning is idempotent
class TestProvisioningIsIdempotent:
    """**Validates: Requirements 9.4**"""

    @given(row=_auth_user_rows(), n=_repeat_counts)
    @settings(max_examples=100)
    def test_provision_n_times_creates_exactly_one_user(
        self, row: AuthUserRow, n: int
    ):
        """Provisioning the same row ``n`` times never duplicates the user."""

        async def _run() -> None:
            admin = FakeSuperTokensAdmin()
            store = FakeAuthUserStore()

            results = [
                await provision_user(row, admin=admin, store=store)
                for _ in range(n)
            ]

            email = row.email.strip()

            # Exactly one user was created across all n runs (Req 9.4).
            assert admin.create_calls == [email], (
                f"expected a single create for {email!r}, "
                f"got {admin.create_calls!r}"
            )
            # Exactly one user is registered for that email.
            assert len(admin.users) == 1
            assert email in admin.users

            st_user_id = admin.users[email]
            # Every run resolved to that same SuperTokens user id.
            assert all(r.st_user_id == st_user_id for r in results)

            # Only the first run created; the rest reconciled the existing user.
            assert results[0].status is ProvisionStatus.CREATED
            assert all(
                r.status is ProvisionStatus.UPDATED for r in results[1:]
            )

        asyncio.run(_run())
