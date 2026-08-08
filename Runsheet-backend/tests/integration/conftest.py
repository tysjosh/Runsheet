"""
Integration test configuration and fixtures.

This module provides fixtures for integration testing with real or test
Elasticsearch instances and other external services.

Validates:
- Requirement 12.1: Set up integration test environment with test Elasticsearch
- Requirement 12.6: Create test data fixtures and cleanup utilities
"""
import os
import pytest
import asyncio
import logging
from typing import Generator, AsyncGenerator, Dict, Any, List, Optional
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta
from dataclasses import dataclass, field
import json
import uuid


logger = logging.getLogger(__name__)

# Test configuration constants
TEST_ES_INDEX_PREFIX = "test_"
TEST_INDICES = ["trucks", "orders", "inventory", "support_tickets", "locations"]


# The real-cluster fixtures stood here and are gone: ``TestElasticsearchConfig``,
# ``test_es_config``, ``real_es_client``, ``test_cleanup_real``,
# ``test_index_mappings``, ``setup_test_indices``, ``test_data_seeder_real`` and
# ``skip_if_no_real_es``. They connected to an Elasticsearch instance named by
# ``TEST_ELASTIC_ENDPOINT`` / ``TEST_ELASTIC_API_KEY`` when ``TEST_USE_MOCK_ES``
# was false, created prefixed test indices, and tore them down afterwards.
#
# Two reasons, and the second is why they went rather than being adapted:
#
# 1. There is no Elasticsearch, and ``import elasticsearch`` was the last one left
#    in the tree — the package is no longer in ``requirements.txt``, so the
#    fixture's ``except ImportError: pytest.skip`` was the only thing standing
#    between it and a collection error.
# 2. **Nothing requested them.** They formed a closed island — ``real_es_client``
#    fed ``test_cleanup_real``, which fed ``setup_test_indices`` and
#    ``test_data_seeder_real`` — and no test in the suite named any of the four.
#    So they were never exercised even when a cluster existed, which is worth
#    knowing before anyone rebuilds the equivalent: the mock fixtures below
#    (``test_cleanup``, ``test_data_seeder``) are what the integration tests
#    actually use, and ``tests/postgres/`` is where a real datastore is exercised.
#
# ``TestDataCleanup`` and ``TestDataSeeder`` themselves stay — the mock fixtures
# build on them.


