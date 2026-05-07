"""
Cross-contamination Compatibility_Matrix rule engine.

Capability 7 / Requirements 7.2.1 and 7.2.4 of the fuel-ops hardening spec
require a tenant-overridable rule set that decides whether a truck compartment
holding ``previous_product`` may next load ``next_product``. This module is the
single source of truth for that decision and is consumed by:

* The ``Compartment_Loading_Agent`` before every compartment assignment (Task
  6.5).
* The ``GET /api/fuel/mvp/compartments/{id}/load-eligibility`` endpoint (Task
  6.7) which surfaces the decision and the governing rule.

The module exposes four public items:

* :data:`DEFAULT_COMPATIBILITY_RULES` — the seed rule table covering the nine
  US-catalog products (``DIESEL_2``, ``HEATING_OIL``, ``GASOLINE_REG``,
  ``GASOLINE_PREM``, ``PROPANE``, ``KEROSENE``, ``OFF_ROAD_DIESEL``, ``DEF``,
  ``ETHANOL_E85``). The table transcribes the design-document matrix verbatim
  for the eight products listed there and extends it to ``ETHANOL_E85`` using
  the gasoline-family analog.
* :func:`check_compatibility` — the pure decision function. Given the previous
  product, the proposed next product, a compartment state carrying
  ``last_loaded_at`` / ``last_cleaned_at``, and a rule table, it returns a
  structured decision with ``decision`` ∈ {``allowed``, ``blocked``,
  ``requires_cleaning``}, a machine-readable ``reason`` code, and the
  ``governing_rule`` that drove the decision.
* :func:`load_tenant_compatibility_rules` — the async helper that reads the
  tenant override from Redis key ``compatibility_matrix_config:{tenant_id}``
  and merges it over the default table. Failures (missing backend, missing
  key, malformed JSON, unknown product codes) degrade gracefully to the
  default table: safety-critical code paths cannot block on a config outage.
* :func:`parse_rule_overrides` — the JSON parsing helper used by
  ``load_tenant_compatibility_rules``. Exposed separately so the admin UI's
  override submission path can re-use the same validation logic without
  round-tripping through Redis.

Design notes:

1. **Canonicalization** — both product codes pass through
   :func:`fuel.services.fuel_product_catalog.canonicalize` so callers may pass
   either the canonical US code (``"GASOLINE_REG"``) or a legacy Nigerian alias
   (``"PMS"``). Invalid codes raise :class:`UnknownFuelProductError` so
   misconfiguration surfaces as a hard failure at the call site rather than a
   silent "allowed" default.
2. **Empty / cleaned compartment short-circuit** — a ``None`` or blank
   ``previous_product`` is treated as "fresh" and any next product is allowed.
   This is the bootstrap case for a brand-new compartment.
3. **Same-product short-circuit** — loading the same canonical product twice
   in a row is always allowed; the matrix lookup is skipped so tenant
   overrides cannot accidentally block it.
4. **Unlisted pairs** — ``rules.get((prev, next), "allowed")`` keeps the
   design-pseudocode semantics: pairs the tenant has not opted into default to
   allowed. The seed table lists every pair of the nine catalog products so
   this fallback only fires for tenant-custom products introduced outside the
   catalog.
5. **Cleaning freshness** — a ``requires_cleaning`` rule downgrades to
   ``allowed`` iff ``compartment_state.last_cleaned_at >
   compartment_state.last_loaded_at``. A missing ``last_loaded_at`` implies
   the compartment has never carried cargo, so cleaning is moot and the
   decision is ``allowed``; a missing ``last_cleaned_at`` on a loaded
   compartment fails the check and surfaces ``requires_cleaning``.
6. **Determinism (Req 7.2.7)** — two calls with identical inputs always return
   an equal dict. The function holds no hidden state and never consults a
   clock.

Validates: Requirements 7.2.1, 7.2.4.
"""
from __future__ import annotations

import json
import logging
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Tuple,
    TypedDict,
)

from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public type aliases and constants
# ---------------------------------------------------------------------------


#: Possible values returned in the ``decision`` field.
Decision = Literal["allowed", "blocked", "requires_cleaning"]

