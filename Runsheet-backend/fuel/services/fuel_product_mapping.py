"""
Fuel Product Code Mapping Service.

Provides bidirectional mapping between US market product codes and FuelGrade enum values.
This allows the system to work with both naming conventions seamlessly.

US Market Codes → FuelGrade Enum:
- DIESEL_2, DIESEL_1, OFF_ROAD_DIESEL → AGO (Automotive Gas Oil)
- GASOLINE_REG, GASOLINE_MID, GASOLINE_PREM, E85 → PMS (Premium Motor Spirit)
- KEROSENE, JET_A, JET_A1 → ATK (Aviation Turbine Kerosene)
- PROPANE, LPG → LPG (Liquefied Petroleum Gas)
- HEATING_OIL, DEF → AGO (default to diesel category)

Validates: Requirements for fuel-ops product compatibility
"""

from typing import Dict, Optional, Set
from enum import Enum

from Agents.support.fuel_distribution_models import FuelGrade


class USProductCode(str, Enum):
    """US market fuel product codes."""
    # Diesel products
    DIESEL_2 = "DIESEL_2"
    DIESEL_1 = "DIESEL_1"
    OFF_ROAD_DIESEL = "OFF_ROAD_DIESEL"
    HEATING_OIL = "HEATING_OIL"
    DEF = "DEF"  # Diesel Exhaust Fluid
    
    # Gasoline products
    GASOLINE_REG = "GASOLINE_REG"
    GASOLINE_MID = "GASOLINE_MID"
    GASOLINE_PREM = "GASOLINE_PREM"
    E85 = "E85"
    
    # Aviation/Kerosene products
    KEROSENE = "KEROSENE"
    JET_A = "JET_A"
    JET_A1 = "JET_A1"
    
    # LPG products
    PROPANE = "PROPANE"
    LPG = "LPG"


# Mapping from US product codes to FuelGrade enum
US_TO_FUEL_GRADE: Dict[str, FuelGrade] = {
    # Diesel products → AGO
    "DIESEL_2": FuelGrade.AGO,
    "DIESEL_1": FuelGrade.AGO,
    "OFF_ROAD_DIESEL": FuelGrade.AGO,
    "HEATING_OIL": FuelGrade.AGO,
    "DEF": FuelGrade.AGO,
    
    # Gasoline products → PMS
    "GASOLINE_REG": FuelGrade.PMS,
    "GASOLINE_MID": FuelGrade.PMS,
    "GASOLINE_PREM": FuelGrade.PMS,
    "E85": FuelGrade.PMS,
    
    # Aviation/Kerosene products → ATK
    "KEROSENE": FuelGrade.ATK,
    "JET_A": FuelGrade.ATK,
    "JET_A1": FuelGrade.ATK,
    
    # LPG products → LPG
    "PROPANE": FuelGrade.LPG,
    "LPG": FuelGrade.LPG,
}


# Reverse mapping from FuelGrade to primary US product code
FUEL_GRADE_TO_US: Dict[FuelGrade, str] = {
    FuelGrade.AGO: "DIESEL_2",
    FuelGrade.PMS: "GASOLINE_REG",
    FuelGrade.ATK: "KEROSENE",
    FuelGrade.LPG: "PROPANE",
}


# Mapping from FuelGrade to all compatible US product codes
FUEL_GRADE_TO_US_ALL: Dict[FuelGrade, Set[str]] = {
    FuelGrade.AGO: {"DIESEL_2", "DIESEL_1", "OFF_ROAD_DIESEL", "HEATING_OIL", "DEF"},
    FuelGrade.PMS: {"GASOLINE_REG", "GASOLINE_MID", "GASOLINE_PREM", "E85"},
    FuelGrade.ATK: {"KEROSENE", "JET_A", "JET_A1"},
    FuelGrade.LPG: {"PROPANE", "LPG"},
}


class FuelProductMapper:
    """Service for mapping between US product codes and FuelGrade enum."""
    
    @staticmethod
    def us_to_fuel_grade(product_code: str) -> Optional[FuelGrade]:
        """Convert US product code to FuelGrade enum.
        
        Args:
            product_code: US market product code (e.g., "DIESEL_2", "GASOLINE_REG")
            
        Returns:
            FuelGrade enum value, or None if not recognized
        """
        return US_TO_FUEL_GRADE.get(product_code.upper())
    
    @staticmethod
    def fuel_grade_to_us(fuel_grade: FuelGrade, prefer_primary: bool = True) -> str:
        """Convert FuelGrade enum to US product code.
        
        Args:
            fuel_grade: FuelGrade enum value
            prefer_primary: If True, return the primary US code; if False, return first compatible
            
        Returns:
            US product code string
        """
        if prefer_primary:
            return FUEL_GRADE_TO_US.get(fuel_grade, "DIESEL_2")
        else:
            compatible = FUEL_GRADE_TO_US_ALL.get(fuel_grade, set())
            return list(compatible)[0] if compatible else "DIESEL_2"
    
    @staticmethod
    def are_compatible(product_code: str, fuel_grade: FuelGrade) -> bool:
        """Check if a US product code is compatible with a FuelGrade.
        
        Args:
            product_code: US market product code
            fuel_grade: FuelGrade enum value
            
        Returns:
            True if compatible, False otherwise
        """
        compatible_codes = FUEL_GRADE_TO_US_ALL.get(fuel_grade, set())
        return product_code.upper() in compatible_codes
    
    @staticmethod
    def normalize_product_code(product_code: str) -> str:
        """Normalize a product code to FuelGrade and back to primary US code.
        
        This ensures consistent product code representation across the system.
        
        Args:
            product_code: Any product code (US or FuelGrade string)
            
        Returns:
            Normalized US product code
        """
        # Try to parse as FuelGrade first
        try:
            grade = FuelGrade(product_code)
            return FUEL_GRADE_TO_US[grade]
        except (ValueError, KeyError):
            pass
        
        # Try to map from US code
        grade = US_TO_FUEL_GRADE.get(product_code.upper())
        if grade:
            return FUEL_GRADE_TO_US[grade]
        
        # Return as-is if not recognized
        return product_code
    
    @staticmethod
    def get_all_us_codes_for_grade(fuel_grade: FuelGrade) -> Set[str]:
        """Get all US product codes compatible with a FuelGrade.
        
        Args:
            fuel_grade: FuelGrade enum value
            
        Returns:
            Set of compatible US product codes
        """
        return FUEL_GRADE_TO_US_ALL.get(fuel_grade, set()).copy()


# Singleton instance
fuel_product_mapper = FuelProductMapper()