@pytest.fixture
def mock_es_client() -> MagicMock:
    """
    Create a comprehensive mock Elasticsearch client for integration tests.
    
    This mock simulates Elasticsearch behavior for tests that don't require
    a real ES instance.
    
    Validates:
    - Requirement 12.1: Set up integration test environment
    """
    mock = MagicMock()
    
    # Mock ping
    mock.ping = MagicMock(return_value=True)
    
    # Mock indices operations
    mock.indices = MagicMock()
    mock.indices.exists = MagicMock(return_value=True)
    mock.indices.create = MagicMock(return_value={"acknowledged": True})
    mock.indices.delete = MagicMock(return_value={"acknowledged": True})
    mock.indices.get_mapping = MagicMock(return_value={})
    mock.indices.put_settings = MagicMock(return_value={"acknowledged": True})
    mock.indices.refresh = MagicMock(return_value={"_shards": {"successful": 1}})
    mock.indices.get = MagicMock(return_value={})
    
    # Mock ILM operations
    mock.ilm = MagicMock()
    mock.ilm.get_lifecycle = MagicMock(return_value={})
    mock.ilm.put_lifecycle = MagicMock(return_value={"acknowledged": True})
    mock.ilm.explain_lifecycle = MagicMock(return_value={"indices": {}})
    
    # Mock search operations with configurable responses
    mock._search_responses = []
    def mock_search(*args, **kwargs):
        if mock._search_responses:
            return mock._search_responses.pop(0)
        return {
            "hits": {
                "hits": [],
                "total": {"value": 0, "relation": "eq"}
            },
            "took": 1,
            "_shards": {"successful": 1, "total": 1, "failed": 0}
        }
    mock.search = MagicMock(side_effect=mock_search)
    
    # Mock index operations
    mock.index = MagicMock(return_value={
        "_index": "test_index",
        "_id": "test_id",
        "result": "created",
        "_version": 1
    })
    
    # Mock bulk operations with partial failure support
    mock._bulk_responses = []
    def mock_bulk(*args, **kwargs):
        if mock._bulk_responses:
            return mock._bulk_responses.pop(0)
        return {
            "errors": False,
            "items": [],
            "took": 1
        }
    mock.bulk = MagicMock(side_effect=mock_bulk)
    
    # Mock get operations
    mock._get_responses = {}
    def mock_get(*args, **kwargs):
        index = kwargs.get("index", args[0] if args else "test_index")
        doc_id = kwargs.get("id", args[1] if len(args) > 1 else "test_id")
        key = f"{index}:{doc_id}"
        if key in mock._get_responses:
            return mock._get_responses[key]
        return {
            "_index": index,
            "_id": doc_id,
            "found": True,
            "_source": {}
        }
    mock.get = MagicMock(side_effect=mock_get)
    
    # Mock delete operations
    mock.delete = MagicMock(return_value={
        "_index": "test_index",
        "_id": "test_id",
        "result": "deleted"
    })
    
    # Mock count operations
    mock.count = MagicMock(return_value={"count": 0})
    
    # Mock update operations
    mock.update = MagicMock(return_value={
        "_index": "test_index",
        "_id": "test_id",
        "result": "updated",
        "_version": 2
    })
    
    # Mock delete_by_query operations
    mock.delete_by_query = MagicMock(return_value={
        "deleted": 0,
        "total": 0,
        "failures": []
    })
    
    # Helper methods for test setup
    def add_search_response(response):
        mock._search_responses.append(response)
    mock.add_search_response = add_search_response
    
    def add_bulk_response(response):
        mock._bulk_responses.append(response)
    mock.add_bulk_response = add_bulk_response
    
    def set_get_response(index, doc_id, response):
        mock._get_responses[f"{index}:{doc_id}"] = response
    mock.set_get_response = set_get_response
    
    return mock


# ============================================================================
# Test Data Fixtures (Requirement 12.6)
# ============================================================================

def generate_test_id(prefix: str = "TEST") -> str:
    """Generate a unique test ID with prefix."""
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture
def truck_fixtures() -> List[Dict[str, Any]]:
    """
    Provide sample truck data for integration tests.
    
    Validates:
    - Requirement 12.6: Create test data fixtures
    """
    base_time = datetime.utcnow()
    return [
        {
            "truck_id": "TEST-TRUCK-001",
            "driver_name": "Test Driver 1",
            "driver_phone": "+1-555-0101",
            "status": "active",
            "current_location": {
                "coordinates": {"lat": 37.7749, "lon": -122.4194},
                "address": "San Francisco, CA",
                "last_updated": base_time.isoformat()
            },
            "capacity_kg": 5000,
            "current_load_kg": 2500,
            "fuel_level_percent": 75,
            "next_maintenance": (base_time + timedelta(days=30)).isoformat(),
            "license_plate": "TEST-001",
            "model": "Freightliner Cascadia",
            "year": 2022
        },
        {
            "truck_id": "TEST-TRUCK-002",
            "driver_name": "Test Driver 2",
            "driver_phone": "+1-555-0102",
            "status": "in_transit",
            "current_location": {
                "coordinates": {"lat": 34.0522, "lon": -118.2437},
                "address": "Los Angeles, CA",
                "last_updated": base_time.isoformat()
            },
            "capacity_kg": 8000,
            "current_load_kg": 6000,
            "fuel_level_percent": 50,
            "next_maintenance": (base_time + timedelta(days=15)).isoformat(),
            "license_plate": "TEST-002",
            "model": "Peterbilt 579",
            "year": 2021
        },
        {
            "truck_id": "TEST-TRUCK-003",
            "driver_name": "Test Driver 3",
            "driver_phone": "+1-555-0103",
            "status": "idle",
            "current_location": {
                "coordinates": {"lat": 47.6062, "lon": -122.3321},
                "address": "Seattle, WA",
                "last_updated": base_time.isoformat()
            },
            "capacity_kg": 3000,
            "current_load_kg": 0,
            "fuel_level_percent": 90,
            "next_maintenance": (base_time + timedelta(days=60)).isoformat(),
            "license_plate": "TEST-003",
            "model": "Kenworth T680",
            "year": 2023
        },
        {
            "truck_id": "TEST-TRUCK-004",
            "driver_name": "Test Driver 4",
            "driver_phone": "+1-555-0104",
            "status": "maintenance",
            "current_location": {
                "coordinates": {"lat": 45.5152, "lon": -122.6784},
                "address": "Portland, OR",
                "last_updated": base_time.isoformat()
            },
            "capacity_kg": 6000,
            "current_load_kg": 0,
            "fuel_level_percent": 30,
            "next_maintenance": base_time.isoformat(),
            "license_plate": "TEST-004",
            "model": "Volvo VNL",
            "year": 2020
        }
    ]


