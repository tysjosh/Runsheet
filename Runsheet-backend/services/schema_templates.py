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
            description="Customer order records including delivery details, status, and tracking information",
            es_index="orders",
            fields=[
                FieldDef(name="order_id", type=FieldType.STRING, required=True, description="Unique order identifier"),
                FieldDef(name="customer", type=FieldType.STRING, required=True, description="Customer name"),
                FieldDef(name="customer_id", type=FieldType.STRING, required=False, description="Customer identifier"),
                FieldDef(name="status", type=FieldType.ENUM, required=True, description="Order status", enum_values=["pending", "confirmed", "in_transit", "delivered", "cancelled"]),
                FieldDef(name="value", type=FieldType.NUMBER, required=False, description="Order monetary value"),
                FieldDef(name="items", type=FieldType.STRING, required=False, description="Order items description"),
                FieldDef(name="truck_id", type=FieldType.STRING, required=False, description="Assigned truck identifier"),
                FieldDef(name="region", type=FieldType.STRING, required=False, description="Delivery region"),
                FieldDef(name="priority", type=FieldType.ENUM, required=False, description="Order priority", enum_values=["low", "medium", "high", "critical"]),
                FieldDef(name="created_at", type=FieldType.DATE, required=False, description="Order creation timestamp", date_format="ISO8601"),
                FieldDef(name="delivery_eta", type=FieldType.DATE, required=False, description="Estimated delivery time", date_format="ISO8601"),
                FieldDef(name="delivered_at", type=FieldType.DATE, required=False, description="Actual delivery timestamp", date_format="ISO8601"),
            ],
        ),
        "riders": SchemaTemplate(
            data_type="riders",
            description="Delivery rider profiles including contact details, vehicle info, and availability",
            es_index="riders",
            fields=[
                FieldDef(name="rider_id", type=FieldType.STRING, required=True, description="Unique rider identifier"),
                FieldDef(name="name", type=FieldType.STRING, required=True, description="Rider full name"),
                FieldDef(name="phone", type=FieldType.STRING, required=False, description="Contact phone number"),
                FieldDef(name="email", type=FieldType.STRING, required=False, description="Contact email address"),
                FieldDef(name="vehicle_type", type=FieldType.ENUM, required=False, description="Type of delivery vehicle", enum_values=["motorcycle", "bicycle", "van", "car"]),
                FieldDef(name="license_number", type=FieldType.STRING, required=False, description="Driver license number"),
                FieldDef(name="status", type=FieldType.ENUM, required=True, description="Rider availability status", enum_values=["available", "on_delivery", "offline", "suspended"]),
                FieldDef(name="region", type=FieldType.STRING, required=False, description="Operating region"),
                FieldDef(name="rating", type=FieldType.NUMBER, required=False, description="Average rider rating (1-5)"),
                FieldDef(name="joined_at", type=FieldType.DATE, required=False, description="Date rider joined the platform", date_format="ISO8601"),
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
                FieldDef(name="capacity_liters", type=FieldType.NUMBER, required=False, description="Total fuel storage capacity in liters"),
                FieldDef(name="current_stock_liters", type=FieldType.NUMBER, required=False, description="Current fuel stock in liters"),
                FieldDef(name="price_per_liter", type=FieldType.NUMBER, required=False, description="Current price per liter"),
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
        "support_tickets": SchemaTemplate(
            data_type="support_tickets",
            description="Customer support ticket records including issue details, priority, and resolution status",
            es_index="support_tickets",
            fields=[
                FieldDef(name="ticket_id", type=FieldType.STRING, required=True, description="Unique ticket identifier"),
                FieldDef(name="customer", type=FieldType.STRING, required=True, description="Customer name"),
                FieldDef(name="customer_id", type=FieldType.STRING, required=False, description="Customer identifier"),
                FieldDef(name="issue", type=FieldType.STRING, required=True, description="Brief issue summary"),
                FieldDef(name="description", type=FieldType.STRING, required=False, description="Detailed issue description"),
                FieldDef(name="priority", type=FieldType.ENUM, required=False, description="Ticket priority", enum_values=["low", "medium", "high", "critical"]),
                FieldDef(name="status", type=FieldType.ENUM, required=True, description="Ticket status", enum_values=["open", "in_progress", "resolved", "closed"]),
                FieldDef(name="assigned_to", type=FieldType.STRING, required=False, description="Assigned support agent"),
                FieldDef(name="related_order", type=FieldType.STRING, required=False, description="Related order identifier"),
                FieldDef(name="created_at", type=FieldType.DATE, required=False, description="Ticket creation timestamp", date_format="ISO8601"),
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
        "orders": "orders",
        "inventory": "inventory",
        "support_tickets": "support_tickets",
        "riders": "riders",
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
            {"order_id": "ORD-001", "customer": "Acme Corp", "customer_id": "CUST-100", "status": "in_transit", "value": "2500.00", "items": "Industrial parts x20", "truck_id": "TRK-001", "region": "North", "priority": "high", "created_at": "2024-03-10T08:00:00Z", "delivery_eta": "2024-03-15T16:00:00Z", "delivered_at": ""},
            {"order_id": "ORD-002", "customer": "Global Logistics", "customer_id": "CUST-200", "status": "delivered", "value": "800.50", "items": "Office supplies x5", "truck_id": "TRK-002", "region": "South", "priority": "low", "created_at": "2024-03-08T10:00:00Z", "delivery_eta": "2024-03-12T12:00:00Z", "delivered_at": "2024-03-12T11:30:00Z"},
            {"order_id": "ORD-003", "customer": "Fresh Foods Ltd", "customer_id": "CUST-300", "status": "pending", "value": "15000.00", "items": "Refrigerated goods x50", "truck_id": "", "region": "East", "priority": "critical", "created_at": "2024-03-14T14:00:00Z", "delivery_eta": "2024-03-16T08:00:00Z", "delivered_at": ""},
        ],
        "riders": [
            {"rider_id": "RDR-001", "name": "Ali Hassan", "phone": "+254700100200", "email": "ali@example.com", "vehicle_type": "motorcycle", "license_number": "DL-12345", "status": "available", "region": "Central", "rating": "4.8", "joined_at": "2023-06-15T00:00:00Z"},
            {"rider_id": "RDR-002", "name": "Mary Wanjiku", "phone": "+254711200300", "email": "mary@example.com", "vehicle_type": "bicycle", "license_number": "", "status": "on_delivery", "region": "West", "rating": "4.5", "joined_at": "2023-09-01T00:00:00Z"},
            {"rider_id": "RDR-003", "name": "James Ochieng", "phone": "+254722300400", "email": "james@example.com", "vehicle_type": "van", "license_number": "DL-67890", "status": "offline", "region": "North", "rating": "4.2", "joined_at": "2024-01-10T00:00:00Z"},
        ],
        "fuel_stations": [
            {"station_id": "FS-001", "name": "Central Depot Fuel Station", "location": "123 Main Road, Nairobi", "coordinates": "-1.2921,36.8219", "fuel_types": "diesel,petrol", "capacity_liters": "50000", "current_stock_liters": "35000", "price_per_liter": "1.45", "status": "open", "operating_hours": "06:00-22:00", "last_restocked": "2024-03-14T06:00:00Z"},
            {"station_id": "FS-002", "name": "Highway Rest Stop", "location": "KM 45, Mombasa Highway", "coordinates": "-1.5000,37.0000", "fuel_types": "diesel", "capacity_liters": "30000", "current_stock_liters": "8000", "price_per_liter": "1.50", "status": "open", "operating_hours": "00:00-23:59", "last_restocked": "2024-03-10T08:00:00Z"},
            {"station_id": "FS-003", "name": "Port Area Station", "location": "Dock Road, Mombasa", "coordinates": "-4.0435,39.6682", "fuel_types": "diesel,petrol,lpg", "capacity_liters": "75000", "current_stock_liters": "60000", "price_per_liter": "1.42", "status": "maintenance", "operating_hours": "06:00-20:00", "last_restocked": "2024-03-12T10:00:00Z"},
        ],
        "inventory": [
            {"item_id": "INV-001", "name": "Brake Pads Set", "category": "spare_parts", "quantity": "150", "unit": "sets", "location": "Warehouse A, Shelf 3", "status": "in_stock", "last_updated": "2024-03-15T08:00:00Z"},
            {"item_id": "INV-002", "name": "Engine Oil 5W-30", "category": "fluids", "quantity": "5", "unit": "liters", "location": "Warehouse B, Bay 1", "status": "low_stock", "last_updated": "2024-03-14T16:00:00Z"},
            {"item_id": "INV-003", "name": "Tire 295/80R22.5", "category": "tires", "quantity": "0", "unit": "units", "location": "Warehouse A, Shelf 7", "status": "out_of_stock", "last_updated": "2024-03-13T12:00:00Z"},
        ],
        "support_tickets": [
            {"ticket_id": "TKT-001", "customer": "Acme Corp", "customer_id": "CUST-100", "issue": "Delayed delivery", "description": "Order ORD-001 has not arrived by the expected date", "priority": "high", "status": "open", "assigned_to": "agent-sarah", "related_order": "ORD-001", "created_at": "2024-03-15T09:00:00Z"},
            {"ticket_id": "TKT-002", "customer": "Global Logistics", "customer_id": "CUST-200", "issue": "Damaged goods", "description": "Items received in damaged packaging", "priority": "medium", "status": "in_progress", "assigned_to": "agent-mike", "related_order": "ORD-002", "created_at": "2024-03-13T14:00:00Z"},
            {"ticket_id": "TKT-003", "customer": "Fresh Foods Ltd", "customer_id": "CUST-300", "issue": "Invoice discrepancy", "description": "Billed amount does not match the agreed price", "priority": "low", "status": "resolved", "assigned_to": "agent-sarah", "related_order": "ORD-003", "created_at": "2024-03-10T11:00:00Z"},
        ],
        "jobs": [
            {"job_id": "JOB-001", "title": "Pickup from Warehouse A", "job_type": "pickup", "assigned_truck": "TRK-001", "assigned_driver": "DRV-010", "origin": "Warehouse A, Nairobi", "destination": "Distribution Center, Thika", "scheduled_at": "2024-03-15T07:00:00Z", "completed_at": "", "status": "in_progress", "priority": "high", "notes": "Fragile items, handle with care"},
            {"job_id": "JOB-002", "title": "Delivery to Mombasa Port", "job_type": "delivery", "assigned_truck": "TRK-003", "assigned_driver": "DRV-030", "origin": "Central Depot", "destination": "Mombasa Port", "scheduled_at": "2024-03-16T05:00:00Z", "completed_at": "", "status": "scheduled", "priority": "critical", "notes": "Time-sensitive shipment"},
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
