"""
Unit tests for the disruption confidence scoring utility.

Tests cover:
- compute_confidence_score weighted formula correctness
- Freshness factor computation (1.0 for fresh, 0.0 for stale)
- Entity factor computation (1.0 for 1 entity, 0.0 for 100+)
- Output clamped to [0.0, 1.0]
- Confidence rationale is a non-empty list of strings
- Edge cases: zero inputs, maximum inputs, boundary values
- Risk class override: confidence < 0.5 → HIGH risk

Requirements: 17.1, 17.2, 17.3, 17.4
"""
import pytest

from Agents.overlay.confidence_utils import (
    ENTITY_DECAY_COUNT,
    FRESHNESS_DECAY_SECONDS,
    WEIGHT_ENTITY,
    WEIGHT_FRESHNESS,
    WEIGHT_HISTORY,
    WEIGHT_SIGNAL,
    compute_confidence_score,
)
from Agents.overlay.data_contracts import RiskClass


# ---------------------------------------------------------------------------
# Tests: Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_weight_signal(self):
        assert WEIGHT_SIGNAL == 0.4

    def test_weight_history(self):
        assert WEIGHT_HISTORY == 0.3

    def test_weight_freshness(self):
        assert WEIGHT_FRESHNESS == 0.2

    def test_weight_entity(self):
        assert WEIGHT_ENTITY == 0.1

    def test_weights_sum_to_one(self):
        total = WEIGHT_SIGNAL + WEIGHT_HISTORY + WEIGHT_FRESHNESS + WEIGHT_ENTITY
        assert total == pytest.approx(1.0)

    def test_freshness_decay_seconds(self):
        assert FRESHNESS_DECAY_SECONDS == 3600

    def test_entity_decay_count(self):
        assert ENTITY_DECAY_COUNT == 100


# ---------------------------------------------------------------------------
# Tests: compute_confidence_score — basic formula
# ---------------------------------------------------------------------------


class TestComputeConfidenceScore:
    def test_all_perfect_inputs(self):
        """All factors at maximum → score should be 1.0."""
        score, rationale = compute_confidence_score(
            signal_confidence=1.0,
            historical_success_rate=1.0,
            data_freshness_seconds=0.0,
            affected_entity_count=0,
        )
        # 0.4*1 + 0.3*1 + 0.2*1 + 0.1*1 = 1.0
        assert score == pytest.approx(1.0)
        assert isinstance(rationale, list)
        assert len(rationale) > 0

    def test_all_zero_inputs(self):
        """All factors at minimum → score should be 0.1 (entity_factor=1 for 0 entities)."""
        score, rationale = compute_confidence_score(
            signal_confidence=0.0,
            historical_success_rate=0.0,
            data_freshness_seconds=3600.0,
            affected_entity_count=100,
        )
        # 0.4*0 + 0.3*0 + 0.2*0 + 0.1*0 = 0.0
        assert score == pytest.approx(0.0)
        assert len(rationale) > 0

    def test_weighted_formula(self):
        """Verify the exact weighted formula."""
        score, _ = compute_confidence_score(
            signal_confidence=0.8,
            historical_success_rate=0.6,
            data_freshness_seconds=1800.0,  # 30 min → freshness=0.5
            affected_entity_count=50,  # → entity_factor=0.5
        )
        expected = 0.4 * 0.8 + 0.3 * 0.6 + 0.2 * 0.5 + 0.1 * 0.5
        assert score == pytest.approx(expected)

    def test_freshness_factor_fresh_data(self):
        """0 seconds → freshness_factor = 1.0."""
        score, _ = compute_confidence_score(
            signal_confidence=0.0,
            historical_success_rate=0.0,
            data_freshness_seconds=0.0,
            affected_entity_count=100,
        )
        # Only freshness contributes: 0.2 * 1.0 = 0.2
        assert score == pytest.approx(0.2)

    def test_freshness_factor_stale_data(self):
        """3600 seconds → freshness_factor = 0.0."""
        score, _ = compute_confidence_score(
            signal_confidence=0.0,
            historical_success_rate=0.0,
            data_freshness_seconds=3600.0,
            affected_entity_count=100,
        )
        assert score == pytest.approx(0.0)

    def test_freshness_factor_half_hour(self):
        """1800 seconds → freshness_factor = 0.5."""
        score, _ = compute_confidence_score(
            signal_confidence=0.0,
            historical_success_rate=0.0,
            data_freshness_seconds=1800.0,
            affected_entity_count=100,
        )
        # 0.2 * 0.5 = 0.1
        assert score == pytest.approx(0.1)

    def test_freshness_factor_beyond_one_hour(self):
        """Beyond 3600 seconds → freshness_factor clamped to 0.0."""
        score, _ = compute_confidence_score(
            signal_confidence=0.0,
            historical_success_rate=0.0,
            data_freshness_seconds=7200.0,
            affected_entity_count=100,
        )
        assert score == pytest.approx(0.0)

    def test_entity_factor_single_entity(self):
        """1 entity → entity_factor = 0.99."""
        score, _ = compute_confidence_score(
            signal_confidence=0.0,
            historical_success_rate=0.0,
            data_freshness_seconds=3600.0,
            affected_entity_count=1,
        )
        # 0.1 * 0.99 = 0.099
        assert score == pytest.approx(0.1 * 0.99)

    def test_entity_factor_hundred_entities(self):
        """100 entities → entity_factor = 0.0."""
        score, _ = compute_confidence_score(
            signal_confidence=0.0,
            historical_success_rate=0.0,
            data_freshness_seconds=3600.0,
            affected_entity_count=100,
        )
        assert score == pytest.approx(0.0)

    def test_entity_factor_beyond_hundred(self):
        """Beyond 100 entities → entity_factor clamped to 0.0."""
        score, _ = compute_confidence_score(
            signal_confidence=0.0,
            historical_success_rate=0.0,
            data_freshness_seconds=3600.0,
            affected_entity_count=200,
        )
        assert score == pytest.approx(0.0)

    def test_entity_factor_zero_entities(self):
        """0 entities → entity_factor = 1.0."""
        score, _ = compute_confidence_score(
            signal_confidence=0.0,
            historical_success_rate=0.0,
            data_freshness_seconds=3600.0,
            affected_entity_count=0,
        )
        # 0.1 * 1.0 = 0.1
        assert score == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Tests: Output clamping