@pytest.fixture
def order_fixtures() -> List[Dict[str, Any]]:
    """
    Provide sample order data for integration tests.
    
    Validates:
    - Requirement 12.6: Create test data fixtures
    """
    base_time = datetime.utcnow()
    return [
        {
            "order_id": "TEST-ORDER-001",
            "customer_name": "Test Customer 1",
            "customer_email": "customer1@test.com",
            "customer_phone": "+1-555-1001",
            "status": "pending",
            "priority": "high",
            "items": [
                {"name": "Item A", "quantity": 10, "weight_kg": 50, "sku": "SKU-A"},
                {"name": "Item B", "quantity": 5, "weight_kg": 25, "sku": "SKU-B"}
            ],
            "total_weight_kg": 75,
            "pickup_location": {
                "address": "123 Test St, San Francisco, CA",
                "coordinates": {"lat": 37.7749, "lon": -122.4194}
            },
            "delivery_location": {
                "address": "456 Test Ave, Los Angeles, CA",
                "coordinates": {"lat": 34.0522, "lon": -118.2437}
            },
            "created_at": base_time.isoformat(),
            "estimated_delivery": (base_time + timedelta(days=2)).isoformat(),
            "special_instructions": "Handle with care"
        },
        {
            "order_id": "TEST-ORDER-002",
            "customer_name": "Test Customer 2",
            "customer_email": "customer2@test.com",
            "customer_phone": "+1-555-1002",
            "status": "in_transit",
            "priority": "medium",
            "items": [
                {"name": "Item C", "quantity": 20, "weight_kg": 100, "sku": "SKU-C"}
            ],
            "total_weight_kg": 100,
            "pickup_location": {
                "address": "789 Test Blvd, Seattle, WA",
                "coordinates": {"lat": 47.6062, "lon": -122.3321}
            },
            "delivery_location": {
                "address": "321 Test Rd, Portland, OR",
                "coordinates": {"lat": 45.5152, "lon": -122.6784}
            },
            "assigned_truck": "TEST-TRUCK-002",
            "created_at": (base_time - timedelta(days=1)).isoformat(),
            "estimated_delivery": (base_time + timedelta(days=1)).isoformat(),
            "special_instructions": None
        },
        {
            "order_id": "TEST-ORDER-003",
            "customer_name": "Test Customer 3",
            "customer_email": "customer3@test.com",
            "customer_phone": "+1-555-1003",
            "status": "delivered",
            "priority": "low",
            "items": [
                {"name": "Item D", "quantity": 5, "weight_kg": 10, "sku": "SKU-D"},
                {"name": "Item E", "quantity": 3, "weight_kg": 15, "sku": "SKU-E"}
            ],
            "total_weight_kg": 25,
            "pickup_location": {
                "address": "100 Warehouse Way, San Francisco, CA",
                "coordinates": {"lat": 37.7849, "lon": -122.4094}
            },
            "delivery_location": {
                "address": "200 Customer Lane, Oakland, CA",
                "coordinates": {"lat": 37.8044, "lon": -122.2712}
            },
            "assigned_truck": "TEST-TRUCK-001",
            "created_at": (base_time - timedelta(days=3)).isoformat(),
            "delivered_at": (base_time - timedelta(days=1)).isoformat(),
            "estimated_delivery": (base_time - timedelta(days=1)).isoformat(),
            "special_instructions": "Leave at door"
        }
    ]


