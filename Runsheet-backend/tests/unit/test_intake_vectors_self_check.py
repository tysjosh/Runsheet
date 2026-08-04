"""
Example (non-property) tests for the intake-vector startup self-check.

These cover task 3.7 of the dinee-voice-integration spec: the startup
self-check MUST pass on the shipped placeholder fixture and MUST raise
``IntakeVectorError`` (fail-closed) when a vector's recorded ``signature``
is inconsistent with the recomputed HMAC-SHA256 digest.

Validates: Requirements 3.5, 3.6

Unit under test: ``run_intake_vector_self_check`` / ``load_intake_vectors`` /
``IntakeVectorError`` in ``fuel/voice/intake_vectors.py``.
"""

from __future__ import annotations

import base64
import json

import pytest

from fuel.voice.intake_vectors import (
    DEFAULT_VECTORS_PATH,
    IntakeVectorError,
    load_intake_vectors,
    run_intake_vector_self_check,
)
from ops.webhooks.hmac_util import compute_hmac_sha256_hex


# ---------------------------------------------------------------------------
# Passing case — the shipped placeholder fixture is self-consistent.
# ---------------------------------------------------------------------------


class TestSelfCheckPassesOnShippedFixture:
    """Requirement 3.5/3.6: the shipped placeholder fixture verifies cleanly."""

    def test_self_check_passes_on_shipped_placeholder_fixture(self):
        # Uses DEFAULT_VECTORS_PATH (fuel/voice/fixtures/intakeVectors.json).
        verified = run_intake_vector_self_check()

        # At least one vector was verified and the count matches the fixture.
        assert verified >= 1
        assert verified == len(load_intake_vectors())

    def test_shipped_fixture_signatures_match_recomputed_digest(self):
        # Directly recompute each recorded signature to make the self-check's
        # contract explicit (byte-for-byte agreement with the shared HMAC util).
        for entry in load_intake_vectors():
            body_bytes = base64.b64decode(entry["body_base64"], validate=True)
            computed = compute_hmac_sha256_hex(entry["secret"], body_bytes)
            assert computed.lower() == entry["signature"].strip().lower()


# ---------------------------------------------------------------------------
# Failing (fail-closed) case — a deliberately-inconsistent vector raises.
# ---------------------------------------------------------------------------


def _write_fixture(path, vectors):
    path.write_text(json.dumps({"vectors": vectors}), encoding="utf-8")
    return str(path)


class TestSelfCheckFailsClosedOnBadSignature:
    """Requirement 3.5/3.6: any mismatch raises so startup fails closed."""

    def test_self_check_raises_on_wrong_recorded_signature(self, tmp_path):
        # A structurally valid vector whose recorded signature is deliberately
        # wrong (all zeros) — the recomputed digest cannot match it.
        bad_vector = {
            "name": "deliberately-inconsistent",
            "secret": "some-shared-secret",
            "body_base64": base64.b64encode(b'{"callId":"x"}').decode("ascii"),
            "signature": "0" * 64,
        }
        fixture_path = _write_fixture(tmp_path / "intakeVectors.json", [bad_vector])

        with pytest.raises(IntakeVectorError) as exc_info:
            run_intake_vector_self_check(fixture_path)

        # The error names the offending vector and reports a signature mismatch.
        message = str(exc_info.value)
        assert "deliberately-inconsistent" in message
        assert "mismatch" in message

    def test_self_check_raises_when_body_is_tampered(self, tmp_path):
        # Take a genuinely self-consistent vector, then tamper the body so the
        # recorded signature no longer matches the recomputed digest.
        secret = "some-shared-secret"
        original_body = b'{"callId":"original"}'
        signature = compute_hmac_sha256_hex(secret, original_body)

        tampered_vector = {
            "name": "tampered-body",
            "secret": secret,
            "body_base64": base64.b64encode(b'{"callId":"tampered"}').decode("ascii"),
            "signature": signature,  # matches original_body, not the tampered body
        }
        fixture_path = _write_fixture(tmp_path / "intakeVectors.json", [tampered_vector])

        with pytest.raises(IntakeVectorError):
            run_intake_vector_self_check(fixture_path)
