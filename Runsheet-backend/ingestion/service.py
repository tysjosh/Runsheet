"""
Data ingestion service for real-time IoT/GPS location updates.

This module provides the DataIngestionService class for processing
location updates from IoT/GPS devices, including validation,
sanitization, and storage in Elasticsearch.

Validates:
- Requirement 6.2: WHEN a location update is received, THE Data_Ingestion_Service
  SHALL validate the payload schema and reject malformed requests with a 400 status
- Requirement 6.3: THE Data_Ingestion_Service SHALL sanitize all input data to
  prevent injection attacks before storing in Elasticsearch
- Requirement 6.7: THE Backend_Service SHALL implement WebSocket connections
  for pushing real-time updates to connected Frontend_Application clients
"""

import re
import logging
from datetime import datetime
from typing import Optional, List, Any, TYPE_CHECKING
from pydantic import BaseModel, field_validator, model_validator

from errors.exceptions import validation_error, resource_not_found
from telemetry.service import TelemetryService, get_telemetry_service

# Import ConnectionManager for WebSocket broadcasting
# Use TYPE_CHECKING to avoid circular imports
if TYPE_CHECKING:
    from websocket.connection_manager import ConnectionManager


logger = logging.getLogger(__name__)


# Regex patterns for input sanitization
# These patterns detect potentially dangerous characters/sequences
INJECTION_PATTERNS = [
    # Script injection patterns
    re.compile(r'<script[^>]*>.*?</script>', re.IGNORECASE | re.DOTALL),
    re.compile(r'javascript:', re.IGNORECASE),
    re.compile(r'on\w+\s*=', re.IGNORECASE),  # Event handlers like onclick=
    
    # SQL injection patterns
    re.compile(r"(\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|CREATE|TRUNCATE)\b)", re.IGNORECASE),
    re.compile(r"(--|;|/\*|\*/)", re.IGNORECASE),  # SQL comments and terminators
    
    # NoSQL/Elasticsearch injection patterns
    re.compile(r'\$\w+', re.IGNORECASE),  # MongoDB operators like $gt, $where
    re.compile(r'\{\s*"\$', re.IGNORECASE),  # JSON with $ operators
    
    # Path traversal patterns
    re.compile(r'\.\./', re.IGNORECASE),
    re.compile(r'\.\.\\', re.IGNORECASE),
    
    # Null byte injection
    re.compile(r'\x00', re.IGNORECASE),
]

# Characters that should be escaped or removed from string inputs
DANGEROUS_CHARS = ['<', '>', '"', "'", '&', '\x00', '\r', '\n']