@pytest.fixture
def inventory_fixtures() -> List[Dict[str, Any]]:
    """
    Provide sample inventory data for integration tests.
    
    Validates:
    - Requirement 12.6: Create test data fixtures
    """
    base_time = datetime.utcnow()
    return [
        {
            "item_id": "TEST-INV-001",
            "name": "Test Product A",
            "sku": "SKU-A-001",
            "category": "Electronics",
            "quantity": 100,
            "unit": "pieces",
            "warehouse_location": "Warehouse A - Shelf 1",
            "reorder_level": 20,
            "unit_price": 49.99,
            "last_restocked": base_time.isoformat(),
            "supplier": "Test Supplier 1",
            "weight_kg": 0.5
        },
        {
            "item_id": "TEST-INV-002",
            "name": "Test Product B",
            "sku": "SKU-B-002",
            "category": "Furniture",
            "quantity": 15,
            "unit": "pieces",
            "warehouse_location": "Warehouse B - Section 3",
            "reorder_level": 10,
            "unit_price": 199.99,
            "last_restocked": (base_time - timedelta(days=7)).isoformat(),
            "supplier": "Test Supplier 2",
            "weight_kg": 25.0
        },
        {
            "item_id": "TEST-INV-003",
            "name": "Test Product C",
            "sku": "SKU-C-003",
            "category": "Office Supplies",
            "quantity": 500,
            "unit": "boxes",
            "warehouse_location": "Warehouse A - Shelf 5",
            "reorder_level": 100,
            "unit_price": 9.99,
            "last_restocked": (base_time - timedelta(days=3)).isoformat(),
            "supplier": "Test Supplier 1",
            "weight_kg": 2.0
        },
        {
            "item_id": "TEST-INV-004",
            "name": "Test Product D - Low Stock",
            "sku": "SKU-D-004",
            "category": "Electronics",
            "quantity": 5,
            "unit": "pieces",
            "warehouse_location": "Warehouse A - Shelf 2",
            "reorder_level": 10,
            "unit_price": 299.99,
            "last_restocked": (base_time - timedelta(days=30)).isoformat(),
            "supplier": "Test Supplier 3",
            "weight_kg": 1.5
        }
    ]


@pytest.fixture
def support_ticket_fixtures() -> List[Dict[str, Any]]:
    """
    Provide sample support ticket data for integration tests.
    
    Validates:
    - Requirement 12.6: Create test data fixtures
    """
    base_time = datetime.utcnow()
    return [
        {
            "ticket_id": "TEST-TICKET-001",
            "subject": "Delivery Delay Issue",
            "description": "Order TEST-ORDER-001 is delayed by 2 days",
            "status": "open",
            "priority": "high",
            "customer_email": "customer1@test.com",
            "customer_name": "Test Customer 1",
            "category": "delivery",
            "created_at": base_time.isoformat(),
            "updated_at": base_time.isoformat(),
            "related_order": "TEST-ORDER-001"
        },
        {
            "ticket_id": "TEST-TICKET-002",
            "subject": "Damaged Package",
            "description": "Package arrived with visible damage to outer box",
            "status": "in_progress",
            "priority": "medium",
            "customer_email": "customer2@test.com",
            "customer_name": "Test Customer 2",
            "category": "quality",
            "assigned_to": "support_agent_1",
            "created_at": (base_time - timedelta(hours=5)).isoformat(),
            "updated_at": base_time.isoformat(),
            "related_order": "TEST-ORDER-002"
        },
        {
            "ticket_id": "TEST-TICKET-003",
            "subject": "Wrong Item Delivered",
            "description": "Received Item B instead of Item A",
            "status": "resolved",
            "priority": "high",
            "customer_email": "customer3@test.com",
            "customer_name": "Test Customer 3",
            "category": "fulfillment",
            "assigned_to": "support_agent_2",
            "created_at": (base_time - timedelta(days=2)).isoformat(),
            "updated_at": (base_time - timedelta(hours=12)).isoformat(),
            "resolved_at": (base_time - timedelta(hours=12)).isoformat(),
            "resolution": "Replacement item shipped",
            "related_order": "TEST-ORDER-003"
        },
        {
            "ticket_id": "TEST-TICKET-004",
            "subject": "Tracking Information Not Updating",
            "description": "Tracking shows no movement for 3 days",
            "status": "open",
            "priority": "low",
            "customer_email": "customer4@test.com",
            "customer_name": "Test Customer 4",
            "category": "tracking",
            "created_at": (base_time - timedelta(hours=2)).isoformat(),
            "updated_at": (base_time - timedelta(hours=2)).isoformat(),
            "related_order": None
        }
    ]