#: Possible values appearing in a rule table.
RuleType = Literal["allowed", "blocked", "requires_cleaning"]

DECISION_ALLOWED: Decision = "allowed"
DECISION_BLOCKED: Decision = "blocked"
DECISION_REQUIRES_CLEANING: Decision = "requires_cleaning"

RULE_ALLOWED: RuleType = "allowed"
RULE_BLOCKED: RuleType = "blocked"
RULE_REQUIRES_CLEANING: RuleType = "requires_cleaning"

#: Reason codes surfaced back to the Compartment_Loading_Agent. These mirror
#: the literal strings required by Requirements 7.2.2 and 7.2.3.
REASON_CROSS_CONTAMINATION_BLOCKED: str = "cross_contamination_blocked"
REASON_CLEANING_REQUIRED: str = "cleaning_required"

#: Valid rule values accepted from tenant override payloads.
VALID_RULES: frozenset[str] = frozenset(
    {RULE_ALLOWED, RULE_BLOCKED, RULE_REQUIRES_CLEANING}
)

#: Redis key prefix used by :func:`load_tenant_compatibility_rules`. The full
#: key for tenant ``t`` is ``compatibility_matrix_config:t``.
REDIS_KEY_TENANT_PREFIX: str = "compatibility_matrix_config"


# ---------------------------------------------------------------------------
# Default rule table
# ---------------------------------------------------------------------------


