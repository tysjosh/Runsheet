"""
Data ingestion module for real-time IoT/GPS data processing.

This module provides services for receiving, validating, and storing
real-time location updates from IoT/GPS devices.

Validates:
- Requirement 6.2: Validate payload schema and reject malformed requests
- Requirement 6.3: Sanitize all input data to prevent injection attacks
"""

from ingestion.service import (
    DataIngestionService,
    LocationUpdate,
    BatchLocationUpdate,
    LocationUpdateResult,
    BatchUpdateResult,
)

__all__ = [
    "DataIngestionService",
    "LocationUpdate",
    "BatchLocationUpdate",
    "LocationUpdateResult",
    "BatchUpdateResult",
]
