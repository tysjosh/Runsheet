"""
Storm_Mode-aware notification resolver.

Task 10.9 of the fuel-ops hardening spec (Requirement 9.2.6):

    > WHEN Storm_Mode is active, THE Customer_Notification pipeline
    > SHALL use severe-weather message templates for keep-full and
    > generator customers and SHALL include the Weather_Alert reference
    > in the notification metadata.

Responsibilities
----------------

The resolver encapsulates every Storm_Mode-specific decision the
:class:`notifications.services.notification_service.NotificationService`
needs to make so the core orchestrator stays Storm_Mode-agnostic and
non-fuel tenants don't pay a Storm_Mode cost:

1. Read the current Storm_Mode state for the recipient's tenant from a
   :class:`fuel.services.storm_mode_evaluator.StormModeEvaluator`-
   compatible ``state_provider``. When state is ``inactive``, the
   resolver short-circuits and the core pipeline uses its default
   templates without modification.

2. Check whether the recipient is a *keep-full* or *generator*
   customer via a pluggable ``profile_resolver`` (default: look up
   the ``customers`` ES index and parse
   :class:`fuel.storm_mode_models.CustomerProfile`). Non-eligible
   customers still receive the default templates even while Storm_Mode
   is active, which matches the intent of Requirement 9.2.6 — only the
   prioritized cohort receives severe-weather language.

3. Compute a ``storm_event_type`` derived by suffixing the base event
   type with ``_storm``. If the corresponding severe-weather template
   is missing for the tenant/channel, the caller falls back to the
   default event_type so the notification still ships.

4. Build the ``weather_alert_ref`` payload from the triggering alert
   (or cached state), matching the mapping added to
   :mod:`notifications.services.notification_es_mappings`.

Design notes
------------

* All upstream failures (missing StormModeEvaluator, unreachable ES,
  malformed persisted state, profile index absent) degrade to the
  "Storm_Mode inactive for notification purposes" branch so a bad
  Storm_Mode signal never blocks customer notifications.

* The resolver is purposefully stateless: it reads per-call and
  returns a dict the caller merges into ``event_data`` /
  persistence. That keeps unit tests trivial and avoids any implicit
  coupling to Redis.

* The module has no hard dependency on the fuel Storm_Mode modules —
  the ``StormModeEvaluator``-compatible ``state_provider`` is typed
  structurally so notifications can be unit-tested without pulling the
  full Phase 10 surface. The runtime wiring (see
  :mod:`bootstrap.notifications`) injects the actual evaluator.

Validates: Requirement 9.2.6 / Task 10.9.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Literal state value that indicates Storm_Mode is currently active for
#: a tenant. Mirrors :data:`fuel.services.storm_mode_evaluator.ACTIVE`
#: without importing the fuel module so the notifications package does
#: not reach into the fuel domain.
STORM_MODE_ACTIVE: str = "active"

#: Reason tag written to ``storm_variant_reason`` on the notification
#: document so dispatchers and the audit timeline can trace *why* the
#: severe-weather template was chosen.
STORM_REASON_KEEP_FULL: str = "keep_full_storm_mode"
STORM_REASON_GENERATOR: str = "generator_storm_mode"

#: The ``_storm`` suffix applied to the base event_type to look up the
#: severe-weather variant. Exposed as a constant so tests can cross-
#: check against the DEFAULT_TEMPLATES keys in
#: :mod:`notifications.services.template_renderer`.
STORM_TEMPLATE_SUFFIX: str = "_storm"

#: Index that stores :class:`fuel.storm_mode_models.CustomerProfile`
#: records. Declared here rather than imported from the fuel module so
#: the notifications package stays import-light.
CUSTOMERS_INDEX: str = "customers"


# ---------------------------------------------------------------------------
# Typed protocols
# ---------------------------------------------------------------------------


class StormStateProvider(Protocol):
    """Structural type for any component that can report Storm_Mode state.

    Matches :meth:`fuel.services.storm_mode_evaluator.StormModeEvaluator.get_state`
    so the production bootstrap can pass that evaluator directly. Tests
    satisfy the protocol with a lightweight stub.
    """

    async def get_state(self, tenant_id: str) -> Any:  # pragma: no cover
        ...


#: Callable returning a Customer_Profile-compatible mapping (or ``None``
#: when no profile is stored). Used to decide whether the recipient is
#: a keep-full or generator customer. The return type is intentionally
#: ``Any`` so tests can pass a plain dict; production wiring passes
#: parsed :class:`fuel.storm_mode_models.CustomerProfile` instances.
CustomerProfileResolver = Callable[
    [str, str], Awaitable[Optional[Any]]
]


# ---------------------------------------------------------------------------
# Result payload
# ---------------------------------------------------------------------------


class StormNotificationDecision:
    """Immutable outcome returned by :class:`StormModeNotificationResolver`.

    Fields:

    * ``storm_mode_active`` — ``True`` when the caller should use the
      severe-weather template and attach the alert reference. ``False``
      when Storm_Mode is inactive, the recipient is not eligible, or
      any upstream dependency failed.
    * ``storm_event_type`` — the ``_storm``-suffixed event_type that
      the caller should attempt first when resolving a template.
      ``None`` when ``storm_mode_active`` is ``False``.
    * ``weather_alert_ref`` — the metadata dict to persist on the
      notification document. ``None`` when ``storm_mode_active`` is
      ``False`` or the persisted state carried no alert.
    * ``storm_variant_reason`` — one of
      :data:`STORM_REASON_KEEP_FULL` / :data:`STORM_REASON_GENERATOR`
      when eligible, else ``None``.
    * ``placeholder_data`` — additional placeholder values
      (``weather_alert_type``, ``weather_alert_headline``, …) the
      caller should merge into ``event_data`` before template
      rendering. Always a dict; empty when ``storm_mode_active`` is
      ``False``.
    """

    __slots__ = (
        "storm_mode_active",
        "storm_event_type",
        "weather_alert_ref",
        "storm_variant_reason",
        "placeholder_data",
    )

    def __init__(
        self,
        *,
        storm_mode_active: bool,
        storm_event_type: Optional[str] = None,
        weather_alert_ref: Optional[Dict[str, Any]] = None,
        storm_variant_reason: Optional[str] = None,
        placeholder_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.storm_mode_active = storm_mode_active
        self.storm_event_type = storm_event_type
        self.weather_alert_ref = weather_alert_ref
        self.storm_variant_reason = storm_variant_reason
        self.placeholder_data = placeholder_data or {}

    @classmethod
    def inactive(cls) -> "StormNotificationDecision":
        """Return the canonical "Storm_Mode inactive" decision."""
        return cls(storm_mode_active=False)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class StormModeNotificationResolver:
    """Resolve Storm_Mode notification treatment for a single event.

    Args:
        state_provider: A :class:`StormStateProvider`-compatible object.
            When ``None`` the resolver always returns "inactive" so
            tenants without Phase 10 wiring see unchanged behavior.
        profile_resolver: Optional async callable
            ``(tenant_id, customer_id) -> profile-or-None`` used to
            decide keep-full / generator eligibility. When ``None``
            the resolver falls back to a direct ES lookup against
            ``customers`` using the injected ``es_service``.
        es_service: Optional ElasticsearchService used by the default
            profile lookup path. When both ``profile_resolver`` and
            ``es_service`` are ``None`` the resolver cannot determine
            eligibility and returns "inactive".
    """

    def __init__(
        self,
        *,
        state_provider: Optional[StormStateProvider] = None,
        profile_resolver: Optional[CustomerProfileResolver] = None,
        es_service: Optional[Any] = None,
    ) -> None:
        self._state_provider = state_provider
        self._profile_resolver = profile_resolver
        self._es = es_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        event_type: str,
    ) -> StormNotificationDecision:
        """Return the Storm_Mode decision for the given notification event.

        The method is intentionally generous with falsy / malformed
        inputs — any upstream failure degrades to "inactive" so a
        broken Storm_Mode signal never suppresses a notification that
        would have shipped otherwise.
        """
        if not tenant_id or not customer_id or not event_type:
            return StormNotificationDecision.inactive()

        if self._state_provider is None:
            return StormNotificationDecision.inactive()

        state = await self._load_state(tenant_id)
        if state is None:
            return StormNotificationDecision.inactive()

        if self._extract_state_value(state) != STORM_MODE_ACTIVE:
            return StormNotificationDecision.inactive()

        profile = await self._resolve_profile(tenant_id, customer_id)
        reason = self._customer_reason(profile)
        if reason is None:
            return StormNotificationDecision.inactive()

        alert_ref = self._extract_alert_ref(state)
        placeholders = self._build_placeholder_data(alert_ref)

        storm_event_type = f"{event_type}{STORM_TEMPLATE_SUFFIX}"

        return StormNotificationDecision(
            storm_mode_active=True,
            storm_event_type=storm_event_type,
            weather_alert_ref=alert_ref,
            storm_variant_reason=reason,
            placeholder_data=placeholders,
        )

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    async def _load_state(self, tenant_id: str) -> Optional[Any]:
        """Call the state provider; return ``None`` on any failure."""
        try:
            return await self._state_provider.get_state(tenant_id)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "StormModeNotificationResolver: state lookup failed "
                "for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return None

    @staticmethod
    def _extract_state_value(state: Any) -> Optional[str]:
        """Pull a state string out of the provider result.

        Supports three shapes:

        * ``PersistedState`` dataclass exposing a ``state`` attribute.
        * Plain dict with a ``state`` key.
        * Plain string.
        """
        if state is None:
            return None
        if isinstance(state, str):
            return state
        if isinstance(state, dict):
            value = state.get("state")
            return value if isinstance(value, str) else None
        value = getattr(state, "state", None)
        return value if isinstance(value, str) else None

    # ------------------------------------------------------------------
    # Profile helpers
    # ------------------------------------------------------------------

    async def _resolve_profile(
        self, tenant_id: str, customer_id: str
    ) -> Optional[Any]:
        """Return the Customer_Profile-compatible object for the recipient."""
        if self._profile_resolver is not None:
            try:
                return await self._profile_resolver(tenant_id, customer_id)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "StormModeNotificationResolver: profile resolver "
                    "raised for tenant=%s customer=%s: %s",
                    tenant_id,
                    customer_id,
                    exc,
                )
                return None

        if self._es is None:
            return None

        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"customer_id": customer_id}},
                    ],
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                }
            },
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(CUSTOMERS_INDEX, query, 1)
        except Exception as exc:
            logger.debug(
                "StormModeNotificationResolver: ES profile lookup "
                "skipped for tenant=%s customer=%s: %s",
                tenant_id,
                customer_id,
                exc,
            )
            return None

        hits = ((resp or {}).get("hits") or {}).get("hits") or []
        if not hits:
            return None
        source = hits[0].get("_source") if isinstance(hits[0], dict) else None
        if not isinstance(source, dict):
            return None
        if source.get("tenant_id") != tenant_id:
            # Defensive: never leak cross-tenant data even if the ES
            # query already scopes by tenant_id.
            return None
        return source

    @staticmethod
    def _customer_reason(profile: Any) -> Optional[str]:
        """Return the storm-variant reason for the given profile, or ``None``.

        Recognized shapes:

        * :class:`fuel.storm_mode_models.CustomerProfile` (Pydantic model)
          with a nested ``keep_full.keep_full_enabled`` and a
          ``is_generator_fuel`` attribute.
        * Plain dict with the same nested keys.

        Customers flagged as *both* keep-full and generator receive the
        ``keep_full_storm_mode`` reason — the label is used for audit and
        both cohorts receive the same template treatment, so the tie
        break is stable and deterministic.
        """
        if profile is None:
            return None

        keep_full_enabled = False
        is_generator = False

        # Pydantic / attr-based shape.
        kf_attr = getattr(profile, "keep_full", None)
        if kf_attr is not None:
            keep_full_enabled = bool(
                getattr(kf_attr, "keep_full_enabled", False)
            )
        if getattr(profile, "is_generator_fuel", None) is not None:
            is_generator = bool(getattr(profile, "is_generator_fuel"))

        # Dict shape fallback (e.g., raw ES source).
        if isinstance(profile, dict):
            kf = profile.get("keep_full") or {}
            if isinstance(kf, dict):
                keep_full_enabled = keep_full_enabled or bool(
                    kf.get("keep_full_enabled", False)
                )
            # Flat fallback — some ES writers store ``keep_full_enabled``
            # at the top level rather than under ``keep_full``.
            keep_full_enabled = keep_full_enabled or bool(
                profile.get("keep_full_enabled", False)
            )
            is_generator = is_generator or bool(
                profile.get("is_generator_fuel", False)
            )

        if keep_full_enabled:
            return STORM_REASON_KEEP_FULL
        if is_generator:
            return STORM_REASON_GENERATOR
        return None

    # ------------------------------------------------------------------
    # Alert-ref helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_alert_ref(state: Any) -> Optional[Dict[str, Any]]:
        """Build the ``weather_alert_ref`` payload from persisted state.

        Matches the ES mapping added by Task 10.9: ``alert_id``,
        ``alert_type``, ``severity``, ``headline``, ``source``,
        ``region_code``, ``expected_start_at``, ``expected_end_at``,
        ``affected_zip_codes``. Fields absent from the persisted state
        are omitted rather than ``None`` to keep the document compact.
        """
        if state is None:
            return None

        # PersistedState dataclass shape (see storm_mode_evaluator.py):
        # ``{state, updated_at, triggering_alert_ids, expected_end_at}``.
        triggering_alerts = getattr(state, "triggering_alert_ids", None)
        expected_end = getattr(state, "expected_end_at", None)
        if isinstance(state, dict):
            triggering_alerts = triggering_alerts or state.get(
                "triggering_alert_ids"
            )
            expected_end = expected_end or state.get("expected_end_at")

        # Prefer a fully-formed triggering_alert dict when the caller
        # passes one directly (tests, SignalBus payload).
        triggering_alert: Any = None
        if isinstance(state, dict):
            triggering_alert = state.get("triggering_alert")
        else:
            triggering_alert = getattr(state, "triggering_alert", None)

        ref: Dict[str, Any] = {}
        if isinstance(triggering_alert, dict):
            for key in (
                "alert_id",
                "alert_type",
                "severity",
                "headline",
                "source",
                "region_code",
                "expected_start_at",
                "expected_end_at",
                "affected_zip_codes",
            ):
                value = triggering_alert.get(key)
                if value is None:
                    continue
                if isinstance(value, datetime):
                    value = value.isoformat()
                ref[key] = value

        if "alert_id" not in ref:
            alert_ids: List[str] = []
            if isinstance(triggering_alerts, list):
                for raw in triggering_alerts:
                    if isinstance(raw, str) and raw.strip():
                        alert_ids.append(raw.strip())
            if alert_ids:
                ref["alert_id"] = alert_ids[0]

        if "expected_end_at" not in ref and expected_end is not None:
            if isinstance(expected_end, datetime):
                ref["expected_end_at"] = expected_end.isoformat()
            elif isinstance(expected_end, str) and expected_end.strip():
                ref["expected_end_at"] = expected_end

        return ref or None

    @staticmethod
    def _build_placeholder_data(
        alert_ref: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Flatten the alert_ref into template placeholders.

        The severe-weather templates reference
        ``{weather_alert_type}``, ``{weather_alert_headline}``,
        ``{weather_alert_severity}``, and ``{weather_alert_expected_end_at}``.
        Missing fields are mapped to empty strings so
        :class:`notifications.services.template_renderer.SafeDict`
        renders cleanly.
        """
        if not alert_ref:
            return {
                "weather_alert_type": "severe weather",
                "weather_alert_headline": "Severe weather alert",
                "weather_alert_severity": "",
                "weather_alert_expected_end_at": "",
                "weather_alert_id": "",
            }
        return {
            "weather_alert_type": alert_ref.get("alert_type") or "severe weather",
            "weather_alert_headline": (
                alert_ref.get("headline") or "Severe weather alert"
            ),
            "weather_alert_severity": alert_ref.get("severity") or "",
            "weather_alert_expected_end_at": (
                alert_ref.get("expected_end_at") or ""
            ),
            "weather_alert_id": alert_ref.get("alert_id") or "",
        }


__all__ = [
    "STORM_MODE_ACTIVE",
    "STORM_REASON_KEEP_FULL",
    "STORM_REASON_GENERATOR",
    "STORM_TEMPLATE_SUFFIX",
    "CUSTOMERS_INDEX",
    "StormStateProvider",
    "CustomerProfileResolver",
    "StormNotificationDecision",
    "StormModeNotificationResolver",
]