# Row-major transcription of the design.md "Compatibility_Matrix rule engine"
# table (Capability 7). The eight products in the design document plus
# ETHANOL_E85 — which is not in the published table and is treated as a
# gasoline-family product analogous to GASOLINE_REG for symmetry. Every pair is
# listed explicitly so the matrix is auditable at a glance and adding a new
# product fails loud rather than silently defaulting to "allowed".
_DEFAULT_MATRIX_ROWS: Tuple[Tuple[str, Tuple[Tuple[str, RuleType], ...]], ...] = (
    (
        "GASOLINE_REG",
        (
            ("GASOLINE_REG", RULE_ALLOWED),
            ("GASOLINE_PREM", RULE_ALLOWED),
            ("DIESEL_2", RULE_REQUIRES_CLEANING),
            ("OFF_ROAD_DIESEL", RULE_REQUIRES_CLEANING),
            ("HEATING_OIL", RULE_REQUIRES_CLEANING),
            ("KEROSENE", RULE_REQUIRES_CLEANING),
            ("PROPANE", RULE_BLOCKED),
            ("DEF", RULE_BLOCKED),
            ("ETHANOL_E85", RULE_ALLOWED),
        ),
    ),
    (
        "GASOLINE_PREM",
        (
            ("GASOLINE_REG", RULE_ALLOWED),
            ("GASOLINE_PREM", RULE_ALLOWED),
            ("DIESEL_2", RULE_REQUIRES_CLEANING),
            ("OFF_ROAD_DIESEL", RULE_REQUIRES_CLEANING),
            ("HEATING_OIL", RULE_REQUIRES_CLEANING),
            ("KEROSENE", RULE_REQUIRES_CLEANING),
            ("PROPANE", RULE_BLOCKED),
            ("DEF", RULE_BLOCKED),
            ("ETHANOL_E85", RULE_ALLOWED),
        ),
    ),
    (
        "DIESEL_2",
        (
            ("GASOLINE_REG", RULE_REQUIRES_CLEANING),
            ("GASOLINE_PREM", RULE_REQUIRES_CLEANING),
            ("DIESEL_2", RULE_ALLOWED),
            ("OFF_ROAD_DIESEL", RULE_ALLOWED),
            ("HEATING_OIL", RULE_ALLOWED),
            ("KEROSENE", RULE_ALLOWED),
            ("PROPANE", RULE_BLOCKED),
            ("DEF", RULE_BLOCKED),
            ("ETHANOL_E85", RULE_REQUIRES_CLEANING),
        ),
    ),
    (
        "OFF_ROAD_DIESEL",
        (
            ("GASOLINE_REG", RULE_REQUIRES_CLEANING),
            ("GASOLINE_PREM", RULE_REQUIRES_CLEANING),
            ("DIESEL_2", RULE_ALLOWED),
            ("OFF_ROAD_DIESEL", RULE_ALLOWED),
            ("HEATING_OIL", RULE_ALLOWED),
            ("KEROSENE", RULE_ALLOWED),
            ("PROPANE", RULE_BLOCKED),
            ("DEF", RULE_BLOCKED),
            ("ETHANOL_E85", RULE_REQUIRES_CLEANING),
        ),
    ),
    (
        # HEATING_OIL → GASOLINE_* is the canonical "blocked" entry from
        # Req 7.2.1: dyed heating oil leaves dye residue that contaminates
        # gasoline tanks and is a regulatory violation.
        "HEATING_OIL",
        (
            ("GASOLINE_REG", RULE_BLOCKED),
            ("GASOLINE_PREM", RULE_BLOCKED),
            ("DIESEL_2", RULE_ALLOWED),
            ("OFF_ROAD_DIESEL", RULE_ALLOWED),
            ("HEATING_OIL", RULE_ALLOWED),
            ("KEROSENE", RULE_ALLOWED),
            ("PROPANE", RULE_BLOCKED),
            ("DEF", RULE_BLOCKED),
            ("ETHANOL_E85", RULE_BLOCKED),
        ),
    ),
    (
        "KEROSENE",
        (
            ("GASOLINE_REG", RULE_ALLOWED),
            ("GASOLINE_PREM", RULE_ALLOWED),
            ("DIESEL_2", RULE_ALLOWED),
            ("OFF_ROAD_DIESEL", RULE_ALLOWED),
            ("HEATING_OIL", RULE_ALLOWED),
            ("KEROSENE", RULE_ALLOWED),
            ("PROPANE", RULE_BLOCKED),
            ("DEF", RULE_BLOCKED),
            ("ETHANOL_E85", RULE_ALLOWED),
        ),
    ),
    (
        # Req 7.2.1: PROPANE with any non-PROPANE requires_cleaning. The only
        # exception is DEF, which is absolutely isolated in either direction.
        "PROPANE",
        (
            ("GASOLINE_REG", RULE_REQUIRES_CLEANING),
            ("GASOLINE_PREM", RULE_REQUIRES_CLEANING),
            ("DIESEL_2", RULE_REQUIRES_CLEANING),
            ("OFF_ROAD_DIESEL", RULE_REQUIRES_CLEANING),
            ("HEATING_OIL", RULE_REQUIRES_CLEANING),
            ("KEROSENE", RULE_REQUIRES_CLEANING),
            ("PROPANE", RULE_ALLOWED),
            ("DEF", RULE_BLOCKED),
            ("ETHANOL_E85", RULE_REQUIRES_CLEANING),
        ),
    ),
    (
        # Req 7.2.1: DEF with any non-DEF blocked. DEF is urea in de-ionized
        # water and any hydrocarbon residue renders it non-conforming.
        "DEF",
        (
            ("GASOLINE_REG", RULE_BLOCKED),
            ("GASOLINE_PREM", RULE_BLOCKED),
            ("DIESEL_2", RULE_BLOCKED),
            ("OFF_ROAD_DIESEL", RULE_BLOCKED),
            ("HEATING_OIL", RULE_BLOCKED),
            ("KEROSENE", RULE_BLOCKED),
            ("PROPANE", RULE_BLOCKED),
            ("DEF", RULE_ALLOWED),
            ("ETHANOL_E85", RULE_BLOCKED),
        ),
    ),
    (
        # ETHANOL_E85 is in the gasoline family; it interchanges with
        # GASOLINE_* freely, requires cleaning before diesels and kerosene,
        # and is blocked against HEATING_OIL / PROPANE / DEF.
        "ETHANOL_E85",
        (
            ("GASOLINE_REG", RULE_ALLOWED),
            ("GASOLINE_PREM", RULE_ALLOWED),
            ("DIESEL_2", RULE_REQUIRES_CLEANING),
            ("OFF_ROAD_DIESEL", RULE_REQUIRES_CLEANING),
            ("HEATING_OIL", RULE_REQUIRES_CLEANING),
            ("KEROSENE", RULE_REQUIRES_CLEANING),
            ("PROPANE", RULE_BLOCKED),
            ("DEF", RULE_BLOCKED),
            ("ETHANOL_E85", RULE_ALLOWED),
        ),
    ),
)


