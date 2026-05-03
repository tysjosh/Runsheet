"""
Disruption Confidence Scoring utility.

Computes a weighted confidence score for replan InterventionProposals
based on signal confidence, historical success rate, data freshness,
and affected entity count.

Weighted formula:
    0.4 * signal_confidence
  + 0.3 * historical_success_rate
  + 0.2 * freshness_factor
  + 0.1 * entity_factor

Where:
    freshness_factor = max(0, 1 - (data_freshness_seconds / 3600))
    entity_factor    = max(0, 1 - (affected_entity_count / 100))

Validates: Requirements 17.1, 17.2, 17.3, 17.4
"""

from typing import List, Tuple

# Weight constants
WEIGHT_SIGNAL = 0.4
WEIGHT_HISTORY = 0.3
WEIGHT_FRESHNESS = 0.2
WEIGHT_ENTITY = 0.1

# Freshness decays to 0 over 1 hour (3600 seconds)
FRESHNESS_DECAY_SECONDS = 3600

# Entity factor decays to 0 at 100 entities
ENTITY_DECAY_COUNT = 100


def compute_confidence_score(
    signal_confidence: float,
    historical_success_rate: float,
    data_freshness_seconds: float,
    affected_entity_count: int,
) -> Tuple[float, List[str]]:
    """Compute a weighted confidence score for a replan proposal.

    Args:
        signal_confidence: Confidence of the triggering RiskSignal (0.0–1.0).
        historical_success_rate: Success rate of similar past interventions (0.0–1.0).
        data_freshness_seconds: Age of the data in seconds (0 = fresh, 3600+ = stale).
        affected_entity_count: Number of entities affected by the disruption.

    Returns:
        A tuple of (confidence_score, confidence_rationale) where:
        - confidence_score is a float clamped to [0.0, 1.0]
        - confidence_rationale is a list of strings explaining each factor
    """
    # Clamp inputs to valid ranges
    signal = max(0.0, min(1.0, signal_confidence))
    history = max(0.0, min(1.0, historical_success_rate))
    freshness_seconds = max(0.0, data_freshness_seconds)
    entity_count = max(0, affected_entity_count)

    # Compute derived factors
    freshness_factor = max(0.0, 1.0 - (freshness_seconds / FRESHNESS_DECAY_SECONDS))
    entity_factor = max(0.0, 1.0 - (entity_count / ENTITY_DECAY_COUNT))

    # Weighted combination
    raw_score = (
        WEIGHT_SIGNAL * signal
        + WEIGHT_HISTORY * history
        + WEIGHT_FRESHNESS * freshness_factor
        + WEIGHT_ENTITY * entity_factor
    )

    # Clamp to [0.0, 1.0]
    confidence_score = max(0.0, min(1.0, raw_score))

    # Build rationale
    rationale: List[str] = [
        f"signal_confidence={signal:.2f} (weight={WEIGHT_SIGNAL}, contribution={WEIGHT_SIGNAL * signal:.3f})",
        f"historical_success_rate={history:.2f} (weight={WEIGHT_HISTORY}, contribution={WEIGHT_HISTORY * history:.3f})",
        f"data_freshness={freshness_seconds:.0f}s → freshness_factor={freshness_factor:.2f} (weight={WEIGHT_FRESHNESS}, contribution={WEIGHT_FRESHNESS * freshness_factor:.3f})",
        f"affected_entities={entity_count} → entity_factor={entity_factor:.2f} (weight={WEIGHT_ENTITY}, contribution={WEIGHT_ENTITY * entity_factor:.3f})",
        f"total_confidence_score={confidence_score:.3f}",
    ]

    return confidence_score, rationale