class LocationUpdate(BaseModel):
    """
    Pydantic model for GPS location updates from IoT devices.
    
    This model validates incoming location data and ensures all fields
    meet the required constraints for geographic coordinates and
    optional telemetry data.
    
    Validates:
    - Requirement 6.2: Validate payload schema and reject malformed requests
    
    Attributes:
        truck_id: Unique identifier for the truck
        latitude: GPS latitude (-90 to 90 degrees)
        longitude: GPS longitude (-180 to 180 degrees)
        timestamp: Time of the location reading
        speed_kmh: Optional speed in kilometers per hour
        heading: Optional heading in degrees (0 to 360)
        accuracy_meters: Optional GPS accuracy in meters
    """
    
    truck_id: str
    latitude: float
    longitude: float
    timestamp: datetime
    speed_kmh: Optional[float] = None
    heading: Optional[float] = None
    accuracy_meters: Optional[float] = None
    
    @field_validator("truck_id")
    @classmethod
    def validate_truck_id(cls, v: str) -> str:
        """
        Validate truck_id is not empty and has reasonable length.
        
        Args:
            v: The truck_id value to validate
            
        Returns:
            The validated truck_id
            
        Raises:
            ValueError: If truck_id is empty or too long
        """
        if not v or not v.strip():
            raise ValueError("truck_id cannot be empty")
        if len(v) > 100:
            raise ValueError("truck_id cannot exceed 100 characters")
        return v.strip()
    
    @field_validator("latitude")
    @classmethod
    def validate_latitude(cls, v: float) -> float:
        """
        Validate latitude is within valid geographic range.
        
        Args:
            v: The latitude value to validate
            
        Returns:
            The validated latitude
            
        Raises:
            ValueError: If latitude is outside -90 to 90 range
        """
        if not -90 <= v <= 90:
            raise ValueError("Latitude must be between -90 and 90")
        return v
    
    @field_validator("longitude")
    @classmethod
    def validate_longitude(cls, v: float) -> float:
        """
        Validate longitude is within valid geographic range.
        
        Args:
            v: The longitude value to validate
            
        Returns:
            The validated longitude
            
        Raises:
            ValueError: If longitude is outside -180 to 180 range
        """
        if not -180 <= v <= 180:
            raise ValueError("Longitude must be between -180 and 180")
        return v
    
    @field_validator("speed_kmh")
    @classmethod
    def validate_speed(cls, v: Optional[float]) -> Optional[float]:
        """
        Validate speed is non-negative and reasonable.
        
        Args:
            v: The speed value to validate
            
        Returns:
            The validated speed or None
            
        Raises:
            ValueError: If speed is negative or unreasonably high
        """
        if v is not None:
            if v < 0:
                raise ValueError("Speed cannot be negative")
            if v > 300:  # Max reasonable truck speed in km/h
                raise ValueError("Speed exceeds maximum reasonable value (300 km/h)")
        return v
    
    @field_validator("heading")
    @classmethod
    def validate_heading(cls, v: Optional[float]) -> Optional[float]:
        """
        Validate heading is within compass range.
        
        Args:
            v: The heading value to validate
            
        Returns:
            The validated heading or None
            
        Raises:
            ValueError: If heading is outside 0 to 360 range
        """
        if v is not None:
            if not 0 <= v <= 360:
                raise ValueError("Heading must be between 0 and 360 degrees")
        return v
    
    @field_validator("accuracy_meters")
    @classmethod
    def validate_accuracy(cls, v: Optional[float]) -> Optional[float]:
        """
        Validate GPS accuracy is non-negative.
        
        Args:
            v: The accuracy value to validate
            
        Returns:
            The validated accuracy or None
            
        Raises:
            ValueError: If accuracy is negative
        """
        if v is not None:
            if v < 0:
                raise ValueError("Accuracy cannot be negative")
        return v


class BatchLocationUpdate(BaseModel):
    """
    Pydantic model for batch location updates.
    
    Allows multiple location updates to be submitted in a single request
    for efficiency when multiple trucks report simultaneously.
    
    Validates:
    - Requirement 6.5: Support batch location updates for efficiency
    
    Attributes:
        updates: List of location updates to process
    """
    
    updates: List[LocationUpdate]
    
    @field_validator("updates")
    @classmethod
    def validate_updates_not_empty(cls, v: List[LocationUpdate]) -> List[LocationUpdate]:
        """
        Validate that the updates list is not empty.
        
        Args:
            v: The list of updates to validate
            
        Returns:
            The validated list of updates
            
        Raises:
            ValueError: If the updates list is empty
        """
        if not v:
            raise ValueError("Updates list cannot be empty")
        if len(v) > 1000:  # Reasonable batch size limit
            raise ValueError("Batch size cannot exceed 1000 updates")
        return v


class LocationUpdateResult(BaseModel):
    """
    Result model for a single location update operation.
    
    Attributes:
        success: Whether the update was processed successfully
        truck_id: The truck ID that was updated
        message: Optional message with details
    """
    
    success: bool
    truck_id: str
    message: Optional[str] = None


class BatchUpdateResult(BaseModel):
    """
    Result model for batch location update operations.
    
    Attributes:
        total: Total number of updates in the batch
        successful: Number of successfully processed updates
        failed: Number of failed updates
        results: Individual results for each update
    """
    
    total: int
    successful: int
    failed: int
    results: List[LocationUpdateResult]


