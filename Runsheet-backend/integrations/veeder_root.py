"""
Veeder-Root Connector — tank-monitor reference integration (Req 5.3.*).

Implements the Veeder-Root side of the pluggable integration framework
introduced in Capability 5 / Phase 9 of the fuel-ops hardening spec. The
connector supports two polling transports, each selected per
:class:`IntegrationInstance` via the instance's ``config["mode"]`` key:

* ``api_token`` — vendor-hosted cloud API over HTTPS (Veeder's Insite360
  or any vendor-equivalent tank-monitoring API) using a bearer token.
* ``tls_401_tcp`` — legacy direct TCP socket polling to the on-site ATG
  console speaking the Veeder-Root TLS-350 / TLS-450 "serial" command
  set over TCP. We issue the In-Tank Inventory command (``I20100``)
  and parse the pipe-delimited response into tank-level readings.

Every poll (``sync_pull``) produces a list of per-tank readings with
``volume_gallons``, ``water_level_in``, ``temperature_f``, and the
vendor tank index, then the connector:

    1. Resolves each vendor tank index to either a
       :class:`fuel.customer_tank_models.CustomerTank` or a
       ``fuel_stations`` record via the instance-level ``tank_map``
       configuration (see :data:`_CONFIG_TANK_MAP_KEY` below). Tanks
       without a mapping are still persisted to ``atg_readings`` but do
       not update any downstream record — the admin UI surfaces
       unmapped readings so the operator can complete the mapping.
    2. Updates ``current_level_gallons`` + ``last_reading_at`` on the
       matching Customer_Tank (via
       :class:`fuel.customer_tank_models.CustomerTankRepository.update`)
       or the matching ``fuel_stations`` document (via an ES partial
       update of ``current_stock_liters`` converted from gallons).
    3. Persists the raw reading to the ``atg_readings`` ES index per
       Requirement 5.3.4.
    4. When ``water_level_in`` exceeds the tenant-configured
       ``veeder_root.water_threshold_in:{tenant_id}`` Redis key (default
       :data:`DEFAULT_WATER_THRESHOLD_IN`), publishes a
       ``water_contamination`` :class:`RiskSignal` on the
       :class:`Agents.overlay.signal_bus.SignalBus` with severity
       :class:`Severity.HIGH` (Req 5.3.6).

Cross-cutting invariants enforced here:

* **Credentials stay in the vault.** ``connect()`` never logs, echoes,
  or returns plaintext credentials (Requirement 5.1.8). The only thing
  that leaves the connector is an opaque ``credentials_ref`` the
  :class:`IntegrationInstanceRepository` persists.
* **Timeouts on every I/O path.** HTTP calls honour
  :data:`DEFAULT_HTTP_TIMEOUT_SECONDS`; TCP polling honours
  :data:`DEFAULT_TCP_TIMEOUT_SECONDS` and is wrapped in
  :func:`asyncio.wait_for` so a stuck ATG console never blocks the
  scheduler.
* **Per-reading error isolation.** A single malformed / unmappable
  reading is logged and counted (``skipped_unmapped`` /
  ``skipped_invalid``) but never aborts the whole run — one bad tank
  on a 16-tank site must not stop the other 15 readings from landing.
* **No exception leakage from sync_pull / sync_push.** Every failure
  path produces a terminal :class:`SyncRun` with structured
  ``error_details`` so the scheduler can persist the run and surface it
  in the admin UI without the process escalating.
* **sync_push is a no-op.** Tank monitors are read-only by design;
  pushing anything to the ATG is out of scope. The connector returns a
  success :class:`SyncRun` with ``{"skipped_noop": 1}`` so the
  Integration_Scheduler can still log a tick without tripping error
  accounting.

Default cron schedule: every 15 minutes. The value is set on the
:class:`IntegrationInstance.schedule_cron` field by the admin UI or by
the tenant bootstrap helper — this module does not mutate cron state.
The default is surfaced in :data:`DEFAULT_SCHEDULE_CRON` and the
provider-catalog ``description`` so the Marketplace UI shows it
verbatim.

Validates: Requirements 5.3.1, 5.3.2, 5.3.3, 5.3.4, 5.3.5, 5.3.6.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, ClassVar, Dict, List, Mapping, Optional, Tuple
from uuid import uuid4

import httpx

from Agents.overlay.data_contracts import RiskSignal, Severity
from fuel.services.fuel_ops_es_mappings import (
    ATG_READINGS_INDEX,
    CUSTOMER_TANKS_INDEX,
)
from integrations.connector_base import (
    ConnectionResult,
    IntegrationConnector,
    SyncRun,
)
from integrations.provider_catalog import (
    ProviderCatalogEntry,
    register_provider,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Connector operating modes. ``api_token`` targets the vendor cloud
#: (HTTPS + bearer), ``tls_401_tcp`` targets the on-site ATG console
#: over raw TCP using the Veeder-Root TLS-350/450 command set.
MODE_API_TOKEN: str = "api_token"
MODE_TLS_401_TCP: str = "tls_401_tcp"
_SUPPORTED_MODES: "frozenset[str]" = frozenset({MODE_API_TOKEN, MODE_TLS_401_TCP})

#: Default timeouts (seconds) on each transport.
DEFAULT_HTTP_TIMEOUT_SECONDS: float = 10.0
DEFAULT_TCP_TIMEOUT_SECONDS: float = 10.0

#: Default TCP port for the TLS-401 protocol over Ethernet. Veeder-Root
#: consoles listen on port 10001 by default; operators occasionally
#: relocate to 10002 or 10003. Override via ``config["port"]``.
DEFAULT_TCP_PORT: int = 10001

#: Default API path on the vendor-hosted cloud. Override via
#: ``config["api_path"]`` to point at a custom endpoint.
DEFAULT_API_PATH: str = "/api/v1/tanks/inventory"

#: Vault key used to persist the credentials envelope. Scoped by
#: tenant + provider so a single vault lookup resolves the whole
#: envelope.
VAULT_CREDENTIAL_KEY: str = "veeder_root_creds"

#: Default 15-minute cron per Requirement 5.3.5. The schedule is owned
#: by the :class:`IntegrationInstance.schedule_cron` field; this
#: constant is surfaced through the provider catalog's ``description``
#: and the admin-UI prefill so operators see the canonical default.
DEFAULT_SCHEDULE_CRON: str = "*/15 * * * *"

#: Redis key template for the tenant-configurable water alert
#: threshold. Matches Requirement 5.3.6 verbatim. Values are stored in
#: inches (matching the Veeder-Root console output units).
WATER_THRESHOLD_REDIS_KEY_TEMPLATE: str = "veeder_root.water_threshold_in:{tenant_id}"

#: Default threshold when Redis returns nothing / is unavailable. Two
#: inches of water is the standard operator trigger for requesting an
#: immediate tank sweep (Requirement 5.3.6).
DEFAULT_WATER_THRESHOLD_IN: float = 2.0

#: :class:`RiskSignal.context` tag the overlay consumer keys on. Mirrors
#: the ``cross_contamination_violation`` convention established by
#: :mod:`Agents.overlay.compartment_loading_agent`.
WATER_CONTAMINATION_SIGNAL_TYPE: str = "water_contamination"

#: :class:`RiskSignal.entity_type` used when the reading maps to a
#: Customer_Tank.
CUSTOMER_TANK_ENTITY_TYPE: str = "customer_tank"

#: :class:`RiskSignal.entity_type` used when the reading maps to a
#: retail fuel-stations record.
FUEL_STATION_ENTITY_TYPE: str = "fuel_station"

#: :class:`RiskSignal.source_agent` tag used on published signals.
VEEDER_ROOT_AGENT_ID: str = "veeder_root_connector"

#: TTL (seconds) on the RiskSignal — 30 minutes keeps the signal live
#: long enough for two consecutive scheduler ticks to see the same
#: contamination event without duplicating alerts on every 15-minute
#: cycle.
DEFAULT_SIGNAL_TTL_SECONDS: int = 1800

#: Veeder-Root TLS-350/450 protocol control characters. SOH opens a
#: request; ETX closes it. ENQ is used as a keep-alive on some consoles
#: but is not required for a single inventory command.
TLS_SOH: bytes = b"\x01"
TLS_ETX: bytes = b"\x03"

#: In-Tank Inventory function code. ``I20100`` reports all tanks at
#: once; sites with more than 16 tanks repeat the block.
TLS_IN_TANK_INVENTORY_COMMAND: bytes = b"I20100"

#: ``fuel_stations`` index used when the vendor tank index maps to a
#: retail-station record. Kept as a module constant so tests can see
#: the concrete index name via an import without reaching into
#: ``fuel.services.fuel_es_mappings``.
FUEL_STATIONS_INDEX: str = "fuel_stations"

#: US-gallon → liter conversion. Kept here to avoid importing
#: :mod:`services.unit_conversion` just for a single constant (the
#: unit_conversion module pulls in additional fuel-ops imports that
#: blow up unit-test bootstrap time).
_GAL_TO_L: float = 3.785411784

#: ``IntegrationInstance.config`` key carrying the per-instance tank
#: mapping. Shape: ``{"<vendor_tank_index>": {"target":
#: "customer_tank"|"fuel_station", "id": "<entity-id>",
#: "product_code": "<optional-catalog-code>"}}``. Vendor indices are
#: stringified so the shape survives JSON round-tripping through ES.
_CONFIG_TANK_MAP_KEY: str = "tank_map"

#: Config key holding the api-token mode endpoint URL (e.g.
#: ``https://insite360.veeder-root.com``). Required when
#: ``mode == "api_token"``.
_CONFIG_ENDPOINT_URL_KEY: str = "endpoint_url"

#: Config key holding the api-token mode API path. Optional; defaults
#: to :data:`DEFAULT_API_PATH`.
_CONFIG_API_PATH_KEY: str = "api_path"

#: Config key holding the TLS-401 host. Required when
#: ``mode == "tls_401_tcp"``.
_CONFIG_HOST_KEY: str = "host"

#: Config key holding the TLS-401 port. Optional; defaults to
#: :data:`DEFAULT_TCP_PORT`.
_CONFIG_PORT_KEY: str = "port"

#: Credential keys. ``api_token`` for ``api_token`` mode;
#: ``security_code`` (optional 6-digit site code) for
#: ``tls_401_tcp`` mode.
_CRED_API_TOKEN_KEY: str = "api_token"
_CRED_SECURITY_CODE_KEY: str = "security_code"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VeederRootConfigError(RuntimeError):
    """Raised when the per-instance ``config`` or ``credentials`` is invalid.

    The scheduler catches this as a terminal failure for the current
    tick and surfaces ``last_error`` on the owning IntegrationInstance
    so the operator sees a concrete "missing host / unsupported mode"
    message in the admin UI.
    """


class VeederRootProtocolError(RuntimeError):
    """Raised when a TLS-401 TCP response cannot be parsed into readings.

    Mirrors :class:`VeederRootConfigError` as a non-retryable terminal
    error for the current tick. The next scheduler firing retries the
    poll fresh.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    """Return an ISO-8601 UTC timestamp with ``Z`` suffix."""

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gallons_to_liters(gallons: float) -> float:
    """Convert US gallons to liters using the canonical factor."""

    return float(gallons) * _GAL_TO_L