def _build_default_rules() -> Dict[Tuple[str, str], RuleType]:
    """Flatten :data:`_DEFAULT_MATRIX_ROWS` into a ``{(from, to): rule}`` dict.

    Verifies at import time that every rule value is valid; a typo in the
    transcription fails the import rather than producing a surprise at call
    time. Also verifies the matrix is square (same product set in rows and
    columns) so auditability claims stay true.
    """

    rows: Dict[Tuple[str, str], RuleType] = {}
    row_keys: List[str] = []
    for from_code, entries in _DEFAULT_MATRIX_ROWS:
        row_keys.append(from_code)
        column_keys = [to for to, _ in entries]
        if len(set(column_keys)) != len(column_keys):
            raise RuntimeError(
                f"compatibility_matrix: row {from_code!r} has duplicate columns"
            )
        for to_code, rule in entries:
            if rule not in VALID_RULES:
                raise RuntimeError(
                    f"compatibility_matrix: invalid rule {rule!r} at "
                    f"({from_code}, {to_code})"
                )
            rows[(from_code, to_code)] = rule
    # Ensure rows and columns span the same product set (no missing cells).
    column_keys_set = {to for _, entries in _DEFAULT_MATRIX_ROWS for to, _ in entries}
    missing = set(row_keys).symmetric_difference(column_keys_set)
    if missing:
        raise RuntimeError(
            "compatibility_matrix: default matrix is not square; "
            f"asymmetric products={sorted(missing)}"
        )
    return rows


#: The shipped default rule table. Read-only at runtime — ``load_tenant_*``
#: returns a fresh copy merged with tenant overrides so callers cannot mutate
#: the seed in-place.
DEFAULT_COMPATIBILITY_RULES: Mapping[Tuple[str, str], RuleType] = _build_default_rules()


# ---------------------------------------------------------------------------
# Public decision function
# ---------------------------------------------------------------------------


class CompatibilityDecision(TypedDict):
    """Structured return type from :func:`check_compatibility`.

    ``decision`` is always populated. ``reason`` is ``None`` on ``allowed``
    and a stable string code otherwise. ``governing_rule`` records the rule
    value (``allowed`` / ``blocked`` / ``requires_cleaning``) that drove the
    decision, which the load-eligibility endpoint surfaces to explain *why*
    a blocked or cleaning-gated decision was returned.
    """

    decision: Decision
    reason: Optional[str]
    governing_rule: RuleType


