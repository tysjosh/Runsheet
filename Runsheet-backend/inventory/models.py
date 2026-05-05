"""
Pydantic models for the Fleet Inventory & Maintenance Supplies module.

Provides data models for inventory items, stock adjustments, and
aggregated summaries used by the inventory service and API endpoints.
"""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InventoryCategory(str, Enum):
    """Categories of fleet maintenance and operational supplies."""

    TIRES = "tires"
    ENGINE_PARTS = "engine_parts"
    BRAKE_PARTS = "brake_parts"
    FLUIDS = "fluids"  # oil, coolant, brake fluid
    FILTERS = "filters"
    ELECTRICAL = "electrical"  # batteries, alternators
    FUEL_EQUIPMENT = "fuel_equipment"  # hoses, nozzles, seals
    SAFETY = "safety"  # fire extinguishers, PPE
    GENERAL = "general"


class InventoryStatus(str, Enum):
    """Current stock status of an inventory item."""

    IN_STOCK = "in_stock"
    LOW_STOCK = "low_stock"
    OUT_OF_STOCK = "out_of_stock"
    ON_ORDER = "on_order"


# ---------------------------------------------------------------------------
# Core Models
# ---------------------------------------------------------------------------


class InventoryItem(BaseModel):
    """Full representation of an inventory item stored in Elasticsearch."""

    item_id: str = Field(..., description="Unique item identifier (INV_xxxxxxxx)")
    name: str = Field(..., description="Item display name")
    category: InventoryCategory = Field(..., description="Item category")
    quantity: int = Field(..., ge=0, description="Current quantity in stock")
    unit: str = Field(..., description="Unit of measure (pieces, liters, sets, etc.)")
    min_threshold: int = Field(..., ge=0, description="Low-stock alert threshold")
    max_capacity: int = Field(..., gt=0, description="Maximum storage capacity")
    location: str = Field(..., description="Warehouse/depot name")
    status: InventoryStatus = Field(..., description="Current stock status")
    unit_cost: Optional[float] = Field(default=None, ge=0, description="Cost per unit")
    supplier: Optional[str] = Field(default=None, description="Supplier name")
    compatible_assets: Optional[List[str]] = Field(
        default=None, description="Asset types this part is compatible with"
    )
    last_restocked: Optional[str] = Field(
        default=None, description="ISO-8601 timestamp of last restock"
    )
    tenant_id: str = Field(..., description="Tenant identifier for data isolation")


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------


class CreateInventoryItem(BaseModel):
    """Payload for registering a new inventory item."""

    name: str = Field(..., min_length=1, description="Item display name")
    category: InventoryCategory = Field(..., description="Item category")
    quantity: int = Field(default=0, ge=0, description="Initial quantity")
    unit: str = Field(..., min_length=1, description="Unit of measure")
    min_threshold: int = Field(..., ge=0, description="Low-stock alert threshold")
    max_capacity: int = Field(..., gt=0, description="Maximum storage capacity")
    location: str = Field(..., min_length=1, description="Warehouse/depot name")
    unit_cost: Optional[float] = Field(default=None, ge=0, description="Cost per unit")
    supplier: Optional[str] = Field(default=None, description="Supplier name")
    compatible_assets: Optional[List[str]] = Field(
        default=None, description="Asset types this part is compatible with"
    )


class UpdateInventoryItem(BaseModel):
    """Payload for partially updating an inventory item (PATCH)."""

    name: Optional[str] = Field(default=None, min_length=1, description="Item display name")
    category: Optional[InventoryCategory] = Field(default=None, description="Item category")
    quantity: Optional[int] = Field(default=None, ge=0, description="Current quantity")
    min_threshold: Optional[int] = Field(default=None, ge=0, description="Low-stock threshold")
    max_capacity: Optional[int] = Field(default=None, gt=0, description="Maximum capacity")
    location: Optional[str] = Field(default=None, min_length=1, description="Warehouse/depot")
    unit_cost: Optional[float] = Field(default=None, ge=0, description="Cost per unit")
    supplier: Optional[str] = Field(default=None, description="Supplier name")
    compatible_assets: Optional[List[str]] = Field(
        default=None, description="Compatible asset types"
    )


class StockAdjustment(BaseModel):
    """Payload for recording stock in/out movements."""

    quantity_change: int = Field(
        ..., description="Positive = restock, negative = consumption"
    )
    reason: str = Field(
        ..., min_length=1,
        description="Reason: restock, used_for_maintenance, damaged, transferred"
    )
    reference_id: Optional[str] = Field(
        default=None, description="Related job_id or asset_id"
    )
    notes: Optional[str] = Field(default=None, description="Additional notes")


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------


class InventorySummary(BaseModel):
    """Aggregated inventory summary for a tenant."""

    total_items: int = Field(..., ge=0, description="Total number of inventory items")
    total_value: float = Field(..., ge=0, description="Total estimated inventory value")
    in_stock: int = Field(..., ge=0, description="Items with in_stock status")
    low_stock: int = Field(..., ge=0, description="Items with low_stock status")
    out_of_stock: int = Field(..., ge=0, description="Items with out_of_stock status")
    on_order: int = Field(..., ge=0, description="Items with on_order status")
    categories: Dict[str, int] = Field(
        default_factory=dict, description="Item count per category"
    )


class StockAdjustmentResult(BaseModel):
    """Result returned after a stock adjustment."""

    item_id: str = Field(..., description="Item identifier")
    previous_quantity: int = Field(..., description="Quantity before adjustment")
    new_quantity: int = Field(..., description="Quantity after adjustment")
    previous_status: InventoryStatus = Field(..., description="Status before adjustment")
    new_status: InventoryStatus = Field(..., description="Status after adjustment")
    event_id: str = Field(..., description="Stock movement event identifier")
