"""
Redis-based session store implementation.

This module provides a Redis-backed implementation of the SessionStore
interface for storing AI agent conversation memory externally, enabling
stateless backend operation and horizontal scaling.

Requirement 8.4: THE Session_Store SHALL support configurable TTL for
conversation sessions with a default of 24 hours.
"""

import json
from typing import Optional, Any
from datetime import timedelta

from session.store import SessionStore


# Default TTL of 24 hours as per requirement 8.4
DEFAULT_SESSION_TTL = timedelta(hours=24)


class RedisSessionStore(SessionStore):
    """
    Redis-backed session store implementation.
    
    This implementation uses Redis for storing session data with support
    for configurable TTL. Sessions are stored as JSON-serialized strings
    with a key prefix of "session:" for namespace isolation.
    
    Attributes:
        redis_url: Redis connection URL (e.g., "redis://localhost:6379")
        default_ttl: Default time-to-live for sessions (default: 24 hours)
        client: Redis async client instance (initialized via connect())
    """
    
    def __init__(
        self, 
        redis_url: str, 
        default_ttl: timedelta = DEFAULT_SESSION_TTL
    ):
        """
        Initialize the Redis session store.
        
        Args:
            redis_url: Redis connection URL (e.g., "redis://localhost:6379/0")
            default_ttl: Default TTL for sessions. Defaults to 24 hours
                as per requirement 8.4.
        """
        self.redis_url = redis_url
        self.default_ttl = default_ttl
        self.client = None
    
    async def connect(self) -> None:
        """
        Establish connection to Redis.
        
        This method must be called before using any other methods.
        It initializes the async Redis client from the configured URL.
        
        Raises:
            ConnectionError: If unable to connect to Redis.
        """
        import redis.asyncio as redis
        self.client = redis.from_url(self.redis_url, decode_responses=True)
    
    async def disconnect(self) -> None:
        """
        Close the Redis connection.
        
        Should be called during application shutdown to cleanly
        release resources.
        """
        if self.client:
            await self.client.close()
            self.client = None
    
    def _get_key(self, session_id: str) -> str:
        """
        Generate the Redis key for a session.
        
        Args:
            session_id: The session identifier.
            
        Returns:
            Redis key with "session:" prefix.
        """
        return f"session:{session_id}"
    
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
                error with Redis.
        """
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        
        key = self._get_key(session_id)
        data = await self.client.get(key)
        
        if data is None:
            return None
        
        return json.loads(data)
    
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
                uses the default TTL (24 hours per requirement 8.4).
                
        Raises:
            SessionStoreError: If there is a connectivity or operational
                error with Redis.
        """
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        
        key = self._get_key(session_id)
        effective_ttl = ttl or self.default_ttl
        ttl_seconds = int(effective_ttl.total_seconds())
        
        serialized_data = json.dumps(data)
        await self.client.setex(key, ttl_seconds, serialized_data)
    
    async def delete(self, session_id: str) -> None:
        """
        Delete session data by session ID.
        
        This operation is idempotent - deleting a non-existent
        session does not raise an error.
        
        Args:
            session_id: Unique identifier for the session to delete.
            
        Raises:
            SessionStoreError: If there is a connectivity or operational
                error with Redis.
        """
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        
        key = self._get_key(session_id)
        await self.client.delete(key)
    
    async def health_check(self) -> bool:
        """
        Check connectivity and health of the Redis store.
        
        This method is used by health check endpoints to verify that
        Redis is available and operational.
        
        Returns:
            True if Redis is healthy and accessible, False otherwise.
            
        Note:
            This method does not raise exceptions - connectivity issues
            are caught and result in a False return value.
        """
        if not self.client:
            return False
        
        try:
            # Use PING command to verify connectivity
            result = await self.client.ping()
            return result is True
        except Exception:
            return False
    
    async def refresh_ttl(
        self, 
        session_id: str, 
        ttl: Optional[timedelta] = None
    ) -> bool:
        """
        Refresh the TTL of an existing session without modifying its data.
        
        This is useful for extending session lifetime on activity
        without needing to read and rewrite the session data.
        
        Args:
            session_id: Unique identifier for the session.
            ttl: New TTL to set. If not provided, uses the default TTL.
            
        Returns:
            True if the session exists and TTL was refreshed,
            False if the session does not exist.
            
        Raises:
            RuntimeError: If the Redis client is not connected.
        """
        if not self.client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        
        key = self._get_key(session_id)
        effective_ttl = ttl or self.default_ttl
        ttl_seconds = int(effective_ttl.total_seconds())
        
        # EXPIRE returns True if the key exists and timeout was set
        result = await self.client.expire(key, ttl_seconds)
        return bool(result)