def check_compatibility(
    previous_product: Optional[str],
    next_product: str,
    compartment_state: Any,
    rules: Optional[Mapping[Tuple[str, str], RuleType]] = None,
) -> CompatibilityDecision:
    """Return whether ``next_product`` may be loaded after ``previous_product``.

    Validates: Requirement 7.2.1 (rule lookup and {allowed, blocked,
    requires_cleaning} value set) and Requirement 7.2.4 (requires_cleaning
    downgrades to allowed when a fresh Cleaning_Event exists).

    Args:
        previous_product: The canonical catalog product_code (or legacy alias)
            of the most recent load. ``None`` or an empty string means the
            compartment has no prior load (e.g. freshly commissioned or fully
            purged); any next product is allowed.
        next_product: The canonical catalog product_code (or legacy alias) of
            the proposed load. Required; an empty or invalid value raises.
        compartment_state: Any object exposing ``last_loaded_at`` and
            ``last_cleaned_at`` attributes (both ``datetime`` or ``None``).
            The canonical producer is
            :class:`fuel.compartment_state_models.CompartmentState` but tests
            and the load-eligibility endpoint may pass duck-typed stand-ins.
            A ``None`` state is tolerated and treated as "no history".
        rules: Optional rule table. Defaults to
            :data:`DEFAULT_COMPATIBILITY_RULES`. Callers that have already
            merged tenant overrides should pass the merged table in.

    Returns:
        A :class:`CompatibilityDecision` dict with stable keys. The returned
        dict is a fresh instance so callers may mutate it freely.

    Raises:
        UnknownFuelProductError: if either product code fails to canonicalize
            against the fuel product catalog. The caller is responsible for
            translating this into an HTTP 422 at the API boundary.
        TypeError: if ``next_product`` is not a string.
    """

    effective_rules = rules if rules is not None else DEFAULT_COMPATIBILITY_RULES

    # Canonicalize the next product first; a bad next_product is always a
    # hard failure regardless of compartment state.
    next_canonical = canonicalize(next_product)

    # Empty / cleaned compartment: any product is allowed. We deliberately
    # short-circuit before the matrix lookup so tenants cannot accidentally
    # override the bootstrap case.
    if _is_empty_previous(previous_product):
        return CompatibilityDecision(
            decision=DECISION_ALLOWED,
            reason=None,
            governing_rule=RULE_ALLOWED,
        )

    prev_canonical = canonicalize(previous_product)  # type: ignore[arg-type]

    # Same-product chain is always allowed. Short-circuiting here keeps
    # tenants from breaking the common case via a botched override.
    if prev_canonical == next_canonical:
        return CompatibilityDecision(
            decision=DECISION_ALLOWED,
            reason=None,
            governing_rule=RULE_ALLOWED,
        )

    rule = effective_rules.get((prev_canonical, next_canonical), RULE_ALLOWED)

    if rule == RULE_ALLOWED:
        return CompatibilityDecision(
            decision=DECISION_ALLOWED,
            reason=None,
            governing_rule=RULE_ALLOWED,
        )

    if rule == RULE_BLOCKED:
        return CompatibilityDecision(
            decision=DECISION_BLOCKED,
            reason=REASON_CROSS_CONTAMINATION_BLOCKED,
            governing_rule=RULE_BLOCKED,
        )

    # rule == RULE_REQUIRES_CLEANING — downgrade to allowed when a fresh
    # Cleaning_Event exists for this compartment (Req 7.2.4).
    if _has_fresh_cleaning(compartment_state):
        return CompatibilityDecision(
            decision=DECISION_ALLOWED,
            reason=None,
            governing_rule=RULE_REQUIRES_CLEANING,
        )

    return CompatibilityDecision(
        decision=DECISION_REQUIRES_CLEANING,
        reason=REASON_CLEANING_REQUIRED,
        governing_rule=RULE_REQUIRES_CLEANING,
    )


# ---------------------------------------------------------------------------
# Tenant override loader
# ---------------------------------------------------------------------------


async def load_tenant_compatibility_rules(
    tenant_id: str,
    tenant_config: Any = None,
) -> Dict[Tuple[str, str], RuleType]:
    """Return the effective rule table for ``tenant_id``.

    Merges tenant overrides stored under the Redis key
    ``compatibility_matrix_config:{tenant_id}`` on top of
    :data:`DEFAULT_COMPATIBILITY_RULES`. The function never raises on config
    backend failure, missing key, or malformed payload — it logs a warning
    and returns the defaults so a safety-critical code path is never blocked
    by a config outage. The caller may treat the absence of overrides as a
    "rollback to defaults" signal.

    Args:
        tenant_id: The tenant whose overrides to load. Falsy values skip the
            lookup entirely and return the defaults.
        tenant_config: An optional object exposing ``async get(key) -> raw``.
            When ``None`` the defaults are returned unchanged. This matches
            the ``_FakeTenantConfig`` / Redis handle contract used elsewhere
            in the fuel-ops agents (see ``Agents/overlay/tank_forecasting_agent.py``).

    Returns:
        A fresh dict keyed by ``(previous_code, next_code)`` tuples. Callers
        may mutate it without affecting the seed table.
    """

    base: Dict[Tuple[str, str], RuleType] = dict(DEFAULT_COMPATIBILITY_RULES)
    if tenant_config is None or not isinstance(tenant_id, str) or not tenant_id.strip():
        return base

    key = f"{REDIS_KEY_TENANT_PREFIX}:{tenant_id.strip()}"
    try:
        raw = await tenant_config.get(key)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "compatibility_matrix: tenant config get(%s) failed: %s", key, exc
        )
        return base

    if raw is None:
        return base

    try:
        overrides = parse_rule_overrides(raw)
    except ValueError as exc:
        logger.warning(
            "compatibility_matrix: failed to parse overrides for %s: %s",
            key,
            exc,
        )
        return base

    if not overrides:
        return base

    merged = dict(base)
    for pair, rule in overrides.items():
        merged[pair] = rule
    return merged


