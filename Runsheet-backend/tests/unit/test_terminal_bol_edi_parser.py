"""Unit tests for terminal_bol_edi_parser.py.

Tests both parser strategies (X12 856 and pipe-delimited), the EDI parser
registry auto-detection, error handling, and round-trip serialization.

Validates: Requirements 10.1, 10.8
"""

from __future__ import annotations

import pytest

from compliance.services.terminal_bol_edi_parser import (
    BOLParserStrategy,
    EDIParseError,
    EDIParserRegistry,
    PipeDelimitedParser,
    REQUIRED_FIELDS,
    X12856Parser,
    create_default_registry,
)


# ---------------------------------------------------------------------------
# Fixtures — sample payloads
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_x12_payload() -> bytes:
    """A valid X12 856 EDI payload with all required BOL fields."""
    segments = [
        "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *240115*1030*U*00401*000000001*0*P*>",
        "GS*SH*SENDER*RECEIVER*20240115*1030*1*X*004010",
        "ST*856*0001",
        "BSN*00*0001*20240115*1030",
        "HL*1**S*1",
        "TD1*CTN*1*UNL87",
        "REF*LN*LOAD-2024-001",
        "REF*DR*DRV-100",
        "QTY*GR*8500.0",
        "QTY*NT*8450.5",
        "MEA*TM*TE*72.5",
        "MEA*PD*AG*58.2",
        "N1*SU*Marathon Petroleum",
        "N1*TL*Pasadena Terminal",
        "DTM*011*202401151030",
        "SE*13*0001",
        "GE*1*1",
        "IEA*1*000000001",
    ]
    return "~".join(segments).encode("utf-8") + b"~"


@pytest.fixture
def valid_pipe_payload() -> bytes:
    """A valid pipe-delimited payload with all required BOL fields."""
    header = "load_number|product_code|gross_gallons|net_gallons|observed_temperature|api_gravity|supplier_name|terminal_name|driver_id|timestamp"
    data = "LOAD-2024-001|UNL87|8500.0|8450.5|72.5|58.2|Marathon Petroleum|Pasadena Terminal|DRV-100|2024-01-15T10:30:00"
    return f"{header}\n{data}\n".encode("utf-8")


@pytest.fixture
def x12_parser() -> X12856Parser:
    """X12 856 parser instance."""
    return X12856Parser()


@pytest.fixture
def pipe_parser() -> PipeDelimitedParser:
    """Pipe-delimited parser instance."""
    return PipeDelimitedParser()


@pytest.fixture
def registry() -> EDIParserRegistry:
    """Pre-configured registry with both strategies."""
    return create_default_registry()


# ---------------------------------------------------------------------------
# X12 856 Parser Tests
# ---------------------------------------------------------------------------


