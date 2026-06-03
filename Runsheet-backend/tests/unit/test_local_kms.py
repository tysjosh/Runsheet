"""
Unit tests for LocalKMSClient — the no-AWS KMS stand-in used in dev/CI.

Validates that the client implements the boto3 KMS subset the credentials vault
relies on (``generate_data_key`` / ``decrypt``) with real AES-GCM envelope
encryption, binds the encryption context as AAD, and round-trips through
:class:`TenantCredentialsVault`.
"""
from __future__ import annotations

import pytest

from services.local_kms import LOCAL_KMS_DEFAULT_KEY_ID, LocalKMSClient
from services.credentials_vault import TenantCredentialsVault


class _FakeES:
    """Minimal in-memory ES stub implementing the vault's coroutine surface."""

    def __init__(self):
        self.docs = {}

    async def index_document(self, index, doc_id, doc):
        self.docs[(index, doc_id)] = dict(doc)
        return {"_id": doc_id}

    async def get_document(self, index, doc_id):
        return self.docs.get((index, doc_id))

    async def update_document(self, index, doc_id, partial):
        self.docs[(index, doc_id)].update(partial)
        return {"_id": doc_id}

    async def delete_document(self, index, doc_id):
        return self.docs.pop((index, doc_id), None) is not None


class TestGenerateAndDecrypt:
    def test_generate_returns_32_byte_dek_and_blob(self):
        client = LocalKMSClient()
        out = client.generate_data_key(
            KeyId=LOCAL_KMS_DEFAULT_KEY_ID,
            KeySpec="AES_256",
            EncryptionContext={"tenant_id": "t1", "key": "k"},
        )
        assert len(out["Plaintext"]) == 32
        assert isinstance(out["CiphertextBlob"], (bytes, bytearray))
        # The wrapped blob must not equal the plaintext DEK.
        assert out["CiphertextBlob"] != out["Plaintext"]

    def test_round_trip_decrypt_recovers_dek(self):
        client = LocalKMSClient()
        ctx = {"tenant_id": "t1", "key": "k"}
        gen = client.generate_data_key(KeyId=LOCAL_KMS_DEFAULT_KEY_ID, EncryptionContext=ctx)
        dec = client.decrypt(CiphertextBlob=gen["CiphertextBlob"], EncryptionContext=ctx)
        assert dec["Plaintext"] == gen["Plaintext"]

    def test_wrong_encryption_context_fails(self):
        client = LocalKMSClient()
        gen = client.generate_data_key(
            KeyId=LOCAL_KMS_DEFAULT_KEY_ID,
            EncryptionContext={"tenant_id": "t1", "key": "k"},
        )
        # AAD mismatch (different tenant) must fail the GCM tag check.
        with pytest.raises(Exception):
            client.decrypt(
                CiphertextBlob=gen["CiphertextBlob"],
                EncryptionContext={"tenant_id": "t2", "key": "k"},
            )

    def test_each_generate_is_unique(self):
        client = LocalKMSClient()
        ctx = {"tenant_id": "t1", "key": "k"}
        a = client.generate_data_key(KeyId=LOCAL_KMS_DEFAULT_KEY_ID, EncryptionContext=ctx)
        b = client.generate_data_key(KeyId=LOCAL_KMS_DEFAULT_KEY_ID, EncryptionContext=ctx)
        assert a["Plaintext"] != b["Plaintext"]
        assert a["CiphertextBlob"] != b["CiphertextBlob"]

    def test_blob_survives_fresh_client_instance(self):
        # Same master key (default) => a new client can unwrap an old blob,
        # mirroring blobs persisted to ES surviving a backend restart.
        ctx = {"tenant_id": "t1", "key": "k"}
        gen = LocalKMSClient().generate_data_key(
            KeyId=LOCAL_KMS_DEFAULT_KEY_ID, EncryptionContext=ctx
        )
        dec = LocalKMSClient().decrypt(
            CiphertextBlob=gen["CiphertextBlob"], EncryptionContext=ctx
        )
        assert dec["Plaintext"] == gen["Plaintext"]


class TestVaultIntegration:
    @pytest.mark.asyncio
    async def test_put_get_round_trip_through_vault(self):
        vault = TenantCredentialsVault(
            es_service=_FakeES(),
            kms_key_id=LOCAL_KMS_DEFAULT_KEY_ID,
            kms_client=LocalKMSClient(),
        )
        secret = {"secret": "hmac-abc-123"}
        ref = await vault.put("demo-tenant", "intake_channel_hmac:ch-1", secret)
        assert await vault.get("demo-tenant", ref) == secret

    @pytest.mark.asyncio
    async def test_cross_tenant_access_denied(self):
        vault = TenantCredentialsVault(
            es_service=_FakeES(),
            kms_key_id=LOCAL_KMS_DEFAULT_KEY_ID,
            kms_client=LocalKMSClient(),
        )
        ref = await vault.put("demo-tenant", "k", {"secret": "x"})
        with pytest.raises(PermissionError):
            await vault.get("other-tenant", ref)

    @pytest.mark.asyncio
    async def test_rotate_preserves_plaintext(self):
        vault = TenantCredentialsVault(
            es_service=_FakeES(),
            kms_key_id=LOCAL_KMS_DEFAULT_KEY_ID,
            kms_client=LocalKMSClient(),
        )
        secret = {"secret": "rotate-me"}
        ref = await vault.put("demo-tenant", "k", secret)
        await vault.rotate("demo-tenant", ref)
        assert await vault.get("demo-tenant", ref) == secret
