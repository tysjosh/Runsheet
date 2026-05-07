"""
Unit tests for ``fuel.services.compatibility_matrix``.

Covers Capability 7 / Requirements 7.2.1 and 7.2.4:

* The default rule table contains the design-document entries (GASOLINE_REG ↔
  PREM allowed, DIESEL_2 ↔ OFF_ROAD_DIESEL allowed, HEATING_OIL → GASOLINE_*
  blocked, PROPANE with any non-PROPANE requires_cleaning, DEF strict against
  any non-DEF).
* ``check_compatibility`` returns the ``{decision, reason, governing_rule}``
  shape with the expected values for each rule class.
* Canonicalization flows through both product codes so legacy NG aliases
  (``PMS``, ``AGO``, ``LPG``, ``ATK``) resolve correctly.
* A ``requires_cleaning`` rule downgrades to ``allowed`` when
  ``last_cleaned_at > last_loaded_at`` and stays ``requires_cleaning`` otherwise
  (Req 7.2.4).
* ``load_tenant_compatibility_rules`` merges tenant overrides from the Redis
  key ``compatibility_matrix_config:{tenant_id}`` on top of the defaults and
  degrades gracefully on missing / malformed payloads.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from fuel.services.compatibility_matrix import (
    DECISION_ALLOWED,
    DECISION_BLOCKED,
    DECISION_REQUIRES_CLEANING,
    DEFAULT_COMPATIBILITY_RULES,
    REASON_CLEANING_REQUIRED,
    REASON_CROSS_CONTAMINATION_BLOCKED,
    REDIS_KEY_TENANT_PREFIX,
    RULE_ALLOWED,
    RULE_BLOCKED,
    RULE_REQUIRES_CLEANING,
    VALID_RULES,
    check_compatibility,
    load_tenant_compatibility_rules,
    parse_rule_overrides,
)
from fuel.services.fuel_product_catalog import UnknownFuelProductError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(
    *,
    last_loaded_at: Optional[datetime] = None,
    last_cleaned_at: Optional[datetime] = None,
) -> SimpleNamespace:
    """Construct a minimal compartment-state duck type for the engine."""

    return SimpleNamespace(
        last_loaded_at=last_loaded_at,
        last_cleaned_at=last_cleaned_at,
    )


def _empty_state() -> SimpleNamespace:
    return _state()


class _FakeTenantConfig:
    """Async Redis-handle stand-in; matches the agent-side contract."""

    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        self.data: Dict[str, Any] = dict(data or {})
        self.calls: list[str] = []

    async def get(self, key: str) -> Any:
        self.calls.append(key)
        return self.data.get(key)


class _RaisingTenantConfig:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def get(self, key: str) -> Any:  # pragma: no cover - exercised via test
        raise self._exc


NOW = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Default matrix shape (Req 7.2.1)
# ---------------------------------------------------------------------------


class TestDefaultMatrix:
    def test_gasoline_reg_prem_mutually_allowed(self) -> None:
        assert DEFAULT_COMPATIBILITY_RULES[("GASOLINE_REG", "GASOLINE_PREM")] == RULE_ALLOWED
        assert DEFAULT_COMPATIBILITY_RULES[("GASOLINE_PREM", "GASOLINE_REG")] == RULE_ALLOWED

    def test_diesel_off_road_mutually_allowed(self) -> None:
        assert DEFAULT_COMPATIBILITY_RULES[("DIESEL_2", "OFF_ROAD_DIESEL")] == RULE_ALLOWED
        assert DEFAULT_COMPATIBILITY_RULES[("OFF_ROAD_DIESEL", "DIESEL_2")] == RULE_ALLOWED

    @pytest.mark.parametrize("gasoline", ["GASOLINE_REG", "GASOLINE_PREM"])
    def test_heating_oil_blocks_gasoline(self, gasoline: str) -> None:
        assert DEFAULT_COMPATIBILITY_RULES[("HEATING_OIL", gasoline)] == RULE_BLOCKED

    @pytest.mark.parametrize(
        "other",
        [
            "GASOLINE_REG",
            "GASOLINE_PREM",
            "DIESEL_2",
            "OFF_ROAD_DIESEL",
            "HEATING_OIL",
            "KEROSENE",
            "ETHANOL_E85",
        ],
    )
    def test_propane_requires_cleaning_with_non_propane(self, other: str) -> None:
        assert (
            DEFAULT_COMPATIBILITY_RULES[("PROPANE", other)] == RULE_REQUIRES_CLEANING
        )

    def test_propane_self_allowed(self) -> None:
        assert DEFAULT_COMPATIBILITY_RULES[("PROPANE", "PROPANE")] == RULE_ALLOWED

    def test_propane_with_def_blocked_both_directions(self) -> None:
        """DEF is absolutely isolated; propane↔def is blocked either way."""

        assert DEFAULT_COMPATIBILITY_RULES[("PROPANE", "DEF")] == RULE_BLOCKED
        assert DEFAULT_COMPATIBILITY_RULES[("DEF", "PROPANE")] == RULE_BLOCKED

    @pytest.mark.parametrize(
        "other",
        [
            "GASOLINE_REG",
            "GASOLINE_PREM",
            "DIESEL_2",
            "OFF_ROAD_DIESEL",
            "HEATING_OIL",
            "KEROSENE",
            "PROPANE",
            "ETHANOL_E85",
        ],
    )
    def test_def_blocks_every_non_def(self, other: str) -> None:
        assert DEFAULT_COMPATIBILITY_RULES[("DEF", other)] == RULE_BLOCKED
        assert DEFAULT_COMPATIBILITY_RULES[(other, "DEF")] == RULE_BLOCKED

    def test_def_self_allowed(self) -> None:
        assert DEFAULT_COMPATIBILITY_RULES[("DEF", "DEF")] == RULE_ALLOWED

    def test_every_default_rule_value_is_valid(self) -> None:
        for pair, rule in DEFAULT_COMPATIBILITY_RULES.items():
            assert rule in VALID_RULES, (pair, rule)

    def test_matrix_covers_nine_catalog_products_in_both_axes(self) -> None:
        products = {
            "DIESEL_2",
            "HEATING_OIL",
            "GASOLINE_REG",
            "GASOLINE_PREM",
            "PROPANE",
            "KEROSENE",
            "OFF_ROAD_DIESEL",
            "DEF",
            "ETHANOL_E85",
        }
        from_codes = {p for p, _ in DEFAULT_COMPATIBILITY_RULES}
        to_codes = {p for _, p in DEFAULT_COMPATIBILITY_RULES}
        assert from_codes == products
        assert to_codes == products
        # Complete matrix means all 9×9 = 81 entries are present.
        assert len(DEFAULT_COMPATIBILITY_RULES) == 81


# ---------------------------------------------------------------------------
# check_compatibility — core decisions
# ---------------------------------------------------------------------------


class TestCheckCompatibilityAllowedRule:
    def test_allowed_rule_returns_allowed_decision(self) -> None:
        decision = check_compatibility(
            "GASOLINE_REG", "GASOLINE_PREM", _empty_state()
        )
        assert decision == {
            "decision": DECISION_ALLOWED,
            "reason": None,
            "governing_rule": RULE_ALLOWED,
        }

    def test_same_product_short_circuits_allowed(self) -> None:
        decision = check_compatibility("DIESEL_2", "DIESEL_2", _empty_state())
        assert decision["decision"] == DECISION_ALLOWED
        assert decision["reason"] is None

    def test_empty_previous_is_allowed_for_any_next(self) -> None:
        for prev in (None, "", "   "):
            decision = check_compatibility(prev, "PROPANE", _empty_state())
            assert decision == {
                "decision": DECISION_ALLOWED,
                "reason": None,
                "governing_rule": RULE_ALLOWED,
            }

    def test_none_compartment_state_is_tolerated(self) -> None:
        decision = check_compatibility("DIESEL_2", "DIESEL_2", None)
        assert decision["decision"] == DECISION_ALLOWED


class TestCheckCompatibilityBlockedRule:
    @pytest.mark.parametrize(
        "prev,nxt",
        [
            ("HEATING_OIL", "GASOLINE_REG"),
            ("HEATING_OIL", "GASOLINE_PREM"),
            ("DEF", "DIESEL_2"),
            ("GASOLINE_REG", "DEF"),
        ],
    )
    def test_blocked_rule_surfaces_cross_contamination_reason(
        self, prev: str, nxt: str
    ) -> None:
        decision = check_compatibility(prev, nxt, _empty_state())
        assert decision == {
            "decision": DECISION_BLOCKED,
            "reason": REASON_CROSS_CONTAMINATION_BLOCKED,
            "governing_rule": RULE_BLOCKED,
        }

    def test_cleaning_does_not_unblock_a_blocked_rule(self) -> None:
        # Even a freshly-cleaned compartment cannot load gasoline after
        # heating oil — that pairing is blocked regardless of cleaning.
        state = _state(
            last_loaded_at=NOW - timedelta(days=1),
            last_cleaned_at=NOW,
        )
        decision = check_compatibility("HEATING_OIL", "GASOLINE_REG", state)
        assert decision["decision"] == DECISION_BLOCKED
        assert decision["reason"] == REASON_CROSS_CONTAMINATION_BLOCKED


class TestCheckCompatibilityRequiresCleaning:
    def test_stale_cleaning_returns_requires_cleaning(self) -> None:
        # Cleaning is older than the last load → stale → gate remains.
        state = _state(
            last_loaded_at=NOW,
            last_cleaned_at=NOW - timedelta(hours=6),
        )
        decision = check_compatibility("PROPANE", "DIESEL_2", state)
        assert decision == {
            "decision": DECISION_REQUIRES_CLEANING,
            "reason": REASON_CLEANING_REQUIRED,
            "governing_rule": RULE_REQUIRES_CLEANING,
        }

    def test_fresh_cleaning_downgrades_to_allowed(self) -> None:
        # Req 7.2.4: cleaning newer than last load lets the load proceed.
        state = _state(
            last_loaded_at=NOW - timedelta(days=1),
            last_cleaned_at=NOW,
        )
        decision = check_compatibility("PROPANE", "DIESEL_2", state)
        assert decision == {
            "decision": DECISION_ALLOWED,
            "reason": None,
            "governing_rule": RULE_REQUIRES_CLEANING,
        }

    def test_never_cleaned_and_loaded_requires_cleaning(self) -> None:
        state = _state(last_loaded_at=NOW, last_cleaned_at=None)
        decision = check_compatibility("GASOLINE_REG", "DIESEL_2", state)
        assert decision["decision"] == DECISION_REQUIRES_CLEANING
        assert decision["reason"] == REASON_CLEANING_REQUIRED

    def test_no_previous_load_is_allowed_under_requires_cleaning(self) -> None:
        # The compartment has no record of ever being loaded, so there is
        # nothing to clean up from. Allowed.
        state = _state(last_loaded_at=None, last_cleaned_at=None)
        decision = check_compatibility("GASOLINE_REG", "DIESEL_2", state)
        assert decision["decision"] == DECISION_ALLOWED

    def test_incomparable_timestamps_treated_as_stale(self) -> None:
        # If the timestamps are on different tz footings, the comparison may
        # raise — the engine should conservatively surface requires_cleaning.
        naive = datetime(2024, 1, 15, 11, 0, 0)
        aware = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        state = _state(last_loaded_at=naive, last_cleaned_at=aware)
        decision = check_compatibility("GASOLINE_REG", "DIESEL_2", state)
        assert decision["decision"] == DECISION_REQUIRES_CLEANING


# ---------------------------------------------------------------------------
# check_compatibility — canonicalization and determinism (Req 7.2.7)
# ---------------------------------------------------------------------------


class TestCanonicalization:
    def test_legacy_aliases_are_canonicalized(self) -> None:
        # PMS → GASOLINE_REG, AGO → DIESEL_2. The pair should still resolve
        # to the "gasoline → diesel requires_cleaning" cell.
        state = _state(last_loaded_at=NOW, last_cleaned_at=None)
        decision = check_compatibility("PMS", "AGO", state)
        assert decision["decision"] == DECISION_REQUIRES_CLEANING

    def test_case_and_whitespace_insensitive(self) -> None:
        decision = check_compatibility("  heating_oil  ", "gasoline_reg", _empty_state())
        assert decision["decision"] == DECISION_BLOCKED

    def test_unknown_product_raises(self) -> None:
        with pytest.raises(UnknownFuelProductError):
            check_compatibility("GASOLINE_REG", "NOT_A_REAL_PRODUCT", _empty_state())

    def test_non_string_next_product_raises(self) -> None:
        with pytest.raises(TypeError):
            check_compatibility("GASOLINE_REG", 123, _empty_state())  # type: ignore[arg-type]


class TestDeterminism:
    def test_identical_inputs_return_equal_decisions(self) -> None:
        state = _state(last_loaded_at=NOW, last_cleaned_at=NOW - timedelta(hours=1))
        first = check_compatibility("PROPANE", "DIESEL_2", state)
        second = check_compatibility("PROPANE", "DIESEL_2", state)
        assert first == second

    def test_returned_dict_is_independent_copy(self) -> None:
        first = check_compatibility("GASOLINE_REG", "GASOLINE_PREM", _empty_state())
        first["reason"] = "tampered"
        # A fresh call must not see the mutation.
        second = check_compatibility("GASOLINE_REG", "GASOLINE_PREM", _empty_state())
        assert second["reason"] is None


# ---------------------------------------------------------------------------
# check_compatibility — tenant overrides applied via ``rules`` arg
# ---------------------------------------------------------------------------


class TestTenantOverridesInline:
    def test_override_flipping_allowed_to_blocked(self) -> None:
        rules = dict(DEFAULT_COMPATIBILITY_RULES)
        rules[("GASOLINE_REG", "GASOLINE_PREM")] = RULE_BLOCKED
        decision = check_compatibility(
            "GASOLINE_REG", "GASOLINE_PREM", _empty_state(), rules=rules
        )
        assert decision["decision"] == DECISION_BLOCKED
        assert decision["reason"] == REASON_CROSS_CONTAMINATION_BLOCKED

    def test_unknown_pair_falls_back_to_allowed(self) -> None:
        # With an empty override set, every unlisted pair is ``allowed``.
        decision = check_compatibility(
            "GASOLINE_REG", "GASOLINE_PREM", _empty_state(), rules={}
        )
        assert decision["decision"] == DECISION_ALLOWED

    def test_same_product_cannot_be_overridden_by_tenant(self) -> None:
        # Even a malicious override cannot break the same-product invariant.
        rules = {("DIESEL_2", "DIESEL_2"): RULE_BLOCKED}
        decision = check_compatibility("DIESEL_2", "DIESEL_2", _empty_state(), rules=rules)
        assert decision["decision"] == DECISION_ALLOWED


# ---------------------------------------------------------------------------
# load_tenant_compatibility_rules
# ---------------------------------------------------------------------------


class TestLoadTenantCompatibilityRules:
    async def test_no_tenant_config_returns_defaults(self) -> None:
        result = await load_tenant_compatibility_rules("tenant-1", tenant_config=None)
        assert result == dict(DEFAULT_COMPATIBILITY_RULES)

    async def test_blank_tenant_id_returns_defaults(self) -> None:
        result = await load_tenant_compatibility_rules(
            "   ", tenant_config=_FakeTenantConfig()
        )
        assert result == dict(DEFAULT_COMPATIBILITY_RULES)

    async def test_missing_key_returns_defaults(self) -> None:
        tc = _FakeTenantConfig({})
        result = await load_tenant_compatibility_rules("tenant-1", tenant_config=tc)
        assert result == dict(DEFAULT_COMPATIBILITY_RULES)
        assert tc.calls == [f"{REDIS_KEY_TENANT_PREFIX}:tenant-1"]

    async def test_overrides_merge_on_top_of_defaults(self) -> None:
        payload = '{"GASOLINE_REG|GASOLINE_PREM": "blocked"}'
        tc = _FakeTenantConfig(
            {f"{REDIS_KEY_TENANT_PREFIX}:tenant-1": payload}
        )
        rules = await load_tenant_compatibility_rules("tenant-1", tenant_config=tc)

        # The override is applied…
        assert rules[("GASOLINE_REG", "GASOLINE_PREM")] == RULE_BLOCKED
        # …and every other entry is preserved.
        assert rules[("HEATING_OIL", "GASOLINE_REG")] == RULE_BLOCKED  # default
        assert rules[("PROPANE", "DIESEL_2")] == RULE_REQUIRES_CLEANING
        # Length still matches the default set (override replaced, did not add).
        assert len(rules) == len(DEFAULT_COMPATIBILITY_RULES)

    async def test_override_with_legacy_alias_canonicalizes(self) -> None:
        payload = '{"PMS|AGO": "blocked"}'
        tc = _FakeTenantConfig(
            {f"{REDIS_KEY_TENANT_PREFIX}:tenant-1": payload}
        )
        rules = await load_tenant_compatibility_rules("tenant-1", tenant_config=tc)
        # PMS → GASOLINE_REG, AGO → DIESEL_2
        assert rules[("GASOLINE_REG", "DIESEL_2")] == RULE_BLOCKED

    async def test_malformed_json_falls_back_to_defaults(self) -> None:
        tc = _FakeTenantConfig(
            {f"{REDIS_KEY_TENANT_PREFIX}:tenant-1": "{not: valid json"}
        )
        rules = await load_tenant_compatibility_rules("tenant-1", tenant_config=tc)
        assert rules == dict(DEFAULT_COMPATIBILITY_RULES)

    async def test_backend_failure_falls_back_to_defaults(self) -> None:
        tc = _RaisingTenantConfig(RuntimeError("redis timeout"))
        rules = await load_tenant_compatibility_rules("tenant-1", tenant_config=tc)
        assert rules == dict(DEFAULT_COMPATIBILITY_RULES)

    async def test_empty_overrides_returns_defaults(self) -> None:
        tc = _FakeTenantConfig(
            {f"{REDIS_KEY_TENANT_PREFIX}:tenant-1": "{}"}
        )
        rules = await load_tenant_compatibility_rules("tenant-1", tenant_config=tc)
        assert rules == dict(DEFAULT_COMPATIBILITY_RULES)


# ---------------------------------------------------------------------------
# parse_rule_overrides
# ---------------------------------------------------------------------------


class TestParseRuleOverrides:
    def test_none_returns_empty_dict(self) -> None:
        assert parse_rule_overrides(None) == {}

    def test_empty_string_returns_empty_dict(self) -> None:
        assert parse_rule_overrides("   ") == {}

    def test_mapping_with_pipe_separator(self) -> None:
        result = parse_rule_overrides({"DIESEL_2|DEF": "blocked"})
        assert result == {("DIESEL_2", "DEF"): RULE_BLOCKED}

    def test_mapping_with_arrow_separator(self) -> None:
        result = parse_rule_overrides({"DIESEL_2->DEF": "blocked"})
        assert result == {("DIESEL_2", "DEF"): RULE_BLOCKED}

    def test_mapping_with_unicode_arrow_separator(self) -> None:
        result = parse_rule_overrides({"DIESEL_2→DEF": "blocked"})
        assert result == {("DIESEL_2", "DEF"): RULE_BLOCKED}

    def test_list_of_entries(self) -> None:
        payload = [
            {"from": "HEATING_OIL", "to": "KEROSENE", "rule": "blocked"},
            {
                "from_product_code": "PROPANE",
                "to_product_code": "PROPANE",
                "rule_type": "allowed",
            },
        ]
        result = parse_rule_overrides(payload)
        assert result == {
            ("HEATING_OIL", "KEROSENE"): RULE_BLOCKED,
            ("PROPANE", "PROPANE"): RULE_ALLOWED,
        }

    def test_json_string_roundtrip(self) -> None:
        result = parse_rule_overrides('[{"from":"DEF","to":"DEF","rule":"allowed"}]')
        assert result == {("DEF", "DEF"): RULE_ALLOWED}

    def test_bytes_payload(self) -> None:
        result = parse_rule_overrides(b'{"DEF|DEF": "allowed"}')
        assert result == {("DEF", "DEF"): RULE_ALLOWED}

    def test_forbidden_alias_maps_to_blocked(self) -> None:
        result = parse_rule_overrides({"GASOLINE_REG|DEF": "forbidden"})
        assert result == {("GASOLINE_REG", "DEF"): RULE_BLOCKED}

    def test_invalid_rule_values_are_dropped(self) -> None:
        result = parse_rule_overrides(
            {
                "GASOLINE_REG|GASOLINE_PREM": "yes",  # invalid
                "DIESEL_2|DEF": "blocked",  # valid
            }
        )
        assert result == {("DIESEL_2", "DEF"): RULE_BLOCKED}

    def test_unknown_products_are_dropped_with_valid_entries_preserved(self) -> None:
        result = parse_rule_overrides(
            {
                "UNKNOWN|DIESEL_2": "blocked",  # unknown from
                "DIESEL_2|UNKNOWN": "blocked",  # unknown to
                "DIESEL_2|DEF": "blocked",  # valid
            }
        )
        assert result == {("DIESEL_2", "DEF"): RULE_BLOCKED}

    def test_malformed_keys_are_dropped(self) -> None:
        result = parse_rule_overrides(
            {
                "no-separator-here": "blocked",
                "|": "blocked",
                "DIESEL_2|DEF": "blocked",
            }
        )
        assert result == {("DIESEL_2", "DEF"): RULE_BLOCKED}

    def test_malformed_json_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_rule_overrides("{not: valid")

    def test_top_level_non_mapping_non_list_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_rule_overrides(42)
