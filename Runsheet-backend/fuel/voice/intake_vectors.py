"""
Intake vectors loader + startup self-check (Canonicalization determinism).

This module loads the vendored cross-language HMAC/canonicalization test
vectors and verifies, at startup, that the Runsheet HMAC-SHA256 computation
agrees byte-for-byte with each recorded vector. Any mismatch raises so the
voice submission endpoint is *not* served (fail-closed).

Vector shape (each entry):
    {
        "name": <str>,           # human-readable identifier
        "secret": <str>,         # shared HMAC secret
        "body_base64": <str>,    # base64 of the exact canonical body bytes
        "signature": <str>       # lowercase-hex HMAC-SHA256 of body under secret
    }

The vendored fixture ``fuel/voice/fixtures/intakeVectors.json`` is a PLACEHOLDER
pending the real Dinee ``__tests__/fixtures/intakeVectors.json`` (assumption A3).
It ships with at least one self-consistent vector so the self-check passes.

See design.md section "A6. Canonicalization determinism + startup self-check".

Requirements: 3.3, 3.4, 3.5, 3.6
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any, Dict, List

from ops.webhooks.hmac_util import compute_hmac_sha256_hex

__all__ = [
    "IntakeVectorError",
    "DEFAULT_VECTORS_PATH",
    "load_intake_vectors",
    "run_intake_vector_self_check",
]

# Absolute path to the vendored placeholder fixture that ships with the backend.
DEFAULT_VECTORS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "intakeVectors.json"
)

# Metadata keys carried in the fixture that are not themselves vectors.
_META_PREFIX = "_"


class IntakeVectorError(RuntimeError):
    """Raised when the intake-vector self-check fails (fail-closed at startup)."""


def load_intake_vectors(path: str = DEFAULT_VECTORS_PATH) -> List[Dict[str, Any]]:
    """Load and structurally validate the intake test vectors.

    Args:
        path: Absolute path to the vectors JSON file.

    Returns:
        The list of vector entries under the ``vectors`` key.

    Raises:
        IntakeVectorError: If the file is missing, malformed, or contains no
            usable vectors.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise IntakeVectorError(f"intake vectors file not found: {path}") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise IntakeVectorError(f"intake vectors file is unreadable: {path} ({exc})") from exc

    vectors = data.get("vectors") if isinstance(data, dict) else None
    if not isinstance(vectors, list) or not vectors:
        raise IntakeVectorError(
            f"intake vectors file must contain a non-empty 'vectors' array: {path}"
        )
    return vectors


def _decode_body(entry: Dict[str, Any], index: int) -> bytes:
    body_base64 = entry.get("body_base64")
    if not isinstance(body_base64, str):
        raise IntakeVectorError(
            f"intake vector #{index} ({entry.get('name', '?')}) missing 'body_base64'"
        )
    try:
        return base64.b64decode(body_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IntakeVectorError(
            f"intake vector #{index} ({entry.get('name', '?')}) has invalid base64 body: {exc}"
        ) from exc


def run_intake_vector_self_check(path: str = DEFAULT_VECTORS_PATH) -> int:
    """Recompute HMAC-SHA256 for every vector and compare to the recorded signature.

    For each vector, this recomputes ``compute_hmac_sha256_hex(secret, body_bytes)``
    over the base64-decoded body and compares it (case-insensitively) to the
    recorded ``signature``. Any mismatch — or any structurally invalid entry —
    raises :class:`IntakeVectorError` so startup fails closed and the voice
    submission endpoint is not served.

    Args:
        path: Absolute path to the vectors JSON file.

    Returns:
        The number of vectors that were verified successfully.

    Raises:
        IntakeVectorError: On the first mismatched or malformed vector.
    """
    vectors = load_intake_vectors(path)

    for index, entry in enumerate(vectors):
        if not isinstance(entry, dict):
            raise IntakeVectorError(f"intake vector #{index} is not an object")

        name = entry.get("name", f"#{index}")
        secret = entry.get("secret")
        recorded = entry.get("signature")

        if not isinstance(secret, str):
            raise IntakeVectorError(f"intake vector '{name}' missing string 'secret'")
        if not isinstance(recorded, str):
            raise IntakeVectorError(f"intake vector '{name}' missing string 'signature'")

        body_bytes = _decode_body(entry, index)
        computed = compute_hmac_sha256_hex(secret, body_bytes)

        if computed.lower() != recorded.strip().lower():
            raise IntakeVectorError(
                f"intake vector '{name}' signature mismatch: "
                f"computed {computed} but fixture recorded {recorded}"
            )

    return len(vectors)
