"""
Per-driver salted PIN hashing and vault-backed storage (Surface B, Req 19).

Drivers verify their identity to the voice agent with a short numeric PIN
(``GET /drivers/verify``). PINs are low-entropy secrets, so they are **never**
stored in cleartext and **never** placed on the ``drivers_current`` document
(where they could be returned by a read endpoint or leak through a projection).
Instead this module:

    1. Derives a **salted, slow** hash of the PIN with PBKDF2-HMAC-SHA256 and a
       fresh per-driver random salt (:func:`hash_pin`).
    2. Persists the resulting hash record inside the encrypted
       :class:`~services.credentials_vault.TenantCredentialsVault` under the
       logical key ``driver_pin:{driver_id}`` (:class:`DriverPinVault`), so the
       secret material is KMS-envelope-encrypted at rest and tenant-scoped.
    3. Verifies a presented PIN in **constant time** via
       :func:`hmac.compare_digest` (:func:`verify_pin` /
       :meth:`DriverPinVault.verify_pin`), so a wrong PIN and a wrong length are
       indistinguishable by timing.

The stored hash record is an internal artifact: it is never returned from any
API surface. ``GET /drivers/verify`` (Task 7.6) resolves the driver, calls
:meth:`DriverPinVault.verify_pin`, and returns only ``pinVerified`` plus a
PIN-free ``driver`` object.

Requirements: 19.1, 19.2, 19.3
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Dict, Optional

__all__ = [
    "PIN_HASH_ALGORITHM",
    "DEFAULT_ITERATIONS",
    "SALT_BYTES",
    "hash_pin",
    "verify_pin",
    "DriverPinVault",
]

#: Algorithm identifier stored alongside each record for forward-compatibility.
PIN_HASH_ALGORITHM = "PBKDF2_HMAC_SHA256_V1"

#: PBKDF2 iteration count. High enough to make brute-forcing a short numeric PIN
#: costly, since PINs are low-entropy. Stored per-record so it can be raised
#: later without invalidating existing hashes.
DEFAULT_ITERATIONS = 200_000

#: Random salt length in bytes (128-bit).
SALT_BYTES = 16

#: Logical vault key template. The PIN hash lives ONLY here — never in
#: ``drivers_current``.
_VAULT_KEY_TEMPLATE = "driver_pin:{driver_id}"

#: Deterministic vault ref so a driver's PIN hash can be retrieved by identity
#: without storing a ref pointer on the driver document. Tenant-scoped for
#: global uniqueness within the shared credentials index.
_VAULT_REF_TEMPLATE = "driver_pin:{tenant_id}:{driver_id}"


def _coerce_pin(pin: str) -> bytes:
    """Normalize a PIN to bytes (UTF-8). Rejects empty PINs."""
    if pin is None or pin == "":
        raise ValueError("pin must be a non-empty string")
    if not isinstance(pin, str):
        raise TypeError("pin must be a string")
    return pin.encode("utf-8")


def _derive(pin_bytes: bytes, salt: bytes, iterations: int) -> bytes:
    """Return the raw PBKDF2-HMAC-SHA256 derived key."""
    return hashlib.pbkdf2_hmac("sha256", pin_bytes, salt, iterations)


def hash_pin(
    pin: str,
    *,
    salt: Optional[bytes] = None,
    iterations: int = DEFAULT_ITERATIONS,
) -> Dict[str, Any]:
    """Derive a salted PBKDF2 hash record for ``pin``.

    A fresh random salt is generated when one is not supplied, so two drivers
    with the same PIN produce different hashes. The returned record is
    JSON-serializable and self-describing (it carries its own salt/iterations),
    so :func:`verify_pin` can re-derive without external configuration.

    Args:
        pin: The cleartext PIN. Must be non-empty.
        salt: Optional explicit salt (primarily for tests/re-hash). A fresh
            random :data:`SALT_BYTES`-byte salt is generated when omitted.
        iterations: PBKDF2 iteration count. Defaults to :data:`DEFAULT_ITERATIONS`.

    Returns:
        A dict ``{algorithm, iterations, salt, hash}`` with ``salt``/``hash``
        as lowercase hex. This is the value persisted in the vault; it is
        NEVER returned from an API surface.
    """
    pin_bytes = _coerce_pin(pin)
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    if salt is None:
        salt = os.urandom(SALT_BYTES)
    derived = _derive(pin_bytes, salt, iterations)
    return {
        "algorithm": PIN_HASH_ALGORITHM,
        "iterations": iterations,
        "salt": salt.hex(),
        "hash": derived.hex(),
    }


def verify_pin(pin: str, record: Optional[Dict[str, Any]]) -> bool:
    """Constant-time verify a presented ``pin`` against a stored hash ``record``.

    Re-derives the PBKDF2 hash using the salt/iterations recorded in ``record``
    and compares it against the stored digest with :func:`hmac.compare_digest`
    so a mismatch reveals nothing through timing. Returns ``False`` (never
    raises) for an absent/malformed record or an empty PIN, so callers can treat
    "no PIN on file" and "wrong PIN" identically (Req 19.2, 19.3).

    Args:
        pin: The presented cleartext PIN.
        record: The stored hash record produced by :func:`hash_pin`, or ``None``.

    Returns:
        ``True`` iff the PIN reproduces the stored digest.
    """
    if not record or not isinstance(record, dict):
        return False
    if record.get("algorithm") != PIN_HASH_ALGORITHM:
        return False
    if not pin or not isinstance(pin, str):
        return False
    try:
        salt = bytes.fromhex(record["salt"])
        iterations = int(record["iterations"])
        expected = bytes.fromhex(record["hash"])
    except (KeyError, ValueError, TypeError):
        return False
    if iterations <= 0:
        return False
    candidate = _derive(pin.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


class DriverPinVault:
    """Vault-backed per-driver PIN hash store (Req 19.1–19.3).

    Wraps a :class:`~services.credentials_vault.TenantCredentialsVault`,
    persisting each driver's salted PIN hash under the logical key
    ``driver_pin:{driver_id}`` at a deterministic, tenant-scoped ref so it can
    be retrieved by driver identity without a ref pointer on the driver record.
    The PIN hash therefore lives ONLY in the encrypted vault — never on
    ``drivers_current`` and never in any response body.
    """

    def __init__(self, vault: Any, *, kms_key_id: Optional[str] = None) -> None:
        """
        Args:
            vault: A :class:`TenantCredentialsVault`-compatible instance.
            kms_key_id: Optional CMK override forwarded to ``vault.put``; when
                omitted the vault's configured default key is used.
        """
        if vault is None:
            raise ValueError("vault is required")
        self._vault = vault
        self._kms_key_id = kms_key_id

    @staticmethod
    def _vault_key(driver_id: str) -> str:
        if not driver_id:
            raise ValueError("driver_id must be non-empty")
        return _VAULT_KEY_TEMPLATE.format(driver_id=driver_id)

    @staticmethod
    def _vault_ref(tenant_id: str, driver_id: str) -> str:
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not driver_id:
            raise ValueError("driver_id must be non-empty")
        return _VAULT_REF_TEMPLATE.format(tenant_id=tenant_id, driver_id=driver_id)

    async def set_pin(
        self,
        tenant_id: str,
        driver_id: str,
        pin: str,
        *,
        iterations: int = DEFAULT_ITERATIONS,
    ) -> None:
        """Hash ``pin`` and persist (upsert) it for ``(tenant_id, driver_id)``.

        The cleartext PIN is discarded after hashing; only the salted hash
        record is written to the vault. Re-calling for the same driver replaces
        the prior hash in place (same deterministic ref).
        """
        record = hash_pin(pin, iterations=iterations)
        await self._vault.put(
            tenant_id=tenant_id,
            key=self._vault_key(driver_id),
            plaintext=record,
            kms_key_id=self._kms_key_id,
            provider_name="driver_pin",
            ref=self._vault_ref(tenant_id, driver_id),
        )

    async def verify_pin(self, tenant_id: str, driver_id: str, pin: str) -> bool:
        """Constant-time verify ``pin`` for ``(tenant_id, driver_id)``.

        Returns ``False`` when no PIN is on file for the driver (KeyError from
        the vault) or the PIN is wrong, so "unknown driver" and "wrong PIN" are
        indistinguishable to the caller (Req 19.2, 19.3). Cross-tenant access is
        already blocked by the vault (``PermissionError``), which is allowed to
        propagate.
        """
        try:
            record = await self._vault.get(
                tenant_id, self._vault_ref(tenant_id, driver_id)
            )
        except KeyError:
            return False
        return verify_pin(pin, record)

    async def has_pin(self, tenant_id: str, driver_id: str) -> bool:
        """Report whether a PIN hash exists for ``(tenant_id, driver_id)``.

        Returns a **boolean only** — never the stored record, and never any
        part of it. This is what an enrollment-state read is allowed to know
        (driver-mobile-app R2.9): the hash, the salt, and the iteration count
        stay inside the vault. ``False`` for "no PIN on file" and for a record
        the vault holds but that carries no digest, so a half-written record
        reads as not enrolled rather than as enrolled-but-unverifiable.

        Cross-tenant access is blocked by the vault (``PermissionError``),
        which is allowed to propagate.
        """
        try:
            record = await self._vault.get(
                tenant_id, self._vault_ref(tenant_id, driver_id)
            )
        except KeyError:
            return False
        return isinstance(record, dict) and bool(record.get("hash"))

    async def delete_pin(self, tenant_id: str, driver_id: str) -> bool:
        """Remove a driver's stored PIN hash. Returns ``True`` if one existed."""
        return bool(
            await self._vault.delete(
                tenant_id, self._vault_ref(tenant_id, driver_id)
            )
        )