@pytest.fixture
def location_update_fixtures() -> List[Dict[str, Any]]:
    """
    Provide sample location update data for integration tests.
    
    Validates:
    - Requirement 12.6: Create test data fixtures
    """
    base_time = datetime.utcnow()
    return [
        {
            "truck_id": "TEST-TRUCK-001",
            "latitude": 37.7849,
            "longitude": -122.4094,
            "timestamp": base_time.isoformat(),
            "speed_kmh": 45.5,
            "heading": 180.0,
            "accuracy_meters": 5.0
        },
        {
            "truck_id": "TEST-TRUCK-002",
            "latitude": 34.0622,
            "longitude": -118.2537,
            "timestamp": base_time.isoformat(),
            "speed_kmh": 60.0,
            "heading": 90.0,
            "accuracy_meters": 3.0
        },
        {
            "truck_id": "TEST-TRUCK-001",
            "latitude": 37.7950,
            "longitude": -122.3994,
            "timestamp": (base_time + timedelta(minutes=5)).isoformat(),
            "speed_kmh": 55.0,
            "heading": 175.0,
            "accuracy_meters": 4.0
        },
        {
            "truck_id": "TEST-TRUCK-003",
            "latitude": 47.6162,
            "longitude": -122.3421,
            "timestamp": base_time.isoformat(),
            "speed_kmh": 0.0,
            "heading": 0.0,
            "accuracy_meters": 10.0
        }
    ]


@pytest.fixture
def batch_location_updates() -> List[Dict[str, Any]]:
    """
    Provide a batch of location updates for testing batch processing.
    
    Validates:
    - Requirement 12.6: Create test data fixtures
    """
    base_time = datetime.utcnow()
    updates = []
    
    # Generate 10 location updates for different trucks
    for i in range(10):
        truck_num = (i % 3) + 1
        updates.append({
            "truck_id": f"TEST-TRUCK-00{truck_num}",
            "latitude": 37.7749 + (i * 0.01),
            "longitude": -122.4194 + (i * 0.01),
            "timestamp": (base_time + timedelta(minutes=i)).isoformat(),
            "speed_kmh": 30.0 + (i * 5),
            "heading": (i * 36) % 360,
            "accuracy_meters": 5.0
        })
    
    return updates


# ============================================================================
# Cleanup Utilities (Requirement 12.6)
# ============================================================================

