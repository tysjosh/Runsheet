"""Schema templates for the data import/migration tool.

Defines field definitions and schema templates for each supported data type.
Serves as the single source of truth for validation, auto-mapping, and CSV
template generation.
"""

import csv
import io
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class FieldType(str, Enum):
    """Supported field types for import schema validation."""
    STRING = "string"
    NUMBER = "number"
    DATE = "date"
    ENUM = "enum"
    BOOLEAN = "boolean"
    GEO_POINT = "geo_point"


class FieldDef(BaseModel):
    """Definition of a single field within a schema template."""
    name: str
    type: FieldType
    required: bool
    description: str
    enum_values: Optional[list[str]] = None
    date_format: Optional[str] = None


class SchemaTemplate(BaseModel):
    """Schema template for a supported data type."""
    data_type: str
    description: str
    es_index: str
    fields: list[FieldDef]


class SchemaTemplates:
    """Registry of schema templates for all supported data types.

    Provides lookup methods for templates, index names, field lists,
    and CSV template generation.
    """

    TEMPLATES: dict[str, SchemaTemplate] = {
        "fleet": SchemaTemplate(
            data_type="fleet",
            description="Vehicle and fleet asset records including trucks, drivers, and cargo information",
            es_index="trucks",
            fields=[
                FieldDef(name="truck_id", type=FieldType.STRING, required=True, description="Unique vehicle identifier"),
                FieldDef(name="plate_number", type=FieldType.STRING, required=True, description="License plate number"),
                FieldDef(name="driver_id", type=FieldType.STRING, required=False, description="Assigned driver identifier"),
                FieldDef(name="driver_name", type=FieldType.STRING, required=False, description="Assigned driver name"),
                FieldDef(name="status", type=FieldType.ENUM, required=True, description="Vehicle status", enum_values=["on_time", "delayed", "idle", "maintenance"]),
                FieldDef(name="estimated_arrival", type=FieldType.DATE, required=False, description="Estimated arrival time", date_format="ISO8601"),
                FieldDef(name="last_update", type=FieldType.DATE, required=False, description="Last status update timestamp", date_format="ISO8601"),
                FieldDef(name="cargo_type", type=FieldType.STRING, required=False, description="Type of cargo being transported"),
                FieldDef(name="cargo_weight", type=FieldType.NUMBER, required=False, description="Cargo weight in kg"),
                FieldDef(name="cargo_volume", type=FieldType.NUMBER, required=False, description="Cargo volume in cubic meters"),
                FieldDef(name="cargo_priority", type=FieldType.ENUM, required=False, description="Cargo priority level", enum_values=["low", "medium", "high", "critical"]),
            ],
        ),
        "orders": SchemaTemplate(
            data_type="orders",
            description="Canonical fuel delivery orders imported from an ERP or partner system",
            es_index="fuel_orders_current",
            fields=[
                FieldDef(name="source_system", type=FieldType.STRING, required=True, description="ERP or source-system name"),
                FieldDef(name="source_order_id", type=FieldType.STRING, required=True, description="Order identifier in the source system"),
                FieldDef(name="source_updated_at", type=FieldType.DATE, required=False, description="Last update timestamp in the source system", date_format="ISO8601"),
                FieldDef(name="customer_id", type=FieldType.STRING, required=True, description="Canonical customer identifier"),
                FieldDef(name="customer_name", type=FieldType.STRING, required=True, description="Customer name"),
                FieldDef(name="customer_phone", type=FieldType.STRING, required=False, description="Customer phone number"),
                FieldDef(name="customer_email", type=FieldType.STRING, required=False, description="Customer email address"),
                FieldDef(name="ship_to_address", type=FieldType.STRING, required=True, description="Delivery address"),
                FieldDef(name="ship_to_lat", type=FieldType.NUMBER, required=True, description="Delivery latitude"),
                FieldDef(name="ship_to_lon", type=FieldType.NUMBER, required=True, description="Delivery longitude"),
                FieldDef(name="customer_tank_id", type=FieldType.STRING, required=False, description="Linked Runsheet customer tank"),
                FieldDef(name="product_code", type=FieldType.STRING, required=True, description="Canonical fuel product code or supported alias"),
                FieldDef(name="gallons_requested", type=FieldType.NUMBER, required=False, description="Requested US gallons; omit only for fill-to-full"),
                FieldDef(name="unit_price_usd", type=FieldType.NUMBER, required=False, description="Contract price per gallon in dollars; supports four or more decimal places"),
                FieldDef(name="tax_cents", type=FieldType.NUMBER, required=False, description="Optional source-system tax for the planned order, in integer cents"),
                FieldDef(name="fill_to_full", type=FieldType.BOOLEAN, required=False, description="Whether the delivery should fill the linked tank"),
                FieldDef(name="call_type", type=FieldType.ENUM, required=True, description="Fuel order call type", enum_values=["will_call", "auto_fill", "keep_full", "one_off"]),
                FieldDef(name="delivery_window_start", type=FieldType.DATE, required=False, description="Delivery window start", date_format="ISO8601"),
                FieldDef(name="delivery_window_end", type=FieldType.DATE, required=False, description="Delivery window end", date_format="ISO8601"),
                FieldDef(name="po_number", type=FieldType.STRING, required=False, description="Customer purchase-order number"),
                FieldDef(name="special_instructions", type=FieldType.STRING, required=False, description="Dispatcher or delivery instructions"),
            ],
        ),
        "customer_tanks": SchemaTemplate(
            data_type="customer_tanks",
            description="Customer tank master data used by forecasting and delivery planning",
            es_index="customer_tanks",
            fields=[
                FieldDef(name="source_system", type=FieldType.STRING, required=True, description="ERP or tank-master source"),
                FieldDef(name="external_tank_id", type=FieldType.STRING, required=True, description="Tank identifier in the source system"),
                FieldDef(name="customer_tank_id", type=FieldType.STRING, required=False, description="Optional Runsheet tank identifier"),
                FieldDef(name="customer_id", type=FieldType.STRING, required=True, description="Owning customer identifier"),
                FieldDef(name="customer_type", type=FieldType.ENUM, required=True, description="Customer service type", enum_values=["residential", "commercial", "keep_full", "will_call", "auto_fill"]),
                FieldDef(name="fuel_type", type=FieldType.ENUM, required=True, description="Fuel family", enum_values=["propane", "heating_oil", "diesel", "generator_fuel", "farm_fuel", "gasoline"]),
                FieldDef(name="fuel_product_code", type=FieldType.STRING, required=True, description="Canonical fuel product code or supported alias"),
                FieldDef(name="capacity_gallons", type=FieldType.NUMBER, required=True, description="Tank capacity in US gallons"),
                FieldDef(name="current_level_gallons", type=FieldType.NUMBER, required=True, description="Current tank level in US gallons"),
                FieldDef(name="last_reading_at", type=FieldType.DATE, required=False, description="Timestamp for the current tank level", date_format="ISO8601"),
                FieldDef(name="location_lat", type=FieldType.NUMBER, required=True, description="Tank latitude"),
                FieldDef(name="location_lon", type=FieldType.NUMBER, required=True, description="Tank longitude"),
                FieldDef(name="zip_code", type=FieldType.STRING, required=True, description="ZIP code, stored as text to preserve leading zeroes"),
                FieldDef(name="k_factor", type=FieldType.NUMBER, required=False, description="Optional consumption coefficient"),
                FieldDef(name="use_case", type=FieldType.ENUM, required=False, description="Tank use case", enum_values=["residential_heat", "commercial_heat", "generator", "farm", "other"]),
                FieldDef(name="status", type=FieldType.ENUM, required=False, description="Tank operational status", enum_values=["active", "inactive", "maintenance"]),
            ],
        ),
        "tank_readings": SchemaTemplate(
            data_type="tank_readings",
            description="Time-stamped customer tank telemetry readings",
            es_index="atg_readings",
            fields=[
                FieldDef(name="source_system", type=FieldType.STRING, required=True, description="Tank-monitor or telemetry source"),
                FieldDef(name="external_tank_id", type=FieldType.STRING, required=True, description="Tank identifier in the source system"),
                FieldDef(name="source_reading_id", type=FieldType.STRING, required=False, description="Optional reading identifier supplied by the source"),
                FieldDef(name="customer_tank_id", type=FieldType.STRING, required=False, description="Optional explicit Runsheet tank identifier"),
                FieldDef(name="volume_gallons", type=FieldType.NUMBER, required=True, description="Observed volume in US gallons"),
                FieldDef(name="water_level_in", type=FieldType.NUMBER, required=False, description="Observed water level in inches"),
                FieldDef(name="temperature_f", type=FieldType.NUMBER, required=False, description="Observed temperature in Fahrenheit"),
                FieldDef(name="reading_at", type=FieldType.DATE, required=True, description="Time the reading was measured", date_format="ISO8601"),
            ],
        ),

        "fuel_stations": SchemaTemplate(
            data_type="fuel_stations",
            description="Fuel station locations including capacity, pricing, and operational status",
            es_index="fuel_stations",
            fields=[
                FieldDef(name="station_id", type=FieldType.STRING, required=True, description="Unique fuel station identifier"),
                FieldDef(name="name", type=FieldType.STRING, required=True, description="Station name"),
                FieldDef(name="location", type=FieldType.STRING, required=False, description="Station address or location description"),
                FieldDef(name="coordinates", type=FieldType.GEO_POINT, required=False, description="GPS coordinates (lat,lon)"),
                FieldDef(name="fuel_types", type=FieldType.STRING, required=False, description="Available fuel types (comma-separated)"),
                FieldDef(name="capacity_gallons", type=FieldType.NUMBER, required=False, description="Total fuel storage capacity in gallons"),
                FieldDef(name="current_stock_gallons", type=FieldType.NUMBER, required=False, description="Current fuel stock in gallons"),
                FieldDef(name="price_per_gallon", type=FieldType.NUMBER, required=False, description="Current price per gallon"),
                FieldDef(name="status", type=FieldType.ENUM, required=True, description="Station operational status", enum_values=["open", "closed", "maintenance"]),
                FieldDef(name="operating_hours", type=FieldType.STRING, required=False, description="Operating hours (e.g. 06:00-22:00)"),
                FieldDef(name="last_restocked", type=FieldType.DATE, required=False, description="Last restock timestamp", date_format="ISO8601"),
            ],
        ),
        "inventory": SchemaTemplate(
            data_type="inventory",
            description="Warehouse and inventory item records including stock levels and locations",
            es_index="inventory",
            fields=[
                FieldDef(name="item_id", type=FieldType.STRING, required=True, description="Unique item identifier"),
                FieldDef(name="name", type=FieldType.STRING, required=True, description="Item name"),
                FieldDef(name="category", type=FieldType.STRING, required=False, description="Item category"),
                FieldDef(name="quantity", type=FieldType.NUMBER, required=True, description="Current stock quantity"),
                FieldDef(name="unit", type=FieldType.STRING, required=False, description="Unit of measurement"),
                FieldDef(name="location", type=FieldType.STRING, required=False, description="Storage location"),
                FieldDef(name="status", type=FieldType.ENUM, required=False, description="Item status", enum_values=["in_stock", "low_stock", "out_of_stock", "discontinued"]),
                FieldDef(name="last_updated", type=FieldType.DATE, required=False, description="Last inventory update timestamp", date_format="ISO8601"),
            ],
        ),
        "jobs": SchemaTemplate(
            data_type="jobs",
            description="Logistics job and scheduling records including assignments, routes, and completion status",
            es_index="jobs",
            fields=[
                FieldDef(name="job_id", type=FieldType.STRING, required=True, description="Unique job identifier"),
                FieldDef(name="title", type=FieldType.STRING, required=True, description="Job title or description"),
                FieldDef(name="job_type", type=FieldType.ENUM, required=False, description="Type of logistics job", enum_values=["pickup", "delivery", "transfer", "inspection", "maintenance"]),
                FieldDef(name="assigned_truck", type=FieldType.STRING, required=False, description="Assigned truck identifier"),
                FieldDef(name="assigned_driver", type=FieldType.STRING, required=False, description="Assigned driver identifier"),
                FieldDef(name="origin", type=FieldType.STRING, required=False, description="Job origin location"),
                FieldDef(name="destination", type=FieldType.STRING, required=False, description="Job destination location"),
                FieldDef(name="scheduled_at", type=FieldType.DATE, required=False, description="Scheduled start time", date_format="ISO8601"),
                FieldDef(name="completed_at", type=FieldType.DATE, required=False, description="Actual completion time", date_format="ISO8601"),
                FieldDef(name="status", type=FieldType.ENUM, required=True, description="Job status", enum_values=["scheduled", "in_progress", "completed", "cancelled", "failed"]),
                FieldDef(name="priority", type=FieldType.ENUM, required=False, description="Job priority", enum_values=["low", "medium", "high", "critical"]),
                FieldDef(name="notes", type=FieldType.STRING, required=False, description="Additional job notes"),
            ],
        ),
    }

    DATA_TYPE_INDEX_MAP: dict[str, str] = {
        "fleet": "trucks",
        "orders": "fuel_orders_current",
        "customer_tanks": "customer_tanks",
        "tank_readings": "atg_readings",
        "inventory": "inventory",
        "fuel_stations": "fuel_stations",
        "jobs": "jobs",
    }

    # Example data rows for CSV template generation, keyed by data type
    _EXAMPLE_DATA: dict[str, list[dict[str, str]]] = {
        "fleet": [
            {"truck_id": "TRK-001", "plate_number": "ABC-1234", "driver_id": "DRV-010", "driver_name": "John Smith", "status": "on_time", "estimated_arrival": "2024-03-15T14:30:00Z", "last_update": "2024-03-15T12:00:00Z", "cargo_type": "electronics", "cargo_weight": "1500.5", "cargo_volume": "12.3", "cargo_priority": "high"},
            {"truck_id": "TRK-002", "plate_number": "XYZ-5678", "driver_id": "DRV-020", "driver_name": "Jane Doe", "status": "idle", "estimated_arrival": "", "last_update": "2024-03-15T10:00:00Z", "cargo_type": "", "cargo_weight": "", "cargo_volume": "", "cargo_priority": ""},
            {"truck_id": "TRK-003", "plate_number": "DEF-9012", "driver_id": "DRV-030", "driver_name": "Bob Wilson", "status": "maintenance", "estimated_arrival": "", "last_update": "2024-03-14T16:00:00Z", "cargo_type": "fuel", "cargo_weight": "5000.0", "cargo_volume": "6.0", "cargo_priority": "critical"},
        ],
        "orders": [
            {"source_system": "sample_erp", "source_order_id": "SO-1001", "source_updated_at": "2026-07-29T12:00:00Z", "customer_id": "CUST-100", "customer_name": "Acme Farms", "customer_phone": "", "customer_email": "", "ship_to_address": "100 Farm Road, Indianapolis, IN 46201", "ship_to_lat": "39.7684", "ship_to_lon": "-86.1581", "customer_tank_id": "tank-100", "product_code": "DIESEL_2", "gallons_requested": "850", "fill_to_full": "false", "call_type": "one_off", "delivery_window_start": "2026-07-30T08:00:00Z", "delivery_window_end": "2026-07-30T12:00:00Z", "po_number": "PO-441", "special_instructions": "Call before delivery"},
            {"source_system": "sample_erp", "source_order_id": "SO-1002", "source_updated_at": "2026-07-29T12:05:00Z", "customer_id": "CUST-200", "customer_name": "Northside Apartments", "customer_phone": "", "customer_email": "", "ship_to_address": "200 North Street, Indianapolis, IN 46202", "ship_to_lat": "39.7795", "ship_to_lon": "-86.1470", "customer_tank_id": "tank-200", "product_code": "PROPANE", "gallons_requested": "", "fill_to_full": "true", "call_type": "keep_full", "delivery_window_start": "2026-07-30T13:00:00Z", "delivery_window_end": "2026-07-30T17:00:00Z", "po_number": "", "special_instructions": "Use rear service entrance"},
        ],
        "customer_tanks": [
            {"source_system": "sample_erp", "external_tank_id": "T-100", "customer_tank_id": "tank-100", "customer_id": "CUST-100", "customer_type": "commercial", "fuel_type": "diesel", "fuel_product_code": "DIESEL_2", "capacity_gallons": "1000", "current_level_gallons": "275", "last_reading_at": "2026-07-29T12:00:00Z", "location_lat": "39.7684", "location_lon": "-86.1581", "zip_code": "46201", "k_factor": "", "use_case": "farm", "status": "active"},
            {"source_system": "sample_erp", "external_tank_id": "T-200", "customer_tank_id": "tank-200", "customer_id": "CUST-200", "customer_type": "keep_full", "fuel_type": "propane", "fuel_product_code": "PROPANE", "capacity_gallons": "500", "current_level_gallons": "110", "last_reading_at": "2026-07-29T12:05:00Z", "location_lat": "39.7795", "location_lon": "-86.1470", "zip_code": "46202", "k_factor": "0.8", "use_case": "commercial_heat", "status": "active"},
        ],
        "tank_readings": [
            {"source_system": "veeder_root", "external_tank_id": "T-100", "source_reading_id": "VR-20260729-1200", "customer_tank_id": "tank-100", "volume_gallons": "275", "water_level_in": "0.1", "temperature_f": "68.5", "reading_at": "2026-07-29T12:00:00Z"},
            {"source_system": "veeder_root", "external_tank_id": "T-200", "source_reading_id": "VR-20260729-1205", "customer_tank_id": "tank-200", "volume_gallons": "110", "water_level_in": "0.0", "temperature_f": "69.0", "reading_at": "2026-07-29T12:05:00Z"},
        ],

        "fuel_stations": [
            {"station_id": "FS-001", "name": "Central Depot Fuel Station", "location": "4500 Industrial Blvd, Houston, TX 77001", "coordinates": "29.7604,-95.3698", "fuel_types": "diesel,gasoline", "capacity_gallons": "50000", "current_stock_gallons": "35000", "price_per_gallon": "3.85", "status": "open", "operating_hours": "06:00-22:00", "last_restocked": "2024-03-14T06:00:00Z"},
            {"station_id": "FS-002", "name": "I-10 Corridor Stop", "location": "Mile 45, I-10 Corridor, TX", "coordinates": "29.8500,-95.8000", "fuel_types": "diesel", "capacity_gallons": "30000", "current_stock_gallons": "8000", "price_per_gallon": "3.95", "status": "open", "operating_hours": "00:00-23:59", "last_restocked": "2024-03-10T08:00:00Z"},
            {"station_id": "FS-003", "name": "Port Area Station", "location": "1200 Dock Rd, Houston, TX 77015", "coordinates": "29.7350,-95.2800", "fuel_types": "diesel,gasoline,propane", "capacity_gallons": "75000", "current_stock_gallons": "60000", "price_per_gallon": "3.79", "status": "maintenance", "operating_hours": "06:00-20:00", "last_restocked": "2024-03-12T10:00:00Z"},
        ],
        "inventory": [
            {"item_id": "INV-001", "name": "Brake Pads Set", "category": "spare_parts", "quantity": "150", "unit": "sets", "location": "Warehouse A, Shelf 3", "status": "in_stock", "last_updated": "2024-03-15T08:00:00Z"},
            {"item_id": "INV-002", "name": "Engine Oil 5W-30", "category": "fluids", "quantity": "5", "unit": "liters", "location": "Warehouse B, Bay 1", "status": "low_stock", "last_updated": "2024-03-14T16:00:00Z"},
            {"item_id": "INV-003", "name": "Tire 295/80R22.5", "category": "tires", "quantity": "0", "unit": "units", "location": "Warehouse A, Shelf 7", "status": "out_of_stock", "last_updated": "2024-03-13T12:00:00Z"},
        ],
        "jobs": [
            {"job_id": "JOB-001", "title": "Fuel pickup from Houston Terminal", "job_type": "pickup", "assigned_truck": "TRK-001", "assigned_driver": "DRV-010", "origin": "Houston Terminal", "destination": "Dallas Depot", "scheduled_at": "2024-03-15T07:00:00Z", "completed_at": "", "status": "in_progress", "priority": "high", "notes": "HAZMAT load, handle with care"},
            {"job_id": "JOB-002", "title": "Delivery to Atlanta Terminal", "job_type": "delivery", "assigned_truck": "TRK-003", "assigned_driver": "DRV-030", "origin": "Chicago Yard", "destination": "Atlanta Terminal", "scheduled_at": "2024-03-16T05:00:00Z", "completed_at": "", "status": "scheduled", "priority": "critical", "notes": "Time-sensitive shipment"},
            {"job_id": "JOB-003", "title": "Vehicle inspection TRK-002", "job_type": "inspection", "assigned_truck": "TRK-002", "assigned_driver": "", "origin": "Maintenance Bay", "destination": "Maintenance Bay", "scheduled_at": "2024-03-15T14:00:00Z", "completed_at": "2024-03-15T15:30:00Z", "status": "completed", "priority": "medium", "notes": "Routine quarterly inspection"},
        ],
    }

    def get_template(self, data_type: str) -> SchemaTemplate:
        """Get the schema template for a data type.

        Args:
            data_type: One of the supported data type keys.

        Returns:
            The SchemaTemplate for the given data type.

        Raises:
            ValueError: If the data type is not supported.
        """
        if data_type not in self.TEMPLATES:
            supported = ", ".join(sorted(self.TEMPLATES.keys()))
            raise ValueError(f"Unsupported data type: {data_type}. Supported: {supported}")
        return self.TEMPLATES[data_type]

    def get_index(self, data_type: str) -> str:
        """Get the Elasticsearch index name for a data type.

        Args:
            data_type: One of the supported data type keys.

        Returns:
            The ES index name.

        Raises:
            ValueError: If the data type is not supported.
        """
        if data_type not in self.DATA_TYPE_INDEX_MAP:
            supported = ", ".join(sorted(self.DATA_TYPE_INDEX_MAP.keys()))
            raise ValueError(f"Unsupported data type: {data_type}. Supported: {supported}")
        return self.DATA_TYPE_INDEX_MAP[data_type]

    def get_required_fields(self, data_type: str) -> list[FieldDef]:
        """Get the required fields for a data type.

        Args:
            data_type: One of the supported data type keys.

        Returns:
            List of FieldDef objects where required is True.
        """
        template = self.get_template(data_type)
        return [f for f in template.fields if f.required]

    def get_optional_fields(self, data_type: str) -> list[FieldDef]:
        """Get the optional fields for a data type.

        Args:
            data_type: One of the supported data type keys.

        Returns:
            List of FieldDef objects where required is False.
        """
        template = self.get_template(data_type)
        return [f for f in template.fields if not f.required]

    def generate_csv_template(self, data_type: str) -> str:
        """Generate a CSV template string for a data type.

        The template includes a header row matching the schema field names
        and 2-3 example data rows demonstrating expected formats.

        Args:
            data_type: One of the supported data type keys.

        Returns:
            A CSV-formatted string with headers and example rows.

        Raises:
            ValueError: If the data type is not supported.
        """
        template = self.get_template(data_type)
        field_names = [f.name for f in template.fields]
        example_rows = self._EXAMPLE_DATA.get(data_type, [])

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(field_names)

        for row in example_rows:
            writer.writerow([row.get(field, "") for field in field_names])

        return output.getvalue()