# ---------------------------------------------------------------------------


class TestOutputClamping:
    def test_score_never_exceeds_one(self):
        """Even with inputs > 1.0, score is clamped to 1.0."""
        score, _ = compute_confidence_score(
            signal_confidence=1.5,
            historical_success_rate=1.5,
            data_freshness_seconds=0.0,
            affected_entity_count=0,
        )
        assert score <= 1.0

    def test_score_never_below_zero(self):
        """Even with negative inputs, score is clamped to 0.0."""
        score, _ = compute_confidence_score(
            signal_confidence=-1.0,
            historical_success_rate=-1.0,
            data_freshness_seconds=99999.0,
            affected_entity_count=9999,
        )
        assert score >= 0.0

    def test_score_is_float(self):
        score, _ = compute_confidence_score(
            signal_confidence=0.5,
            historical_success_rate=0.5,
            data_freshness_seconds=0.0,
            affected_entity_count=0,
        )
        assert isinstance(score, float)


# ---------------------------------------------------------------------------
# Tests: Confidence rationale
# ---------------------------------------------------------------------------


class TestConfidenceRationale:
    def test_rationale_is_list_of_strings(self):
        _, rationale = compute_confidence_score(
            signal_confidence=0.8,
            historical_success_rate=0.6,
            data_freshness_seconds=300.0,
            affected_entity_count=5,
        )
        assert isinstance(rationale, list)
        assert all(isinstance(r, str) for r in rationale)

    def test_rationale_is_non_empty(self):
        _, rationale = compute_confidence_score(
            signal_confidence=0.5,
            historical_success_rate=0.5,
            data_freshness_seconds=0.0,
            affected_entity_count=1,
        )
        assert len(rationale) >= 4  # At least one per factor + total

    def test_rationale_mentions_signal_confidence(self):
        _, rationale = compute_confidence_score(
            signal_confidence=0.8,
            historical_success_rate=0.5,
            data_freshness_seconds=0.0,
            affected_entity_count=1,
        )
        assert any("signal_confidence" in r for r in rationale)

    def test_rationale_mentions_historical_success_rate(self):
        _, rationale = compute_confidence_score(
            signal_confidence=0.5,
            historical_success_rate=0.7,
            data_freshness_seconds=0.0,
            affected_entity_count=1,
        )
        assert any("historical_success_rate" in r for r in rationale)

    def test_rationale_mentions_freshness(self):
        _, rationale = compute_confidence_score(
            signal_confidence=0.5,
            historical_success_rate=0.5,
            data_freshness_seconds=600.0,
            affected_entity_count=1,
        )
        assert any("freshness" in r for r in rationale)

    def test_rationale_mentions_entities(self):
        _, rationale = compute_confidence_score(
            signal_confidence=0.5,
            historical_success_rate=0.5,
            data_freshness_seconds=0.0,
            affected_entity_count=10,
        )
        assert any("entities" in r or "entity" in r for r in rationale)

    def test_rationale_mentions_total_score(self):
        _, rationale = compute_confidence_score(
            signal_confidence=0.5,
            historical_success_rate=0.5,
            data_freshness_seconds=0.0,
            affected_entity_count=1,
        )
        assert any("total" in r for r in rationale)


# ---------------------------------------------------------------------------
# Tests: Risk class override (Req 17.3)
# ---------------------------------------------------------------------------


class TestRiskClassOverride:
    """Tests for the risk_class override behavior when confidence < 0.5.

    The compute_confidence_score function itself doesn't set risk_class,
    but the integration in overlay agents does. These tests verify the
    score thresholds that trigger the override.
    """

    def test_low_confidence_score_below_threshold(self):
        """Score < 0.5 should trigger HIGH risk override in agents."""
        score, _ = compute_confidence_score(
            signal_confidence=0.1,
            historical_success_rate=0.1,
            data_freshness_seconds=3000.0,
            affected_entity_count=80,
        )
        assert score < 0.5

    def test_high_confidence_score_above_threshold(self):
        """Score >= 0.5 should not trigger risk override."""
        score, _ = compute_confidence_score(
            signal_confidence=0.8,
            historical_success_rate=0.7,
            data_freshness_seconds=300.0,
            affected_entity_count=5,
        )
        assert score >= 0.5

    def test_boundary_at_half(self):
        """Test score near the 0.5 boundary."""
        # 0.4*0.5 + 0.3*0.5 + 0.2*0.5 + 0.1*0.5 = 0.5
        score, _ = compute_confidence_score(
            signal_confidence=0.5,
            historical_success_rate=0.5,
            data_freshness_seconds=1800.0,  # freshness=0.5
            affected_entity_count=50,  # entity=0.5
        )
        assert score == pytest.approx(0.5)