class TestDataCleanup:
    """
    Utility class for cleaning up test data.
    
    Provides methods to track and clean up test indices and documents
    created during integration tests. Supports both synchronous and
    asynchronous cleanup operations.
    
    Validates:
    - Requirement 12.6: Add cleanup utilities
    """
    
    def __init__(self, es_client, index_prefix: str = TEST_ES_INDEX_PREFIX):
        self.es_client = es_client
        self.index_prefix = index_prefix
        self._created_indices: List[str] = []
        self._created_documents: List[Dict[str, str]] = []
        self._cleanup_callbacks: List[callable] = []
    
    def track_index(self, index_name: str) -> None:
        """Track an index for cleanup."""
        if index_name not in self._created_indices:
            self._created_indices.append(index_name)
            logger.debug(f"Tracking index for cleanup: {index_name}")
    
    def track_document(self, index_name: str, doc_id: str) -> None:
        """Track a document for cleanup."""
        self._created_documents.append({"index": index_name, "id": doc_id})
        logger.debug(f"Tracking document for cleanup: {index_name}/{doc_id}")
    
    def add_cleanup_callback(self, callback: callable) -> None:
        """Add a custom cleanup callback to be executed during cleanup."""
        self._cleanup_callbacks.append(callback)
    
    def cleanup_documents(self) -> int:
        """
        Delete all tracked documents.
        
        Returns:
            Number of documents deleted
        """
        deleted = 0
        for doc in self._created_documents:
            try:
                self.es_client.delete(index=doc["index"], id=doc["id"])
                deleted += 1
                logger.debug(f"Deleted document: {doc['index']}/{doc['id']}")
            except Exception as e:
                logger.debug(f"Could not delete document {doc['index']}/{doc['id']}: {e}")
        self._created_documents.clear()
        return deleted
    
    def cleanup_indices(self) -> int:
        """
        Delete all tracked test indices.
        
        Returns:
            Number of indices deleted
        """
        deleted = 0
        for index_name in self._created_indices:
            try:
                if self.es_client.indices.exists(index=index_name):
                    self.es_client.indices.delete(index=index_name)
                    deleted += 1
                    logger.debug(f"Deleted index: {index_name}")
            except Exception as e:
                logger.debug(f"Could not delete index {index_name}: {e}")
        self._created_indices.clear()
        return deleted
    
    def cleanup_all(self) -> Dict[str, int]:
        """
        Clean up all tracked test data.
        
        Returns:
            Dict with counts of deleted documents and indices
        """
        # Execute custom cleanup callbacks first
        callbacks_executed = 0
        for callback in self._cleanup_callbacks:
            try:
                callback()
                callbacks_executed += 1
            except Exception as e:
                logger.warning(f"Cleanup callback failed: {e}")
        self._cleanup_callbacks.clear()
        
        return {
            "documents_deleted": self.cleanup_documents(),
            "indices_deleted": self.cleanup_indices(),
            "callbacks_executed": callbacks_executed
        }
    
    def cleanup_test_indices_by_prefix(self) -> int:
        """
        Delete all indices with the test prefix.
        
        This is a more aggressive cleanup that removes all test indices,
        not just tracked ones.
        
        Returns:
            Number of indices deleted
        """
        deleted = 0
        try:
            # Get all indices matching the test prefix
            indices = self.es_client.indices.get(index=f"{self.index_prefix}*")
            for index_name in indices.keys():
                try:
                    self.es_client.indices.delete(index=index_name)
                    deleted += 1
                    logger.info(f"Deleted test index: {index_name}")
                except Exception as e:
                    logger.warning(f"Could not delete index {index_name}: {e}")
        except Exception as e:
            logger.debug(f"No indices found with prefix {self.index_prefix}: {e}")
        return deleted
    
    def get_tracked_items(self) -> Dict[str, Any]:
        """Get summary of tracked items for debugging."""
        return {
            "indices": list(self._created_indices),
            "documents": list(self._created_documents),
            "callbacks": len(self._cleanup_callbacks)
        }


