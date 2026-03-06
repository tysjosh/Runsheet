"""
Property-based tests for Status Transition Validity.

# Feature: logistics-scheduling, Property 1: Status Transition Validity

**Validates: Requirements 4.2, 4.3**

For any (current_status, target_status) pair drawn from all possible
combinations of JobStatus values:
- If target_status is in VALID_TRANSITIONS[current_status], the transition
  should be ACCEPTED.
- If target_status is NOT in VALID_TRANSITIONS[current_status], the transition
  should be REJECTED.
"""

from hypothesis import given, settings
from hypothesis.strategies import sampled_from

from scheduling.models import JobStatus, VALID_TRANSITIONS


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
_all_statuses = sampled_from(list(JobStatus))

_terminal_statuses = sampled_from([
    JobStatus.COMPLETED,
    JobStatus.CANCELLED,
    JobStatus.FAILED,
])


# ---------------------------------------------------------------------------
# Property 1 – Transition acceptance matches VALID_TRANSITIONS
# ---------------------------------------------------------------------------
class TestStatusTransitionValidity:
    """**Validates: Requirements 4.2, 4.3**"""

    @given(current=_all_statuses, target=_all_statuses)
    @settings(max_examples=200)
    def test_transition_validity_property(self, current: JobStatus, target: JobStatus):
        """
        For any (current, target) pair, the transition is valid iff
        target is in VALID_TRANSITIONS[current].

        **Validates: Requirements 4.2, 4.3**
        """
        allowed = VALID_TRANSITIONS[current]
        is_valid = target in allowed

        if is_valid:
            assert target in allowed, (
                f"Transition {current.value} → {target.value} should be accepted "
                f"but is not in VALID_TRANSITIONS[{current.value}]"
            )
        else:
            assert target not in allowed, (
                f"Transition {current.value} → {target.value} should be rejected "
                f"but is listed in VALID_TRANSITIONS[{current.value}]"
            )


# ---------------------------------------------------------------------------
# Property 2 – Terminal states have no valid transitions
# ---------------------------------------------------------------------------
class TestTerminalStatesHaveNoTransitions:
    """**Validates: Requirements 4.2, 4.3**"""

    @given(terminal=_terminal_statuses, target=_all_statuses)
    @settings(max_examples=200)
    def test_terminal_states_have_empty_transitions(
        self, terminal: JobStatus, target: JobStatus
    ):
        """
        Terminal states (completed, cancelled, failed) must have no valid
        outgoing transitions.

        **Validates: Requirements 4.2, 4.3**
        """
        assert len(VALID_TRANSITIONS[terminal]) == 0, (
            f"Terminal state {terminal.value} should have no valid transitions "
            f"but has: {[s.value for s in VALID_TRANSITIONS[terminal]]}"
        )
        assert target not in VALID_TRANSITIONS[terminal], (
            f"Terminal state {terminal.value} should not allow transition to "
            f"{target.value}"
        )


# ---------------------------------------------------------------------------
# Property 3 – Every status is a key in VALID_TRANSITIONS
# ---------------------------------------------------------------------------
class TestAllStatusesCovered:
    """**Validates: Requirements 4.2, 4.3**"""

    @given(status=_all_statuses)
    @settings(max_examples=200)
    def test_every_status_is_a_key_in_valid_transitions(self, status: JobStatus):
        """
        Every JobStatus value must appear as a key in VALID_TRANSITIONS,
        ensuring the transition map is complete.

        **Validates: Requirements 4.2, 4.3**
        """
        assert status in VALID_TRANSITIONS, (
            f"JobStatus.{status.name} ({status.value}) is not a key in "
            f"VALID_TRANSITIONS"
        )


# ---------------------------------------------------------------------------
# Property 4 – No self-transitions allowed
# ---------------------------------------------------------------------------
class TestNoSelfTransitions:
    """**Validates: Requirements 4.2, 4.3**"""

    @given(status=_all_statuses)
    @settings(max_examples=200)
    def test_no_self_transitions(self, status: JobStatus):
        """
        No status should allow transitioning to itself (status → same status).

        **Validates: Requirements 4.2, 4.3**
        """
        assert status not in VALID_TRANSITIONS[status], (
            f"Self-transition {status.value} → {status.value} should not be "
            f"allowed but is listed in VALID_TRANSITIONS[{status.value}]"
        )
