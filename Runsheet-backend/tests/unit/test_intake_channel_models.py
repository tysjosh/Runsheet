"""
Unit tests for fuel.intake_channel_models — IntakeChannel Pydantic model.

Validates: Requirement 2.1.2.
"""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from fuel.intake_channel_models import IntakeChannel, RegistrableChannelType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_channel(**overrides) -> dict:
    """Return a minimal valid IntakeChannel payload."""
    base = {
        "channel_id": "my-voice-provider-01",
        "tenant_id": "tenant-abc",
        "channel_type": "voice",
        "display_name": "Voice AI Provider",
        "hmac_secret_ref": "vault://intake_channel_hmac:my-voice-provider-01",
        "supported_schema_versions": ["1.0"],
        "rate_limit_per_minute": None,
        "enabled": True,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestIntakeChannelValid:
    """Valid IntakeChannel construction."""

    def test_minimal_valid(self):
        ch = IntakeChannel(**_valid_channel())
        assert ch.channel_id == "my-voice-provider-01"
        assert ch.channel_type == "voice"
        assert ch.supported_schema_versions == ["1.0"]
        assert ch.rate_limit_per_minute is None
        assert ch.enabled is True

    def test_all_channel_types(self):
        for ct in ("voice", "web_portal", "dispatcher", "csv", "edi", "api_partner"):
            ch = IntakeChannel(**_valid_channel(channel_type=ct))
            assert ch.channel_type == ct

    def test_rate_limit_set(self):
        ch = IntakeChannel(**_valid_channel(rate_limit_per_minute=100))
        assert ch.rate_limit_per_minute == 100

    def test_multiple_schema_versions(self):
        ch = IntakeChannel(**_valid_channel(supported_schema_versions=["1.0", "2.0", "2.1"]))
        assert ch.supported_schema_versions == ["1.0", "2.0", "2.1"]

    def test_channel_id_boundary_min_length(self):
        # Minimum valid: 3 chars (start + 1 middle + end)
        ch = IntakeChannel(**_valid_channel(channel_id="a0b"))
        assert ch.channel_id == "a0b"

    def test_channel_id_boundary_max_length(self):
        # Maximum valid: 64 chars (start + 62 middle + end)
        mid = "a" * 62
        channel_id = f"a{mid}b"
        assert len(channel_id) == 64
        ch = IntakeChannel(**_valid_channel(channel_id=channel_id))
        assert ch.channel_id == channel_id

    def test_channel_id_with_hyphens(self):
        ch = IntakeChannel(**_valid_channel(channel_id="my-voice-provider-01"))
        assert ch.channel_id == "my-voice-provider-01"

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError, match="extra"):
            IntakeChannel(**_valid_channel(unknown_field="oops"))


# ---------------------------------------------------------------------------
# channel_id validation
# ---------------------------------------------------------------------------


class TestChannelIdValidation:
    """channel_id regex: ^[a-z0-9][a-z0-9\\-]{1,62}[a-z0-9]$"""

    def test_starts_with_hyphen_rejected(self):
        with pytest.raises(ValidationError, match="channel_id"):
            IntakeChannel(**_valid_channel(channel_id="-abc"))

    def test_ends_with_hyphen_rejected(self):
        with pytest.raises(ValidationError, match="channel_id"):
            IntakeChannel(**_valid_channel(channel_id="abc-"))

    def test_uppercase_rejected(self):
        with pytest.raises(ValidationError, match="channel_id"):
            IntakeChannel(**_valid_channel(channel_id="My-Channel"))

    def test_too_short_rejected(self):
        # 2 chars doesn't match (needs at least 3)
        with pytest.raises(ValidationError):
            IntakeChannel(**_valid_channel(channel_id="ab"))

    def test_too_long_rejected(self):
        # 65 chars exceeds max_length=64
        channel_id = "a" * 63 + "b"
        assert len(channel_id) == 64  # this is still valid
        # 66 chars should fail
        channel_id_long = "a" * 64 + "b"
        with pytest.raises(ValidationError):
            IntakeChannel(**_valid_channel(channel_id=channel_id_long))

    def test_special_chars_rejected(self):
        with pytest.raises(ValidationError, match="channel_id"):
            IntakeChannel(**_valid_channel(channel_id="my_channel_01"))

    def test_spaces_rejected(self):
        with pytest.raises(ValidationError, match="channel_id"):
            IntakeChannel(**_valid_channel(channel_id="my channel"))


# ---------------------------------------------------------------------------
# channel_type validation
# ---------------------------------------------------------------------------


class TestChannelTypeValidation:
    """channel_type must be one of the 6 registrable types."""

    def test_legacy_rejected(self):
        with pytest.raises(ValidationError, match="channel_type"):
            IntakeChannel(**_valid_channel(channel_type="legacy"))

    def test_unknown_type_rejected(self):
        with pytest.raises(ValidationError, match="channel_type"):
            IntakeChannel(**_valid_channel(channel_type="unknown"))


# ---------------------------------------------------------------------------
# supported_schema_versions validation
# ---------------------------------------------------------------------------


class TestSchemaVersionsValidation:
    """supported_schema_versions must be non-empty."""

    def test_empty_list_rejected(self):
        with pytest.raises(ValidationError):
            IntakeChannel(**_valid_channel(supported_schema_versions=[]))


# ---------------------------------------------------------------------------
# rate_limit_per_minute validation
# ---------------------------------------------------------------------------


class TestRateLimitValidation:
    """rate_limit_per_minute > 0 when set."""

    def test_zero_rejected(self):
        with pytest.raises(ValidationError, match="rate_limit_per_minute"):
            IntakeChannel(**_valid_channel(rate_limit_per_minute=0))

    def test_negative_rejected(self):
        with pytest.raises(ValidationError, match="rate_limit_per_minute"):
            IntakeChannel(**_valid_channel(rate_limit_per_minute=-5))

    def test_none_accepted(self):
        ch = IntakeChannel(**_valid_channel(rate_limit_per_minute=None))
        assert ch.rate_limit_per_minute is None

    def test_positive_accepted(self):
        ch = IntakeChannel(**_valid_channel(rate_limit_per_minute=1))
        assert ch.rate_limit_per_minute == 1
