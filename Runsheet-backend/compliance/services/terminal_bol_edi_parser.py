"""Terminal BOL EDI Parser — pluggable strategy pattern.

This module implements the EDI parsing layer for the Terminal BOL Ingestion
Service (design §10). It supports two common formats used by US fuel terminals:

1. **ANSI X12 856** (Ship Notice/Manifest) — the standard EDI format used by
   larger terminals and rack automation systems.
2. **Pipe-delimited flat file** — a simpler format common among smaller US
   terminals that transmit BOL data as pipe-separated values.

The parser is pluggable via a strategy pattern:

- :class:`BOLParserStrategy` defines the protocol (ABC) that all parsers must
  implement: ``parse``, ``serialize``, and ``can_parse``.
- :class:`X12856Parser` handles ANSI X12 856 payloads.
- :class:`PipeDelimitedParser` handles pipe-delimited flat files.
- :class:`EDIParserRegistry` auto-detects the format and dispatches to the
  correct parser, raising :class:`EDIParseError` if no parser can handle the
  payload.

Validates: Requirements 10.1, 10.8
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EDIParseError(Exception):
    """Raised when an EDI payload cannot be parsed by any registered strategy."""

    def __init__(self, message: str, payload_preview: Optional[str] = None):
        self.payload_preview = payload_preview
        super().__init__(message)


# ---------------------------------------------------------------------------
# Required fields extracted from EDI payloads (Req 10.1)
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = frozenset(
    [
        "load_number",
        "product_code",
        "gross_gallons",
        "net_gallons",
        "observed_temperature",
        "api_gravity",
        "supplier_name",
        "terminal_name",
        "driver_id",
        "timestamp",
    ]
)


# ---------------------------------------------------------------------------
# Strategy Protocol / ABC
# ---------------------------------------------------------------------------


class BOLParserStrategy(ABC):
    """Abstract base class for BOL EDI parser strategies.

    Each concrete strategy must implement:
    - ``parse(payload)`` — extract BOL fields from raw bytes.
    - ``serialize(fields)`` — convert fields back to the EDI format.
    - ``can_parse(payload)`` — detect whether this strategy handles the payload.
    """

    @abstractmethod
    def parse(self, payload: bytes) -> Dict[str, Any]:
        """Parse raw EDI payload into a dictionary of BOL fields.

        Returns a dict with at minimum the keys defined in
        :data:`REQUIRED_FIELDS`. Numeric values (gross_gallons, net_gallons,
        observed_temperature, api_gravity) are returned as floats. The
        ``timestamp`` field is returned as an ISO-8601 string.

        Raises:
            EDIParseError: If the payload is malformed or missing required data.
        """
        ...

    @abstractmethod
    def serialize(self, fields: Dict[str, Any]) -> bytes:
        """Serialize parsed BOL fields back to the EDI format.

        This enables the round-trip property (Req 10.8): parsing then
        serializing should produce a semantically equivalent document.

        Args:
            fields: Dictionary of BOL fields as returned by ``parse()``.

        Returns:
            Raw bytes in the format this strategy handles.
        """
        ...

    @abstractmethod
    def can_parse(self, payload: bytes) -> bool:
        """Detect whether this strategy can handle the given payload.

        Returns True if the payload matches the expected format signature.
        This method should be fast and non-throwing.
        """
        ...


# ---------------------------------------------------------------------------
# X12 856 Parser Strategy
# ---------------------------------------------------------------------------


class X12856Parser(BOLParserStrategy):
    """Parser for ANSI X12 856 (Ship Notice/Manifest) EDI format.

    The X12 856 format uses segment terminators (``~``) and element separators
    (``*``). Key segments for terminal BOL data:

    - ISA: Interchange control header
    - GS: Functional group header
    - ST: Transaction set header (856)
    - BSN: Beginning segment for ship notice
    - HL: Hierarchical level
    - TD1: Carrier details (product info)
    - TD5: Routing/transit info
    - REF: Reference identification (load number, driver ID)
    - DTM: Date/time reference
    - N1: Party identification (supplier, terminal)
    - QTY: Quantity (gross/net gallons)
    - MEA: Measurements (temperature, API gravity)
    - SE: Transaction set trailer
    - GE: Functional group trailer
    - IEA: Interchange control trailer
    """

    #: Segment terminator for X12 messages
    SEGMENT_TERMINATOR = "~"
    #: Element separator for X12 messages
    ELEMENT_SEPARATOR = "*"

    def can_parse(self, payload: bytes) -> bool:
        """Detect X12 856 format by checking for ISA header and ST*856."""
        try:
            text = payload.decode("utf-8", errors="replace").strip()
            return text.startswith("ISA") and "ST*856" in text
        except Exception:
            return False

    def parse(self, payload: bytes) -> Dict[str, Any]:
        """Parse X12 856 EDI payload into BOL fields.

        Extracts fields from the following segments:
        - REF*LN: load_number
        - REF*DR: driver_id
        - TD1*...*product_code
        - QTY*GR: gross_gallons
        - QTY*NT: net_gallons
        - MEA*TM*TE: observed_temperature
        - MEA*PD*AG: api_gravity
        - N1*SU: supplier_name
        - N1*TL: terminal_name
        - DTM*011: timestamp
        """
        try:
            text = payload.decode("utf-8").strip()
        except UnicodeDecodeError as e:
            raise EDIParseError(f"Cannot decode X12 payload as UTF-8: {e}")

        segments = [
            s.strip() for s in text.split(self.SEGMENT_TERMINATOR) if s.strip()
        ]

        fields: Dict[str, Any] = {}

        for segment in segments:
            elements = segment.split(self.ELEMENT_SEPARATOR)
            seg_id = elements[0] if elements else ""

            if seg_id == "REF" and len(elements) >= 3:
                qualifier = elements[1]
                if qualifier == "LN":
                    fields["load_number"] = elements[2]
                elif qualifier == "DR":
                    fields["driver_id"] = elements[2]

            elif seg_id == "TD1" and len(elements) >= 3:
                # TD1*packaging_code*qty*product_code
                if len(elements) >= 4:
                    fields["product_code"] = elements[3]
                elif len(elements) >= 3:
                    fields["product_code"] = elements[2]

            elif seg_id == "QTY" and len(elements) >= 3:
                qualifier = elements[1]
                try:
                    value = float(elements[2])
                except (ValueError, IndexError):
                    continue
                if qualifier == "GR":
                    fields["gross_gallons"] = value
                elif qualifier == "NT":
                    fields["net_gallons"] = value

            elif seg_id == "MEA" and len(elements) >= 4:
                mea_qualifier = elements[1]
                mea_type = elements[2]
                try:
                    value = float(elements[3])
                except (ValueError, IndexError):
                    continue
                if mea_qualifier == "TM" and mea_type == "TE":
                    fields["observed_temperature"] = value
                elif mea_qualifier == "PD" and mea_type == "AG":
                    fields["api_gravity"] = value

            elif seg_id == "N1" and len(elements) >= 3:
                qualifier = elements[1]
                if qualifier == "SU":
                    fields["supplier_name"] = elements[2]
                elif qualifier == "TL":
                    fields["terminal_name"] = elements[2]

            elif seg_id == "DTM" and len(elements) >= 3:
                qualifier = elements[1]
                if qualifier == "011":
                    fields["timestamp"] = self._parse_x12_datetime(elements[2])

        # Validate all required fields are present
        missing = REQUIRED_FIELDS - set(fields.keys())
        if missing:
            raise EDIParseError(
                f"X12 856 payload missing required fields: {sorted(missing)}",
                payload_preview=text[:200],
            )

        return fields

    def serialize(self, fields: Dict[str, Any]) -> bytes:
        """Serialize BOL fields back to X12 856 format.

        Produces a minimal but valid X12 856 document containing all the
        segments needed to round-trip the BOL data.
        """
        sep = self.ELEMENT_SEPARATOR
        term = self.SEGMENT_TERMINATOR

        timestamp_str = self._format_x12_datetime(fields.get("timestamp", ""))

        segments = [
            f"ISA{sep}00{sep}          {sep}00{sep}          {sep}ZZ{sep}SENDER         {sep}ZZ{sep}RECEIVER       {sep}000000{sep}0000{sep}U{sep}00401{sep}000000001{sep}0{sep}P{sep}>",
            f"GS{sep}SH{sep}SENDER{sep}RECEIVER{sep}20240101{sep}0000{sep}1{sep}X{sep}004010",
            f"ST{sep}856{sep}0001",
            f"BSN{sep}00{sep}0001{sep}20240101{sep}0000",
            f"HL{sep}1{sep}{sep}S{sep}1",
            f"TD1{sep}CTN{sep}1{sep}{fields.get('product_code', '')}",
            f"REF{sep}LN{sep}{fields.get('load_number', '')}",
            f"REF{sep}DR{sep}{fields.get('driver_id', '')}",
            f"QTY{sep}GR{sep}{fields.get('gross_gallons', 0)}",
            f"QTY{sep}NT{sep}{fields.get('net_gallons', 0)}",
            f"MEA{sep}TM{sep}TE{sep}{fields.get('observed_temperature', 0)}",
            f"MEA{sep}PD{sep}AG{sep}{fields.get('api_gravity', 0)}",
            f"N1{sep}SU{sep}{fields.get('supplier_name', '')}",
            f"N1{sep}TL{sep}{fields.get('terminal_name', '')}",
            f"DTM{sep}011{sep}{timestamp_str}",
            f"SE{sep}13{sep}0001",
            f"GE{sep}1{sep}1",
            f"IEA{sep}1{sep}000000001",
        ]

        return (term.join(segments) + term).encode("utf-8")

    @staticmethod
    def _parse_x12_datetime(value: str) -> str:
        """Parse X12 date/time format (YYYYMMDD or YYYYMMDDHHmm) to ISO-8601."""
        value = value.strip()
        try:
            if len(value) == 8:
                dt = datetime.strptime(value, "%Y%m%d")
            elif len(value) == 12:
                dt = datetime.strptime(value, "%Y%m%d%H%M")
            elif len(value) == 14:
                dt = datetime.strptime(value, "%Y%m%d%H%M%S")
            else:
                # Try ISO format as fallback
                dt = datetime.fromisoformat(value)
            return dt.isoformat()
        except ValueError:
            return value

    @staticmethod
    def _format_x12_datetime(value: str) -> str:
        """Format ISO-8601 timestamp back to X12 format (YYYYMMDDHHmm)."""
        if not value:
            return "000000000000"
        try:
            dt = datetime.fromisoformat(value)
            return dt.strftime("%Y%m%d%H%M")
        except (ValueError, TypeError):
            return str(value)


# ---------------------------------------------------------------------------
# Pipe-Delimited Parser Strategy
# ---------------------------------------------------------------------------


class PipeDelimitedParser(BOLParserStrategy):
    """Parser for pipe-delimited flat file format.

    Common among smaller US fuel terminals. The format consists of:
    - A header row with field names separated by ``|``
    - One or more data rows with values separated by ``|``

    Expected header fields (case-insensitive):
    load_number|product_code|gross_gallons|net_gallons|observed_temperature|
    api_gravity|supplier_name|terminal_name|driver_id|timestamp
    """

    #: Field separator
    SEPARATOR = "|"

    #: Canonical header field names (lowercase, order-independent)
    CANONICAL_HEADERS = [
        "load_number",
        "product_code",
        "gross_gallons",
        "net_gallons",
        "observed_temperature",
        "api_gravity",
        "supplier_name",
        "terminal_name",
        "driver_id",
        "timestamp",
    ]

    #: Numeric fields that should be parsed as floats
    NUMERIC_FIELDS = frozenset(
        ["gross_gallons", "net_gallons", "observed_temperature", "api_gravity"]
    )

    def can_parse(self, payload: bytes) -> bool:
        """Detect pipe-delimited format by checking for pipe characters and header row."""
        try:
            text = payload.decode("utf-8", errors="replace").strip()
            lines = text.split("\n")
            if not lines:
                return False
            header = lines[0].strip()
            # Must contain pipes and at least some recognized field names
            if self.SEPARATOR not in header:
                return False
            header_fields = [
                f.strip().lower() for f in header.split(self.SEPARATOR)
            ]
            recognized = set(header_fields) & set(self.CANONICAL_HEADERS)
            return len(recognized) >= 5
        except Exception:
            return False

    def parse(self, payload: bytes) -> Dict[str, Any]:
        """Parse pipe-delimited payload into BOL fields.

        Expects a header row followed by at least one data row.
        Returns the first data row parsed into a field dictionary.
        """
        try:
            text = payload.decode("utf-8").strip()
        except UnicodeDecodeError as e:
            raise EDIParseError(
                f"Cannot decode pipe-delimited payload as UTF-8: {e}"
            )

        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if len(lines) < 2:
            raise EDIParseError(
                "Pipe-delimited payload must have at least a header and one data row",
                payload_preview=text[:200],
            )

        # Parse header
        header_fields = [
            f.strip().lower() for f in lines[0].split(self.SEPARATOR)
        ]

        # Parse first data row
        data_values = [v.strip() for v in lines[1].split(self.SEPARATOR)]

        if len(data_values) != len(header_fields):
            raise EDIParseError(
                f"Data row has {len(data_values)} fields but header has "
                f"{len(header_fields)} fields",
                payload_preview=text[:200],
            )

        # Build fields dict
        fields: Dict[str, Any] = {}
        for header, value in zip(header_fields, data_values):
            normalized = header.strip().lower()
            if normalized in self.NUMERIC_FIELDS:
                try:
                    fields[normalized] = float(value)
                except ValueError:
                    raise EDIParseError(
                        f"Cannot parse numeric field '{normalized}' value: '{value}'",
                        payload_preview=text[:200],
                    )
            else:
                fields[normalized] = value

        # Validate all required fields are present
        missing = REQUIRED_FIELDS - set(fields.keys())
        if missing:
            raise EDIParseError(
                f"Pipe-delimited payload missing required fields: {sorted(missing)}",
                payload_preview=text[:200],
            )

        return fields

    def serialize(self, fields: Dict[str, Any]) -> bytes:
        """Serialize BOL fields back to pipe-delimited format.

        Produces a header row followed by a data row with all required fields.
        """
        # Use canonical header order
        headers = list(self.CANONICAL_HEADERS)
        values = []
        for h in headers:
            val = fields.get(h, "")
            values.append(str(val))

        header_line = self.SEPARATOR.join(headers)
        data_line = self.SEPARATOR.join(values)

        return f"{header_line}\n{data_line}\n".encode("utf-8")


# ---------------------------------------------------------------------------
# EDI Parser Registry
# ---------------------------------------------------------------------------


class EDIParserRegistry:
    """Registry of BOL parser strategies with auto-detection.

    Holds registered parser strategies and dispatches incoming payloads to
    the first strategy whose ``can_parse()`` returns True. If no strategy
    matches, raises :class:`EDIParseError`.

    Usage::

        registry = EDIParserRegistry()
        registry.register(X12856Parser())
        registry.register(PipeDelimitedParser())

        fields = registry.parse(raw_payload)
    """

    def __init__(self) -> None:
        self._strategies: List[BOLParserStrategy] = []

    def register(self, strategy: BOLParserStrategy) -> None:
        """Register a parser strategy.

        Strategies are evaluated in registration order during auto-detection.
        """
        self._strategies.append(strategy)
        logger.info(
            "Registered EDI parser strategy: %s",
            type(strategy).__name__,
        )

    @property
    def strategies(self) -> List[BOLParserStrategy]:
        """Return the list of registered strategies (read-only copy)."""
        return list(self._strategies)

    def detect_parser(self, payload: bytes) -> BOLParserStrategy:
        """Auto-detect the appropriate parser for the given payload.

        Returns the first registered strategy whose ``can_parse()`` returns
        True.

        Raises:
            EDIParseError: If no registered strategy can handle the payload.
        """
        for strategy in self._strategies:
            try:
                if strategy.can_parse(payload):
                    return strategy
            except Exception as e:
                logger.warning(
                    "Strategy %s raised during can_parse: %s",
                    type(strategy).__name__,
                    e,
                )
                continue

        preview = payload[:100].decode("utf-8", errors="replace")
        raise EDIParseError(
            "No registered parser strategy can handle this payload",
            payload_preview=preview,
        )

    def parse(self, payload: bytes) -> Dict[str, Any]:
        """Auto-detect format and parse the payload.

        Convenience method that combines ``detect_parser()`` and
        ``strategy.parse()``.

        Returns:
            Dictionary of parsed BOL fields.

        Raises:
            EDIParseError: If no parser matches or parsing fails.
        """
        strategy = self.detect_parser(payload)
        logger.debug(
            "Using parser strategy: %s", type(strategy).__name__
        )
        return strategy.parse(payload)

    def serialize(self, fields: Dict[str, Any], strategy: BOLParserStrategy) -> bytes:
        """Serialize fields using the specified strategy.

        Args:
            fields: Dictionary of BOL fields.
            strategy: The parser strategy to use for serialization.

        Returns:
            Raw bytes in the strategy's format.
        """
        return strategy.serialize(fields)


# ---------------------------------------------------------------------------
# Module-level factory — convenience for service wiring
# ---------------------------------------------------------------------------


def create_default_registry() -> EDIParserRegistry:
    """Create an EDIParserRegistry pre-loaded with the standard strategies.

    Returns a registry with X12856Parser and PipeDelimitedParser registered
    in that order (X12 is checked first since it has a more distinctive
    signature).
    """
    registry = EDIParserRegistry()
    registry.register(X12856Parser())
    registry.register(PipeDelimitedParser())
    return registry
