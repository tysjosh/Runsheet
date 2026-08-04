"""
Shared HMAC-SHA256 helpers for webhook / voice-intake signature handling.

This module is the single implementation of HMAC-SHA256 signing and
verification used across the codebase. Both the legacy Dinee webhook
receiver (``ops/webhooks/receiver.py``) and the order-intake pipeline
(``fuel/services/order_intake_pipeline.py``) delegate here so there is
exactly one behavior for computing and comparing signatures.

Design notes:
    - The signature is computed as HMAC-SHA256 over the *raw* body bytes
      using the shared secret, and rendered as a lowercase hexadecimal
      digest.
    - Verification is constant-time (``hmac.compare_digest``) and tolerant
      of an optional ``sha256=`` prefix and any letter case on the presented
      digest, matching the fixed Dinee ``X-Signature: sha256=<hex>`` contract.

Requirements: 3.1
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Union

__all__ = ["compute_hmac_sha256_hex", "verify_hmac_sha256_hex"]

_SHA256_PREFIX = "sha256="


def _coerce_secret(secret: Union[str, bytes]) -> bytes:
    """Normalize a secret to bytes (UTF-8 for ``str``)."""
    if isinstance(secret, bytes):
        return secret
    return secret.encode("utf-8")


def compute_hmac_sha256_hex(secret: Union[str, bytes], body: bytes) -> str:
    """Compute the HMAC-SHA256 of ``body`` under ``secret``.

    Args:
        secret: The shared HMAC secret, as ``str`` (UTF-8 encoded) or ``bytes``.
        body: The raw body bytes over which the digest is computed.

    Returns:
        The lowercase hexadecimal HMAC-SHA256 digest.
    """
    return hmac.new(_coerce_secret(secret), body, hashlib.sha256).hexdigest()


def verify_hmac_sha256_hex(
    secret: Union[str, bytes], body: bytes, hex_digest: str
) -> bool:
    """Constant-time verify a presented hex digest against ``body``/``secret``.

    The presented digest may carry an optional ``sha256=`` prefix and may be
    in any letter case; both are normalized before comparison. Comparison is
    constant-time via :func:`hmac.compare_digest`.

    Args:
        secret: The shared HMAC secret, as ``str`` (UTF-8 encoded) or ``bytes``.
        body: The raw body bytes over which the expected digest is computed.
        hex_digest: The presented signature digest, with or without the
            ``sha256=`` prefix and in any letter case.

    Returns:
        ``True`` if the presented digest matches the computed digest.
    """
    if hex_digest is None:
        return False

    presented = hex_digest.strip()
    if presented.lower().startswith(_SHA256_PREFIX):
        presented = presented[len(_SHA256_PREFIX):]
    presented = presented.lower()

    expected = compute_hmac_sha256_hex(secret, body)
    return hmac.compare_digest(expected, presented)