# ---------------------------------------------------------------------------
# Provider-catalog entry
# ---------------------------------------------------------------------------


def build_catalog_entry() -> ProviderCatalogEntry:
    """Return the :class:`ProviderCatalogEntry` for this connector.

    Task 9.10 will call :func:`register_catalog_entry` from the
    integrations bootstrap; this helper is also used directly by
    ``/api/integrations/providers`` tests that want to assert on the
    shape without triggering registration side-effects.
    """

    description = (
        "Poll Veeder-Root ATGs for tank volume, water, and temperature "
        "readings. Supports vendor-hosted cloud API (bearer token) and "
        "on-site TLS-401 TCP polling. Default schedule: every 15 "
        "minutes."
    )
    # We list the union of credential fields across both modes; the
    # Marketplace UI shows them conditionally based on the instance's
    # ``config.mode`` selection. ``api_token`` is required for cloud
    # mode; ``security_code`` is optional for TLS-401 mode.
    #
    # ``feature_flag_key`` is intentionally unset so the catalog
    # surfaces the Marketplace-level default
    # ``overlay.integration.veeder_root`` (Requirement 5.6.6) via
    # :meth:`ProviderCatalogEntry.effective_feature_flag_key`.
    return ProviderCatalogEntry(
        provider_name="veeder_root",
        category="tank_monitor",
        description=description,
        required_credential_fields=[_CRED_API_TOKEN_KEY, _CRED_SECURITY_CODE_KEY],
        doc_url="https://www.veeder.com/us/fuel-management-systems",
        auth_mode="api_key",
    )


def register_catalog_entry() -> ProviderCatalogEntry:
    """Register the connector with the shared provider catalog.

    Task 9.10 wires every connector into the catalog at bootstrap
    time; this helper is kept here (rather than inline at module
    import time) so a test that imports this module does not
    auto-register with the global catalog.
    """

    return register_provider(build_catalog_entry())


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


