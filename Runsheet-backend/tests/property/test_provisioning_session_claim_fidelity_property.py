"""
Property-based test for User_Provisioner session-claim fidelity (task 2.5).

**Validates: Requirements 9.3, 9.6**

Property 13: Provisioning preserves identity attributes — the session claims a
verified SuperTokens session would carry (``tenant_id``, ``roles``,
``has_pii_access``) equal the source ``auth_users`` row that was provisioned.

Per the design (§Data Models, §User_Provisioner) the access-token payload claims
``tenant_id`` / ``roles`` / ``has_pii_access`` are not stored verbatim by the
provisioner; they are reconstructed at session-creation time from the
SuperTokens identity the provisioner writes:

* ``roles``           ← the UserRoles assigned to the user
* ``tenant_id``       ← the user's ``tenant_id`` metadata
* ``has_pii_access``  ← the user's ``has_pii_access`` metadata

This test provisions a generated row into an in-memory "fake managed core"
(reusing the ``FakeSuperTokensAdmin`` / ``FakeAuthUserStore`` seams from the unit
suite — no SuperTokens SDK or live database), then *reconstructs the session
claims* from what the fake core now holds and asserts that reconstruction equals
the source row. This makes the "session claims equal the source row" guarantee
explicit rather than only checking the raw stored attributes.

A distinct file name is used so this task's test cannot collide with a sibling
provisioning property test authored concurrently.
"""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from auth.provisioner import AuthUserRow, provision_user
from tests.unit.test_provisioner import FakeAuthUserStore, FakeSuperTokensAdmin


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# ``provision_user`` raises on a blank email / tenant_id, so the identity-bearing
# fields are always generated non-empty and non-whitespace.
_non_blank_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zs")),
    min_size=1,
    max_size=24,
).filter(lambda s: s.strip() != "")

# Email is the idempotency key; keep it simple but varied.
_emails = st.builds(
    lambda local, domain: f"{local}@{domain}.example",
    _non_blank_text,
    _non_blank_text,
)

# The four canonical roles plus arbitrary names so the property holds beyond the
# sanctioned set. ``provision_user`` strips + de-duplicates while preserving
# order, so the oracle compares as a set.
_canonical_roles = st.sampled_from(["admin", "dispatcher", "ops_manager", "driver"])
_arbitrary_roles = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zs")),
    min_size=1,
    max_size=16,
).filter(lambda s: s.strip() != "")
_roles = st.lists(st.one_of(_canonical_roles, _arbitrary_roles), max_size=6).map(tuple)

_driver_ids = st.one_of(st.none(), _non_blank_text)


def _expected_role_claim(roles) -> set:
    """Mirror ``provisioner._normalize_roles`` (strip + de-dup) for the oracle."""
    return {r.strip() for r in roles if isinstance(r, str) and r.strip()}


def _reconstruct_session_claims(admin: FakeSuperTokensAdmin, st_user_id: str) -> dict:
    """Rebuild the session claims from what the fake core now holds.

    This mirrors how ``createNewSession`` (design task 4.1) would derive the
    access-token payload from the provisioned SuperTokens identity.
    """
    metadata = admin.metadata[st_user_id]
    return {
        "tenant_id": metadata["tenant_id"],
        "roles": set(admin.roles[st_user_id]),
        "has_pii_access": metadata["has_pii_access"],
    }


# ---------------------------------------------------------------------------
# Property 13 — provisioning preserves identity attributes
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 13: Provisioning preserves identity attributes — resulting session claims (tenant_id, roles, has_pii_access) equal the source row.
class TestProvisioningPreservesSessionClaims:
    """**Validates: Requirements 9.3, 9.6**"""

    @given(
        email=_emails,
        tenant_id=_non_blank_text,
        roles=_roles,
        has_pii_access=st.booleans(),
        driver_id=_driver_ids,
    )
    @settings(max_examples=100)
    def test_reconstructed_session_claims_equal_source_row(
        self,
        email: str,
        tenant_id: str,
        roles,
        has_pii_access: bool,
        driver_id,
    ):
        """The claims a session would carry equal the provisioned source row."""
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

        claims = _reconstruct_session_claims(admin, st_user_id)

        # The session claims (tenant_id, roles, has_pii_access) equal the source
        # auth_users row exactly (Req 9.3, 9.6).
        assert claims == {
            "tenant_id": row.tenant_id,
            "roles": _expected_role_claim(roles),
            "has_pii_access": row.has_pii_access,
        }
