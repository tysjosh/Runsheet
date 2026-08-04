"""
Property-based tests for the shared voice-intake HMAC helper and the vendored
cross-language intake test vectors.

# Feature: dinee-voice-integration, Property 2: Canonicalization and vector
# agreement (sign-verify round trip)

**Validates: Requirements 2.2, 2.5, 3.1, 3.3, 3.4**

Property 2 asserts that the shared HMAC helper in ``ops/webhooks/hmac_util.py``:
    * round-trips: ``verify_hmac_sha256_hex(secret, body, compute_hmac_sha256_hex(secret, body))`` is True (Req 3.1, 3.3);
    * tolerates the ``sha256=`` prefix and arbitrary letter case on the presented
      digest, matching the fixed Dinee ``X-Signature: sha256=<hex>`` contract (Req 2.2);
    * rejects any mutation of a single body byte, the secret, or the digest (Req 2.5, 3.1);
and that every entry in ``fuel/voice/fixtures/intakeVectors.json`` computes to its
recorded ``signature`` (Req 3.4) — the byte-for-byte cross-language agreement guarantee.
"""

import base64

import pytest
from hypothesis import given, settings, assume
from hypothesis.strategies import binary, text

from ops.webhooks.hmac_util import compute_hmac_sha256_hex, verify_hmac_sha256_hex
from fuel.voice.intake_vectors import load_intake_vectors


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Secrets: non-empty text (an HMAC key must have at least one byte of material).
_secrets = text(min_size=1, max_size=256)
# Bodies: arbitrary raw bytes, including the empty body.
_bodies = binary(min_size=0, max_size=4096)


def _flip_case(hex_digest: str) -> str:
    """Return the digest with each letter's case swapped (a>=A, 0..9 unchanged)."""
    return hex_digest.swapcase()


# ---------------------------------------------------------------------------
# Property 2a - sign -> verify round trip always succeeds
# ---------------------------------------------------------------------------
class TestHmacRoundTrip:
    """# Feature: dinee-voice-integration, Property 2 (round trip)

    **Validates: Requirements 3.1, 3.3**
    """

    @given(secret=_secrets, body=_bodies)
    @settings(max_examples=100)
    def test_compute_then_verify_succeeds(self, secret: str, body: bytes):
        digest = compute_hmac_sha256_hex(secret, body)
        assert verify_hmac_sha256_hex(secret, body, digest) is True


# ---------------------------------------------------------------------------
# Property 2b - prefix / case tolerance (Dinee "sha256=<hex>" contract)
# ---------------------------------------------------------------------------
class TestHmacPrefixAndCaseTolerance:
    """# Feature: dinee-voice-integration, Property 2 (prefix/case tolerance)

    **Validates: Requirements 2.2**
    """

    @given(secret=_secrets, body=_bodies)
    @settings(max_examples=100)
    def test_verify_tolerates_prefix_and_case(self, secret: str, body: bytes):
        digest = compute_hmac_sha256_hex(secret, body)
        variants = [
            digest,
            f"sha256={digest}",
            digest.upper(),
            f"sha256={digest.upper()}",
            _flip_case(digest),
            f"SHA256={digest}",  # prefix match is case-insensitive
        ]
        for presented in variants:
            assert verify_hmac_sha256_hex(secret, body, presented) is True


# ---------------------------------------------------------------------------
# Property 2c - any mutation of body / secret / digest is rejected
# ---------------------------------------------------------------------------
class TestHmacMutationRejection:
    """# Feature: dinee-voice-integration, Property 2 (mutation rejection)

    **Validates: Requirements 2.5, 3.1**
    """

    @given(secret=_secrets, body=_bodies, index=binary(min_size=1, max_size=1))
    @settings(max_examples=100)
    def test_mutated_body_byte_rejected(self, secret: str, body: bytes, index: bytes):
        assume(len(body) > 0)
        digest = compute_hmac_sha256_hex(secret, body)
        pos = index[0] % len(body)
        mutated = bytearray(body)
        mutated[pos] ^= 0x01  # flip a single bit in one byte
        assert bytes(mutated) != body
        assert verify_hmac_sha256_hex(secret, bytes(mutated), digest) is False

    @given(secret=_secrets, other_secret=_secrets, body=_bodies)
    @settings(max_examples=100)
    def test_mutated_secret_rejected(self, secret: str, other_secret: str, body: bytes):
        assume(secret != other_secret)
        digest = compute_hmac_sha256_hex(secret, body)
        assert verify_hmac_sha256_hex(other_secret, body, digest) is False

    @given(secret=_secrets, body=_bodies)
    @settings(max_examples=100)
    def test_mutated_digest_rejected(self, secret: str, body: bytes):
        digest = compute_hmac_sha256_hex(secret, body)
        # Flip the first hex nibble to a definitely-different value.
        first = digest[0]
        replacement = "0" if first != "0" else "1"
        mutated = replacement + digest[1:]
        assume(mutated != digest)
        assert verify_hmac_sha256_hex(secret, body, mutated) is False


# ---------------------------------------------------------------------------
# Property 2d - every vendored vector agrees byte-for-byte
# ---------------------------------------------------------------------------
class TestVectorAgreement:
    """# Feature: dinee-voice-integration, Property 2 (vector agreement)

    **Validates: Requirements 3.4**
    """

    def test_every_vector_computes_to_recorded_signature(self):
        vectors = load_intake_vectors()
        assert vectors, "expected at least one intake vector"
        for entry in vectors:
            name = entry.get("name", "?")
            secret = entry["secret"]
            body = base64.b64decode(entry["body_base64"], validate=True)
            recorded = entry["signature"]
            computed = compute_hmac_sha256_hex(secret, body)
            assert computed.lower() == recorded.strip().lower(), (
                f"vector '{name}' mismatch: computed {computed} != recorded {recorded}"
            )
            # The verify path must also accept the recorded digest (prefix/case tolerant).
            assert verify_hmac_sha256_hex(secret, body, recorded) is True
            assert verify_hmac_sha256_hex(secret, body, f"sha256={recorded}") is True