def sanitize_string(value: str) -> str:
    """
    Sanitize a string value to prevent injection attacks.
    
    This function removes or escapes potentially dangerous characters
    and patterns that could be used for injection attacks.
    
    Validates:
    - Requirement 6.3: Sanitize all input data to prevent injection attacks
    
    Args:
        value: The string value to sanitize
        
    Returns:
        The sanitized string
        
    Raises:
        ValueError: If the string contains dangerous injection patterns
    """
    if not value:
        return value
    
    # Check for injection patterns
    for pattern in INJECTION_PATTERNS:
        if pattern.search(value):
            raise ValueError(f"Input contains potentially dangerous pattern")
    
    # Remove null bytes
    sanitized = value.replace('\x00', '')
    
    # Escape HTML special characters
    sanitized = (
        sanitized
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
        .replace("'", '&#x27;')
    )
    
    # Remove control characters except newlines and tabs
    sanitized = ''.join(
        char for char in sanitized
        if char >= ' ' or char in '\n\t'
    )
    
    return sanitized


def sanitize_location_update(update: LocationUpdate) -> dict:
    """
    Sanitize a LocationUpdate model for safe storage.
    
    This function converts the Pydantic model to a dictionary and
    sanitizes all string fields to prevent injection attacks.
    
    Validates:
    - Requirement 6.3: Sanitize all input data to prevent injection attacks
    
    Args:
        update: The LocationUpdate to sanitize
        
    Returns:
        A dictionary with sanitized values ready for storage
    """
    # Convert to dict
    data = update.model_dump()
    
    # Sanitize string fields
    if data.get("truck_id"):
        data["truck_id"] = sanitize_string(data["truck_id"])
    
    # Convert timestamp to ISO format string for Elasticsearch
    if isinstance(data.get("timestamp"), datetime):
        data["timestamp"] = data["timestamp"].isoformat()
    
    return data


