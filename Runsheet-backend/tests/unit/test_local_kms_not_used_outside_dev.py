"""Staging must not encrypt real tenant credentials with a key from this repo.

``LocalKMSClient`` derives its master key from ``LOCAL_KMS_MASTER_KEY`` or, when
that is unset, a literal default committed to source. It exists so credential
flows work end-to-end off-AWS on a laptop and in CI.

The bootstrap gate used to be ``_env != "production"``, which includes STAGING.
Staging holds real QuickBooks, Stripe and Geotab tokens, so that combination
encrypted live credentials under a key anyone with repo access can derive — and
silently, because the only log line was an INFO saying the fallback was in use
"for dev/CI".

The gate is now an allow-list of the two environments that genuinely have no AWS.
"""
from __future__ import annotations

import inspect
import re

import pytest

from services.local_kms import LOCAL_KMS_DEFAULT_KEY_ID, LocalKMSClient


def _bootstrap_gate_source() -> str:
    """The gate expression, read from source.

    Executing ``bootstrap.agents.initialize`` needs ES, Redis and a live
    scheduler, so this asserts on the condition itself. That is enough: the
    regression was the condition, and it is the condition that must not widen
    again.
    """
    import bootstrap.agents as agents_module

    return inspect.getsource(agents_module)


class TestTheFallbackIsRestrictedToDevAndTest:
    def test_the_gate_is_an_allow_list_not_a_production_exclusion(self):
        source = _bootstrap_gate_source()

        assert 'if not kms_key_id and _env in ("development", "test"):' in source, (
            "the LocalKMSClient gate is no longer an explicit development/test "
            "allow-list; if it has reverted to excluding only production then "
            "staging encrypts real tenant credentials with the master key "
            "committed to this repository"
        )

    def test_the_old_production_only_exclusion_is_gone(self):
        source = _bootstrap_gate_source()
        offending = re.search(
            r'if not kms_key_id and _env\s*!=\s*"production"', source
        )
        assert offending is None, (
            "found the production-only exclusion again — staging would fall "
            "through to LocalKMSClient"
        )

    def test_staging_without_a_cmk_is_warned_about_at_boot(self):
        """The failure used to surface as a 500 from deep inside ``put``."""
        source = _bootstrap_gate_source()
        assert "the credentials vault " in source and "cannot encrypt" in source, (
            "no boot-time warning for a missing CMK outside dev/test"
        )


class TestTheLocalMasterKeyIsWorthProtecting:
    """Establishes *why* the gate matters, so the tests above are not arbitrary."""

    def test_the_default_master_key_is_a_literal_in_source(self):
        import services.local_kms as local_kms

        source = inspect.getsource(local_kms)
        assert "do-not-use-in-prod" in source, (
            "the local master key default is no longer a source literal; if it "
            "became a required secret the staging gate could be relaxed"
        )

    def test_blobs_are_reproducible_from_that_literal(self):
        """Anyone with the repo can decrypt what this client wrapped.

        Two independently constructed clients round-trip each other's data
        without sharing any state, which is exactly the property that makes the
        key unsuitable for an environment holding real credentials.
        """
        writer = LocalKMSClient(key_id=LOCAL_KMS_DEFAULT_KEY_ID)
        reader = LocalKMSClient(key_id=LOCAL_KMS_DEFAULT_KEY_ID)
        context = {"tenant_id": "tenant-a", "key": "quickbooks"}

        generated = writer.generate_data_key(
            KeyId=LOCAL_KMS_DEFAULT_KEY_ID,
            KeySpec="AES_256",
            EncryptionContext=context,
        )
        recovered = reader.decrypt(
            CiphertextBlob=generated["CiphertextBlob"],
            EncryptionContext=context,
        )

        assert recovered["Plaintext"] == generated["Plaintext"]

    def test_the_encryption_context_is_bound(self):
        """Not a reason to trust it in staging, but worth not regressing."""
        client = LocalKMSClient(key_id=LOCAL_KMS_DEFAULT_KEY_ID)
        generated = client.generate_data_key(
            KeyId=LOCAL_KMS_DEFAULT_KEY_ID,
            KeySpec="AES_256",
            EncryptionContext={"tenant_id": "tenant-a", "key": "quickbooks"},
        )

        with pytest.raises(Exception):
            client.decrypt(
                CiphertextBlob=generated["CiphertextBlob"],
                EncryptionContext={"tenant_id": "tenant-b", "key": "quickbooks"},
            )
