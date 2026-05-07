"""
Unit tests for TenantCredentialsVault — KMS envelope encryption, tenant isolation.

Validates Req 5.1.3 (Tenant_Credentials_Vault scoped reads/writes) and Req 10.1
(cross-tenant access rejected). boto3 KMS is mocked so no AWS calls are made.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.credentials_vault import (
    ALGORITHM,
    TenantCredentialsVault,
    aes_gcm_decrypt,
    aes_gcm_encrypt,
)
from fuel.services.fuel_ops_es_mappings import TENANT_CREDENTIALS_INDEX


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeKMS:
    """In-memory stand-in for boto3 KMS.

    ``generate_data_key`` returns a random 32-byte DEK whose "wrapped" form is a
    reversible opaque blob keyed by the DEK itself. ``decrypt`` looks it up.
    Each call is also recorded for assertions.
    """

    def __init__(self) -> None:
        self._wrap_by_blob: Dict[bytes, bytes] = {}
        self.generate_calls: list[dict] = []
        self.decrypt_calls: list[dict] = []

    def generate_data_key(self, *, KeyId: str, KeySpec: str, EncryptionContext: Optional[dict] = None):
        self.generate_calls.append(
            {"KeyId": KeyId, "KeySpec": KeySpec, "EncryptionContext": EncryptionContext}
        )
        plaintext = os.urandom(32)
        wrapped = b"WRAP|" + plaintext  # reversible toy "wrapping"
        self._wrap_by_blob[wrapped] = plaintext
        return {"Plaintext": plaintext, "CiphertextBlob": wrapped, "KeyId": KeyId}

    def decrypt(self, *, CiphertextBlob: bytes, EncryptionContext: Optional[dict] = None):
        self.decrypt_calls.append(
            {"CiphertextBlob": CiphertextBlob, "EncryptionContext": EncryptionContext}
        )
        if CiphertextBlob not in self._wrap_by_blob:
            raise ValueError("InvalidCiphertextException")
        return {"Plaintext": self._wrap_by_blob[CiphertextBlob]}


class _FakeES:
    """Minimal in-memory ES service exposing the subset the vault calls."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]):
        assert index == TENANT_CREDENTIALS_INDEX
        self.docs[doc_id] = dict(document)
        return {"result": "created"}

    async def get_document(self, index: str, doc_id: str):
        assert index == TENANT_CREDENTIALS_INDEX
        if doc_id not in self.docs:
            err = Exception("NotFoundError: not_found")
            err.status_code = 404  # type: ignore[attr-defined]
            raise err
        return dict(self.docs[doc_id])

    async def update_document(self, index: str, doc_id: str, partial: Dict[str, Any]):
        assert index == TENANT_CREDENTIALS_INDEX
        if doc_id not in self.docs:
            err = Exception("NotFoundError")
            err.status_code = 404  # type: ignore[attr-defined]
            raise err
        self.docs[doc_id].update(partial)
        return {"result": "updated"}

    async def delete_document(self, index: str, doc_id: str) -> bool:
        assert index == TENANT_CREDENTIALS_INDEX
        if doc_id in self.docs:
            del self.docs[doc_id]
            return True
        return False

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 100):
        assert index == TENANT_CREDENTIALS_INDEX
        filters = query.get("query", {}).get("bool", {}).get("filter", []) or []
        want: Dict[str, Any] = {}
        for flt in filters:
            term = flt.get("term", {})
            want.update(term)
        hits = []
        for doc in self.docs.values():
            if all(doc.get(k) == v for k, v in want.items()):
                hits.append({"_source": dict(doc)})
        return {"hits": {"hits": hits[: size or 100]}}


@pytest.fixture
def fake_kms() -> _FakeKMS:
    return _FakeKMS()


@pytest.fixture
def fake_es() -> _FakeES:
    return _FakeES()


@pytest.fixture
def vault(fake_es: _FakeES, fake_kms: _FakeKMS) -> TenantCredentialsVault:
    return TenantCredentialsVault(
        es_service=fake_es, kms_key_id="arn:aws:kms:us-east-1:111:key/default", kms_client=fake_kms
    )


# ---------------------------------------------------------------------------
# aes_gcm primitives
# ---------------------------------------------------------------------------


