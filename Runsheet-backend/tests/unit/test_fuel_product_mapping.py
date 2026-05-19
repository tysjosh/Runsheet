"""
Unit tests for fuel product code mapping service.

Validates bidirectional mapping between US market product codes
and FuelGrade enum values.
"""

import pytest

from fuel.services.fuel_product_mapping import (
    FuelProductMapper,
    fuel_product_mapper,
    US_TO_FUEL_GRADE,
    FUEL_GRADE_TO_US,
)
from Agents.support.fuel_distribution_models import FuelGrade


class TestFuelProductMapper:
    """Test the FuelProductMapper service."""

    def test_us_to_fuel_grade_diesel(self):
        """Test mapping diesel products to AGO."""
        assert fuel_product_mapper.us_to_fuel_grade("DIESEL_2") == FuelGrade.AGO
        assert fuel_product_mapper.us_to_fuel_grade("DIESEL_1") == FuelGrade.AGO
        assert fuel_product_mapper.us_to_fuel_grade("OFF_ROAD_DIESEL") == FuelGrade.AGO
        assert fuel_product_mapper.us_to_fuel_grade("HEATING_OIL") == FuelGrade.AGO
        assert fuel_product_mapper.us_to_fuel_grade("DEF") == FuelGrade.AGO

    def test_us_to_fuel_grade_gasoline(self):
        """Test mapping gasoline products to PMS."""
        assert fuel_product_mapper.us_to_fuel_grade("GASOLINE_REG") == FuelGrade.PMS
        assert fuel_product_mapper.us_to_fuel_grade("GASOLINE_MID") == FuelGrade.PMS
        assert fuel_product_mapper.us_to_fuel_grade("GASOLINE_PREM") == FuelGrade.PMS
        assert fuel_product_mapper.us_to_fuel_grade("E85") == FuelGrade.PMS

    def test_us_to_fuel_grade_kerosene(self):
        """Test mapping kerosene/aviation products to ATK."""
        assert fuel_product_mapper.us_to_fuel_grade("KEROSENE") == FuelGrade.ATK
        assert fuel_product_mapper.us_to_fuel_grade("JET_A") == FuelGrade.ATK
        assert fuel_product_mapper.us_to_fuel_grade("JET_A1") == FuelGrade.ATK

    def test_us_to_fuel_grade_lpg(self):
        """Test mapping LPG products."""
        assert fuel_product_mapper.us_to_fuel_grade("PROPANE") == FuelGrade.LPG
        assert fuel_product_mapper.us_to_fuel_grade("LPG") == FuelGrade.LPG

    def test_us_to_fuel_grade_case_insensitive(self):
        """Test that mapping is case-insensitive."""
        assert fuel_product_mapper.us_to_fuel_grade("diesel_2") == FuelGrade.AGO
        assert fuel_product_mapper.us_to_fuel_grade("Gasoline_Reg") == FuelGrade.PMS
        assert fuel_product_mapper.us_to_fuel_grade("PROPANE") == FuelGrade.LPG

    def test_us_to_fuel_grade_unknown(self):
        """Test that unknown codes return None."""
        assert fuel_product_mapper.us_to_fuel_grade("UNKNOWN_FUEL") is None
        assert fuel_product_mapper.us_to_fuel_grade("") is None

    def test_fuel_grade_to_us_primary(self):
        """Test mapping FuelGrade to primary US code."""
        assert fuel_product_mapper.fuel_grade_to_us(FuelGrade.AGO) == "DIESEL_2"
        assert fuel_product_mapper.fuel_grade_to_us(FuelGrade.PMS) == "GASOLINE_REG"
        assert fuel_product_mapper.fuel_grade_to_us(FuelGrade.ATK) == "KEROSENE"
        assert fuel_product_mapper.fuel_grade_to_us(FuelGrade.LPG) == "PROPANE"

    def test_are_compatible(self):
        """Test product code compatibility checking."""
        # Diesel products compatible with AGO
        assert fuel_product_mapper.are_compatible("DIESEL_2", FuelGrade.AGO)
        assert fuel_product_mapper.are_compatible("HEATING_OIL", FuelGrade.AGO)
        assert not fuel_product_mapper.are_compatible("DIESEL_2", FuelGrade.PMS)

        # Gasoline products compatible with PMS
        assert fuel_product_mapper.are_compatible("GASOLINE_REG", FuelGrade.PMS)
        assert fuel_product_mapper.are_compatible("GASOLINE_PREM", FuelGrade.PMS)
        assert not fuel_product_mapper.are_compatible("GASOLINE_REG", FuelGrade.AGO)

        # Kerosene products compatible with ATK
        assert fuel_product_mapper.are_compatible("KEROSENE", FuelGrade.ATK)
        assert fuel_product_mapper.are_compatible("JET_A", FuelGrade.ATK)
        assert not fuel_product_mapper.are_compatible("KEROSENE", FuelGrade.LPG)

        # LPG products compatible with LPG
        assert fuel_product_mapper.are_compatible("PROPANE", FuelGrade.LPG)
        assert fuel_product_mapper.are_compatible("LPG", FuelGrade.LPG)
        assert not fuel_product_mapper.are_compatible("PROPANE", FuelGrade.AGO)

    def test_normalize_product_code(self):
        """Test product code normalization."""
        # US codes normalize to primary US code
        assert fuel_product_mapper.normalize_product_code("DIESEL_1") == "DIESEL_2"
        assert fuel_product_mapper.normalize_product_code("GASOLINE_PREM") == "GASOLINE_REG"
        assert fuel_product_mapper.normalize_product_code("JET_A") == "KEROSENE"

        # FuelGrade enum values normalize to primary US code
        assert fuel_product_mapper.normalize_product_code("AGO") == "DIESEL_2"
        assert fuel_product_mapper.normalize_product_code("PMS") == "GASOLINE_REG"
        assert fuel_product_mapper.normalize_product_code("ATK") == "KEROSENE"
        assert fuel_product_mapper.normalize_product_code("LPG") == "PROPANE"

        # Unknown codes return as-is
        assert fuel_product_mapper.normalize_product_code("UNKNOWN") == "UNKNOWN"

    def test_get_all_us_codes_for_grade(self):
        """Test getting all compatible US codes for a FuelGrade."""
        ago_codes = fuel_product_mapper.get_all_us_codes_for_grade(FuelGrade.AGO)
        assert "DIESEL_2" in ago_codes
        assert "DIESEL_1" in ago_codes
        assert "HEATING_OIL" in ago_codes
        assert "DEF" in ago_codes
        assert len(ago_codes) == 5

        pms_codes = fuel_product_mapper.get_all_us_codes_for_grade(FuelGrade.PMS)
        assert "GASOLINE_REG" in pms_codes
        assert "GASOLINE_MID" in pms_codes
        assert "GASOLINE_PREM" in pms_codes
        assert "E85" in pms_codes
        assert len(pms_codes) == 4

    def test_bidirectional_mapping_consistency(self):
        """Test that bidirectional mapping is consistent."""
        # Every US code should map to a FuelGrade and back
        for us_code, fuel_grade in US_TO_FUEL_GRADE.items():
            assert fuel_product_mapper.us_to_fuel_grade(us_code) == fuel_grade
            primary_us = FUEL_GRADE_TO_US[fuel_grade]
            # The primary US code should map back to the same FuelGrade
            assert fuel_product_mapper.us_to_fuel_grade(primary_us) == fuel_grade