class AsyncTestDataCleanup:
    """
    Async version of TestDataCleanup for use with async Elasticsearch clients.
    
    Validates:
    - Requirement 12.6: Add cleanup utilities
    """
    
    def __init__(self, es_client, index_prefix: str = TEST_ES_INDEX_PREFIX):
        self.es_client = es_client
        self.index_prefix = index_prefix
        self._created_indices: List[str] = []
        self._created_documents: List[Dict[str, str]] = []
        self._cleanup_callbacks: List[callable] = []
    
    def track_index(self, index_name: str) -> None:
        """Track an index for cleanup."""
        if index_name not in self._created_indices:
            self._created_indices.append(index_name)
    
    def track_document(self, index_name: str, doc_id: str) -> None:
        """Track a document for cleanup."""
        self._created_documents.append({"index": index_name, "id": doc_id})
    
    def add_cleanup_callback(self, callback: callable) -> None:
        """Add a custom cleanup callback."""
        self._cleanup_callbacks.append(callback)
    
    async def cleanup_documents(self) -> int:
        """Delete all tracked documents asynchronously."""
        deleted = 0
        for doc in self._created_documents:
            try:
                await self.es_client.delete(index=doc["index"], id=doc["id"])
                deleted += 1
            except Exception:
                pass
        self._created_documents.clear()
        return deleted
    
    async def cleanup_indices(self) -> int:
        """Delete all tracked test indices asynchronously."""
        deleted = 0
        for index_name in self._created_indices:
            try:
                exists = await self.es_client.indices.exists(index=index_name)
                if exists:
                    await self.es_client.indices.delete(index=index_name)
                    deleted += 1
            except Exception:
                pass
        self._created_indices.clear()
        return deleted
    
    async def cleanup_all(self) -> Dict[str, int]:
        """Clean up all tracked test data asynchronously."""
        # Execute async cleanup callbacks
        callbacks_executed = 0
        for callback in self._cleanup_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback()
                else:
                    callback()
                callbacks_executed += 1
            except Exception as e:
                logger.warning(f"Async cleanup callback failed: {e}")
        self._cleanup_callbacks.clear()
        
        return {
            "documents_deleted": await self.cleanup_documents(),
            "indices_deleted": await self.cleanup_indices(),
            "callbacks_executed": callbacks_executed
        }


@pytest.fixture
def test_cleanup(mock_es_client) -> Generator[TestDataCleanup, None, None]:
    """
    Provide a cleanup utility that automatically cleans up after tests.
    
    Validates:
    - Requirement 12.6: Add cleanup utilities
    """
    cleanup = TestDataCleanup(mock_es_client)
    yield cleanup
    # Automatic cleanup after test
    result = cleanup.cleanup_all()
    logger.debug(f"Test cleanup completed: {result}")