def parse_rule_overrides(payload: Any) -> Dict[Tuple[str, str], RuleType]:
    """Parse a tenant override payload into ``{(from, to): rule}``.

    Supported input shapes:

    * ``str`` / ``bytes`` — decoded as UTF-8 (for bytes) and parsed as JSON.
      The inner value must be one of the mapping or list shapes below.
    * :class:`collections.abc.Mapping` — keys are either ``"FROM|TO"`` or
      ``"FROM->TO"`` (the arrow form may also be the unicode ``"→"``) and
      values are one of ``"allowed"`` / ``"blocked"`` / ``"requires_cleaning"``.
    * Iterable of dicts — each entry has ``from`` (or ``from_product_code``
      or ``previous_product``), ``to`` (or ``to_product_code`` or
      ``next_product``), and ``rule`` (or ``rule_type``). This form mirrors
      the ``contamination_rules`` ES document shape so future migrations
      from that index do not require a separate parser.

    Unknown product codes and unknown rule values are **dropped with a
    warning** rather than failing the whole payload: a single bad entry must
    not poison the entire tenant rule set. Callers that want strict
    validation should read the admin UI's dedicated schema validator before
    persisting.

    Aliases accepted for rule values:

    * ``"forbidden"`` → ``"blocked"`` (matches the ``rule_type`` enum used
      by the ``contamination_rules`` ES index, Requirements 7.2 second-half
      table).

    Returns:
        A fresh dict. Empty payloads (empty string, empty mapping, empty
        list) return an empty dict.

    Raises:
        ValueError: if the outer shape is not parseable (e.g. top-level
            JSON is an int, or the string payload is not valid JSON). This
            lets the caller distinguish "tenant supplied junk" from "tenant
            supplied valid but empty overrides".
    """

    if payload is None:
        return {}

    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("override payload is not valid UTF-8") from exc

    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped:
            return {}
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"override payload is not valid JSON: {exc}") from exc

    if isinstance(payload, Mapping):
        return _parse_mapping_overrides(payload)

    if isinstance(payload, (list, tuple)):
        return _parse_list_overrides(payload)

    raise ValueError(
        f"override payload must be a mapping or list, got {type(payload).__name__}"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_empty_previous(previous_product: Optional[str]) -> bool:
    """Return True when the previous-product field denotes an empty compartment."""

    if previous_product is None:
        return True
    if isinstance(previous_product, str) and not previous_product.strip():
        return True
    return False


def _has_fresh_cleaning(compartment_state: Any) -> bool:
    """Return True iff the compartment was cleaned strictly after its last load.

    Tolerance rules:

    * ``compartment_state is None`` → no history, treat as clean.
    * ``last_loaded_at is None`` → nothing to clean up, treat as clean.
    * ``last_cleaned_at is None`` → never cleaned since load, stale.
    * Comparison error (naive vs aware datetime) → treat as stale; we
      prefer a conservative ``requires_cleaning`` outcome over a false
      ``allowed``.
    """

    if compartment_state is None:
        return True

    loaded_at = getattr(compartment_state, "last_loaded_at", None)
    cleaned_at = getattr(compartment_state, "last_cleaned_at", None)

    if loaded_at is None:
        return True
    if cleaned_at is None:
        return False

    try:
        return cleaned_at > loaded_at
    except TypeError:
        logger.warning(
            "compatibility_matrix: incomparable timestamps on compartment_state "
            "(loaded_at=%r, cleaned_at=%r); treating as stale",
            loaded_at,
            cleaned_at,
        )
        return False


def _parse_mapping_overrides(
    payload: Mapping[Any, Any],
) -> Dict[Tuple[str, str], RuleType]:
    out: Dict[Tuple[str, str], RuleType] = {}
    for raw_key, raw_rule in payload.items():
        pair = _split_pair_key(raw_key)
        if pair is None:
            logger.warning(
                "compatibility_matrix: skipping malformed key %r in overrides",
                raw_key,
            )
            continue
        canonical_pair = _canonicalize_pair(*pair)
        if canonical_pair is None:
            # _canonicalize_pair logs its own warning with the original code.
            continue
        rule = _validate_rule(raw_rule)
        if rule is None:
            logger.warning(
                "compatibility_matrix: skipping invalid rule %r for key %r",
                raw_rule,
                raw_key,
            )
            continue
        out[canonical_pair] = rule
    return out


def _parse_list_overrides(
    payload: Iterable[Any],
) -> Dict[Tuple[str, str], RuleType]:
    out: Dict[Tuple[str, str], RuleType] = {}
    for idx, item in enumerate(payload):
        if not isinstance(item, Mapping):
            logger.warning(
                "compatibility_matrix: skipping non-dict override entry at index %d",
                idx,
            )
            continue
        frm = (
            item.get("from")
            or item.get("from_product_code")
            or item.get("previous_product")
        )
        to = (
            item.get("to")
            or item.get("to_product_code")
            or item.get("next_product")
        )
        rule_raw = item.get("rule") or item.get("rule_type")
        if frm is None or to is None or rule_raw is None:
            logger.warning(
                "compatibility_matrix: skipping incomplete override entry at "
                "index %d (from=%r, to=%r, rule=%r)",
                idx,
                frm,
                to,
                rule_raw,
            )
            continue
        if not isinstance(frm, str) or not isinstance(to, str):
            logger.warning(
                "compatibility_matrix: skipping non-string product codes at "
                "index %d (from=%r, to=%r)",
                idx,
                frm,
                to,
            )
            continue
        canonical_pair = _canonicalize_pair(frm, to)
        if canonical_pair is None:
            continue
        rule = _validate_rule(rule_raw)
        if rule is None:
            logger.warning(
                "compatibility_matrix: skipping invalid rule %r at index %d",
                rule_raw,
                idx,
            )
            continue
        out[canonical_pair] = rule
    return out


def _split_pair_key(raw_key: Any) -> Optional[Tuple[str, str]]:
    """Split a ``"FROM|TO"`` / ``"FROM->TO"`` / ``"FROM→TO"`` key into parts.

    Returns ``None`` on any shape that does not look like an ordered pair.
    """

    if not isinstance(raw_key, str):
        return None
    # Check longer separators first so ``->`` is not mis-split as two ``-`` chars.
    for sep in ("->", "→", "|"):
        if sep in raw_key:
            left, _, right = raw_key.partition(sep)
            left_s = left.strip()
            right_s = right.strip()
            if left_s and right_s:
                return left_s, right_s
            return None
    return None


def _canonicalize_pair(frm: str, to: str) -> Optional[Tuple[str, str]]:
    """Canonicalize both halves of a pair, dropping unknown codes."""

    try:
        return canonicalize(frm), canonicalize(to)
    except (UnknownFuelProductError, TypeError) as exc:
        logger.warning(
            "compatibility_matrix: dropping override with unknown product "
            "(from=%r, to=%r): %s",
            frm,
            to,
            exc,
        )
        return None


def _validate_rule(rule_raw: Any) -> Optional[RuleType]:
    """Return the canonical rule string for ``rule_raw`` or ``None``."""

    if not isinstance(rule_raw, str):
        return None
    normalized = rule_raw.strip().lower()
    if normalized in VALID_RULES:
        return normalized  # type: ignore[return-value]
    if normalized == "forbidden":
        # Alias accepted for compatibility with the ``contamination_rules`` ES
        # index rule_type enum.
        return RULE_BLOCKED
    return None


__all__ = [
    "Decision",
    "RuleType",
    "DECISION_ALLOWED",
    "DECISION_BLOCKED",
    "DECISION_REQUIRES_CLEANING",
    "RULE_ALLOWED",
    "RULE_BLOCKED",
    "RULE_REQUIRES_CLEANING",
    "REASON_CROSS_CONTAMINATION_BLOCKED",
    "REASON_CLEANING_REQUIRED",
    "VALID_RULES",
    "REDIS_KEY_TENANT_PREFIX",
    "DEFAULT_COMPATIBILITY_RULES",
    "CompatibilityDecision",
    "check_compatibility",
    "load_tenant_compatibility_rules",
    "parse_rule_overrides",
]
