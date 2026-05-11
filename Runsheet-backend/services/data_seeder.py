"""
Data seeder for Elasticsearch
Seeds the Elasticsearch indices with mock data and handles temporal data updates
"""

import asyncio
import logging
from datetime import datetime, timedelta
from services.elasticsearch_service import elasticsearch_service
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

class DataSeeder:
    def __init__(self):
        self.es_service = elasticsearch_service
    
    async def clear_all_data(self):
        """Clear all existing data from indices"""
        indices = ["trucks", "locations", "inventory", "support_tickets", "analytics_events"]
        for index in indices:
            try:
                # Delete all documents in the index
                query = {"query": {"match_all": {}}}
                self.es_service.client.delete_by_query(index=index, body=query, refresh=True)
                logger.info(f"🗑️ Cleared data from {index}")
            except Exception as e:
                logger.warning(f"Could not clear {index}: {e}")
    
    async def seed_all_data(self, force=False):
        """Seed all indices with mock data (only if empty unless forced).

        All seeded documents are stamped with ``tenant_id=DEMO_TENANT_ID``
        so the dataset lives under a dedicated demo tenant. The per-index
        seeders (``seed_locations``, ``seed_trucks``, ``seed_inventory``,
        ``seed_support_tickets``, ``seed_analytics_events``) do not take a
        ``batch_metadata`` arg in this code path, so we stamp the tenant
        id by monkey-patching ``bulk_index_documents`` for the duration
        of the seed call. This keeps the fixture bodies untouched while
        still guaranteeing every written doc carries the tenant id.
        """
        try:
            logger.info("🌱 Starting data seeding process...")

            # Check if data already exists (unless forced)
            if not force:
                existing_trucks = await self.es_service.get_all_documents("trucks")
                if len(existing_trucks) > 0:
                    logger.info("📋 Data already exists, skipping seeding")
                    return

            demo_tenant_id = self.DEMO_TENANT_ID
            original_bulk = self.es_service.bulk_index_documents

            async def _bulk_with_demo_tenant(index: str, documents: list):
                for doc in documents:
                    if isinstance(doc, dict):
                        doc.setdefault("tenant_id", demo_tenant_id)
                return await original_bulk(index, documents)

            # Swap in the tenant-stamping wrapper for the duration of the seed.
            self.es_service.bulk_index_documents = _bulk_with_demo_tenant
            try:
                # Seed locations first (referenced by other entities)
                await self.seed_locations()

                # Seed other entities
                await self.seed_trucks()
                await self.seed_inventory()
                await self.seed_support_tickets()
                await self.seed_analytics_events()
            finally:
                self.es_service.bulk_index_documents = original_bulk

            logger.info("✅ Data seeding completed successfully!")

        except Exception:
            logger.exception("Data seeding failed")
            raise
    
    # Tenant id stamped on every document produced by ``seed_baseline_data``
    # / ``seed_all_data``. Kept as a module-level constant so the demo
    # dataset lives under a dedicated tenant ("demo") that real tenant
    # queries — which always filter on a specific ``tenant_id`` — cannot
    # resolve. Operators who want to reuse the demo data under a
    # different tenant can override it by subclassing ``DataSeeder`` or
    # passing an explicit tenant_id via ``upsert_batch_data``.
    DEMO_TENANT_ID = "demo"

    async def seed_baseline_data(self, operational_time="09:00"):
        """Seed baseline morning operations data for demo.

        Every document produced here is stamped with
        ``tenant_id=DEMO_TENANT_ID`` so it cannot leak into real tenants'
        reads — all production query paths filter on a specific tenant
        id via ``inject_tenant_filter``.
        """
        try:
            logger.info(f"🌅 Seeding baseline data for {operational_time}...")
            
            # Check if baseline data already exists
            existing_trucks = await self.es_service.get_all_documents("trucks")
            if len(existing_trucks) > 0:
                logger.info("📋 Baseline data already exists, skipping seeding")
                return
            
            # Add temporal metadata to all documents. ``tenant_id`` is
            # embedded here so every downstream ``.update(batch_metadata)``
            # call in ``seed_baseline_*`` stamps the demo tenant on its
            # docs without having to touch every fixture individually.
            base_timestamp = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
            batch_metadata = {
                "batch_id": "morning_baseline",
                "operational_time": operational_time,
                "ingestion_timestamp": utcnow().isoformat(),
                "data_version": "v1",
                "tenant_id": self.DEMO_TENANT_ID,
            }
            
            # Seed locations first
            await self.seed_locations(batch_metadata)
            
            # Seed baseline operational data
            await self.seed_baseline_trucks(batch_metadata, base_timestamp)
            await self.seed_baseline_inventory(batch_metadata, base_timestamp)
            await self.seed_baseline_support_tickets(batch_metadata, base_timestamp)
            await self.seed_analytics_events(batch_metadata)
            
            logger.info("✅ Baseline data seeding completed!")
            
        except Exception:
            logger.exception("Baseline data seeding failed")
            raise
    
    async def upsert_batch_data(self, data_type: str, documents: list, batch_id: str, operational_time: str, tenant_id: str = None):
        """Upsert batch data with temporal metadata.

        When ``tenant_id`` is provided every document is stamped with it so
        the resulting ES rows are tenant-scoped end-to-end. Existing callers
        (bootstrap seeding, tests) that don't pass the arg see the legacy
        behaviour and no tenant_id is written.
        """
        try:
            logger.info(f"📊 Upserting {len(documents)} {data_type} documents for batch {batch_id}")
            
            # Add temporal metadata to all documents
            batch_metadata = {
                "batch_id": batch_id,
                "operational_time": operational_time,
                "ingestion_timestamp": utcnow().isoformat(),
                "data_version": f"v{len(batch_id.split('_')) + 1}"
            }
            
            # Add metadata to each document
            for doc in documents:
                doc.update(batch_metadata)
                doc["operational_timestamp"] = utcnow().replace(
                    hour=int(operational_time.split(':')[0]),
                    minute=int(operational_time.split(':')[1]),
                    second=0,
                    microsecond=0
                ).isoformat()
                if tenant_id:
                    doc["tenant_id"] = tenant_id
            
            # Map data types to correct indices
            index_name = data_type
            if data_type == "fleet":
                index_name = "trucks"  # Fleet data goes to trucks index
            elif data_type == "support":
                index_name = "support_tickets"  # Support data goes to support_tickets index
            
            # Upsert documents (update existing, insert new)
            await self.es_service.bulk_index_documents(index_name, documents)
            
            logger.info(f"✅ Successfully upserted {len(documents)} {data_type} documents")
            return {"status": "success", "recordCount": len(documents)}
            
        except Exception:
            logger.exception("Batch upsert failed")
            raise
    
    async def seed_locations(self, batch_metadata=None):
        """Seed locations data"""
        locations_data = [
            {
                "location_id": "houston-terminal",
                "name": "Houston Terminal",
                "type": "terminal",
                "coordinates": {"lat": 29.7604, "lon": -95.3698},
                "address": "1200 Industrial Blvd, Houston, TX 77001",
                "region": "Southeast"
            },
            {
                "location_id": "dallas-depot",
                "name": "Dallas Depot",
                "type": "depot",
                "coordinates": {"lat": 32.7767, "lon": -96.7970},
                "address": "800 Commerce St, Dallas, TX 75201",
                "region": "Southeast"
            },
            {
                "location_id": "chicago-yard",
                "name": "Chicago Yard",
                "type": "depot",
                "coordinates": {"lat": 41.8781, "lon": -87.6298},
                "address": "2400 S Ashland Ave, Chicago, IL 60608",
                "region": "Midwest"
            },
            {
                "location_id": "denver-hub",
                "name": "Denver Hub",
                "type": "warehouse",
                "coordinates": {"lat": 39.7392, "lon": -104.9903},
                "address": "5500 Quebec St, Denver, CO 80216",
                "region": "Southwest"
            },
            {
                "location_id": "atlanta-terminal",
                "name": "Atlanta Terminal",
                "type": "terminal",
                "coordinates": {"lat": 33.7490, "lon": -84.3880},
                "address": "1500 Fulton Industrial Blvd, Atlanta, GA 30336",
                "region": "Southeast"
            },
            {
                "location_id": "phoenix-depot",
                "name": "Phoenix Depot",
                "type": "depot",
                "coordinates": {"lat": 33.4484, "lon": -112.0740},
                "address": "3200 W Buckeye Rd, Phoenix, AZ 85009",
                "region": "Southwest"
            },
            {
                "location_id": "detroit-terminal",
                "name": "Detroit Terminal",
                "type": "terminal",
                "coordinates": {"lat": 42.3314, "lon": -83.0458},
                "address": "900 Clark Ave, Detroit, MI 48209",
                "region": "Midwest"
            }
        ]
        
        # Add batch metadata if provided
        if batch_metadata:
            for location in locations_data:
                location.update(batch_metadata)
        
        await self.es_service.bulk_index_documents("locations", locations_data)
        logger.info("✅ Seeded locations data")
    
    async def seed_trucks(self):
        """Seed trucks data"""
        trucks_data = [
            {
                "truck_id": "TRK-001",
                "plate_number": "TRK-001",
                "driver_id": "driver-001",
                "driver_name": "Mike Johnson",
                "current_location": {
                    "id": "chicago-yard",
                    "name": "Chicago Yard",
                    "type": "depot",
                    "coordinates": {"lat": 41.8781, "lon": -87.6298},
                    "address": "2400 S Ashland Ave, Chicago, IL 60608"
                },
                "destination": {
                    "id": "detroit-terminal",
                    "name": "Detroit Terminal",
                    "type": "terminal",
                    "coordinates": {"lat": 42.3314, "lon": -83.0458},
                    "address": "900 Clark Ave, Detroit, MI 48209"
                },
                "route": {
                    "id": "chicago-detroit",
                    "distance": 450.0,
                    "estimated_duration": 300,
                    "actual_duration": None
                },
                "status": "on_time",
                "estimated_arrival": "2024-01-15T14:15:00Z",
                "last_update": "2024-01-15T12:00:00Z",
                "cargo": {
                    "type": "Diesel Fuel",
                    "weight": 15000.0,
                    "volume": 45.0,
                    "description": "ULSD diesel delivery to Detroit terminal",
                    "priority": "medium"
                }
            },
            {
                "truck_id": "TRK-002",
                "plate_number": "TRK-002",
                "driver_id": "driver-002",
                "driver_name": "Sarah Williams",
                "current_location": {
                    "id": "houston-terminal",
                    "name": "Houston Terminal",
                    "type": "terminal",
                    "coordinates": {"lat": 29.7604, "lon": -95.3698},
                    "address": "1200 Industrial Blvd, Houston, TX 77001"
                },
                "destination": {
                    "id": "dallas-depot",
                    "name": "Dallas Depot",
                    "type": "depot",
                    "coordinates": {"lat": 32.7767, "lon": -96.7970},
                    "address": "800 Commerce St, Dallas, TX 75201"
                },
                "route": {
                    "id": "houston-dallas",
                    "distance": 385.0,
                    "estimated_duration": 240,
                    "actual_duration": None
                },
                "status": "delayed",
                "estimated_arrival": "2024-01-15T16:25:00Z",
                "last_update": "2024-01-15T12:05:00Z",
                "cargo": {
                    "type": "Gasoline",
                    "weight": 8000.0,
                    "volume": 25.0,
                    "description": "Regular unleaded gasoline for Dallas depot stations",
                    "priority": "high"
                }
            },
            {
                "truck_id": "TRK-003",
                "plate_number": "TRK-003",
                "driver_id": "driver-003",
                "driver_name": "James Rodriguez",
                "current_location": {
                    "id": "chicago-yard",
                    "name": "Chicago Yard",
                    "type": "depot",
                    "coordinates": {"lat": 41.8781, "lon": -87.6298},
                    "address": "2400 S Ashland Ave, Chicago, IL 60608"
                },
                "destination": {
                    "id": "detroit-terminal",
                    "name": "Detroit Terminal",
                    "type": "terminal",
                    "coordinates": {"lat": 42.3314, "lon": -83.0458},
                    "address": "900 Clark Ave, Detroit, MI 48209"
                },
                "route": {
                    "id": "chicago-detroit-2",
                    "distance": 450.0,
                    "estimated_duration": 300,
                    "actual_duration": None
                },
                "status": "delayed",
                "estimated_arrival": "2024-01-15T12:25:00Z",
                "last_update": "2024-01-15T12:10:00Z",
                "cargo": {
                    "type": "Heating Oil",
                    "weight": 20000.0,
                    "volume": 60.0,
                    "description": "Heating oil delivery for residential distribution",
                    "priority": "medium"
                }
            },
            {
                "truck_id": "TRK-004",
                "plate_number": "TRK-004",
                "driver_id": "driver-004",
                "driver_name": "Emily Chen",
                "current_location": {
                    "id": "atlanta-terminal",
                    "name": "Atlanta Terminal",
                    "type": "terminal",
                    "coordinates": {"lat": 33.7490, "lon": -84.3880},
                    "address": "1500 Fulton Industrial Blvd, Atlanta, GA 30336"
                },
                "destination": {
                    "id": "houston-terminal",
                    "name": "Houston Terminal",
                    "type": "terminal",
                    "coordinates": {"lat": 29.7604, "lon": -95.3698},
                    "address": "1200 Industrial Blvd, Houston, TX 77001"
                },
                "route": {
                    "id": "atlanta-houston",
                    "distance": 1260.0,
                    "estimated_duration": 720,
                    "actual_duration": None
                },
                "status": "on_time",
                "estimated_arrival": "2024-01-15T15:30:00Z",
                "last_update": "2024-01-15T12:30:00Z",
                "cargo": {
                    "type": "Propane",
                    "weight": 5000.0,
                    "volume": 20.0,
                    "description": "Propane delivery for commercial accounts",
                    "priority": "high"
                }
            },
            {
                "truck_id": "TRK-005",
                "plate_number": "TRK-005",
                "driver_id": "driver-005",
                "driver_name": "David Thompson",
                "current_location": {
                    "id": "denver-hub",
                    "name": "Denver Hub",
                    "type": "warehouse",
                    "coordinates": {"lat": 39.7392, "lon": -104.9903},
                    "address": "5500 Quebec St, Denver, CO 80216"
                },
                "destination": {
                    "id": "phoenix-depot",
                    "name": "Phoenix Depot",
                    "type": "depot",
                    "coordinates": {"lat": 33.4484, "lon": -112.0740},
                    "address": "3200 W Buckeye Rd, Phoenix, AZ 85009"
                },
                "route": {
                    "id": "denver-phoenix",
                    "distance": 960.0,
                    "estimated_duration": 600,
                    "actual_duration": None
                },
                "status": "on_time",
                "estimated_arrival": "2024-01-15T17:00:00Z",
                "last_update": "2024-01-15T12:45:00Z",
                "cargo": {
                    "type": "DEF (Diesel Exhaust Fluid)",
                    "weight": 12000.0,
                    "volume": 35.0,
                    "description": "DEF delivery for fleet fueling stations",
                    "priority": "medium"
                }
            },
            {
                "truck_id": "TRK-006",
                "plate_number": "TRK-006",
                "driver_id": "driver-006",
                "driver_name": "Maria Garcia",
                "current_location": {
                    "id": "dallas-depot",
                    "name": "Dallas Depot",
                    "type": "depot",
                    "coordinates": {"lat": 32.7767, "lon": -96.7970},
                    "address": "800 Commerce St, Dallas, TX 75201"
                },
                "destination": {
                    "id": "atlanta-terminal",
                    "name": "Atlanta Terminal",
                    "type": "terminal",
                    "coordinates": {"lat": 33.7490, "lon": -84.3880},
                    "address": "1500 Fulton Industrial Blvd, Atlanta, GA 30336"
                },
                "route": {
                    "id": "dallas-atlanta",
                    "distance": 1250.0,
                    "estimated_duration": 720,
                    "actual_duration": None
                },
                "status": "delayed",
                "estimated_arrival": "2024-01-15T19:45:00Z",
                "last_update": "2024-01-15T13:00:00Z",
                "cargo": {
                    "type": "Kerosene",
                    "weight": 8500.0,
                    "volume": 40.0,
                    "description": "Kerosene delivery for aviation fuel blending",
                    "priority": "low"
                }
            }
        ]
        
        await self.es_service.bulk_index_documents("trucks", trucks_data)
        logger.info("✅ Seeded trucks data")
    
    async def seed_inventory(self):
        """Seed inventory data"""
        inventory_data = [
            {
                "item_id": "INV-001",
                "name": "Diesel Fuel Premium Grade",
                "category": "Fuel",
                "quantity": 15000,
                "unit": "gallons",
                "location": "Houston Terminal",
                "status": "in_stock",
                "last_updated": "2024-01-15T10:30:00Z"
            },
            {
                "item_id": "INV-002",
                "name": "Heavy Duty Truck Tires",
                "category": "Parts",
                "quantity": 25,
                "unit": "pieces",
                "location": "Dallas Depot",
                "status": "low_stock",
                "last_updated": "2024-01-15T09:15:00Z"
            },
            {
                "item_id": "INV-003",
                "name": "Synthetic Engine Oil 15W-40",
                "category": "Maintenance",
                "quantity": 0,
                "unit": "bottles",
                "location": "Chicago Yard",
                "status": "out_of_stock",
                "last_updated": "2024-01-14T16:45:00Z"
            },
            {
                "item_id": "INV-004",
                "name": "Ceramic Brake Pads Heavy Duty",
                "category": "Parts",
                "quantity": 120,
                "unit": "sets",
                "location": "Houston Terminal",
                "status": "in_stock",
                "last_updated": "2024-01-15T08:20:00Z"
            },
            {
                "item_id": "INV-005",
                "name": "Radiator Coolant Fluid",
                "category": "Maintenance",
                "quantity": 8,
                "unit": "bottles",
                "location": "Dallas Depot",
                "status": "low_stock",
                "last_updated": "2024-01-15T11:00:00Z"
            }
        ]
        
        await self.es_service.bulk_index_documents("inventory", inventory_data)
        logger.info("✅ Seeded inventory data")
    
    async def seed_support_tickets(self):
        """Seed support tickets data"""
        tickets_data = [
            {
                "ticket_id": "TKT-001",
                "customer": "PetroCorp",
                "customer_id": "CUST-001",
                "issue": "Delivery delay notification and customer communication",
                "description": "Order ORD-001 is running 3 hours behind schedule due to traffic congestion on I-10. Customer needs urgent update on revised ETA and compensation options.",
                "priority": "high",
                "status": "open",
                "related_order": "ORD-001",
                "created_at": "2024-01-15T09:30:00Z"
            },
            {
                "ticket_id": "TKT-002",
                "customer": "FuelNet",
                "customer_id": "CUST-002",
                "issue": "Damaged goods inspection and replacement request",
                "description": "Fuel tanker arrived with visible damage to valve assembly and minor leak detected. Customer requesting immediate replacement and investigation into handling procedures.",
                "priority": "urgent",
                "status": "in_progress",
                "assigned_to": "Mike Johnson",
                "related_order": "ORD-002",
                "created_at": "2024-01-15T11:15:00Z"
            },
            {
                "ticket_id": "TKT-003",
                "customer": "TankPro",
                "customer_id": "CUST-003",
                "issue": "Invoice discrepancy and billing inquiry",
                "description": "Customer questioning additional fuel surcharge and handling fees on delivery invoice. Requesting detailed breakdown of all charges and justification for extra costs.",
                "priority": "medium",
                "status": "resolved",
                "assigned_to": "Sarah Williams",
                "created_at": "2024-01-14T14:20:00Z",
                "resolved_at": "2024-01-15T10:30:00Z"
            },
            {
                "ticket_id": "TKT-004",
                "customer": "FleetEnergy",
                "customer_id": "CUST-005",
                "issue": "Missing items from shipment manifest",
                "description": "Partial delivery received with 500 gallons short from the original order manifest. Customer needs immediate investigation and delivery of remaining volume.",
                "priority": "high",
                "status": "open",
                "created_at": "2024-01-15T13:45:00Z"
            }
        ]
        
        await self.es_service.bulk_index_documents("support_tickets", tickets_data)
        logger.info("✅ Seeded support tickets data")
    
    async def seed_analytics_events(self, batch_metadata=None):
        """Seed analytics events data with time-series data for charts"""
        import random
        from datetime import datetime, timedelta
        
        events_data = []
        base_time = utcnow()
        
        # Generate time-series data for the last 30 days
        for days_back in range(30, 0, -1):
            event_time = base_time - timedelta(days=days_back)
            
            # Daily performance metrics
            events_data.append({
                "event_id": f"PERF-{days_back:03d}",
                "event_type": "daily_performance",
                "timestamp": event_time.isoformat(),
                "region": "All",
                "metrics": {
                    "delivery_performance_pct": round(85 + random.uniform(-10, 10), 1),
                    "average_delay_minutes": round(120 + random.uniform(-60, 120), 1),
                    "fleet_utilization_pct": round(90 + random.uniform(-15, 10), 1),
                    "customer_satisfaction": round(4.0 + random.uniform(-0.5, 1.0), 1),
                    "total_deliveries": random.randint(15, 35),
                    "on_time_deliveries": random.randint(12, 30)
                }
            })
            
            # Route performance events
            routes = [
                ("Houston → Dallas", "houston-dallas"),
                ("Chicago → Detroit", "chicago-detroit"), 
                ("Atlanta → Charlotte", "atlanta-charlotte"),
                ("Denver → Phoenix", "denver-phoenix")
            ]
            
            for route_name, route_id in routes:
                events_data.append({
                    "event_id": f"ROUTE-{route_id}-{days_back:03d}",
                    "event_type": "route_performance",
                    "timestamp": event_time.isoformat(),
                    "route_name": route_name,
                    "route_id": route_id,
                    "metrics": {
                        "performance_pct": round(75 + random.uniform(-15, 20), 1),
                        "avg_delivery_time": round(300 + random.uniform(-120, 180), 1),
                        "delay_incidents": random.randint(0, 5),
                        "completed_trips": random.randint(2, 8)
                    }
                })
        
        # Generate hourly data for the last 24 hours
        for hours_back in range(24, 0, -1):
            event_time = base_time - timedelta(hours=hours_back)
            
            events_data.append({
                "event_id": f"HOURLY-{hours_back:03d}",
                "event_type": "hourly_metrics",
                "timestamp": event_time.isoformat(),
                "region": "All",
                "metrics": {
                    "active_trucks": random.randint(4, 8),
                    "delivery_performance_pct": round(85 + random.uniform(-15, 15), 1),
                    "average_delay_minutes": round(90 + random.uniform(-60, 120), 1),
                    "fleet_utilization_pct": round(88 + random.uniform(-20, 12), 1)
                }
            })
        
        # Delay cause events
        delay_causes = [
            ("Traffic Congestion", 45),
            ("Weather Conditions", 28), 
            ("Vehicle Maintenance", 18),
            ("Loading Delays", 9)
        ]
        
        for cause, base_pct in delay_causes:
            events_data.append({
                "event_id": f"DELAY-{cause.replace(' ', '-').lower()}",
                "event_type": "delay_cause_analysis",
                "timestamp": base_time.isoformat(),
                "delay_cause": cause,
                "metrics": {
                    "percentage": round(base_pct + random.uniform(-5, 5), 1),
                    "incident_count": random.randint(5, 25),
                    "avg_delay_minutes": round(60 + random.uniform(-30, 90), 1)
                }
            })
        
        # Regional performance
        regions = ["Houston", "Dallas", "Chicago", "Atlanta"]
        for region in regions:
            events_data.append({
                "event_id": f"REGIONAL-{region.lower()}",
                "event_type": "regional_performance",
                "timestamp": base_time.isoformat(),
                "region": region,
                "metrics": {
                    "on_time_percentage": round(80 + random.uniform(-15, 15), 1),
                    "total_deliveries": random.randint(20, 50),
                    "avg_delivery_time": round(240 + random.uniform(-60, 120), 1),
                    "customer_rating": round(3.8 + random.uniform(-0.3, 1.2), 1)
                }
            })
        
        # Individual delivery events for more granular data
        delivery_events = [
            {
                "event_id": "DEL-001",
                "event_type": "delivery_completed",
                "timestamp": "2024-01-14T11:45:00Z",
                "truck_id": "TRK-002",
                "order_id": "ORD-003",
                "region": "Southeast",
                "metrics": {
                    "delivery_time_minutes": 385,
                    "delay_minutes": -15,
                    "distance_km": 285.5,
                    "fuel_consumed_liters": 45.2,
                    "customer_rating": 4.5
                }
            },
            {
                "event_id": "DEL-002", 
                "event_type": "delivery_started",
                "timestamp": "2024-01-15T08:00:00Z",
                "truck_id": "TRK-001",
                "order_id": "ORD-001",
                "region": "Midwest",
                "metrics": {
                    "planned_distance_km": 580.0,
                    "estimated_duration_minutes": 480
                }
            },
            {
                "event_id": "DEL-003",
                "event_type": "delay_reported",
                "timestamp": "2024-01-15T12:00:00Z",
                "truck_id": "TRK-003",
                "region": "Midwest",
                "delay_cause": "Traffic Congestion",
                "metrics": {
                    "delay_minutes": 180,
                    "expected_delay_duration": 120
                }
            }
        ]
        
        events_data.extend(delivery_events)
        
        # Add batch metadata if provided
        if batch_metadata:
            for event in events_data:
                event.update(batch_metadata)
        
        await self.es_service.bulk_index_documents("analytics_events", events_data)
        logger.info(f"✅ Seeded {len(events_data)} analytics events with time-series data")
    
    async def seed_baseline_trucks(self, batch_metadata, base_timestamp):
        """Seed baseline morning truck data - all on time"""
        trucks_data = [
            {
                "truck_id": "TRK-001",
                "plate_number": "TRK-001",
                "driver_id": "driver-001",
                "driver_name": "Mike Johnson",
                "current_location": {
                    "id": "houston-terminal",
                    "name": "Houston Terminal",
                    "type": "terminal",
                    "coordinates": {"lat": 29.7604, "lon": -95.3698},
                    "address": "1200 Industrial Blvd, Houston, TX 77001"
                },
                "destination": {
                    "id": "dallas-depot",
                    "name": "Dallas Depot",
                    "type": "depot",
                    "coordinates": {"lat": 32.7767, "lon": -96.7970},
                    "address": "800 Commerce St, Dallas, TX 75201"
                },
                "route": {
                    "id": "houston-dallas",
                    "distance": 385.0,
                    "estimated_duration": 240,
                    "actual_duration": None
                },
                "status": "on_time",
                "estimated_arrival": (base_timestamp + timedelta(hours=7)).isoformat(),
                "last_update": base_timestamp.isoformat(),
                "cargo": {
                    "type": "Diesel Fuel",
                    "weight": 15000.0,
                    "volume": 45.0,
                    "description": "ULSD diesel delivery to Dallas depot",
                    "priority": "medium"
                }
            },
            {
                "truck_id": "TRK-002",
                "plate_number": "TRK-002",
                "driver_id": "driver-002",
                "driver_name": "Sarah Williams",
                "current_location": {
                    "id": "atlanta-terminal",
                    "name": "Atlanta Terminal",
                    "type": "terminal",
                    "coordinates": {"lat": 33.7490, "lon": -84.3880},
                    "address": "1500 Fulton Industrial Blvd, Atlanta, GA 30336"
                },
                "destination": {
                    "id": "houston-terminal",
                    "name": "Houston Terminal",
                    "type": "terminal",
                    "coordinates": {"lat": 29.7604, "lon": -95.3698},
                    "address": "1200 Industrial Blvd, Houston, TX 77001"
                },
                "route": {
                    "id": "atlanta-houston",
                    "distance": 1260.0,
                    "estimated_duration": 720,
                    "actual_duration": None
                },
                "status": "on_time",
                "estimated_arrival": (base_timestamp + timedelta(hours=3)).isoformat(),
                "last_update": base_timestamp.isoformat(),
                "cargo": {
                    "type": "Gasoline",
                    "weight": 8000.0,
                    "volume": 25.0,
                    "description": "Regular unleaded gasoline for Houston terminal",
                    "priority": "high"
                }
            },
            {
                "truck_id": "TRK-003",
                "plate_number": "TRK-003",
                "driver_id": "driver-003",
                "driver_name": "James Rodriguez",
                "current_location": {
                    "id": "chicago-yard",
                    "name": "Chicago Yard",
                    "type": "depot",
                    "coordinates": {"lat": 41.8781, "lon": -87.6298},
                    "address": "2400 S Ashland Ave, Chicago, IL 60608"
                },
                "destination": {
                    "id": "denver-hub",
                    "name": "Denver Hub",
                    "type": "warehouse",
                    "coordinates": {"lat": 39.7392, "lon": -104.9903},
                    "address": "5500 Quebec St, Denver, CO 80216"
                },
                "route": {
                    "id": "chicago-denver",
                    "distance": 1600.0,
                    "estimated_duration": 900,
                    "actual_duration": None
                },
                "status": "on_time",
                "estimated_arrival": (base_timestamp + timedelta(hours=2, minutes=30)).isoformat(),
                "last_update": base_timestamp.isoformat(),
                "cargo": {
                    "type": "Heating Oil",
                    "weight": 20000.0,
                    "volume": 60.0,
                    "description": "Heating oil for Denver distribution",
                    "priority": "medium"
                }
            }
        ]
        
        # Add batch metadata
        for truck in trucks_data:
            truck.update(batch_metadata)
        
        await self.es_service.bulk_index_documents("trucks", trucks_data)
        logger.info("✅ Seeded baseline trucks data")
    
    async def seed_baseline_inventory(self, batch_metadata, base_timestamp):
        """Seed baseline morning inventory - full stock"""
        inventory_data = [
            {
                "item_id": "INV-001",
                "name": "Diesel Fuel Premium Grade",
                "category": "Fuel",
                "quantity": 15000,
                "unit": "gallons",
                "location": "Houston Terminal",
                "status": "in_stock",
                "last_updated": base_timestamp.isoformat()
            },
            {
                "item_id": "INV-002",
                "name": "Heavy Duty Truck Tires",
                "category": "Parts",
                "quantity": 50,
                "unit": "pieces",
                "location": "Dallas Depot",
                "status": "in_stock",
                "last_updated": base_timestamp.isoformat()
            },
            {
                "item_id": "INV-003",
                "name": "Synthetic Engine Oil 15W-40",
                "category": "Maintenance",
                "quantity": 25,
                "unit": "bottles",
                "location": "Chicago Yard",
                "status": "in_stock",
                "last_updated": base_timestamp.isoformat()
            },
            {
                "item_id": "INV-004",
                "name": "Ceramic Brake Pads Heavy Duty",
                "category": "Parts",
                "quantity": 120,
                "unit": "sets",
                "location": "Houston Terminal",
                "status": "in_stock",
                "last_updated": base_timestamp.isoformat()
            }
        ]
        
        # Add batch metadata
        for item in inventory_data:
            item.update(batch_metadata)
        
        await self.es_service.bulk_index_documents("inventory", inventory_data)
        logger.info("✅ Seeded baseline inventory data")
    
    async def seed_baseline_support_tickets(self, batch_metadata, base_timestamp):
        """Seed baseline morning support tickets - minimal issues"""
        tickets_data = [
            {
                "ticket_id": "TKT-001",
                "customer": "General Inquiry",
                "customer_id": "CUST-000",
                "issue": "Route optimization inquiry",
                "description": "Customer requesting information about optimal delivery routes for regular shipments",
                "priority": "low",
                "status": "open",
                "created_at": (base_timestamp - timedelta(minutes=15)).isoformat()
            }
        ]
        
        # Add batch metadata
        for ticket in tickets_data:
            ticket.update(batch_metadata)
        
        await self.es_service.bulk_index_documents("support_tickets", tickets_data)
        logger.info("✅ Seeded baseline support tickets data")

# Global instance
data_seeder = DataSeeder()
