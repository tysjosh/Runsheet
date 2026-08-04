"""
Property-based tests for HMAC Signature Verification.

**Validates: Requirements 1.2, 1.3**

Property 1: For any random payload and secret, computing HMAC and passing
            it to the verifier returns True.
Property 2: For any random payload, secret, and different_secret, computing
            HMAC with secret and verifying with different_secret returns False.
Property 3: For any random payload and secret, modifying the payload after
            signing causes verification to fail.

These properties used to be asserted against ``ops.webhooks.receiver.
_verify_signature``, which was a three-line delegate to
:func:`ops.webhooks.hmac_util.verify_hmac_sha256_hex`. The receiver was removed
with the ``POST /webhooks/dinee`` route, so the properties now bind directly to
the shared implementation — the one the order-intake pipeline and the Dinee
voice bridge both call. That is a strictly better target: it is the code that
actually guards inbound webhooks, rather than a wrapper around it.
"""

import hashlib
import hmac as hmac_mod
import sys
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings, assume
from hypothesis.strategies import binary, text

# ---------------------------------------------------------------------------
# Mock the elasticsearch_service module before importing ops modules
# ---------------------------------------------------------------------------
sys.modules.setdefault("services.elasticsearch_service", MagicMock())

from ops.webhooks.hmac_util import verify_hmac_sha256_hex  # noqa: E402


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Adapt the shared verifier to the argument order these properties use.

    ``verify_hmac_sha256_hex`` takes ``(secret, body, signature)``; the removed
    receiver helper took ``(body, signature, secret)``. Kept as a local shim so
    the property bodies below are unchanged and remain diffable against the
    versions that ran before the route was deleted.
    """
    return verify_hmac_sha256_hex(secret, body, signature)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Secrets: non-empty text (HMAC key must have at least 1 character)
_secrets = text(min_size=1, max_size=256)
# Payloads: arbitrary bytes
_payloads = binary(min_size=0, max_size=4096)


def _compute_hmac(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 hex digest independently of the code under test."""
    return hmac_mod.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# Property 1 – correct HMAC is always accepted
# ---------------------------------------------------------------------------
class TestHMACAcceptance:
    """**Validates: Requirements 1.2, 1.3**"""

    @given(payload=_payloads, secret=_secrets)
    @settings(max_examples=200)
    def test_valid_hmac_always_accepted(self, payload: bytes, secret: str):
        """For any payload and secret, a correctly computed HMAC must verify."""
        signature = _compute_hmac(payload, secret)
        assert _verify_signature(payload, signature, secret) is True


# ---------------------------------------------------------------------------
# Property 2 – wrong secret is always rejected
# ---------------------------------------------------------------------------
class TestHMACWrongSecretRejection:
    """**Validates: Requirements 1.2, 1.3**"""

    @given(payload=_payloads, secret=_secrets, wrong_secret=_secrets)
    @settings(max_examples=200)
    def test_wrong_secret_always_rejected(
        self, payload: bytes, secret: str, wrong_secret: str
    ):
        """HMAC computed with one secret must not verify with a different secret."""
        assume(secret != wrong_secret)
        signature = _compute_hmac(payload, secret)
        assert _verify_signature(payload, signature, wrong_secret) is False


# ---------------------------------------------------------------------------
# Property 3 – tampered payload is always rejected
# ---------------------------------------------------------------------------
class TestHMACTamperedPayloadRejection:
    """**Validates: Requirements 1.2, 1.3**"""

    @given(payload=_payloads, secret=_secrets)
    @settings(max_examples=200)
    def test_tampered_payload_always_rejected(self, payload: bytes, secret: str):
        """Modifying the payload after signing must cause verification to fail."""
        signature = _compute_hmac(payload, secret)
        # Tamper: append a byte that changes the payload
        tampered = payload + b"\x00" if not payload.endswith(b"\x00") else payload + b"\x01"
        assume(tampered != payload)
        assert _verify_signature(tampered, signature, secret) is False
