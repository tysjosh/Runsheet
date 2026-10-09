"""
Data ingestion module for real-time IoT/GPS data processing.

This module provides services for receiving, validating, and storing
real-time location updates from IoT/GPS devices.

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
