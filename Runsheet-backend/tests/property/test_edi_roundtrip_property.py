"""
Property-based tests for EDI Parse → Serialize → Semantic Equivalence.

# Feature: fuel-compliance-backbone, Terminal BOL Ingestion Service

**Validates: Requirement 10.8**

FOR ALL terminal BOLs ingested via EDI, parsing the EDI payload then
serializing the parsed record back to EDI format SHALL produce a
semantically equivalent document (round-trip property for the EDI parser).
"""

import pytest
from hypothesis import given, settings, assume
from hypothesis.strategies import (
    composite,
    floats,
    integers,
    text,
    sampled_from,
)

from compliance.services.terminal_bol_edi_parser import (
    X12856Parser,
    PipeDelimitedParser,
    EDIParserRegistry,
    create_default_registry,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRODUCT_CODES = [
    "UNL87",
    "UNL89",
    "UNL93",
    "DIESEL",
    "OFF_ROAD_DIESEL",
    "PROPANE",
    "HEATING_OIL",
    "KEROSENE",
    "JET_A",
    "BIODIESEL_B20",
]

SUPPLIER_NAMES = [
    "Marathon Petroleum",
    "Valero Energy",
    "Phillips 66",
    "ExxonMobil",
    "Shell Trading",
    "BP Products",
    "Citgo Petroleum",
    "PBF Energy",
]

TERMINAL_NAMES = [
    "Houston Ship Channel",
    "Linden NJ Terminal",
    "Chicago Argo Terminal",
    "Pasadena TX Rack",
    "Baltimore Harbor",
    "Albany NY Terminal",
    "Tampa Port Terminal",
    "Portland OR Rack",
]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Safe text that avoids EDI-breaking characters (no pipes, tildes, asterisks, newlines)
_safe_text = text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- ",
    min_size=1,
    max_size=30,
)

# Load numbers: alphanumeric identifiers
_load_numbers = text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=4,
    max_size=15,
)

# Driver IDs: alphanumeric
_driver_ids = text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=3,
    max_size=12,
)

# Gallons: positive floats in realistic range
_gallons = floats(min_value=100.0, max_value=12000.0, allow_nan=False, allow_infinity=False)

# Temperature: valid range per VCF spec (-50 to 150 °F)
_temperatures = floats(min_value=-50.0, max_value=150.0, allow_nan=False, allow_infinity=False)

# API gravity: valid range (0 to 100)
_api_gravities = floats(min_value=10.0, max_value=80.0, allow_nan=False, allow_infinity=False)

# Timestamps in ISO-8601 format (generate valid datetimes)
_years = integers(min_value=2020, max_value=2030)
_months = integers(min_value=1, max_value=12)
_days = integers(min_value=1, max_value=28)  # safe for all months
_hours = integers(min_value=0, max_value=23)
_minutes = integers(min_value=0, max_value=59)


@composite
def _bol_fields(draw):
    """Generate a complete set of valid BOL fields for round-trip testing."""
    year = draw(_years)
    month = draw(_months)
    day = draw(_days)
    hour = draw(_hours)
    minute = draw(_minutes)
    timestamp = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00"

    load_number = draw(_load_numbers)
    assume(len(load_number.strip()) > 0)

    driver_id = draw(_driver_ids)
    assume(len(driver_id.strip()) > 0)

    return {
        "load_number": load_number,
        "product_code": draw(sampled_from(PRODUCT_CODES)),
        "gross_gallons": round(draw(_gallons), 1),
        "net_gallons": round(draw(_gallons), 1),
        "observed_temperature": round(draw(_temperatures), 1),
        "api_gravity": round(draw(_api_gravities), 1),
        "supplier_name": draw(sampled_from(SUPPLIER_NAMES)),
        "terminal_name": draw(sampled_from(TERMINAL_NAMES)),
        "driver_id": driver_id,
        "timestamp": timestamp,
    }


# ---------------------------------------------------------------------------
# Helper: Semantic equivalence check
# ---------------------------------------------------------------------------

def assert_semantically_equivalent(original: dict, restored: dict):
    """Assert that two BOL field dicts are semantically equivalent.

    Semantic equivalence means:
    - String fields match exactly (after stripping whitespace)
    - Numeric fields match within floating-point tolerance (±0.01)
    - Timestamp fields represent the same point in time (ignoring seconds
      that may be truncated in some formats)
    """
    for field in [
        "load_number",
        "product_code",
        "supplier_name",
        "terminal_name",
        "driver_id",
    ]:
        orig_val = str(original.get(field, "")).strip()
        rest_val = str(restored.get(field, "")).strip()
        assert orig_val == rest_val, (
            f"String field '{field}' mismatch: "
            f"original={orig_val!r}, restored={rest_val!r}"
        )

    for field in ["gross_gallons", "net_gallons", "observed_temperature", "api_gravity"]:
        orig_val = float(original.get(field, 0))
        rest_val = float(restored.get(field, 0))
        assert abs(orig_val - rest_val) < 0.01, (
            f"Numeric field '{field}' mismatch: "
            f"original={orig_val}, restored={rest_val}, "
            f"diff={abs(orig_val - rest_val)}"
        )

    # Timestamp: compare up to minute precision (X12 format truncates seconds)
    orig_ts = str(original.get("timestamp", ""))[:16]
    rest_ts = str(restored.get("timestamp", ""))[:16]
    assert orig_ts == rest_ts, (
        f"Timestamp mismatch: original={orig_ts!r}, restored={rest_ts!r}"
    )


