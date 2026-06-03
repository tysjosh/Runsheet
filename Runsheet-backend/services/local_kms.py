"""Local (no-AWS) KMS client for development and CI.

Implements the small subset of the boto3 KMS interface that
:class:`services.credentials_vault.TenantCredentialsVault` uses —
``generate_data_key`` and ``decrypt`` — without calling AWS. This lets the
credentials vault (and therefore intake-channel registration, integration
credential storage, etc.) work end-to-end on a local developer machine.

Envelope scheme (compatible with the vault's expectations):

* ``generate_data_key`` returns a random 256-bit plaintext DEK plus a
  ``CiphertextBlob`` that is the DEK **wrapped** (AES-GCM encrypted) under a
  local master key. The wrap is bound to the ``EncryptionContext`` as AAD, so
  a blob can only be unwrapped with the same tenant/key context — mirroring
  real KMS encryption-context semantics.
* ``decrypt`` reverses the wrap, verifying the AAD.

The local master key is derived deterministically from a configured secret
(``LOCAL_KMS_MASTER_KEY`` env var, defaulting to a dev constant) so wrapped
blobs persisted to Elasticsearch survive a backend restart. This is NOT for
production — production sets ``FUEL_OPS_KMS_KEY_ID`` and uses real AWS KMS.

Security note: this is a development convenience. It keeps secrets encrypted at
rest with a real AEAD cipher, but the master key lives in process config, so it
provides obfuscation + integrity, not the hardware-backed guarantees of KMS.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: Sentinel key id surfaced to callers that don't set FUEL_OPS_KMS_KEY_ID.
LOCAL_KMS_DEFAULT_KEY_ID = "local-dev-kms-key"

_NONCE_BYTES = 12
_DEK_BYTES = 32


def _master_key() -> bytes:
    """Derive the 32-byte local master key from configured secret material."""
    secret = os.environ.get(
        "LOCAL_KMS_MASTER_KEY",
        # Stable dev default so blobs round-trip across restarts. Override via
        # env for a per-developer key; never used when real KMS is configured.
        "runsheet-local-dev-kms-master-key-do-not-use-in-prod",
    )
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _aad(key_id: str, encryption_context: Optional[Dict[str, str]]) -> bytes:
    """Bind the key id + encryption context into deterministic AAD bytes."""
    payload = {"KeyId": key_id, "EncryptionContext": encryption_context or {}}
    return json.dumps(payload, sort_keys=True).encode("utf-8")


class LocalKMSClient:
    """Drop-in stand-in for the boto3 KMS client (dev/CI only)."""

    def __init__(self, key_id: str = LOCAL_KMS_DEFAULT_KEY_ID) -> None:
        self._key_id = key_id

    def generate_data_key(
        self, *, KeyId: str, KeySpec: str = "AES_256",
        EncryptionContext: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        plaintext = os.urandom(_DEK_BYTES)
        aesgcm = AESGCM(_master_key())
        nonce = os.urandom(_NONCE_BYTES)
        wrapped = nonce + aesgcm.encrypt(nonce, plaintext, _aad(KeyId, EncryptionContext))
        return {"Plaintext": plaintext, "CiphertextBlob": wrapped, "KeyId": KeyId}

    def decrypt(
        self, *, CiphertextBlob: bytes,
        EncryptionContext: Optional[Dict[str, str]] = None,
        KeyId: Optional[str] = None,
    ) -> Dict[str, Any]:
        key_id = KeyId or self._key_id
        nonce, ct = CiphertextBlob[:_NONCE_BYTES], CiphertextBlob[_NONCE_BYTES:]
        aesgcm = AESGCM(_master_key())
        plaintext = aesgcm.decrypt(nonce, ct, _aad(key_id, EncryptionContext))
        return {"Plaintext": plaintext, "KeyId": key_id}