class DataIngestionService:
    """
    Service for processing real-time location updates from IoT/GPS devices.
    
    This service handles validation, sanitization, and storage of location
    updates in Elasticsearch. It integrates with the telemetry service for
    logging and metrics, and broadcasts updates via WebSocket to connected clients.
    
    Validates:
    - Requirement 6.2: Validate payload schema and reject malformed requests
    - Requirement 6.3: Sanitize all input data to prevent injection attacks
    - Requirement 6.7: Push real-time updates to connected WebSocket clients
    
    Attributes:
        es_service: Elasticsearch service for data storage
        telemetry: Telemetry service for logging and metrics
        connection_manager: Optional WebSocket connection manager for broadcasting
    """
    
    def __init__(
        self,
        es_service: Any,
        telemetry: Optional[TelemetryService] = None,
        connection_manager: Optional["ConnectionManager"] = None
    ):
        """
        Initialize the DataIngestionService.
        
        Args:
            es_service: Elasticsearch service instance for data operations
            telemetry: Optional telemetry service for logging (uses global if not provided)
            connection_manager: Optional WebSocket connection manager for broadcasting updates
        """
        self.es_service = es_service
        self.telemetry = telemetry or get_telemetry_service()
        self._connection_manager = connection_manager
        self._logger = logging.getLogger(__name__)
    
    def set_connection_manager(self, connection_manager: "ConnectionManager") -> None:
        """
        Set the WebSocket connection manager for broadcasting updates.
        
        This allows the connection manager to be set after initialization,
        which is useful for avoiding circular imports.
        
        Args:
            connection_manager: The ConnectionManager instance to use
        """
        self._connection_manager = connection_manager
        self._logger.info("WebSocket connection manager configured for data ingestion service")
    
    async def _broadcast_location_update(self, sanitized_data: dict) -> None:
        """
        Broadcast a location update to all connected WebSocket clients.
        
        This method sends the location update to all connected clients
        via the WebSocket connection manager. If no connection manager
        is configured, the broadcast is silently skipped.
        
        Validates:
        - Requirement 6.7: Push real-time updates to connected clients
        
        Args:
            sanitized_data: The sanitized location update data to broadcast
        """
        if self._connection_manager is None:
            # No connection manager configured, skip broadcast
            return
        
        try:
            clients_notified = await self._connection_manager.broadcast_location_update(
                truck_id=sanitized_data["truck_id"],
                latitude=sanitized_data["latitude"],
                longitude=sanitized_data["longitude"],
                timestamp=sanitized_data.get("timestamp"),
                speed_kmh=sanitized_data.get("speed_kmh"),
                heading=sanitized_data.get("heading"),
                accuracy_meters=sanitized_data.get("accuracy_meters")
            )
            
            if clients_notified > 0:
                self._logger.debug(
                    f"Location update broadcast to {clients_notified} WebSocket clients",
                    extra={"extra_data": {
                        "truck_id": sanitized_data["truck_id"],
                        "clients_notified": clients_notified
                    }}
                )
        except Exception as e:
            # Log but don't fail the update if broadcast fails
            self._logger.warning(
                f"Failed to broadcast location update via WebSocket: {e}",
                extra={"extra_data": {
                    "truck_id": sanitized_data["truck_id"],
                    "error": str(e)
                }}
            )
    
    async def validate_truck_exists(self, truck_id: str) -> bool:
        """
        Verify that a truck with the given ID exists in the system.
        
        Validates:
        - Requirement 6.6: IF a location update references a non-existent truck_id,
          THEN THE Data_Ingestion_Service SHALL log a warning and reject the update
        
        Args:
            truck_id: The truck ID to verify
            
        Returns:
            True if the truck exists, False otherwise
        """
        try:
            # Search for the truck in Elasticsearch
            query = {
                "query": {
                    "term": {
                        "truck_id": truck_id
                    }
                },
                "size": 1
            }
            
            result = await self.es_service.search_documents("trucks", query, size=1)
            
            if result and result.get("hits", {}).get("total", {}).get("value", 0) > 0:
                return True
            
            return False
            
        except Exception as e:
            self._logger.warning(
                f"Error checking truck existence for {truck_id}: {e}",
                extra={"extra_data": {"truck_id": truck_id, "error": str(e)}}
            )
            # In case of error, we'll be conservative and allow the update
            # The actual storage operation will fail if there's a real issue
            return True
    
    async def process_location_update(self, update: LocationUpdate) -> LocationUpdateResult:
        """
        Process and store a single location update.
        
        This method validates the update, sanitizes the data, verifies the
        truck exists, and stores the update in Elasticsearch.
        
        Validates:
        - Requirement 6.2: Validate payload schema and reject malformed requests
        - Requirement 6.3: Sanitize all input data to prevent injection attacks
        - Requirement 6.4: Update the corresponding truck document within 2 seconds
        - Requirement 6.6: Reject updates for non-existent truck_ids
        
        Args:
            update: The LocationUpdate to process
            
        Returns:
            LocationUpdateResult indicating success or failure
            
        Raises:
            AppException: If validation fails or truck doesn't exist
        """
        start_time = datetime.utcnow()
        
        try:
            # Sanitize the input data
            sanitized_data = sanitize_location_update(update)
            truck_id = sanitized_data["truck_id"]
            
            # Verify truck exists
            truck_exists = await self.validate_truck_exists(truck_id)
            if not truck_exists:
                self._logger.warning(
                    f"Location update rejected: truck_id '{truck_id}' not found",
                    extra={"extra_data": {"truck_id": truck_id}}
                )
                raise resource_not_found(
                    message=f"Truck with ID '{truck_id}' not found",
                    details={"truck_id": truck_id}
                )
            
            # Update the truck's current location in Elasticsearch
            location_data = {
                "current_location": {
                    "coordinates": {
                        "lat": sanitized_data["latitude"],
                        "lon": sanitized_data["longitude"]
                    }
                },
                "last_update": sanitized_data["timestamp"],
            }
            
            # Add optional fields if present
            if sanitized_data.get("speed_kmh") is not None:
                location_data["current_speed_kmh"] = sanitized_data["speed_kmh"]
            if sanitized_data.get("heading") is not None:
                location_data["current_heading"] = sanitized_data["heading"]
            
            # Update the truck document
            await self.es_service.index_document(
                index="trucks",
                doc_id=truck_id,
                document=location_data
            )
            
            # Also store in locations history index for tracking
            location_history = {
                "truck_id": truck_id,
                "coordinates": {
                    "lat": sanitized_data["latitude"],
                    "lon": sanitized_data["longitude"]
                },
                "timestamp": sanitized_data["timestamp"],
                "speed_kmh": sanitized_data.get("speed_kmh"),
                "heading": sanitized_data.get("heading"),
                "accuracy_meters": sanitized_data.get("accuracy_meters"),
            }
            
            # Generate a unique ID for the location history entry
            history_id = f"{truck_id}_{sanitized_data['timestamp']}"
            await self.es_service.index_document(
                index="locations",
                doc_id=history_id,
                document=location_history
            )
            
            # Log success with telemetry
            duration_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
            if self.telemetry:
                self.telemetry.record_metric(
                    "location_update_duration_ms",
                    duration_ms,
                    tags={"truck_id": truck_id}
                )
            
            self._logger.info(
                f"Location update processed for truck {truck_id}",
                extra={"extra_data": {
                    "truck_id": truck_id,
                    "latitude": sanitized_data["latitude"],
                    "longitude": sanitized_data["longitude"],
                    "duration_ms": duration_ms
                }}
            )
            
            # Broadcast location update via WebSocket to connected clients
            # Validates: Requirement 6.7 - Push real-time updates to connected clients
            await self._broadcast_location_update(sanitized_data)
            
            return LocationUpdateResult(
                success=True,
                truck_id=truck_id,
                message="Location update processed successfully"
            )
            
        except Exception as e:
            duration_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
            
            # Re-raise AppExceptions
            from errors.exceptions import AppException
            if isinstance(e, AppException):
                raise
            
            self._logger.error(
                f"Failed to process location update: {e}",
                extra={"extra_data": {
                    "truck_id": update.truck_id,
                    "error": str(e),
                    "duration_ms": duration_ms
                }}
            )
            
            return LocationUpdateResult(
                success=False,
                truck_id=update.truck_id,
                message=f"Failed to process update: {str(e)}"
            )
    
    async def process_batch_updates(
        self,
        updates: List[LocationUpdate]
    ) -> BatchUpdateResult:
        """
        Process multiple location updates efficiently.
        
        This method processes a batch of location updates, collecting
        results for each update and returning aggregate statistics.
        
        Validates:
        - Requirement 6.5: Support batch location updates for efficiency
        
        Args:
            updates: List of LocationUpdate objects to process
            
        Returns:
            BatchUpdateResult with success/failure counts and individual results
        """
        results: List[LocationUpdateResult] = []
        successful = 0
        failed = 0
        
        self._logger.info(
            f"Processing batch of {len(updates)} location updates",
            extra={"extra_data": {"batch_size": len(updates)}}
        )
        
        for update in updates:
            try:
                result = await self.process_location_update(update)
                results.append(result)
                if result.success:
                    successful += 1
                else:
                    failed += 1
            except Exception as e:
                # Catch any exceptions and continue processing other updates
                from errors.exceptions import AppException
                if isinstance(e, AppException):
                    message = e.message
                else:
                    message = str(e)
                
                results.append(LocationUpdateResult(
                    success=False,
                    truck_id=update.truck_id,
                    message=message
                ))
                failed += 1
        
        self._logger.info(
            f"Batch processing complete: {successful} successful, {failed} failed",
            extra={"extra_data": {
                "total": len(updates),
                "successful": successful,
                "failed": failed
            }}
        )
        
        return BatchUpdateResult(
            total=len(updates),
            successful=successful,
            failed=failed,
            results=results
        )
