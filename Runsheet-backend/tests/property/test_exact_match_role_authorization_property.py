"""
Property-based test for exact-match role authorization in the Role_Authorizer.

**Validates: Requirements 4.2, 4.3, 4.7**

Property 6: Role authorization uses exact matching — access is granted iff the
required role is an exact member of the caller's held roles; superstrings like
``admin_ops`` never satisfy a requirement for ``admin`` (and substrings never
satisfy a longer requirement either).

``auth.authorization.require_role(tenant, *allowed)`` is the single shared role
gate that replaces the inconsistent per-router checks (Req 4.7) — including the
over-permissive substring-match helper it supersedes. This test drives that
helper directly against a :class:`TenantContext` built with a generated set of
held roles and a generated set of allowed roles, asserting the exact-membership
contract holds across all inputs (no network calls, no managed core).

The oracle is pure set logic: ``require_role`` must raise an HTTP 403
``insufficient_role`` error **iff** ``held ∩ allowed`` is empty, and must never
raise when there is at least one exact common member.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

import pytest

from auth.authorization import require_role
from errors.codes import ErrorCode
from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# A pool of role names chosen so that exact-vs-substring confusions are common:
# ``admin`` plus its superstrings (``admin_ops``, ``admin_ops_manager``), and
# the other canonical roles plus a substring of one (``ops`` ⊂ ``ops_manager``).
# A naive substring matcher would wrongly grant access for several of these
# pairs, so generating from this pool actively probes Req 4.2.
_ROLE_POOL = [
    "admin",
    "admin_ops",
    "admin_ops_manager",
    "dispatcher",
    "ops_manager",
    "ops",
    "driver",
    "driver_lead",
    "superadmin",
    "viewer",
]

_held_roles = st.lists(
    st.sampled_from(_ROLE_POOL), min_size=0, max_size=6, unique=True
)

# ``allowed`` is passed as *args to require_role; at least one required role is
# the realistic call shape (a gate always requires something). Generate 1..4.
_allowed_roles = st.lists(
    st.sampled_from(_ROLE_POOL), min_size=1, max_size=4, unique=True
).map(tuple)


def _make_context(roles: list[str]) -> TenantContext:
    """Build a TenantContext carrying the given held roles."""
    return TenantContext(
        tenant_id="tenant-a",
        user_id="user-1",
        has_pii_access=False,
        roles=list(roles),
    )


# ---------------------------------------------------------------------------
# Property 6 — exact-match role authorization
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 6: Role authorization uses exact matching
class TestExactMatchRoleAuthorization:
    """**Validates: Requirements 4.2, 4.3, 4.7**"""

    @given(held=_held_roles, allowed=_allowed_roles)
    @settings(max_examples=100)
    def test_grants_iff_exact_common_member(
        self, held: list[str], allowed: tuple[str, ...]
    ):
        """``require_role`` raises 403 iff held ∩ allowed (exact) is empty."""
        ctx = _make_context(held)
        has_exact_common = not set(held).isdisjoint(allowed)

        if has_exact_common:
            # An exact common member exists — access must be granted (no raise).
            require_role(ctx, *allowed)
        else:
            # No exact common member — must reject with 403 insufficient_role
            # (Req 4.3), even when a held role is a superstring/substring of a
            # required role (Req 4.2).
            with pytest.raises(AppException) as exc_info:
                require_role(ctx, *allowed)
            assert exc_info.value.error_code is ErrorCode.INSUFFICIENT_ROLE
            assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Property 6 (focused) — superstrings/substrings never satisfy a requirement
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 6: Role authorization uses exact matching
class TestSuperstringNeverSatisfies:
    """**Validates: Requirements 4.2, 4.3**

    A held role that merely *contains* (or is *contained by*) a required role
    name — but is never exactly equal to it — must be rejected. This pins the
    specific ``admin_ops`` vs ``admin`` regression the substring matcher had.
    """

    # Pairs of (held_role, required_role) where held is a strict super/substring
    # of required (so a substring matcher would wrongly grant), but they are not
    # equal, so an exact matcher must reject.
    _AFFIX_PAIRS = [
        ("admin_ops", "admin"),
        ("admin_ops_manager", "admin"),
        ("superadmin", "admin"),
        ("ops", "ops_manager"),
        ("driver_lead", "driver"),
        ("admin", "admin_ops"),  # held is a substring of required
    ]

    @given(pair=st.sampled_from(_AFFIX_PAIRS))
    @settings(max_examples=100)
    def test_affix_match_is_rejected(self, pair: tuple[str, str]):
        """Holding only an affix-related role never satisfies the requirement."""
        held_role, required_role = pair
        # Sanity: these are deliberately not equal but are affix-related.
        assert held_role != required_role
        assert held_role in required_role or required_role in held_role

        ctx = _make_context([held_role])
        with pytest.raises(AppException) as exc_info:
            require_role(ctx, required_role)
        assert exc_info.value.error_code is ErrorCode.INSUFFICIENT_ROLE
        assert exc_info.value.status_code == 403
