"""
Integration tests for Dinee voice bootstrap registration + startup self-check.

Covers task 10.2 of the dinee-voice-integration spec. Two independent
guarantees are asserted:

(1) **Bootstrap registration.** After running the fuel bootstrap
    ``initialize``, the constructed ``OrderIntakePipeline`` resolves a
    ``VoiceIntakeAdapter`` for ``(channel_type="voice", schema_version="1.0")``
    through its adapter registry, and a ``VoiceReviewHoldHook`` is among the
    pipeline's registered hooks. This is what makes voice submissions
    dispatched through the bridge (Surface A) resolve to a real adapter and
    get promoted to ``on_hold`` when human review is required.
    (Requirements 1.1, 1.3.)

(2) **Startup self-check (fail-closed).** ``run_intake_vector_self_check()``
    passes on the shipped placeholder fixture and raises ``IntakeVectorError``
    when pointed at a deliberately-inconsistent fixture, so a vector mismatch
    fails startup and the voice submission endpoint is never served. This
    reuses the approach from ``tests/unit/test_intake_vectors_self_check.py``.
    (Requirements 3.5, 3.6.)

Validates: Requirements 1.3, 3.5, 3.6
"""
from __future__ import annotations

import base64
import json
import sys
from unittest.mock import MagicMock

import pytest

from bootstrap.container import ServiceContainer
from fuel.intake.voice_intake_adapter import VoiceIntakeAdapter
from fuel.voice.intake_vectors import (
    IntakeVectorError,
    load_intake_vectors,
    run_intake_vector_self_check,
)
from fuel.voice.voice_review_hold_hook import VoiceReviewHoldHook
from ops.webhooks.hmac_util import compute_hmac_sha256_hex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def container() -> ServiceContainer:
    """A minimal container with a mocked es_service + settings.

    The fuel bootstrap degrades gracefully around optional dependencies
    (idempotency, feature flags, poison queue, credentials vault, intake
    channel repo) via ``container.has(...)``, so a bare container is enough
    to exercise the adapter/hook registration path against a mock ES.
    """
    c = ServiceContainer()
    c.settings = MagicMock()
    c.es_service = MagicMock()
    return c


@pytest.fixture
def mock_app() -> MagicMock:
    return MagicMock()


@pytest.fixture(autouse=True)
def _reset_bootstrap_fuel_module():
    """Ensure a fresh import of bootstrap.fuel per test (module holds state)."""
    saved = sys.modules.pop("bootstrap.fuel", None)
    yield
    sys.modules.pop("bootstrap.fuel", None)
    if saved is not None:
        sys.modules["bootstrap.fuel"] = saved


def _write_fixture(path, vectors) -> str:
    path.write_text(json.dumps({"vectors": vectors}), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# (1) Bootstrap registration of the voice adapter + review-hold hook.
# ---------------------------------------------------------------------------


class TestVoiceBootstrapRegistration:
    """Requirements 1.1/1.3: adapter + hook registered at fuel bootstrap."""

    @pytest.mark.asyncio
    async def test_voice_adapter_resolves_for_voice_1_0(self, mock_app, container):
        from bootstrap.fuel import initialize

        await initialize(mock_app, container)

        assert container.has("order_intake_pipeline")
        pipeline = container.order_intake_pipeline
        assert pipeline is not None, "OrderIntakePipeline was not constructed"

        # The registry resolves a voice adapter for (voice, 1.0).
        adapter = pipeline._adapter_registry.get("voice", "1.0")
        assert isinstance(adapter, VoiceIntakeAdapter)
        assert adapter.channel_type == "voice"

    @pytest.mark.asyncio
    async def test_voice_review_hold_hook_registered(self, mock_app, container):
        from bootstrap.fuel import initialize

        await initialize(mock_app, container)

        pipeline = container.order_intake_pipeline
        assert pipeline is not None

        hold_hooks = [h for h in pipeline._hooks if isinstance(h, VoiceReviewHoldHook)]
        assert len(hold_hooks) == 1, (
            "expected exactly one VoiceReviewHoldHook among the pipeline hooks, "
            f"found {len(hold_hooks)} in {[type(h).__name__ for h in pipeline._hooks]}"
        )


# ---------------------------------------------------------------------------
# (2) Startup self-check — passes on shipped fixture, fails closed otherwise.
# ---------------------------------------------------------------------------


class TestStartupSelfCheck:
    """Requirements 3.5/3.6: fail-closed intake-vector self-check."""

    def test_self_check_passes_on_shipped_placeholder_fixture(self):
        verified = run_intake_vector_self_check()

        # Every shipped vector verified cleanly.
        assert verified >= 1
        assert verified == len(load_intake_vectors())

    def test_self_check_raises_on_deliberately_inconsistent_fixture(self, tmp_path):
        # A structurally valid vector whose recorded signature is deliberately
        # wrong — the recomputed digest cannot match it, so startup fails closed.
        bad_vector = {
            "name": "deliberately-inconsistent",
            "secret": "shared-secret",
            "body_base64": base64.b64encode(b'{"callId":"abc"}').decode("ascii"),
            "signature": "f" * 64,
        }
        fixture_path = _write_fixture(tmp_path / "intakeVectors.json", [bad_vector])

        with pytest.raises(IntakeVectorError) as exc_info:
            run_intake_vector_self_check(fixture_path)

        message = str(exc_info.value)
        assert "deliberately-inconsistent" in message
        assert "mismatch" in message

    def test_self_check_raises_when_a_consistent_vector_is_tampered(self, tmp_path):
        # Genuinely self-consistent vector, then tamper the body so the recorded
        # signature no longer matches — confirms byte-level agreement is enforced.
        secret = "shared-secret"
        good_signature = compute_hmac_sha256_hex(secret, b'{"callId":"original"}')

        tampered_vector = {
            "name": "tampered-body",
            "secret": secret,
            "body_base64": base64.b64encode(b'{"callId":"tampered"}').decode("ascii"),
            "signature": good_signature,  # matches original, not the tampered body
        }
        fixture_path = _write_fixture(tmp_path / "intakeVectors.json", [tampered_vector])

        with pytest.raises(IntakeVectorError):
            run_intake_vector_self_check(fixture_path)