@pytest.fixture
def all_fixtures(
    truck_fixtures,
    order_fixtures,
    inventory_fixtures,
    support_ticket_fixtures,
    location_update_fixtures
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Provide all test fixtures in a single dict.
    
    Validates:
    - Requirement 12.6: Create test data fixtures
    """
    return {
        "trucks": truck_fixtures,
        "orders": order_fixtures,
        "inventory": inventory_fixtures,
        "support_tickets": support_ticket_fixtures,
        "location_updates": location_update_fixtures
    }


# The "Test Index Setup Fixtures" section stood here. It declared mappings for five
# prefixed test indices and created them on a real cluster before each test. There is
# no cluster, and nothing requested the fixtures — see the note at the top of this
# file.


# ============================================================================
# Mock Service Fixtures (Requirement 12.1)
# ============================================================================

@pytest.fixture
def mock_es_service(mock_es_client):
    """
    Create a mock Elasticsearch service for integration tests.
    
    Validates:
    - Requirement 12.1: Set up integration test environment
    """
    mock_service = MagicMock()
    mock_service.client = mock_es_client
    mock_service.settings = MagicMock()
    mock_service.settings.elastic_endpoint = "http://test-elasticsearch:9200"
    mock_service.settings.elastic_api_key = "test-api-key"
    
    # Mock circuit breaker
    mock_service.circuit_breaker = MagicMock()
    mock_service.circuit_breaker.state = "closed"
    mock_service.circuit_breaker.is_open = False
    
    return mock_service


@pytest.fixture
def mock_session_store():
    """
    Create a mock session store for integration tests.
    
    Validates:
    - Requirement 12.1: Set up integration test environment
    """
    mock_store = MagicMock()
    mock_store.get = AsyncMock(return_value=None)
    mock_store.set = AsyncMock(return_value=True)
    mock_store.delete = AsyncMock(return_value=True)
    mock_store.health_check = AsyncMock(return_value=True)
    
    # Store for tracking session data
    mock_store._sessions = {}
    
    async def mock_get(session_id):
        return mock_store._sessions.get(session_id)
    
    async def mock_set(session_id, data, ttl=None):
        mock_store._sessions[session_id] = data
        return True
    
    async def mock_delete(session_id):
        if session_id in mock_store._sessions:
            del mock_store._sessions[session_id]
            return True
        return False
    
    mock_store.get = AsyncMock(side_effect=mock_get)
    mock_store.set = AsyncMock(side_effect=mock_set)
    mock_store.delete = AsyncMock(side_effect=mock_delete)
    
    return mock_store


# ============================================================================
# Test Data Seeding Utilities (Requirement 12.6)
# ============================================================================

class TestDataSeeder:
    """
    Utility class for seeding test data into Elasticsearch.
    
    Validates:
    - Requirement 12.6: Create test data fixtures
    """
    
    def __init__(self, es_client, cleanup: TestDataCleanup, index_prefix: str = TEST_ES_INDEX_PREFIX):
        self.es_client = es_client
        self.cleanup = cleanup
        self.index_prefix = index_prefix
    
    def seed_trucks(self, trucks: List[Dict[str, Any]]) -> List[str]:
        """Seed truck data and return list of created document IDs."""
        index_name = f"{self.index_prefix}trucks"
        doc_ids = []
        
        for truck in trucks:
            doc_id = truck.get("truck_id", generate_test_id("TRUCK"))
            self.es_client.index(index=index_name, id=doc_id, body=truck)
            self.cleanup.track_document(index_name, doc_id)
            doc_ids.append(doc_id)
        
        # Refresh to make documents searchable
        self.es_client.indices.refresh(index=index_name)
        return doc_ids
    
    def seed_orders(self, orders: List[Dict[str, Any]]) -> List[str]:
        """Seed order data and return list of created document IDs."""
        index_name = f"{self.index_prefix}orders"
        doc_ids = []
        
        for order in orders:
            doc_id = order.get("order_id", generate_test_id("ORDER"))
            self.es_client.index(index=index_name, id=doc_id, body=order)
            self.cleanup.track_document(index_name, doc_id)
            doc_ids.append(doc_id)
        
        self.es_client.indices.refresh(index=index_name)
        return doc_ids
    
    def seed_inventory(self, items: List[Dict[str, Any]]) -> List[str]:
        """Seed inventory data and return list of created document IDs."""
        index_name = f"{self.index_prefix}inventory"
        doc_ids = []
        
        for item in items:
            doc_id = item.get("item_id", generate_test_id("INV"))
            self.es_client.index(index=index_name, id=doc_id, body=item)
            self.cleanup.track_document(index_name, doc_id)
            doc_ids.append(doc_id)
        
        self.es_client.indices.refresh(index=index_name)
        return doc_ids
    
    def seed_support_tickets(self, tickets: List[Dict[str, Any]]) -> List[str]:
        """Seed support ticket data and return list of created document IDs."""
        index_name = f"{self.index_prefix}support_tickets"
        doc_ids = []
        
        for ticket in tickets:
            doc_id = ticket.get("ticket_id", generate_test_id("TICKET"))
            self.es_client.index(index=index_name, id=doc_id, body=ticket)
            self.cleanup.track_document(index_name, doc_id)
            doc_ids.append(doc_id)
        
        self.es_client.indices.refresh(index=index_name)
        return doc_ids
    
    def seed_all(self, fixtures: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[str]]:
        """Seed all fixture data and return dict of created document IDs."""
        return {
            "trucks": self.seed_trucks(fixtures.get("trucks", [])),
            "orders": self.seed_orders(fixtures.get("orders", [])),
            "inventory": self.seed_inventory(fixtures.get("inventory", [])),
            "support_tickets": self.seed_support_tickets(fixtures.get("support_tickets", []))
        }


@pytest.fixture
def test_data_seeder(mock_es_client, test_cleanup) -> TestDataSeeder:
    """
    Provide a test data seeder for mock Elasticsearch.
    
    Validates:
    - Requirement 12.6: Create test data fixtures
    """
    return TestDataSeeder(mock_es_client, test_cleanup)




# ============================================================================
# Environment Detection Fixtures
# ============================================================================

@pytest.fixture
def is_ci_environment() -> bool:
    """Detect if running in CI environment."""
    ci_indicators = ["CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "CIRCLECI"]
    return any(os.getenv(indicator) for indicator in ci_indicators)