class TestAesGcmPrimitives:
    def test_round_trip(self):
        key = os.urandom(32)
        blob = aes_gcm_encrypt(key, b"hello world")
        assert aes_gcm_decrypt(key, blob) == b"hello world"

    def test_nonce_is_unique_per_call(self):
        key = os.urandom(32)
        a = aes_gcm_encrypt(key, b"payload")
        b = aes_gcm_encrypt(key, b"payload")
        assert a != b, "AES-GCM nonce must randomize across calls"

    def test_rejects_wrong_key_length(self):
        with pytest.raises(ValueError):
            aes_gcm_encrypt(b"short", b"x")
        with pytest.raises(ValueError):
            aes_gcm_decrypt(b"short", b"x" * 32)

    def test_tamper_detection(self):
        from cryptography.exceptions import InvalidTag

        key = os.urandom(32)
        blob = bytearray(aes_gcm_encrypt(key, b"payload"))
        blob[-1] ^= 0x01  # flip a tag bit
        with pytest.raises(InvalidTag):
            aes_gcm_decrypt(key, bytes(blob))


# ---------------------------------------------------------------------------
# put / get
# ---------------------------------------------------------------------------


class TestPutGetRoundTrip:
    @pytest.mark.asyncio
    async def test_put_persists_kms_envelope_and_returns_ref(
        self, vault: TenantCredentialsVault, fake_es: _FakeES, fake_kms: _FakeKMS
    ):
        secret_value = "TEST-SECRET-abc"
        plaintext = {"api_key": secret_value, "scopes": ["read", "write"]}

        ref = await vault.put(
            tenant_id="tenant-A", key="stripe_secret", plaintext=plaintext, provider_name="stripe"
        )

        assert ref.startswith("cred:tenant-A:stripe_secret:")
        assert ref in fake_es.docs

        doc = fake_es.docs[ref]
        # Metadata that should be persisted.
        assert doc["tenant_id"] == "tenant-A"
        assert doc["key"] == "stripe_secret"
        assert doc["provider_name"] == "stripe"
        assert doc["algorithm"] == ALGORITHM
        assert doc["kms_key_id"] == "arn:aws:kms:us-east-1:111:key/default"
        assert doc["rotated_at"] is None
        # Secret material is present, non-empty, and NOT the plaintext.
        assert doc["wrapped_dek"]
        assert doc["ciphertext"]
        assert secret_value not in doc["ciphertext"]
        # wrapped_dek / ciphertext round-trip as base64.
        assert base64.b64decode(doc["wrapped_dek"])
        assert base64.b64decode(doc["ciphertext"])

        # KMS was invoked once with the expected encryption context.
        assert len(fake_kms.generate_calls) == 1
        call = fake_kms.generate_calls[0]
        assert call["KeySpec"] == "AES_256"
        assert call["EncryptionContext"] == {"tenant_id": "tenant-A", "key": "stripe_secret"}

    @pytest.mark.asyncio
    async def test_get_returns_original_plaintext(
        self, vault: TenantCredentialsVault
    ):
        plaintext = {"token": "abc123", "nested": {"x": 1}}
        ref = await vault.put("tenant-A", "qbo_oauth", plaintext)
        got = await vault.get("tenant-A", ref)
        assert got == plaintext

    @pytest.mark.asyncio
    async def test_put_accepts_explicit_kms_key_override(
        self, fake_es: _FakeES, fake_kms: _FakeKMS
    ):
        v = TenantCredentialsVault(es_service=fake_es, kms_client=fake_kms)  # no default key
        override = "arn:aws:kms:us-east-1:222:key/override"
        ref = await v.put("tenant-A", "k", {"a": 1}, kms_key_id=override)
        assert fake_es.docs[ref]["kms_key_id"] == override

    @pytest.mark.asyncio
    async def test_put_without_any_kms_key_raises(self, fake_es: _FakeES, fake_kms: _FakeKMS):
        v = TenantCredentialsVault(es_service=fake_es, kms_client=fake_kms)
        with pytest.raises(ValueError, match="kms_key_id"):
            await v.put("tenant-A", "k", {"a": 1})

    @pytest.mark.asyncio
    async def test_put_rejects_empty_identifiers(self, vault: TenantCredentialsVault):
        with pytest.raises(ValueError):
            await vault.put("", "k", {"a": 1})
        with pytest.raises(ValueError):
            await vault.put("tenant-A", "", {"a": 1})


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_get_rejects_cross_tenant_ref(self, vault: TenantCredentialsVault):
        ref = await vault.put("tenant-A", "k", {"secret": 1})
        with pytest.raises(PermissionError):
            await vault.get("tenant-B", ref)

    @pytest.mark.asyncio
    async def test_rotate_rejects_cross_tenant_ref(self, vault: TenantCredentialsVault):
        ref = await vault.put("tenant-A", "k", {"secret": 1})
        with pytest.raises(PermissionError):
            await vault.rotate("tenant-B", ref)

    @pytest.mark.asyncio
    async def test_delete_rejects_cross_tenant_ref(
        self, vault: TenantCredentialsVault, fake_es: _FakeES
    ):
        ref = await vault.put("tenant-A", "k", {"secret": 1})
        with pytest.raises(PermissionError):
            await vault.delete("tenant-B", ref)
        # Record must still exist — cross-tenant call did not delete it.
        assert ref in fake_es.docs

    @pytest.mark.asyncio
    async def test_list_for_tenant_excludes_other_tenants(
        self, vault: TenantCredentialsVault
    ):
        await vault.put("tenant-A", "a1", {"x": 1})
        await vault.put("tenant-A", "a2", {"x": 2})
        await vault.put("tenant-B", "b1", {"x": 3})

        listed = await vault.list_for_tenant("tenant-A")
        assert {entry["key"] for entry in listed} == {"a1", "a2"}
        # Secret material must be stripped from listings.
        assert all("wrapped_dek" not in entry for entry in listed)
        assert all("ciphertext" not in entry for entry in listed)

    @pytest.mark.asyncio
    async def test_list_for_tenant_filters_by_provider(self, vault: TenantCredentialsVault):
        await vault.put("tenant-A", "k1", {"x": 1}, provider_name="stripe")
        await vault.put("tenant-A", "k2", {"x": 2}, provider_name="quickbooks_online")

        stripe_only = await vault.list_for_tenant("tenant-A", provider_name="stripe")
        assert [e["key"] for e in stripe_only] == ["k1"]


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