# ---------------------------------------------------------------------------
# Property Tests — X12 856 Format Round-Trip
# ---------------------------------------------------------------------------

class TestX12RoundTrip:
    """EDI parse → serialize → semantic equivalence for X12 856 format.

    **Validates: Requirement 10.8**
    """

    @given(fields=_bol_fields())
    @settings(max_examples=200)
    def test_serialize_then_parse_roundtrip(self, fields: dict):
        """
        Serializing BOL fields to X12 856 format and then parsing the result
        SHALL produce semantically equivalent fields.

        **Validates: Requirement 10.8**
        """
        parser = X12856Parser()

        # Serialize fields → X12 EDI bytes
        edi_payload = parser.serialize(fields)

        # Parse the serialized EDI back to fields
        restored_fields = parser.parse(edi_payload)

        # Assert semantic equivalence
        assert_semantically_equivalent(fields, restored_fields)

    @given(fields=_bol_fields())
    @settings(max_examples=200)
    def test_double_roundtrip_stability(self, fields: dict):
        """
        Performing the round-trip twice (serialize → parse → serialize → parse)
        SHALL produce the same result as a single round-trip, demonstrating
        idempotency of the parse/serialize cycle.

        **Validates: Requirement 10.8**
        """
        parser = X12856Parser()

        # First round-trip
        edi_1 = parser.serialize(fields)
        fields_1 = parser.parse(edi_1)

        # Second round-trip
        edi_2 = parser.serialize(fields_1)
        fields_2 = parser.parse(edi_2)

        # Second round-trip should be identical to first
        assert_semantically_equivalent(fields_1, fields_2)


# ---------------------------------------------------------------------------
# Property Tests — Pipe-Delimited Format Round-Trip
# ---------------------------------------------------------------------------

class TestPipeDelimitedRoundTrip:
    """EDI parse → serialize → semantic equivalence for pipe-delimited format.

    **Validates: Requirement 10.8**
    """

    @given(fields=_bol_fields())
    @settings(max_examples=200)
    def test_serialize_then_parse_roundtrip(self, fields: dict):
        """
        Serializing BOL fields to pipe-delimited format and then parsing the
        result SHALL produce semantically equivalent fields.

        **Validates: Requirement 10.8**
        """
        parser = PipeDelimitedParser()

        # Serialize fields → pipe-delimited bytes
        edi_payload = parser.serialize(fields)

        # Parse the serialized payload back to fields
        restored_fields = parser.parse(edi_payload)

        # Assert semantic equivalence
        assert_semantically_equivalent(fields, restored_fields)

    @given(fields=_bol_fields())
    @settings(max_examples=200)
    def test_double_roundtrip_stability(self, fields: dict):
        """
        Performing the round-trip twice SHALL produce the same result as a
        single round-trip (idempotency).

        **Validates: Requirement 10.8**
        """
        parser = PipeDelimitedParser()

        # First round-trip
        payload_1 = parser.serialize(fields)
        fields_1 = parser.parse(payload_1)

        # Second round-trip
        payload_2 = parser.serialize(fields_1)
        fields_2 = parser.parse(payload_2)

        # Second round-trip should be identical to first
        assert_semantically_equivalent(fields_1, fields_2)


# ---------------------------------------------------------------------------
# Property Tests — Registry Auto-Detection Round-Trip
# ---------------------------------------------------------------------------

class TestRegistryRoundTrip:
    """EDI parse → serialize → semantic equivalence via the registry.

    Tests that the auto-detection mechanism correctly identifies the format
    and the full round-trip through the registry produces equivalent results.

    **Validates: Requirement 10.8**
    """

    @given(fields=_bol_fields())
    @settings(max_examples=150)
    def test_x12_via_registry(self, fields: dict):
        """
        Serializing to X12 format and parsing via the registry (auto-detect)
        SHALL produce semantically equivalent fields.

        **Validates: Requirement 10.8**
        """
        registry = create_default_registry()
        x12_parser = X12856Parser()

        # Serialize using X12 strategy
        edi_payload = x12_parser.serialize(fields)

        # Parse via registry (should auto-detect X12)
        restored_fields = registry.parse(edi_payload)

        # Assert semantic equivalence
        assert_semantically_equivalent(fields, restored_fields)

    @given(fields=_bol_fields())
    @settings(max_examples=150)
    def test_pipe_delimited_via_registry(self, fields: dict):
        """
        Serializing to pipe-delimited format and parsing via the registry
        (auto-detect) SHALL produce semantically equivalent fields.

        **Validates: Requirement 10.8**
        """
        registry = create_default_registry()
        pipe_parser = PipeDelimitedParser()

        # Serialize using pipe-delimited strategy
        edi_payload = pipe_parser.serialize(fields)

        # Parse via registry (should auto-detect pipe-delimited)
        restored_fields = registry.parse(edi_payload)

        # Assert semantic equivalence
        assert_semantically_equivalent(fields, restored_fields)
