"""
Health check service for the Runsheet backend.

This module provides the HealthCheckService class that monitors the health
status of all system dependencies including Elasticsearch.

Validates:
- Requirement 4.4: WHEN the `/health/ready` endpoint is called, THE Health_Check_Service
  SHALL check Elasticsearch connectivity with a timeout of 5 seconds
- Requirement 4.5: THE Health_Check_Service SHALL include response time metrics for
  each dependency in the health check response
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any

logger = logging.getLogger(__name__)


@dataclass
class DependencyHealth:
    """
    Health status of a single dependency.
    
    Attributes:
        name: The name of the dependency (e.g., "elasticsearch", "redis")
        healthy: Whether the dependency is healthy and responding
        response_time_ms: The time taken to check the dependency in milliseconds
        error: Optional error message if the dependency check failed
    """
    name: str
    healthy: bool
    response_time_ms: float
    error: Optional[str] = None
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "name": self.name,
            "healthy": self.healthy,
            "response_time_ms": round(self.response_time_ms, 2),
        }
        if self.error is not None:
            result["error"] = self.error
        return result


@dataclass
class HealthStatus:
    """
    Overall health status of the service.
    
    Attributes:
        status: Overall status - "healthy", "degraded", or "unhealthy"
        timestamp: When the health check was performed
        dependencies: List of individual dependency health statuses
    """
    status: str  # "healthy", "degraded", "unhealthy"
    timestamp: datetime
    dependencies: list[DependencyHealth] = field(default_factory=list)
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "status": self.status,
            "timestamp": self.timestamp.isoformat() + "Z",
            "dependencies": [dep.to_dict() for dep in self.dependencies],
        }


class HealthCheckService:
    """
    Service for checking the health of system dependencies.
    
    This service provides methods to check the readiness and liveness of the
    application by verifying connectivity to external dependencies like
    Elasticsearch.
    
    Validates:
    - Requirement 4.4: Check Elasticsearch connectivity with a timeout of 5 seconds
    - Requirement 4.5: Include response time metrics for each dependency
    
    Attributes:
        es_service: The Elasticsearch service instance to check
        session_store: Optional session store instance to check
        check_timeout: Timeout in seconds for dependency checks (default: 5.0)
    """
    
    def __init__(
        self,
        es_service: Any,
        session_store: Optional[Any] = None,
        check_timeout: float = 5.0
    ):
        """
        Initialize the HealthCheckService.
        
        Args:
            es_service: The Elasticsearch service instance
            session_store: Optional session store instance (Redis/DynamoDB)
            check_timeout: Timeout in seconds for dependency checks (default: 5.0)
        """
        self.es_service = es_service
        self.session_store = session_store
        self.check_timeout = check_timeout
    
    async def check_readiness(self) -> HealthStatus:
        """
        Check all dependencies for readiness.
        
        This method checks all configured dependencies (Elasticsearch, session store)
        and returns an aggregate health status. Each dependency check has a timeout
        of 5 seconds as per Requirement 4.4.
        
        Returns:
            HealthStatus: The aggregate health status with individual dependency statuses
            
        Validates:
        - Requirement 4.4: Check Elasticsearch connectivity with a timeout of 5 seconds
        - Requirement 4.5: Include response time metrics for each dependency
        """
        # Gather all dependency checks concurrently
        check_tasks = [self._check_elasticsearch()]
        
        # Add session store check if configured
        if self.session_store is not None:
            check_tasks.append(self._check_session_store())
        
        # Execute all checks concurrently
        results = await asyncio.gather(*check_tasks, return_exceptions=True)
        
        # Process results
        dependencies: list[DependencyHealth] = []
        for result in results:
            if isinstance(result, Exception):
                # This shouldn't happen as we handle exceptions in individual checks
                logger.error(f"Unexpected exception in health check: {result}")
                dependencies.append(DependencyHealth(
                    name="unknown",
                    healthy=False,
                    response_time_ms=0.0,
                    error=str(result)
                ))
            else:
                dependencies.append(result)
        
        # Determine overall status based on dependency health
        status = self._determine_overall_status(dependencies)
        
        return HealthStatus(
            status=status,
            timestamp=datetime.utcnow(),
            dependencies=dependencies
        )
    
    async def check_liveness(self) -> dict[str, Any]:
        """
        Simple liveness check - process is running.
        
        This check verifies that the process is running and can respond to requests.
        It does not check external dependencies.
        
        Returns:
            dict: A simple status response with "alive" status and timestamp
        """
        return {
            "status": "alive",
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
    
    async def check_health(self) -> dict[str, Any]:
        """
        Basic health check - service is accepting requests.
        
        Returns:
            dict: A simple status response indicating the service is up
        """
        return {
            "status": "ok",
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
    
    async def _check_elasticsearch(self) -> DependencyHealth:
        """
        Check Elasticsearch connectivity with timeout.
        
        Validates:
        - Requirement 4.4: Check Elasticsearch connectivity with a timeout of 5 seconds
        - Requirement 4.5: Include response time metrics for each dependency
        
        Returns:
            DependencyHealth: The health status of Elasticsearch
        """
        start_time = time.perf_counter()
        
        try:
            # Use asyncio.wait_for to enforce the 5-second timeout
            result = await asyncio.wait_for(
                self._ping_elasticsearch(),
                timeout=self.check_timeout
            )
            
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            
            if result:
                logger.debug(f"Elasticsearch health check passed in {elapsed_ms:.2f}ms")
                return DependencyHealth(
                    name="elasticsearch",
                    healthy=True,
                    response_time_ms=elapsed_ms
                )
            else:
                logger.warning(f"Elasticsearch ping returned False after {elapsed_ms:.2f}ms")
                return DependencyHealth(
                    name="elasticsearch",
                    healthy=False,
                    response_time_ms=elapsed_ms,
                    error="Elasticsearch ping returned False"
                )
                
        except asyncio.TimeoutError:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            error_msg = f"Elasticsearch health check timed out after {self.check_timeout} seconds"
            logger.warning(error_msg)
            return DependencyHealth(
                name="elasticsearch",
                healthy=False,
                response_time_ms=elapsed_ms,
                error=error_msg
            )
            
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            error_msg = f"Elasticsearch health check failed: {str(e)}"
            logger.error(error_msg)
            return DependencyHealth(
                name="elasticsearch",
                healthy=False,
                response_time_ms=elapsed_ms,
                error=error_msg
            )
    
    async def _ping_elasticsearch(self) -> bool:
        """
        Ping Elasticsearch to check connectivity.
        
        This method wraps the synchronous Elasticsearch ping in an async context.
        
        Returns:
            bool: True if Elasticsearch is reachable, False otherwise
        """
        # The Elasticsearch client's ping() is synchronous, so we run it in a thread pool
        loop = asyncio.get_event_loop()
        
        def _sync_ping() -> bool:
            if self.es_service is None or self.es_service.client is None:
                return False
            return self.es_service.client.ping()
        
        return await loop.run_in_executor(None, _sync_ping)
    
    async def _check_session_store(self) -> DependencyHealth:
        """
        Check session store connectivity with timeout.
        
        Validates:
        - Requirement 4.5: Include response time metrics for each dependency
        
        Returns:
            DependencyHealth: The health status of the session store
        """
        start_time = time.perf_counter()
        
        try:
            # Use asyncio.wait_for to enforce the timeout
            result = await asyncio.wait_for(
                self._ping_session_store(),
                timeout=self.check_timeout
            )
            
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            
            if result:
                logger.debug(f"Session store health check passed in {elapsed_ms:.2f}ms")
                return DependencyHealth(
                    name="session_store",
                    healthy=True,
                    response_time_ms=elapsed_ms
                )
            else:
                logger.warning(f"Session store health check returned False after {elapsed_ms:.2f}ms")
                return DependencyHealth(
                    name="session_store",
                    healthy=False,
                    response_time_ms=elapsed_ms,
                    error="Session store health check returned False"
                )
                
        except asyncio.TimeoutError:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            error_msg = f"Session store health check timed out after {self.check_timeout} seconds"
            logger.warning(error_msg)
            return DependencyHealth(
                name="session_store",
                healthy=False,
                response_time_ms=elapsed_ms,
                error=error_msg
            )
            
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            error_msg = f"Session store health check failed: {str(e)}"
            logger.error(error_msg)
            return DependencyHealth(
                name="session_store",
                healthy=False,
                response_time_ms=elapsed_ms,
                error=error_msg
            )
    
    async def _ping_session_store(self) -> bool:
        """
        Ping the session store to check connectivity.
        
        Returns:
            bool: True if the session store is reachable, False otherwise
        """
        if self.session_store is None:
            return False
        
        # Check if the session store has a health_check method
        if hasattr(self.session_store, 'health_check'):
            return await self.session_store.health_check()
        
        return False
    
    def _determine_overall_status(self, dependencies: list[DependencyHealth]) -> str:
        """
        Determine the overall health status based on dependency health.
        
        Status determination:
        - "healthy": All dependencies are healthy
        - "degraded": Some non-critical dependencies are unhealthy
        - "unhealthy": Critical dependencies (Elasticsearch) are unhealthy
        
        Args:
            dependencies: List of dependency health statuses
            
        Returns:
            str: Overall status - "healthy", "degraded", or "unhealthy"
        """
        if not dependencies:
            return "healthy"
        
        # Check for critical dependency failures (Elasticsearch)
        critical_deps = ["elasticsearch"]
        critical_healthy = True
        all_healthy = True
        
        for dep in dependencies:
            if not dep.healthy:
                all_healthy = False
                if dep.name in critical_deps:
                    critical_healthy = False
        
        if all_healthy:
            return "healthy"
        elif not critical_healthy:
            return "unhealthy"
        else:
            return "degraded"
