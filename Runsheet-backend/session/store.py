"""
Session store abstraction for external session storage.

This module defines the abstract interface for session stores that enable
stateless backend operation by storing AI agent conversation memory in
external stores (Redis or DynamoDB) rather than in-process memory.

Requirement 8.1: THE Backend_Service SHALL store AI_Agent conversation memory
in an external Session_Store (Redis or DynamoDB) rather than in-process memory.
"""

from abc import ABC, abstractmethod
from typing import Optional, Any
from datetime import timedelta


class SessionStore(ABC):
    """
    Abstract base class for session storage implementations.
    
    This interface defines the contract for session stores that support
    stateless backend operation. Implementations may use Redis, DynamoDB,
    or other external storage systems.
    
    All methods are async to support non-blocking I/O operations with
    external storage systems.
    """
    
    @abstractmethod
    async def get(self, session_id: str) -> Optional[dict[str, Any]]:
        """
        Retrieve session data by session ID.
        
        Args:
            session_id: Unique identifier for the session.
            
        Returns:
            Session data as a dictionary if found, None if the session
            does not exist or has expired.
            
        Raises:
            SessionStoreError: If there is a connectivity or operational
                error with the underlying store.
        """
        pass
    
    @abstractmethod
    async def set(
        self, 
        session_id: str, 
        data: dict[str, Any], 
        ttl: Optional[timedelta] = None
    ) -> None:
        """
        Store session data with optional TTL.
        
        Args:
            session_id: Unique identifier for the session.
            data: Session data to store as a dictionary.
            ttl: Optional time-to-live for the session. If not provided,
                the implementation should use a default TTL (typically 24 hours).
                
        Raises:
            SessionStoreError: If there is a connectivity or operational
                error with the underlying store.
        """
        pass
    
    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """
        Delete session data by session ID.
        
        This operation should be idempotent - deleting a non-existent
        session should not raise an error.
        
        Args:
            session_id: Unique identifier for the session to delete.
            
        Raises:
            SessionStoreError: If there is a connectivity or operational
                error with the underlying store.
        """
        pass
    
    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check connectivity and health of the session store.
        
        This method is used by health check endpoints to verify that
        the session store is available and operational.
        
        Returns:
            True if the store is healthy and accessible, False otherwise.
            
        Note:
            This method should not raise exceptions - connectivity issues
            should be caught and result in a False return value.
        """
        pass