class TestRotate:
    @pytest.mark.asyncio
    async def test_rotate_changes_ciphertext_but_preserves_plaintext(
        self, vault: TenantCredentialsVault, fake_es: _FakeES, fake_kms: _FakeKMS
    ):
        plaintext = {"api_key": "TEST-ROTATE-xyz"}
        ref = await vault.put("tenant-A", "k", plaintext)

        before = dict(fake_es.docs[ref])
        assert before["rotated_at"] is None

        returned_ref = await vault.rotate("tenant-A", ref)
        assert returned_ref == ref  # ref is stable across rotation

        after = fake_es.docs[ref]
        # A fresh DEK and fresh ciphertext were written.
        assert after["wrapped_dek"] != before["wrapped_dek"]
        assert after["ciphertext"] != before["ciphertext"]
        assert after["rotated_at"] is not None
        # Plaintext survives rotation.
        assert await vault.get("tenant-A", ref) == plaintext
        # KMS was called at least twice (once on put, once on rotate).
        assert len(fake_kms.generate_calls) >= 2

    @pytest.mark.asyncio
    async def test_rotate_honors_new_kms_key_id(
        self, vault: TenantCredentialsVault, fake_es: _FakeES
    ):
        ref = await vault.put("tenant-A", "k", {"x": 1})
        new_key = "arn:aws:kms:us-east-1:333:key/rotated"
        await vault.rotate("tenant-A", ref, kms_key_id=new_key)
        assert fake_es.docs[ref]["kms_key_id"] == new_key

    @pytest.mark.asyncio
    async def test_rotate_missing_ref_raises_key_error(self, vault: TenantCredentialsVault):
        with pytest.raises(KeyError):
            await vault.rotate("tenant-A", "cred:tenant-A:missing:nonexistent")


# ---------------------------------------------------------------------------
# Delete + get-not-found behavior
# ---------------------------------------------------------------------------


class TestDeleteAndMissing:
    @pytest.mark.asyncio
    async def test_delete_returns_true_on_success(
        self, vault: TenantCredentialsVault, fake_es: _FakeES
    ):
        ref = await vault.put("tenant-A", "k", {"x": 1})
        assert await vault.delete("tenant-A", ref) is True
        assert ref not in fake_es.docs

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_missing(self, vault: TenantCredentialsVault):
        assert await vault.delete("tenant-A", "cred:tenant-A:x:nonexistent") is False

    @pytest.mark.asyncio
    async def test_get_missing_raises_key_error(self, vault: TenantCredentialsVault):
        with pytest.raises(KeyError):
            await vault.get("tenant-A", "cred:tenant-A:x:nope")


# ---------------------------------------------------------------------------
# Lazy boto3 client creation
# ---------------------------------------------------------------------------


class TestLazyKmsClient:
    @pytest.mark.asyncio
    async def test_injected_kms_client_is_used(
        self, fake_es: _FakeES, fake_kms: _FakeKMS
    ):
        v = TenantCredentialsVault(
            es_service=fake_es, kms_key_id="kms-id", kms_client=fake_kms
        )
        await v.put("tenant-A", "k", {"x": 1})
        assert fake_kms.generate_calls, "injected KMS client must be exercised"

    def test_does_not_call_boto3_at_construction_time(self, fake_es: _FakeES):
        # If boto3 were called at __init__, this would fail in environments
        # without AWS credentials. Constructing without a pre-built client
        # must be safe.
        TenantCredentialsVault(es_service=fake_es, kms_key_id="kms-id")