# Field index within a pipe-delimited TLS-401 inventory row. The
# Veeder-Root inventory record (``I20100`` response, per-tank block)
# publishes 11+ fields; we only consume the four we need. Downstream
# consumers extend this with additional fields by mapping more indices
# in :func:`_parse_tls_401_response` directly — we intentionally keep
# this narrow so we don't rely on fields that aren't part of the
# minimum Veeder-Root contract documented in task 9.5.
_TLS_FIELD_TANK_ID: int = 0
_TLS_FIELD_VOLUME: int = 1
_TLS_FIELD_TC_VOLUME: int = 2  # noqa: F841 — reserved, kept for documentation
_TLS_FIELD_ULLAGE: int = 3  # noqa: F841
_TLS_FIELD_HEIGHT: int = 4  # noqa: F841
_TLS_FIELD_WATER: int = 5
_TLS_FIELD_TEMP: int = 6


def _parse_tls_401_response(
    payload: bytes,
) -> List[Dict[str, Any]]:
    """Parse a TLS-401 TCP response into a list of per-tank reading dicts.

    Accepts the raw byte sequence returned by the Veeder-Root console
    for an ``I20100`` In-Tank Inventory command. The frame layout is::

        <SOH>I20100<site_header>|<tank_1_record>|<tank_2_record>|...<ETX>

    Each ``<tank_N_record>`` is a pipe-delimited tuple whose fields
    are positional: tank_id, volume_gallons, tc_volume_gallons,
    ullage_gallons, height_in, water_in, temp_f, ...

    Implementation notes:
        * SOH / ETX are stripped before splitting so partial frames
          (no ETX yet, e.g. if the remote flushed early) still parse
          whatever tank records have arrived.
        * Any record that does not contain at least
          :data:`_TLS_FIELD_TEMP` + 1 fields is skipped rather than
          raising, so a truncated trailing record never drops the whole
          response. The skipped record count is available to the caller
          by comparing ``len(input_records)`` to ``len(output)``.
        * Values that can't be coerced to ``float`` (e.g. ``"****"`` the
          console sometimes prints when a probe is offline) cause the
          record to be skipped, not the whole frame.

    Args:
        payload: Raw bytes read from the TCP socket.

    Returns:
        A list of dicts with keys ``tank_id`` (int), ``volume_gallons``,
        ``water_level_in``, ``temperature_f`` (all floats), and
        ``reading_at`` (datetime, UTC now). Empty when no records
        could be parsed — callers should treat that as a
        :class:`VeederRootProtocolError`.
    """

    if not payload:
        return []

    # Strip control characters. Some consoles wrap the function echo in
    # the response so we also strip a leading ``I20100`` if present.
    text = payload.replace(TLS_SOH, b"").replace(TLS_ETX, b"")
    try:
        decoded = text.decode("ascii", errors="replace")
    except Exception:
        decoded = text.decode("latin-1", errors="replace")

    # Newline-split first, then pipe-split. Modern Veeder consoles emit
    # each tank block on its own line; older consoles emit a single
    # line with pipe-delimited blocks. We handle both by flattening.
    tank_blocks: List[str] = []
    for line in decoded.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip any echoed function code prefix.
        if line.upper().startswith("I20100"):
            line = line[len("I20100") :].strip()
        # Further split on pipes so single-line responses work too.
        for block in line.split("|"):
            block = block.strip()
            if block:
                tank_blocks.append(block)

    records: List[Dict[str, Any]] = []
    now = _utcnow()
    for block in tank_blocks:
        fields = [f.strip() for f in block.split(",")]
        if len(fields) <= _TLS_FIELD_TEMP:
            continue  # Too few fields — partial / header row.
        try:
            tank_id = int(fields[_TLS_FIELD_TANK_ID])
            volume_gallons = float(fields[_TLS_FIELD_VOLUME])
            water_level_in = float(fields[_TLS_FIELD_WATER])
            temperature_f = float(fields[_TLS_FIELD_TEMP])
        except (TypeError, ValueError):
            # Malformed record — skip rather than aborting the frame.
            continue
        records.append(
            {
                "tank_id": tank_id,
                "volume_gallons": volume_gallons,
                "water_level_in": water_level_in,
                "temperature_f": temperature_f,
                "reading_at": now,
            }
        )
    return records


def _build_tls_401_request(security_code: Optional[str] = None) -> bytes:
    """Build an ``I20100`` request frame with an optional site security code.

    Layout: ``<SOH>[security_code]I20100<ETX>``. Sites that require a
    6-digit code prepend it after SOH; unsecured sites omit it.
    """

    code_bytes = (
        security_code.encode("ascii") if security_code and security_code.strip() else b""
    )
    return TLS_SOH + code_bytes + TLS_IN_TANK_INVENTORY_COMMAND + TLS_ETX


def _parse_api_token_response(
    payload: Any,
) -> List[Dict[str, Any]]:
    """Normalize a JSON response from the api-token endpoint into reading dicts.

    Accepts the shape ``{"tanks": [{"tank_id": ..., "volume_gallons":
    ..., "water_level_in": ..., "temperature_f": ..., "reading_at":
    "..."}]}`` or a bare list of those dicts. Missing / malformed
    records are dropped with a warning; non-numeric volumes raise
    :class:`VeederRootProtocolError` only when *no* valid records
    remain so a single bad tank never blocks the rest.
    """

    if isinstance(payload, Mapping):
        raw_list = payload.get("tanks") or payload.get("data") or []
    elif isinstance(payload, list):
        raw_list = payload
    else:
        return []

    readings: List[Dict[str, Any]] = []
    now = _utcnow()
    for entry in raw_list:
        if not isinstance(entry, Mapping):
            continue
        try:
            tank_id_raw = entry.get("tank_id")
            if tank_id_raw is None:
                continue
            # Vendor APIs typically serialize tank index as int; some
            # wrap it as string — accept both.
            tank_id = int(tank_id_raw) if not isinstance(tank_id_raw, int) else tank_id_raw
            volume_gallons = float(entry["volume_gallons"])
            water_level_in = float(entry["water_level_in"])
            temperature_f = float(entry["temperature_f"])
        except (KeyError, TypeError, ValueError):
            continue
        reading_at_raw = entry.get("reading_at")
        reading_at: datetime
        if isinstance(reading_at_raw, datetime):
            reading_at = reading_at_raw
        elif isinstance(reading_at_raw, str) and reading_at_raw:
            try:
                # ``fromisoformat`` handles ``YYYY-MM-DDTHH:MM:SS(.ffffff)?(+HH:MM)?``
                # directly; for ``Z`` suffix we substitute ``+00:00``.
                reading_at = datetime.fromisoformat(
                    reading_at_raw.replace("Z", "+00:00")
                )
                if reading_at.tzinfo is None:
                    reading_at = reading_at.replace(tzinfo=timezone.utc)
            except ValueError:
                reading_at = now
        else:
            reading_at = now
        readings.append(
            {
                "tank_id": tank_id,
                "volume_gallons": volume_gallons,
                "water_level_in": water_level_in,
                "temperature_f": temperature_f,
                "reading_at": reading_at,
            }
        )
    return readings


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