class TestX12856Parser:
    """Tests for the X12 856 parser strategy."""

    def test_can_parse_valid_x12(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """X12 parser detects valid X12 856 payloads."""
        assert x12_parser.can_parse(valid_x12_payload) is True

    def test_can_parse_rejects_pipe_delimited(self, x12_parser: X12856Parser, valid_pipe_payload: bytes):
        """X12 parser rejects pipe-delimited payloads."""
        assert x12_parser.can_parse(valid_pipe_payload) is False

    def test_can_parse_rejects_empty(self, x12_parser: X12856Parser):
        """X12 parser rejects empty payloads."""
        assert x12_parser.can_parse(b"") is False

    def test_can_parse_rejects_random_bytes(self, x12_parser: X12856Parser):
        """X12 parser rejects random/garbage data."""
        assert x12_parser.can_parse(b"\x00\x01\x02\x03") is False

    def test_parse_extracts_all_required_fields(
        self, x12_parser: X12856Parser, valid_x12_payload: bytes
    ):
        """Parsing a valid X12 payload extracts all required fields."""
        fields = x12_parser.parse(valid_x12_payload)
        assert set(fields.keys()) >= REQUIRED_FIELDS

    def test_parse_load_number(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """Parsing extracts the correct load_number from REF*LN segment."""
        fields = x12_parser.parse(valid_x12_payload)
        assert fields["load_number"] == "LOAD-2024-001"

    def test_parse_product_code(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """Parsing extracts the correct product_code from TD1 segment."""
        fields = x12_parser.parse(valid_x12_payload)
        assert fields["product_code"] == "UNL87"

    def test_parse_gross_gallons(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """Parsing extracts gross_gallons as a float from QTY*GR segment."""
        fields = x12_parser.parse(valid_x12_payload)
        assert fields["gross_gallons"] == 8500.0

    def test_parse_net_gallons(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """Parsing extracts net_gallons as a float from QTY*NT segment."""
        fields = x12_parser.parse(valid_x12_payload)
        assert fields["net_gallons"] == 8450.5

    def test_parse_observed_temperature(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """Parsing extracts observed_temperature from MEA*TM*TE segment."""
        fields = x12_parser.parse(valid_x12_payload)
        assert fields["observed_temperature"] == 72.5

    def test_parse_api_gravity(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """Parsing extracts api_gravity from MEA*PD*AG segment."""
        fields = x12_parser.parse(valid_x12_payload)
        assert fields["api_gravity"] == 58.2

    def test_parse_supplier_name(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """Parsing extracts supplier_name from N1*SU segment."""
        fields = x12_parser.parse(valid_x12_payload)
        assert fields["supplier_name"] == "Marathon Petroleum"

    def test_parse_terminal_name(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """Parsing extracts terminal_name from N1*TL segment."""
        fields = x12_parser.parse(valid_x12_payload)
        assert fields["terminal_name"] == "Pasadena Terminal"

    def test_parse_driver_id(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """Parsing extracts driver_id from REF*DR segment."""
        fields = x12_parser.parse(valid_x12_payload)
        assert fields["driver_id"] == "DRV-100"

    def test_parse_timestamp(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """Parsing extracts and converts timestamp from DTM*011 segment."""
        fields = x12_parser.parse(valid_x12_payload)
        assert "2024-01-15" in fields["timestamp"]
        assert "10:30" in fields["timestamp"]

    def test_parse_missing_fields_raises_error(self, x12_parser: X12856Parser):
        """Parsing an X12 payload with missing required fields raises EDIParseError."""
        # Minimal X12 with only ISA and ST*856 but no data segments
        incomplete = b"ISA*00*          *00*          *ZZ*SENDER*ZZ*RECEIVER*000000*0000*U*00401*000000001*0*P*>~ST*856*0001~SE*2*0001~"
        with pytest.raises(EDIParseError, match="missing required fields"):
            x12_parser.parse(incomplete)

    def test_round_trip_serialization(self, x12_parser: X12856Parser, valid_x12_payload: bytes):
        """Parse → serialize → parse produces semantically equivalent fields (Req 10.8)."""
        original_fields = x12_parser.parse(valid_x12_payload)
        serialized = x12_parser.serialize(original_fields)
        reparsed_fields = x12_parser.parse(serialized)

        # All required fields must match semantically
        for field in REQUIRED_FIELDS:
            if field in ("gross_gallons", "net_gallons", "observed_temperature", "api_gravity"):
                assert float(reparsed_fields[field]) == float(original_fields[field]), (
                    f"Round-trip mismatch for {field}: "
                    f"{reparsed_fields[field]} != {original_fields[field]}"
                )
            elif field == "timestamp":
                # Timestamps may differ in format but must represent the same time
                assert "2024-01-15" in reparsed_fields[field]
                assert "10:30" in reparsed_fields[field]
            else:
                assert reparsed_fields[field] == original_fields[field], (
                    f"Round-trip mismatch for {field}: "
                    f"{reparsed_fields[field]} != {original_fields[field]}"
                )


# ---------------------------------------------------------------------------
# Pipe-Delimited Parser Tests
# ---------------------------------------------------------------------------


class TestPipeDelimitedParser:
    """Tests for the pipe-delimited parser strategy."""

    def test_can_parse_valid_pipe(self, pipe_parser: PipeDelimitedParser, valid_pipe_payload: bytes):
        """Pipe parser detects valid pipe-delimited payloads."""
        assert pipe_parser.can_parse(valid_pipe_payload) is True

    def test_can_parse_rejects_x12(self, pipe_parser: PipeDelimitedParser, valid_x12_payload: bytes):
        """Pipe parser rejects X12 payloads."""
        assert pipe_parser.can_parse(valid_x12_payload) is False

    def test_can_parse_rejects_empty(self, pipe_parser: PipeDelimitedParser):
        """Pipe parser rejects empty payloads."""
        assert pipe_parser.can_parse(b"") is False

    def test_can_parse_rejects_csv(self, pipe_parser: PipeDelimitedParser):
        """Pipe parser rejects CSV (comma-separated) payloads."""
        csv_data = b"load_number,product_code,gross_gallons\nLOAD-001,UNL87,8500\n"
        assert pipe_parser.can_parse(csv_data) is False

    def test_parse_extracts_all_required_fields(
        self, pipe_parser: PipeDelimitedParser, valid_pipe_payload: bytes
    ):
        """Parsing a valid pipe payload extracts all required fields."""
        fields = pipe_parser.parse(valid_pipe_payload)
        assert set(fields.keys()) >= REQUIRED_FIELDS

    def test_parse_load_number(self, pipe_parser: PipeDelimitedParser, valid_pipe_payload: bytes):
        """Parsing extracts the correct load_number."""
        fields = pipe_parser.parse(valid_pipe_payload)
        assert fields["load_number"] == "LOAD-2024-001"

    def test_parse_product_code(self, pipe_parser: PipeDelimitedParser, valid_pipe_payload: bytes):
        """Parsing extracts the correct product_code."""
        fields = pipe_parser.parse(valid_pipe_payload)
        assert fields["product_code"] == "UNL87"

    def test_parse_numeric_fields(self, pipe_parser: PipeDelimitedParser, valid_pipe_payload: bytes):
        """Parsing converts numeric fields to floats."""
        fields = pipe_parser.parse(valid_pipe_payload)
        assert fields["gross_gallons"] == 8500.0
        assert fields["net_gallons"] == 8450.5
        assert fields["observed_temperature"] == 72.5
        assert fields["api_gravity"] == 58.2

    def test_parse_supplier_name(self, pipe_parser: PipeDelimitedParser, valid_pipe_payload: bytes):
        """Parsing extracts supplier_name."""
        fields = pipe_parser.parse(valid_pipe_payload)
        assert fields["supplier_name"] == "Marathon Petroleum"

    def test_parse_terminal_name(self, pipe_parser: PipeDelimitedParser, valid_pipe_payload: bytes):
        """Parsing extracts terminal_name."""
        fields = pipe_parser.parse(valid_pipe_payload)
        assert fields["terminal_name"] == "Pasadena Terminal"

    def test_parse_driver_id(self, pipe_parser: PipeDelimitedParser, valid_pipe_payload: bytes):
        """Parsing extracts driver_id."""
        fields = pipe_parser.parse(valid_pipe_payload)
        assert fields["driver_id"] == "DRV-100"

    def test_parse_timestamp(self, pipe_parser: PipeDelimitedParser, valid_pipe_payload: bytes):
        """Parsing extracts timestamp as string."""
        fields = pipe_parser.parse(valid_pipe_payload)
        assert fields["timestamp"] == "2024-01-15T10:30:00"

    def test_parse_header_only_raises_error(self, pipe_parser: PipeDelimitedParser):
        """Parsing a payload with only a header row raises EDIParseError."""
        header_only = b"load_number|product_code|gross_gallons|net_gallons|observed_temperature|api_gravity|supplier_name|terminal_name|driver_id|timestamp\n"
        with pytest.raises(EDIParseError, match="at least a header and one data row"):
            pipe_parser.parse(header_only)

    def test_parse_mismatched_columns_raises_error(self, pipe_parser: PipeDelimitedParser):
        """Parsing a payload with mismatched column count raises EDIParseError."""
        bad_payload = (
            b"load_number|product_code|gross_gallons|net_gallons|observed_temperature|api_gravity|supplier_name|terminal_name|driver_id|timestamp\n"
            b"LOAD-001|UNL87|8500\n"
        )
        with pytest.raises(EDIParseError, match="fields but header has"):
            pipe_parser.parse(bad_payload)

    def test_parse_invalid_numeric_raises_error(self, pipe_parser: PipeDelimitedParser):
        """Parsing a payload with non-numeric values in numeric fields raises EDIParseError."""
        bad_payload = (
            b"load_number|product_code|gross_gallons|net_gallons|observed_temperature|api_gravity|supplier_name|terminal_name|driver_id|timestamp\n"
            b"LOAD-001|UNL87|NOT_A_NUMBER|8450.5|72.5|58.2|Supplier|Terminal|DRV-100|2024-01-15T10:30:00\n"
        )
        with pytest.raises(EDIParseError, match="Cannot parse numeric field"):
            pipe_parser.parse(bad_payload)

    def test_parse_missing_fields_raises_error(self, pipe_parser: PipeDelimitedParser):
        """Parsing a payload missing required columns raises EDIParseError."""
        # Only 3 fields in header
        bad_payload = (
            b"load_number|product_code|gross_gallons|net_gallons|observed_temperature|api_gravity\n"
            b"LOAD-001|UNL87|8500|8450|72.5|58.2\n"
        )
        with pytest.raises(EDIParseError, match="missing required fields"):
            pipe_parser.parse(bad_payload)

    def test_round_trip_serialization(self, pipe_parser: PipeDelimitedParser, valid_pipe_payload: bytes):
        """Parse → serialize → parse produces semantically equivalent fields (Req 10.8)."""
        original_fields = pipe_parser.parse(valid_pipe_payload)
        serialized = pipe_parser.serialize(original_fields)
        reparsed_fields = pipe_parser.parse(serialized)

        for field in REQUIRED_FIELDS:
            if field in ("gross_gallons", "net_gallons", "observed_temperature", "api_gravity"):
                assert float(reparsed_fields[field]) == float(original_fields[field]), (
                    f"Round-trip mismatch for {field}"
                )
            else:
                assert str(reparsed_fields[field]) == str(original_fields[field]), (
                    f"Round-trip mismatch for {field}: "
                    f"{reparsed_fields[field]} != {original_fields[field]}"
                )

    def test_case_insensitive_headers(self, pipe_parser: PipeDelimitedParser):
        """Parser handles case-insensitive header field names."""
        payload = (
            b"Load_Number|Product_Code|Gross_Gallons|Net_Gallons|Observed_Temperature|API_Gravity|Supplier_Name|Terminal_Name|Driver_ID|Timestamp\n"
            b"LOAD-001|UNL87|8500.0|8450.5|72.5|58.2|Supplier|Terminal|DRV-100|2024-01-15T10:30:00\n"
        )
        fields = pipe_parser.parse(payload)
        assert fields["load_number"] == "LOAD-001"
        assert fields["gross_gallons"] == 8500.0


# ---------------------------------------------------------------------------
# EDI Parser Registry Tests
# ---------------------------------------------------------------------------


class TestEDIParserRegistry:
    """Tests for the EDI parser registry and auto-detection."""

    def test_registry_detects_x12(self, registry: EDIParserRegistry, valid_x12_payload: bytes):
        """Registry auto-detects X12 856 format."""
        parser = registry.detect_parser(valid_x12_payload)
        assert isinstance(parser, X12856Parser)

    def test_registry_detects_pipe(self, registry: EDIParserRegistry, valid_pipe_payload: bytes):
        """Registry auto-detects pipe-delimited format."""
        parser = registry.detect_parser(valid_pipe_payload)
        assert isinstance(parser, PipeDelimitedParser)

    def test_registry_parse_x12(self, registry: EDIParserRegistry, valid_x12_payload: bytes):
        """Registry parses X12 payload correctly."""
        fields = registry.parse(valid_x12_payload)
        assert fields["load_number"] == "LOAD-2024-001"
        assert fields["product_code"] == "UNL87"

    def test_registry_parse_pipe(self, registry: EDIParserRegistry, valid_pipe_payload: bytes):
        """Registry parses pipe-delimited payload correctly."""
        fields = registry.parse(valid_pipe_payload)
        assert fields["load_number"] == "LOAD-2024-001"
        assert fields["product_code"] == "UNL87"

    def test_registry_raises_on_unknown_format(self, registry: EDIParserRegistry):
        """Registry raises EDIParseError for unrecognized formats."""
        garbage = b"This is not a valid EDI format at all"
        with pytest.raises(EDIParseError, match="No registered parser strategy"):
            registry.parse(garbage)

    def test_registry_raises_on_empty_payload(self, registry: EDIParserRegistry):
        """Registry raises EDIParseError for empty payloads."""
        with pytest.raises(EDIParseError, match="No registered parser strategy"):
            registry.parse(b"")

    def test_registry_register_custom_strategy(self):
        """Registry accepts custom parser strategies."""
        registry = EDIParserRegistry()

        class CustomParser(BOLParserStrategy):
            def can_parse(self, payload: bytes) -> bool:
                return payload.startswith(b"CUSTOM:")

            def parse(self, payload: bytes) -> dict:
                return {"load_number": "CUSTOM-001"}

            def serialize(self, fields: dict) -> bytes:
                return b"CUSTOM:serialized"

        registry.register(CustomParser())
        assert len(registry.strategies) == 1
        assert registry.detect_parser(b"CUSTOM:data") is not None

    def test_create_default_registry(self):
        """create_default_registry() returns a registry with both standard parsers."""
        registry = create_default_registry()
        assert len(registry.strategies) == 2
        assert isinstance(registry.strategies[0], X12856Parser)
        assert isinstance(registry.strategies[1], PipeDelimitedParser)

    def test_registry_serialize(self, registry: EDIParserRegistry, valid_pipe_payload: bytes):
        """Registry serialize method delegates to the specified strategy."""
        pipe_parser = registry.strategies[1]
        fields = pipe_parser.parse(valid_pipe_payload)
        serialized = registry.serialize(fields, pipe_parser)
        assert isinstance(serialized, bytes)
        assert b"|" in serialized


# ---------------------------------------------------------------------------
# X12 Parser — Edge Cases in Field Extraction
# ---------------------------------------------------------------------------


class TestX12856ParserEdgeCases:
    """Edge case tests for X12 856 parser field extraction."""

    def test_parse_8_digit_timestamp(self, x12_parser: X12856Parser):
        """X12 parser handles 8-digit date-only timestamp (YYYYMMDD)."""
        segments = [
            "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *240115*1030*U*00401*000000001*0*P*>",
            "GS*SH*SENDER*RECEIVER*20240115*1030*1*X*004010",
            "ST*856*0001",
            "BSN*00*0001*20240115*1030",
            "HL*1**S*1",
            "TD1*CTN*1*UNL87",
            "REF*LN*LOAD-TS-8DIGIT",
            "REF*DR*DRV-100",
            "QTY*GR*8500.0",
            "QTY*NT*8450.5",
            "MEA*TM*TE*72.5",
            "MEA*PD*AG*58.2",
            "N1*SU*Marathon Petroleum",
            "N1*TL*Pasadena Terminal",
            "DTM*011*20240115",
            "SE*13*0001",
            "GE*1*1",
            "IEA*1*000000001",
        ]
        payload = "~".join(segments).encode("utf-8") + b"~"
        fields = x12_parser.parse(payload)
        assert "2024-01-15" in fields["timestamp"]

    def test_parse_14_digit_timestamp(self, x12_parser: X12856Parser):
        """X12 parser handles 14-digit timestamp with seconds (YYYYMMDDHHmmSS)."""
        segments = [
            "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *240115*1030*U*00401*000000001*0*P*>",
            "GS*SH*SENDER*RECEIVER*20240115*1030*1*X*004010",
            "ST*856*0001",
            "BSN*00*0001*20240115*1030",
            "HL*1**S*1",
            "TD1*CTN*1*ULSD",
            "REF*LN*LOAD-TS-14DIGIT",
            "REF*DR*DRV-200",
            "QTY*GR*9200.0",
            "QTY*NT*9150.3",
            "MEA*TM*TE*68.0",
            "MEA*PD*AG*35.5",
            "N1*SU*Valero Energy",
            "N1*TL*Houston Terminal",
            "DTM*011*20240115103045",
            "SE*13*0001",
            "GE*1*1",
            "IEA*1*000000001",
        ]
        payload = "~".join(segments).encode("utf-8") + b"~"
        fields = x12_parser.parse(payload)
        assert "2024-01-15" in fields["timestamp"]
        assert "10:30:45" in fields["timestamp"]

    def test_parse_td1_with_3_elements(self, x12_parser: X12856Parser):
        """X12 parser extracts product_code from TD1 with only 3 elements."""
        segments = [
            "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *240115*1030*U*00401*000000001*0*P*>",
            "GS*SH*SENDER*RECEIVER*20240115*1030*1*X*004010",
            "ST*856*0001",
            "BSN*00*0001*20240115*1030",
            "HL*1**S*1",
            "TD1*CTN*PROP",
            "REF*LN*LOAD-TD1-3ELEM",
            "REF*DR*DRV-300",
            "QTY*GR*5000.0",
            "QTY*NT*4980.0",
            "MEA*TM*TE*55.0",
            "MEA*PD*AG*130.0",
            "N1*SU*AmeriGas",
            "N1*TL*Tulsa Terminal",
            "DTM*011*202401151030",
            "SE*13*0001",
            "GE*1*1",
            "IEA*1*000000001",
        ]
        payload = "~".join(segments).encode("utf-8") + b"~"
        fields = x12_parser.parse(payload)
        assert fields["product_code"] == "PROP"

    def test_parse_non_numeric_qty_skipped(self, x12_parser: X12856Parser):
        """X12 parser skips QTY segments with non-numeric values gracefully."""
        segments = [
            "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *240115*1030*U*00401*000000001*0*P*>",
            "GS*SH*SENDER*RECEIVER*20240115*1030*1*X*004010",
            "ST*856*0001",
            "BSN*00*0001*20240115*1030",
            "HL*1**S*1",
            "TD1*CTN*1*UNL87",
            "REF*LN*LOAD-BADQTY",
            "REF*DR*DRV-100",
            "QTY*GR*INVALID",
            "QTY*GR*8500.0",
            "QTY*NT*8450.5",
            "MEA*TM*TE*72.5",
            "MEA*PD*AG*58.2",
            "N1*SU*Marathon Petroleum",
            "N1*TL*Pasadena Terminal",
            "DTM*011*202401151030",
            "SE*14*0001",
            "GE*1*1",
            "IEA*1*000000001",
        ]
        payload = "~".join(segments).encode("utf-8") + b"~"
        fields = x12_parser.parse(payload)
        # The second valid QTY*GR should be used
        assert fields["gross_gallons"] == 8500.0

    def test_serialize_preserves_all_fields(self, x12_parser: X12856Parser):
        """X12 serialize includes all required fields in the output."""
        fields = {
            "load_number": "LOAD-SER-001",
            "product_code": "ULSD",
            "gross_gallons": 9200.0,
            "net_gallons": 9150.3,
            "observed_temperature": 68.0,
            "api_gravity": 35.5,
            "supplier_name": "Valero Energy",
            "terminal_name": "Houston Terminal",
            "driver_id": "DRV-200",
            "timestamp": "2024-01-15T10:30:00",
        }
        serialized = x12_parser.serialize(fields)
        text = serialized.decode("utf-8")
        assert "LOAD-SER-001" in text
        assert "ULSD" in text
        assert "9200.0" in text
        assert "9150.3" in text
        assert "Valero Energy" in text
        assert "Houston Terminal" in text
        assert "DRV-200" in text


# ---------------------------------------------------------------------------
# Pipe-Delimited Parser — Edge Cases in Field Extraction
# ---------------------------------------------------------------------------


class TestPipeDelimitedParserEdgeCases:
    """Edge case tests for pipe-delimited parser field extraction."""

    def test_parse_extra_whitespace_in_values(self, pipe_parser: PipeDelimitedParser):
        """Pipe parser trims whitespace from field values."""
        payload = (
            b"load_number|product_code|gross_gallons|net_gallons|observed_temperature|api_gravity|supplier_name|terminal_name|driver_id|timestamp\n"
            b" LOAD-WS-001 | UNL87 | 8500.0 | 8450.5 | 72.5 | 58.2 | Marathon Petroleum | Pasadena Terminal | DRV-100 | 2024-01-15T10:30:00 \n"
        )
        fields = pipe_parser.parse(payload)
        assert fields["load_number"] == "LOAD-WS-001"
        assert fields["product_code"] == "UNL87"
        assert fields["gross_gallons"] == 8500.0
        assert fields["supplier_name"] == "Marathon Petroleum"
        assert fields["driver_id"] == "DRV-100"

    def test_parse_multiple_data_rows_uses_first(self, pipe_parser: PipeDelimitedParser):
        """Pipe parser uses only the first data row when multiple are present."""
        payload = (
            b"load_number|product_code|gross_gallons|net_gallons|observed_temperature|api_gravity|supplier_name|terminal_name|driver_id|timestamp\n"
            b"LOAD-FIRST|UNL87|8500.0|8450.5|72.5|58.2|Marathon|Pasadena|DRV-100|2024-01-15T10:30:00\n"
            b"LOAD-SECOND|ULSD|9200.0|9150.3|68.0|35.5|Valero|Houston|DRV-200|2024-01-16T11:00:00\n"
        )
        fields = pipe_parser.parse(payload)
        assert fields["load_number"] == "LOAD-FIRST"
        assert fields["product_code"] == "UNL87"

    def test_parse_extra_trailing_newlines(self, pipe_parser: PipeDelimitedParser):
        """Pipe parser handles extra trailing newlines gracefully."""
        payload = (
            b"load_number|product_code|gross_gallons|net_gallons|observed_temperature|api_gravity|supplier_name|terminal_name|driver_id|timestamp\n"
            b"LOAD-TRAIL|UNL87|8500.0|8450.5|72.5|58.2|Marathon|Pasadena|DRV-100|2024-01-15T10:30:00\n"
            b"\n\n\n"
        )
        fields = pipe_parser.parse(payload)
        assert fields["load_number"] == "LOAD-TRAIL"

    def test_serialize_produces_parseable_output(self, pipe_parser: PipeDelimitedParser):
        """Pipe serialize produces output that can be re-parsed."""
        fields = {
            "load_number": "LOAD-ROUND",
            "product_code": "PROP",
            "gross_gallons": 5000.0,
            "net_gallons": 4980.0,
            "observed_temperature": 55.0,
            "api_gravity": 130.0,
            "supplier_name": "AmeriGas",
            "terminal_name": "Tulsa Terminal",
            "driver_id": "DRV-300",
            "timestamp": "2024-02-20T08:00:00",
        }
        serialized = pipe_parser.serialize(fields)
        reparsed = pipe_parser.parse(serialized)
        assert reparsed["load_number"] == "LOAD-ROUND"
        assert reparsed["gross_gallons"] == 5000.0
        assert reparsed["supplier_name"] == "AmeriGas"

    def test_parse_zero_gallons_still_parses(self, pipe_parser: PipeDelimitedParser):
        """Pipe parser parses zero-value numeric fields without error (validation is model-level)."""
        payload = (
            b"load_number|product_code|gross_gallons|net_gallons|observed_temperature|api_gravity|supplier_name|terminal_name|driver_id|timestamp\n"
            b"LOAD-ZERO|UNL87|0.0|0.0|72.5|58.2|Marathon|Pasadena|DRV-100|2024-01-15T10:30:00\n"
        )
        # Parser should parse successfully — validation is at the model level
        fields = pipe_parser.parse(payload)
        assert fields["gross_gallons"] == 0.0
        assert fields["net_gallons"] == 0.0
