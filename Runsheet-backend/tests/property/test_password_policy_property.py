"""
Property-based test for the SuperTokens EmailPassword password-policy validator.

**Validates: Requirements 1.7**

Property 15: Password policy rejects short passwords — the validator built by
``auth.supertokens_init._make_password_validator(min_length)`` rejects a
candidate password on the length rule *if and only if* its length is less than
``min_length``.

The validator returned by ``_make_password_validator`` is the seam wired into
the EmailPassword sign-up form field (Req 1.7). It is an async callable with the
SuperTokens form-field contract ``validate(value, tenant_id)`` that returns an
error message string when the password is unacceptable and ``None`` when it is
acceptable. This test exercises that seam directly across generated
``min_length`` values and generated password strings, with no network calls to
the managed SaaS core.
"""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from auth.supertokens_init import _make_password_validator


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Configurable minimum length. The settings model constrains this to [8, 128]
# (config.settings.password_min_length), but the validator itself accepts any
# positive minimum, so we generate a band that brackets that range and a bit
# beyond to stress the boundary.
_min_lengths = st.integers(min_value=1, max_value=130)

# Candidate password strings of widely varied length — including the empty
# string and lengths on either side of plausible minimums — so both the
# "reject" and "accept" branches are exercised. ``text`` may emit multi-byte
# code points; the validator measures ``len(value)`` (code points), and the
# oracle below uses the same measure, so the iff holds regardless of encoding.
_passwords = st.text(min_size=0, max_size=160)


# ---------------------------------------------------------------------------
# Property 15 — reject on length iff len(password) < min_length
# ---------------------------------------------------------------------------
# Feature: supertokens-auth-migration, Property 15: Password policy rejects short passwords
class TestPasswordPolicyRejectsShortPasswords:
    """**Validates: Requirements 1.7**"""

    @given(min_length=_min_lengths, password=_passwords)
    @settings(max_examples=100)
    def test_rejects_iff_shorter_than_min_length(
        self, min_length: int, password: str
    ):
        """The validator returns an error iff ``len(password) < min_length``."""
        validate = _make_password_validator(min_length)

        result = asyncio.run(validate(password, "public"))

        rejected = result is not None
        should_reject = len(password) < min_length

        assert rejected == should_reject, (
            f"min_length={min_length}, len(password)={len(password)}: "
            f"expected rejected={should_reject}, got rejected={rejected} "
            f"(result={result!r})"
        )
        # When rejected, the contract requires a human-readable string message;
        # when accepted, the contract requires None.
        if rejected:
            assert isinstance(result, str) and result, (
                "rejection must carry a non-empty error message string"
            )
        else:
            assert result is None