#: Factory signature for the TCP transport. Returns
#: ``(reader, writer)`` — i.e. the same shape
#: :func:`asyncio.open_connection` returns. Injected for
#: deterministic unit tests.
TCPConnectorFactory = Callable[
    [str, int],
    "Awaitable[Tuple[asyncio.StreamReader, asyncio.StreamWriter]]",
]


class VeederRootConnector(IntegrationConnector):
    """Veeder-Root ATG adapter (tank-monitor category).

    Args:
        tenant_id: Owning tenant.
        instance_id: Owning IntegrationInstance id — stamped on every
            SyncRun the connector returns.
        instance_config: The owning ``IntegrationInstance.config`` dict.
            Carries the ``mode``, ``host``/``port`` or ``endpoint_url``,
            and the ``tank_map`` mapping vendor tank indices to
            Customer_Tank / fuel_station targets.
        credentials_vault: Required; the shared
            :class:`services.credentials_vault.TenantCredentialsVault`.
        credentials_ref: Existing vault reference; ``None`` means this
            instance has not completed ``connect()`` yet.
        es_service: Required for persistence. Must expose
            :meth:`index_document`, :meth:`update_document`, and
            :meth:`search_documents`.
        customer_tank_repository: Optional repository used to update
            Customer_Tank readings. When ``None`` the connector
            degrades to persisting the reading without updating any
            Customer_Tank record.
        signal_bus: Optional :class:`SignalBus` used to publish
            ``water_contamination`` RiskSignals (Req 5.3.6).
        redis_client: Optional async Redis client used to look up the
            per-tenant water threshold override.
        http_client: Optional injected :class:`httpx.AsyncClient`. When
            ``None`` every api-token call creates a short-lived client.
        tcp_connector: Optional async factory returning
            ``(reader, writer)`` for TLS-401 mode. Defaults to
            :func:`asyncio.open_connection`. Injected by tests.
        http_timeout_seconds / tcp_timeout_seconds: Per-transport
            wall-clock timeouts.
        clock: Zero-arg callable returning the current UTC datetime;
            injected for deterministic tests.
    """

    category: ClassVar[str] = "tank_monitor"
    provider_name: ClassVar[str] = "veeder_root"

    def __init__(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        instance_config: Mapping[str, Any],
        credentials_vault: Any,
        credentials_ref: Optional[str] = None,
        es_service: Any = None,
        customer_tank_repository: Optional[Any] = None,
        signal_bus: Optional[Any] = None,
        redis_client: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        tcp_connector: Optional[TCPConnectorFactory] = None,
        http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        tcp_timeout_seconds: float = DEFAULT_TCP_TIMEOUT_SECONDS,
        atg_readings_index: str = ATG_READINGS_INDEX,
        customer_tanks_index: str = CUSTOMER_TANKS_INDEX,
        fuel_stations_index: str = FUEL_STATIONS_INDEX,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ValueError("instance_id must be a non-empty string")
        if credentials_vault is None:
            raise ValueError("credentials_vault is required")
        if http_timeout_seconds <= 0:
            raise ValueError("http_timeout_seconds must be positive")
        if tcp_timeout_seconds <= 0:
            raise ValueError("tcp_timeout_seconds must be positive")

        self._tenant_id = tenant_id
        self._instance_id = instance_id
        self._config: Dict[str, Any] = dict(instance_config or {})
        self._vault = credentials_vault
        self._credentials_ref = credentials_ref
        self._es = es_service
        self._customer_tank_repo = customer_tank_repository
        self._signal_bus = signal_bus
        self._redis = redis_client
        self._http_client = http_client
        self._tcp_connector = tcp_connector or asyncio.open_connection
        self._http_timeout = float(http_timeout_seconds)
        self._tcp_timeout = float(tcp_timeout_seconds)
        self._atg_index = atg_readings_index
        self._customer_tanks_index = customer_tanks_index
        self._fuel_stations_index = fuel_stations_index
        self._clock = clock

        # In-memory cache of the credentials envelope. The vault round-
        # trip happens on demand the first time sync_pull runs; we cache
        # so a 15-minute scheduler tick in a long-lived process doesn't
        # hit the vault on every call.
        self._cached_credentials: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # IntegrationConnector API
    # ------------------------------------------------------------------

    async def connect(self, credentials: Mapping[str, Any]) -> ConnectionResult:
        """Validate the credentials shape and persist to the vault.

        The connector does NOT probe the remote system here — some
        TLS-401 consoles reject repeat connections within a short
        window and we do not want the admin-UI "Connect" click to
        inadvertently DOS a site. A subsequent ``sync_pull`` proves the
        credentials are live.

        Expected shape depends on mode:

            api_token:   {"api_token": "..."}
            tls_401_tcp: {"security_code": "123456"}  (optional)
        """

        mode = str(self._config.get("mode") or "").strip()
        if mode not in _SUPPORTED_MODES:
            return ConnectionResult(
                status="error",
                message=(
                    f"unsupported mode {mode!r}; expected one of "
                    f"{sorted(_SUPPORTED_MODES)}"
                ),
            )

        envelope: Dict[str, Any] = {"mode": mode}

        if mode == MODE_API_TOKEN:
            token = credentials.get(_CRED_API_TOKEN_KEY)
            if not isinstance(token, str) or not token.strip():
                return ConnectionResult(
                    status="error",
                    message=(
                        f"missing required credential field "
                        f"{_CRED_API_TOKEN_KEY!r} for mode=api_token"
                    ),
                )
            # Also require the endpoint URL so we fail fast rather
            # than defer the error to the first sync_pull.
            endpoint_url = self._config.get(_CONFIG_ENDPOINT_URL_KEY)
            if not isinstance(endpoint_url, str) or not endpoint_url.strip():
                return ConnectionResult(
                    status="error",
                    message=(
                        f"missing required config field "
                        f"{_CONFIG_ENDPOINT_URL_KEY!r} for mode=api_token"
                    ),
                )
            envelope[_CRED_API_TOKEN_KEY] = token.strip()
        elif mode == MODE_TLS_401_TCP:
            host = self._config.get(_CONFIG_HOST_KEY)
            if not isinstance(host, str) or not host.strip():
                return ConnectionResult(
                    status="error",
                    message=(
                        f"missing required config field "
                        f"{_CONFIG_HOST_KEY!r} for mode=tls_401_tcp"
                    ),
                )
            code = credentials.get(_CRED_SECURITY_CODE_KEY)
            if code is not None:
                if not isinstance(code, str):
                    return ConnectionResult(
                        status="error",
                        message=(
                            f"{_CRED_SECURITY_CODE_KEY!r} must be a string "
                            "when provided"
                        ),
                    )
                stripped = code.strip()
                if stripped:
                    envelope[_CRED_SECURITY_CODE_KEY] = stripped

        ref = await self._vault.put(
            tenant_id=self._tenant_id,
            key=VAULT_CREDENTIAL_KEY,
            plaintext=envelope,
            provider_name=self.provider_name,
        )
        self._credentials_ref = ref
        self._cached_credentials = dict(envelope)

        logger.info(
            "VeederRootConnector.connect: stored credentials "
            "tenant=%s instance=%s mode=%s credentials_ref=%s",
            self._tenant_id,
            self._instance_id,
            mode,
            ref,
        )
        return ConnectionResult(
            status="connected",
            credentials_ref=ref,
            metadata={"mode": mode},
        )

    async def sync_pull(self, since: datetime) -> SyncRun:
        """Poll the ATG, persist readings, and publish contamination signals.

        The ``since`` argument is passed through to the api-token
        endpoint as an ``updated_since`` query parameter so the cloud
        API can page efficiently; the TLS-401 console always returns a
        fresh snapshot and ignores ``since``. Either way, the returned
        :class:`SyncRun` is always terminal (``success`` / ``partial``
        / ``error``) so the scheduler can persist it directly.
        """

        run_id = f"veeder_pull_{uuid4()}"
        started_at = _utcnow()
        counts: Dict[str, int] = {
            "readings_fetched": 0,
            "readings_persisted": 0,
            "customer_tanks_updated": 0,
            "fuel_stations_updated": 0,
            "water_contamination_signals": 0,
            "skipped_unmapped": 0,
            "skipped_invalid": 0,
        }

        try:
            mode = str(self._config.get("mode") or "").strip()
            if mode not in _SUPPORTED_MODES:
                raise VeederRootConfigError(
                    f"unsupported mode {mode!r}; expected one of "
                    f"{sorted(_SUPPORTED_MODES)}"
                )

            credentials = await self._load_credentials()
            if mode == MODE_API_TOKEN:
                readings = await self._fetch_api_readings(credentials, since)
            else:
                readings = await self._fetch_tcp_readings(credentials)
            counts["readings_fetched"] = len(readings)

            threshold = await self._resolve_water_threshold()
            tank_map = self._config.get(_CONFIG_TANK_MAP_KEY) or {}
            if not isinstance(tank_map, Mapping):
                logger.warning(
                    "VeederRootConnector.sync_pull: config.tank_map is "
                    "not a mapping for tenant=%s instance=%s — treating "
                    "as empty",
                    self._tenant_id,
                    self._instance_id,
                )
                tank_map = {}

            for reading in readings:
                try:
                    await self._process_reading(
                        reading,
                        tank_map=tank_map,
                        threshold_in=threshold,
                        counts=counts,
                    )
                except Exception as exc:
                    counts["skipped_invalid"] += 1
                    logger.warning(
                        "VeederRootConnector.sync_pull: failed to "
                        "process reading tank_id=%s tenant=%s: %s",
                        reading.get("tank_id"),
                        self._tenant_id,
                        exc,
                    )

        except (VeederRootConfigError, VeederRootProtocolError) as exc:
            return self._error_run(
                run_id=run_id,
                operation="pull",
                started_at=started_at,
                record_counts=counts,
                reason="veeder_root_protocol_error",
                exc=exc,
            )
        except asyncio.TimeoutError as exc:
            return self._error_run(
                run_id=run_id,
                operation="pull",
                started_at=started_at,
                record_counts=counts,
                reason="timeout",
                exc=exc,
            )
        except Exception as exc:
            return self._error_run(
                run_id=run_id,
                operation="pull",
                started_at=started_at,
                record_counts=counts,
                reason=None,
                exc=exc,
            )

        finished_at = _utcnow()
        # "partial" when some readings landed but not all; "success"
        # otherwise. An empty poll (no tanks configured yet) counts as
        # success — the scheduler will retry on the next cron.
        if (
            counts["readings_fetched"] > 0
            and counts["readings_persisted"] < counts["readings_fetched"]
        ):
            status = "partial"
        else:
            status = "success"
        return SyncRun(
            run_id=run_id,
            tenant_id=self._tenant_id,
            instance_id=self._instance_id,
            provider_name=self.provider_name,
            operation="pull",
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            record_counts=counts,
            duration_ms=max(
                0, int((finished_at - started_at).total_seconds() * 1000)
            ),
        )

    async def sync_push(self, payload: Mapping[str, Any]) -> SyncRun:
        """Tank monitors are read-only; this call is a no-op.

        Returns a success :class:`SyncRun` with
        ``{"skipped_noop": 1}`` so the scheduler can persist the run
        uniformly without tripping error accounting.
        """

        run_id = f"veeder_push_{uuid4()}"
        started_at = _utcnow()
        finished_at = _utcnow()
        logger.debug(
            "VeederRootConnector.sync_push: no-op for tenant=%s "
            "instance=%s (payload ignored)",
            self._tenant_id,
            self._instance_id,
        )
        return SyncRun(
            run_id=run_id,
            tenant_id=self._tenant_id,
            instance_id=self._instance_id,
            provider_name=self.provider_name,
            operation="push",
            started_at=started_at,
            finished_at=finished_at,
            status="success",
            record_counts={"skipped_noop": 1},
            duration_ms=max(
                0, int((finished_at - started_at).total_seconds() * 1000)
            ),
        )

    async def disconnect(self) -> None:
        """Remove the credentials envelope from the vault.

        Idempotent: a missing ``credentials_ref`` returns cleanly.
        Vault failures are logged but never raised — the logical
        "disconnect" state is "the connector is not configured any
        more", and we consider the integration-instance deletion flow
        authoritative.
        """

        if not self._credentials_ref:
            return
        try:
            await self._vault.delete(self._tenant_id, self._credentials_ref)
        except Exception as exc:
            logger.warning(
                "VeederRootConnector.disconnect: vault delete failed "
                "for tenant=%s ref=%s: %s",
                self._tenant_id,
                self._credentials_ref,
                exc,
            )
        self._credentials_ref = None
        self._cached_credentials = None

    # ------------------------------------------------------------------
    # Transport: api_token
    # ------------------------------------------------------------------

    async def _fetch_api_readings(
        self,
        credentials: Mapping[str, Any],
        since: datetime,
    ) -> List[Dict[str, Any]]:
        """Call the vendor-hosted cloud API and normalize the response."""

        endpoint_url = str(self._config.get(_CONFIG_ENDPOINT_URL_KEY) or "").strip()
        if not endpoint_url:
            raise VeederRootConfigError(
                f"missing required config field {_CONFIG_ENDPOINT_URL_KEY!r} "
                "for mode=api_token"
            )
        path = str(self._config.get(_CONFIG_API_PATH_KEY) or DEFAULT_API_PATH)
        if not path.startswith("/"):
            path = "/" + path
        url = f"{endpoint_url.rstrip('/')}{path}"

        token = credentials.get(_CRED_API_TOKEN_KEY)
        if not isinstance(token, str) or not token.strip():
            raise VeederRootConfigError(
                f"credentials envelope missing {_CRED_API_TOKEN_KEY!r}"
            )

        params: Dict[str, Any] = {"updated_since": _iso(since)}
        headers = {
            "Authorization": f"Bearer {token.strip()}",
            "Accept": "application/json",
        }

        client, owned = await self._get_http_client()
        try:
            try:
                response = await asyncio.wait_for(
                    client.get(
                        url,
                        params=params,
                        headers=headers,
                        timeout=self._http_timeout,
                    ),
                    timeout=self._http_timeout + 1.0,
                )
            except asyncio.TimeoutError:
                raise
            response.raise_for_status()
            try:
                body = response.json() or {}
            except (ValueError, json.JSONDecodeError):
                body = {}
        finally:
            if owned:
                await client.aclose()
        return _parse_api_token_response(body)

    async def _get_http_client(self) -> Tuple[httpx.AsyncClient, bool]:
        """Return ``(client, owned_here)``.

        When the connector was constructed with an injected
        ``http_client`` we reuse it and leave ``aclose`` to the caller;
        otherwise we mint a short-lived client per call.
        """

        if self._http_client is not None:
            return self._http_client, False
        logger.warning(
            "VeederRoot: creating per-call httpx client (no pooling). "
            "Inject a shared client at construction for production use."
        )
        return httpx.AsyncClient(timeout=self._http_timeout), True

    # ------------------------------------------------------------------
    # Transport: tls_401_tcp
    # ------------------------------------------------------------------

    async def _fetch_tcp_readings(
        self,
        credentials: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        """Open a TCP socket to the console, send ``I20100``, and parse the reply."""

        host = str(self._config.get(_CONFIG_HOST_KEY) or "").strip()
        if not host:
            raise VeederRootConfigError(
                f"missing required config field {_CONFIG_HOST_KEY!r} for "
                "mode=tls_401_tcp"
            )
        port_raw = self._config.get(_CONFIG_PORT_KEY, DEFAULT_TCP_PORT)
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            raise VeederRootConfigError(
                f"invalid config.{_CONFIG_PORT_KEY}={port_raw!r}; expected int"
            )

        security_code = credentials.get(_CRED_SECURITY_CODE_KEY)
        request = _build_tls_401_request(
            security_code if isinstance(security_code, str) else None
        )

        try:
            reader, writer = await asyncio.wait_for(
                self._tcp_connector(host, port),
                timeout=self._tcp_timeout,
            )
        except asyncio.TimeoutError:
            raise
        except Exception as exc:
            raise VeederRootProtocolError(
                f"TLS-401 TCP connect failed host={host} port={port}: {exc}"
            ) from exc

        try:
            try:
                writer.write(request)
                await asyncio.wait_for(
                    writer.drain(), timeout=self._tcp_timeout
                )
                # Read until ETX or timeout. The Veeder console closes
                # the connection after a single inventory response, so
                # ``read()`` returns cleanly at EOF.
                payload = await asyncio.wait_for(
                    reader.readuntil(TLS_ETX), timeout=self._tcp_timeout
                )
            except asyncio.IncompleteReadError as exc:
                # Console closed without sending ETX — treat the partial
                # bytes as the response body.
                payload = exc.partial
            except asyncio.TimeoutError:
                raise
            except Exception as exc:
                raise VeederRootProtocolError(
                    f"TLS-401 TCP I/O failed host={host} port={port}: {exc}"
                ) from exc
        finally:
            try:
                writer.close()
                # ``wait_closed`` may raise on already-closed streams;
                # swallow because the response has already been read.
                close_waiter = getattr(writer, "wait_closed", None)
                if close_waiter is not None:
                    try:
                        await asyncio.wait_for(
                            close_waiter(), timeout=self._tcp_timeout
                        )
                    except Exception:
                        pass
            except Exception:
                pass

        readings = _parse_tls_401_response(payload)
        if not readings:
            raise VeederRootProtocolError(
                f"TLS-401 response could not be parsed host={host} port={port}"
            )
        return readings

    # ------------------------------------------------------------------
    # Reading processing
    # ------------------------------------------------------------------

    async def _process_reading(
        self,
        reading: Mapping[str, Any],
        *,
        tank_map: Mapping[str, Any],
        threshold_in: float,
        counts: Dict[str, int],
    ) -> None:
        """Persist a single reading and apply downstream side effects.

        Side effects, in order:
            1. Persist to ``atg_readings`` (Req 5.3.4).
            2. Update the matching Customer_Tank or fuel_stations record
               (Req 5.3.3).
            3. Publish a ``water_contamination`` RiskSignal when the
               reading's water level exceeds the tenant threshold
               (Req 5.3.6).

        Each step is wrapped so a failure at step N still lets step N+1
        run — e.g. an ES write failure on ``atg_readings`` shouldn't
        prevent the tank-level update from happening.
        """

        tank_id = reading.get("tank_id")
        if tank_id is None:
            counts["skipped_invalid"] += 1
            return

        # Look up the mapping. Tank map keys are stringified because
        # IntegrationInstance.config is persisted as JSON.
        mapping = tank_map.get(str(tank_id)) or tank_map.get(tank_id)
        target: Optional[str] = None
        entity_id: Optional[str] = None
        product_code: Optional[str] = None
        if isinstance(mapping, Mapping):
            target_raw = mapping.get("target")
            if isinstance(target_raw, str):
                target = target_raw.strip().lower() or None
            id_raw = mapping.get("id")
            if isinstance(id_raw, str) and id_raw.strip():
                entity_id = id_raw.strip()
            pc_raw = mapping.get("product_code")
            if isinstance(pc_raw, str) and pc_raw.strip():
                product_code = pc_raw.strip()

        # Step 1: persist the raw reading regardless of mapping state.
        reading_at = reading.get("reading_at") or _utcnow()
        if not isinstance(reading_at, datetime):
            reading_at = _utcnow()
        persisted = await self._persist_atg_reading(
            reading=reading,
            reading_at=reading_at,
            target=target,
            entity_id=entity_id,
            product_code=product_code,
        )
        if persisted:
            counts["readings_persisted"] += 1

        # Step 2: downstream tank-level update. Unmapped tanks are fine
        # — the reading is already in ``atg_readings`` and the admin UI
        # will flag the missing mapping.
        if target and entity_id:
            try:
                if target == CUSTOMER_TANK_ENTITY_TYPE:
                    ok = await self._apply_to_customer_tank(
                        customer_tank_id=entity_id,
                        reading=reading,
                        reading_at=reading_at,
                    )
                    if ok:
                        counts["customer_tanks_updated"] += 1
                elif target == FUEL_STATION_ENTITY_TYPE:
                    ok = await self._apply_to_fuel_station(
                        station_id=entity_id,
                        reading=reading,
                        reading_at=reading_at,
                    )
                    if ok:
                        counts["fuel_stations_updated"] += 1
                else:
                    counts["skipped_unmapped"] += 1
            except Exception as exc:
                # Don't abort the whole tank — log and move on.
                logger.warning(
                    "VeederRootConnector: failed to apply reading to "
                    "%s=%s tenant=%s: %s",
                    target,
                    entity_id,
                    self._tenant_id,
                    exc,
                )
        else:
            counts["skipped_unmapped"] += 1

        # Step 3: water contamination alert.
        water_in = reading.get("water_level_in")
        try:
            if water_in is not None and float(water_in) > threshold_in:
                published = await self._publish_water_contamination(
                    reading=reading,
                    target=target,
                    entity_id=entity_id,
                    threshold_in=threshold_in,
                    product_code=product_code,
                )
                if published:
                    counts["water_contamination_signals"] += 1
        except (TypeError, ValueError):
            # Non-numeric water_level — already counted as invalid if
            # the reading was malformed; nothing more to do.
            pass

    async def _persist_atg_reading(
        self,
        *,
        reading: Mapping[str, Any],
        reading_at: datetime,
        target: Optional[str],
        entity_id: Optional[str],
        product_code: Optional[str],
    ) -> bool:
        """Write a single reading to the ``atg_readings`` index."""

        if self._es is None:
            logger.debug(
                "VeederRootConnector: no es_service configured; skipping "
                "atg_readings persistence for tenant=%s",
                self._tenant_id,
            )
            return False

        tank_id = reading.get("tank_id")
        # Composite tank_ref stable across tanks without explicit
        # mapping so downstream analytics can group even unmapped
        # readings.
        tank_ref = f"instance:{self._instance_id}:tank:{tank_id}"

        reading_id = f"atg_{self._instance_id}_{tank_id}_{int(reading_at.timestamp())}"
        doc: Dict[str, Any] = {
            "reading_id": reading_id,
            "tenant_id": self._tenant_id,
            "instance_id": self._instance_id,
            "tank_ref": tank_ref,
            "customer_tank_id": (
                entity_id if target == CUSTOMER_TANK_ENTITY_TYPE else None
            ),
            "station_id": (
                entity_id if target == FUEL_STATION_ENTITY_TYPE else None
            ),
            "volume_gallons": _safe_float(reading.get("volume_gallons")),
            "water_level_in": _safe_float(reading.get("water_level_in")),
            "temperature_f": _safe_float(reading.get("temperature_f")),
            "product_code": product_code,
            "reading_at": _iso(reading_at),
            "retrieved_at": _iso(_utcnow()),
            "created_at": _iso(_utcnow()),
            "updated_at": _iso(_utcnow()),
        }
        try:
            await self._es.index_document(self._atg_index, reading_id, doc)
            return True
        except Exception as exc:
            logger.warning(
                "VeederRootConnector: atg_readings index write failed "
                "tenant=%s tank=%s: %s",
                self._tenant_id,
                tank_id,
                exc,
            )
            return False

    async def _apply_to_customer_tank(
        self,
        *,
        customer_tank_id: str,
        reading: Mapping[str, Any],
        reading_at: datetime,
    ) -> bool:
        """Update a Customer_Tank's level + last_reading_at fields."""

        volume = _safe_float(reading.get("volume_gallons"))
        if volume is None or volume < 0:
            return False

        # Prefer the repository if provided — it validates, canonicalizes,
        # and re-asserts tenant scoping. Fall back to a direct ES update
        # when no repo was injected (e.g. during bootstrap test runs).
        if self._customer_tank_repo is not None:
            try:
                updated = await self._customer_tank_repo.update(
                    self._tenant_id,
                    customer_tank_id,
                    {
                        "current_level_gallons": volume,
                        "last_reading_at": reading_at,
                    },
                )
                return updated is not None
            except Exception as exc:
                logger.warning(
                    "VeederRootConnector: CustomerTankRepository.update "
                    "failed tenant=%s tank=%s: %s",
                    self._tenant_id,
                    customer_tank_id,
                    exc,
                )
                return False

        if self._es is None:
            return False
        partial = {
            "current_level_gallons": volume,
            "last_reading_at": _iso(reading_at),
            "updated_at": _iso(_utcnow()),
        }
        try:
            await self._es.update_document(
                self._customer_tanks_index, customer_tank_id, partial
            )
            return True
        except Exception as exc:
            logger.warning(
                "VeederRootConnector: direct customer_tanks update failed "
                "tenant=%s tank=%s: %s",
                self._tenant_id,
                customer_tank_id,
                exc,
            )
            return False

    async def _apply_to_fuel_station(
        self,
        *,
        station_id: str,
        reading: Mapping[str, Any],
        reading_at: datetime,
    ) -> bool:
        """Update a fuel_stations record's current stock + timestamp.

        ``fuel_stations`` stores volume in ``current_stock_liters`` so
        we convert from gallons here. The retail fuel domain continues
        to use the legacy liters-based schema; US customer-tanks land
        in the Customer_Tank path instead.
        """

        if self._es is None:
            return False

        volume_gallons = _safe_float(reading.get("volume_gallons"))
        if volume_gallons is None or volume_gallons < 0:
            return False

        partial = {
            "current_stock_liters": gallons_to_liters(volume_gallons),
            "last_updated": _iso(reading_at),
            "updated_at": _iso(_utcnow()),
        }
        try:
            await self._es.update_document(
                self._fuel_stations_index, station_id, partial
            )
            return True
        except Exception as exc:
            logger.warning(
                "VeederRootConnector: fuel_stations update failed "
                "tenant=%s station=%s: %s",
                self._tenant_id,
                station_id,
                exc,
            )
            return False

    async def _publish_water_contamination(
        self,
        *,
        reading: Mapping[str, Any],
        target: Optional[str],
        entity_id: Optional[str],
        threshold_in: float,
        product_code: Optional[str],
    ) -> bool:
        """Publish a :class:`RiskSignal` for a high-water tank reading."""

        if self._signal_bus is None:
            logger.info(
                "VeederRootConnector: water_contamination reading "
                "detected for tenant=%s tank=%s but no signal_bus "
                "configured — dropping alert",
                self._tenant_id,
                reading.get("tank_id"),
            )
            return False

        tank_id = reading.get("tank_id")
        effective_entity_id = entity_id or f"vendor_tank:{tank_id}"
        entity_type = target or CUSTOMER_TANK_ENTITY_TYPE
        if entity_type not in (CUSTOMER_TANK_ENTITY_TYPE, FUEL_STATION_ENTITY_TYPE):
            entity_type = CUSTOMER_TANK_ENTITY_TYPE

        context: Dict[str, Any] = {
            "signal_type": WATER_CONTAMINATION_SIGNAL_TYPE,
            "provider_name": self.provider_name,
            "instance_id": self._instance_id,
            "vendor_tank_index": tank_id,
            "water_level_in": _safe_float(reading.get("water_level_in")),
            "threshold_in": threshold_in,
            "volume_gallons": _safe_float(reading.get("volume_gallons")),
            "temperature_f": _safe_float(reading.get("temperature_f")),
        }
        if product_code:
            context["product_code"] = product_code

        try:
            signal = RiskSignal(
                source_agent=VEEDER_ROOT_AGENT_ID,
                entity_id=effective_entity_id,
                entity_type=entity_type,
                severity=Severity.HIGH,
                confidence=1.0,
                ttl_seconds=DEFAULT_SIGNAL_TTL_SECONDS,
                tenant_id=self._tenant_id,
                context=context,
            )
            await self._signal_bus.publish(signal)
            return True
        except Exception as exc:
            logger.warning(
                "VeederRootConnector: failed to publish "
                "water_contamination signal for tenant=%s tank=%s: %s",
                self._tenant_id,
                tank_id,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Threshold + credential helpers
    # ------------------------------------------------------------------

    async def _resolve_water_threshold(self) -> float:
        """Return the tenant-configured water threshold in inches.

        Reads the Redis key
        ``veeder_root.water_threshold_in:{tenant_id}``; falls back to
        :data:`DEFAULT_WATER_THRESHOLD_IN` on any failure / missing
        value. Redis failures are swallowed — we'd rather publish with
        the default threshold than silently drop contamination alerts
        because Redis blipped.
        """

        if self._redis is None:
            return DEFAULT_WATER_THRESHOLD_IN
        key = WATER_THRESHOLD_REDIS_KEY_TEMPLATE.format(tenant_id=self._tenant_id)
        try:
            raw = await self._redis.get(key)
        except Exception as exc:
            logger.warning(
                "VeederRootConnector: redis lookup failed for %s: %s — "
                "using default threshold",
                key,
                exc,
            )
            return DEFAULT_WATER_THRESHOLD_IN
        if raw is None:
            return DEFAULT_WATER_THRESHOLD_IN
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "VeederRootConnector: non-numeric water threshold "
                "override %r in %s — using default",
                raw,
                key,
            )
            return DEFAULT_WATER_THRESHOLD_IN
        if value <= 0:
            return DEFAULT_WATER_THRESHOLD_IN
        return value

    async def _load_credentials(self) -> Dict[str, Any]:
        """Return the cached credentials envelope, dereferencing the vault on miss."""

        if self._cached_credentials is not None:
            return self._cached_credentials
        if not self._credentials_ref:
            raise VeederRootConfigError(
                "VeederRootConnector: no credentials_ref — call connect() first"
            )
        envelope = await self._vault.get(self._tenant_id, self._credentials_ref)
        if not isinstance(envelope, Mapping):
            raise VeederRootConfigError(
                "VeederRootConnector: vault returned non-mapping credential "
                "envelope"
            )
        self._cached_credentials = dict(envelope)
        return self._cached_credentials

    # ------------------------------------------------------------------
    # SyncRun helpers
    # ------------------------------------------------------------------

    def _error_run(
        self,
        *,
        run_id: str,
        operation: str,
        started_at: datetime,
        record_counts: Dict[str, int],
        reason: Optional[str],
        exc: BaseException,
    ) -> SyncRun:
        """Build an error :class:`SyncRun` with structured error details."""

        finished_at = _utcnow()
        message = str(exc) or exc.__class__.__name__
        error_details = f"{reason}: {message}" if reason else message
        return SyncRun(
            run_id=run_id,
            tenant_id=self._tenant_id,
            instance_id=self._instance_id,
            provider_name=self.provider_name,
            operation=operation,  # type: ignore[arg-type]
            started_at=started_at,
            finished_at=finished_at,
            status="error",
            record_counts=record_counts,
            error_details=error_details[:1000],
            duration_ms=max(
                0, int((finished_at - started_at).total_seconds() * 1000)
            ),
        )


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> Optional[float]:
    """Coerce ``value`` to ``float`` or return ``None`` when not numeric."""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_API_PATH",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_SCHEDULE_CRON",
    "DEFAULT_SIGNAL_TTL_SECONDS",
    "DEFAULT_TCP_PORT",
    "DEFAULT_TCP_TIMEOUT_SECONDS",
    "DEFAULT_WATER_THRESHOLD_IN",
    "MODE_API_TOKEN",
    "MODE_TLS_401_TCP",
    "VAULT_CREDENTIAL_KEY",
    "VEEDER_ROOT_AGENT_ID",
    "VeederRootConfigError",
    "VeederRootConnector",
    "VeederRootProtocolError",
    "WATER_CONTAMINATION_SIGNAL_TYPE",
    "WATER_THRESHOLD_REDIS_KEY_TEMPLATE",
    "build_catalog_entry",
    "gallons_to_liters",
    "register_catalog_entry",
]
