"""
Property-based test for PII-access gating in the Role_Authorizer.

**Validates: Requirements 4.5**

Property 7: PII access is gated on ``has_pii_access`` — a PII operation is
permitted iff ``has_pii_access`` is true.

``auth.authorization.require_pii_access(tenant)`` is the single shared seam that
gates access to personally identifiable information (Req 4.5). It must permit
the operation (return without raising) exactly when the verified
``TenantContext`` carries ``has_pii_access`` truthy, and reject with an HTTP 403
authorization error otherwise — regardless of the context's other fields
(``tenant_id``, ``user_id``, ``roles``, ``region``, ``measurement_units``).

This test exercises that seam directly across a generated ``has_pii_access``
boolean and generated values for every other ``TenantContext`` field, with no
network calls to the managed SaaS core. The other fields are varied to prove
the decision depends *only* on ``has_pii_access``.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from auth.authorization import require_pii_access
from errors.exceptions import AppException
from errors.codes import ErrorCode
from ops.middleware.tenant_guard import TenantContext


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Canonical role names plus a few off-lexicon variants, so the generated role
# set never accidentally constrains the PII decision (which must ignore roles).
_role_names = st.sampled_from(
    ["admin", "dispatcher", "ops_manager", "driver", "viewer", "admin_ops", ""]
)
_roles = st.lists(_role_names, min_size=0, max_size=5)

_tenant_ids = st.text(min_size=0, max_size=40)
_user_ids = st.text(min_size=0, max_size=40)
_regions = st.sampled_from(["US", "CA", "EU", "UK", ""])
_units = st.dictionaries(
    keys=st.sampled_from(["volume", "distance", "temperature"]),
    values=st.sampled_from(["gallons", "liters", "miles", "km", "F", "C"]),
    max_size=3,
)


@st.composite
def _tenant_contexts(draw, has_pii_access: bool) -> TenantContext:
    """Build a TenantContext with the given PII flag and arbitrary other fields."""
    return TenantContext(
        tenant_id=draw(_tenant_ids),
        user_id=draw(_user_ids),
        has_pii_access=has_pii_access,
        roles=draw(_roles),
        region=draw(_regions),
        measurement_units=draw(_units),
    )


# ---------------------------------------------------------------------------
# Property 7 — permit iff has_pii_access is true
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 7: PII access is gated on has_pii_access
class TestPiiAccessGatedOnFlag:
    """**Validates: Requirements 4.5**"""

    @given(
        has_pii_access=st.booleans(),
        data=st.data(),
    )
    @settings(max_examples=100)
    def test_pii_access_permitted_iff_flag_true(self, has_pii_access: bool, data):
        """require_pii_access raises (403) iff has_pii_access is False."""
        tenant = data.draw(_tenant_contexts(has_pii_access))

        if has_pii_access:
            # Must never raise when access is granted.
            assert require_pii_access(tenant) is None
        else:
            # Must reject with a 403 FORBIDDEN authorization error.
            try:
                require_pii_access(tenant)
            except AppException as exc:
                assert exc.status_code == 403, (
                    f"expected 403, got {exc.status_code}"
                )
                assert exc.error_code == ErrorCode.FORBIDDEN
            else:
                raise AssertionError(
                    "require_pii_access must raise when has_pii_access is False"
                )
