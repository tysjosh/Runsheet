"""
Property-based test for User_Provisioner mapping fidelity.

**Validates: Requirements 9.3, 9.6**

Property 13: Provisioning preserves identity attributes — the resulting
SuperTokens identity attributes (``roles`` plus the ``tenant_id`` /
``has_pii_access`` / ``driver_id`` metadata that back the session claims) equal
the source ``auth_users`` row.

This exercises ``auth.provisioner.provision_user`` through its injectable seams
using the in-memory ``FakeSuperTokensAdmin`` / ``FakeAuthUserStore`` fakes (the
"fake managed core" + source store) reused from the unit suite, so no
SuperTokens SDK or live database is required. For any generated
:class:`AuthUserRow`, after provisioning:

* the fake admin's stored role set for the user equals the row's (de-duplicated)
  role set, and
* the fake admin's stored metadata equals ``{tenant_id, has_pii_access,
  driver_id}`` taken from the row.
"""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from auth.provisioner import AuthUserRow, provision_user
from tests.unit.test_provisioner import FakeAuthUserStore, FakeSuperTokensAdmin


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Non-empty, non-whitespace tokens for the identity-bearing fields. The
# provisioner requires a non-empty email and tenant_id (it raises otherwise), so
# the generators below always produce a usable value.
_non_blank_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zs")),
    min_size=1,
    max_size=24,
).filter(lambda s: s.strip() != "")

# Emails are the idempotency key; keep them simple but varied.
_emails = st.builds(
    lambda local, domain: f"{local}@{domain}.example",
    _non_blank_text,
    _non_blank_text,
)

# The four canonical roles plus some arbitrary role names, so the property holds
# beyond just the sanctioned set. ``provision_user`` de-duplicates while
# preserving order, so the oracle compares as a set.
_canonical_roles = st.sampled_from(["admin", "dispatcher", "ops_manager", "driver"])
_arbitrary_roles = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zs")),
    min_size=1,
    max_size=16,
).filter(lambda s: s.strip() != "")
_roles = st.lists(st.one_of(_canonical_roles, _arbitrary_roles), max_size=6).map(tuple)

_driver_ids = st.one_of(st.none(), _non_blank_text)


def _normalized_role_set(roles) -> set:
    """Mirror provisioner._normalize_roles for the oracle (strip + de-dup)."""
    return {r.strip() for r in roles if isinstance(r, str) and r.strip()}


# ---------------------------------------------------------------------------
# Property 13 — provisioning preserves identity attributes
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 13: Provisioning preserves identity attributes
class TestProvisioningPreservesIdentityAttributes:
    """**Validates: Requirements 9.3, 9.6**"""

    @given(
        email=_emails,
        tenant_id=_non_blank_text,
        roles=_roles,
        has_pii_access=st.booleans(),
        driver_id=_driver_ids,
    )
    @settings(max_examples=100)
    def test_resulting_attributes_equal_source_row(
        self,
        email: str,
        tenant_id: str,
        roles,
        has_pii_access: bool,
        driver_id,
    ):
        """Stored roles + metadata equal the source row after provisioning."""
        row = AuthUserRow(
            email=email,
            tenant_id=tenant_id,
            roles=roles,
            has_pii_access=has_pii_access,
            driver_id=driver_id,
        )

        admin = FakeSuperTokensAdmin()
        store = FakeAuthUserStore()

        result = asyncio.run(provision_user(row, admin=admin, store=store))
        st_user_id = result.st_user_id
        assert st_user_id is not None

        # Roles assigned to the SuperTokens user equal the row's role set
        # (Req 9.3, 9.6) — exactly, after normalization.
        assert admin.roles[st_user_id] == _normalized_role_set(roles)

        # Metadata backing the tenant_id / roles / has_pii_access session claims
        # equals the source row (Req 9.3, 9.6).
        assert admin.metadata[st_user_id] == {
            "tenant_id": tenant_id,
            "has_pii_access": has_pii_access,
            "driver_id": driver_id,
        }
