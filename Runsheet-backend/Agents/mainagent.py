"""
Logistics AI Agent with circuit breaker protection for Gemini API calls
and external session store integration for stateless operation.

Validates:
- Requirement 3.5: Implement circuit breakers for Gemini API
- Requirement 2.5: Return specific error code indicating AI service unavailability
- Requirement 5.4: Record custom metrics for AI response times
- Requirement 8.2: Load conversation history from Session_Store using session identifier
- Requirement 8.3: Persist updated conversation history to Session_Store
- Requirement 8.6: Gracefully degrade when Session_Store is unavailable
"""

import os
import logging
import time
from typing import AsyncGenerator, Optional, Any
from datetime import datetime
from strands import Agent
from strands.models.litellm import LiteLLMModel
from dotenv import load_dotenv
from .tools import ALL_TOOLS
from config.settings import get_settings
from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitOpenException
from errors.codes import ErrorCode
from errors.exceptions import ai_service_unavailable, circuit_open

# Load environment variables
load_dotenv()

# Disable OpenTelemetry to avoid context errors
os.environ['OTEL_SDK_DISABLED'] = 'true'
os.environ['OTEL_PYTHON_DISABLED'] = 'true'
os.environ['OTEL_EXPORTER_OTLP_ENDPOINT'] = ''

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Suppress OpenTelemetry warnings and errors
logging.getLogger('opentelemetry').setLevel(logging.CRITICAL)
logging.getLogger('opentelemetry.context').setLevel(logging.CRITICAL)


def _get_telemetry_service():
    """Get the telemetry service instance for metrics recording."""
    try:
        from telemetry.service import get_telemetry_service
        return get_telemetry_service()
    except ImportError:
        return None


def _get_session_store():
    """
    Get the session store instance for conversation persistence.
    
    Returns None if session store is not configured or unavailable,
    enabling graceful degradation per Requirement 8.6.
    """
    try:
        from session.redis_store import RedisSessionStore
        from datetime import timedelta
        
        settings = get_settings()
        
        # Only create session store if Redis URL is configured
        if settings.redis_url:
            store = RedisSessionStore(
                redis_url=settings.redis_url,
                default_ttl=timedelta(hours=settings.session_ttl_hours)
            )
            return store
        return None
    except ImportError:
        logger.warning("Session store module not available")
        return None
    except Exception as e:
        logger.warning(f"Failed to initialize session store: {e}")
        return None


class LogisticsAgent:
    """
    Logistics AI Agent with circuit breaker protection for Gemini API calls
    and external session store integration for stateless operation.
    
    All Gemini API calls are wrapped with a circuit breaker to prevent
    cascading failures when the AI service is unavailable.
    
    Conversation history is persisted to an external session store (Redis/DynamoDB)
    to enable horizontal scaling without session affinity requirements.
    
    Validates:
    - Requirement 3.5: Implement circuit breakers for Gemini API
    - Requirement 2.5: Return specific error code indicating AI service unavailability
    - Requirement 8.2: Load conversation history from Session_Store using session identifier
    - Requirement 8.3: Persist updated conversation history to Session_Store
    - Requirement 8.6: Gracefully degrade when Session_Store is unavailable
    """
    
    def __init__(self):
        # Load settings from centralized configuration
        self.settings = get_settings()
        
        # Initialize circuit breaker for Gemini API calls
        # Default: 3 failures, 30 second recovery timeout
        self._circuit_breaker = CircuitBreaker(
            name="gemini_api",
            config=CircuitBreakerConfig(
                failure_threshold=3,
            )
        )
        
        # Session store for conversation persistence (may be None if not configured)
        self._session_store = None
        self._session_store_connected = False
        
        # Setup Google credentials
        self.setup_gemini_credentials()
        
        # Initialize Gemini model through LiteLLM
        gemini_model = LiteLLMModel(
            model_id="vertex_ai/gemini-2.5-flash",
            client_args={
                "vertex_project": self.settings.google_cloud_project,
                "vertex_location": self.settings.google_cloud_location,
            },
            params={
                "max_tokens": 8000,
                "temperature": 0.7,
            }
        )
        
        # Initialize Strands Agent with the Gemini model
        self.agent = Agent(
            model=gemini_model,
            system_prompt="""You are a Logistics AI Assistant for a fleet management and logistics platform. You help users manage their transportation operations, track deliveries, and optimize logistics workflows.

            **YOU NOW HAVE ACCESS TO LIVE DATA!** You can search and analyze real fleet, order, and support data using your tools.

            **CHAT MODE:**
            When in Chat Mode, you:
            - Answer questions about logistics using real data from your tools
            - ALWAYS announce your actions: "Let me search for [topic]..." BEFORE using tools
            - Use semantic search to find relevant information
            - Provide insights based on actual data
            - Be conversational and helpful
            - Explain what you found and provide actionable insights

            **AGENT MODE:**
            When in Agent Mode, you:
            - Generate comprehensive reports using multiple tools
            - Provide structured analysis with markdown formatting
            - Use report generation tools for complex analysis
            - Be systematic and thorough in data gathering
            - Present findings in a professional report format
            - Always explain your methodology and data sources

            **Available Tools:**
            - `search_fleet_data(query)` - Search trucks using semantic search
            - `search_orders(query)` - Search orders using semantic search  
            - `search_support_tickets(query)` - Search support tickets using semantic search
            - `search_inventory(query)` - Search inventory items using semantic search
            - `get_inventory_summary()` - Get all inventory items organized by status
            - `get_fleet_summary()` - Get current fleet status overview
            - `get_analytics_overview()` - Get performance metrics and KPIs
            - `get_performance_insights()` - Get actionable performance insights
            - `find_truck_by_id(truck_id)` - Find specific truck by ID/plate number
            - `get_all_locations()` - Get all depots, warehouses, and stations
            - `generate_operations_report()` - Generate comprehensive operations status report
            - `generate_performance_report()` - Generate detailed performance analysis report
            - `generate_incident_analysis(issue)` - Analyze incidents across multiple data sources

            **Your Expertise Areas:**
            - Fleet tracking and vehicle management
            - Route optimization and planning
            - Delivery scheduling and coordination
            - Customer order processing
            - Support ticket analysis
            - Logistics performance analytics
            - Supply chain optimization

            **Your Personality:**
            - Professional logistics expert with access to live data
            - Always explain what you're searching for before using tools
            - Provide actionable insights based on real information
            - Clear communicator who builds trust through transparency

            **Example Interactions:**
            User: "Show me delayed trucks"
            You: "Let me search for delayed vehicles in our fleet..." [calls get_fleet_summary]
            You: "I found [X] delayed trucks. Here's the breakdown: [results and analysis]"

            User: "Find orders with network equipment"  
            You: "Let me search our orders for network equipment..." [calls search_orders]
            You: "I found [X] orders containing network equipment: [results and insights]"

            Always announce your tool usage and explain the results clearly.""",
            tools=ALL_TOOLS
        )
        logger.info("✅ Logistics Agent initialized with Strands + Gemini 2.5 Flash")
    
    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Get the circuit breaker instance for external access."""
        return self._circuit_breaker
    
    async def _ensure_session_store_connected(self) -> bool:
        """
        Ensure the session store is connected.
        
        Returns True if connected successfully, False otherwise.
        Implements graceful degradation per Requirement 8.6.
        """
        if self._session_store_connected:
            return True
        
        if self._session_store is None:
            self._session_store = _get_session_store()
        
        if self._session_store is None:
            logger.debug("Session store not configured, using in-memory conversation")
            return False
        
        try:
            await self._session_store.connect()
            self._session_store_connected = True
            logger.info("✅ Session store connected successfully")
            return True
        except Exception as e:
            logger.warning(f"⚠️ Failed to connect to session store: {e}. Using in-memory conversation.")
            self._session_store = None
            return False
    
    async def _load_conversation_history(self, session_id: str) -> Optional[list]:
        """
        Load conversation history from the session store.
        
        Validates:
        - Requirement 8.2: WHEN a chat request is received, THE AI_Agent SHALL load
          conversation history from the Session_Store using a session identifier
        - Requirement 8.6: WHEN the Session_Store is unavailable, THE Backend_Service
          SHALL gracefully degrade by starting a new conversation rather than failing
        
        Args:
            session_id: Unique identifier for the conversation session.
            
        Returns:
            List of conversation messages if found, None otherwise.
        """
        if not await self._ensure_session_store_connected():
            logger.debug(f"Session store unavailable, starting fresh conversation for session {session_id}")
            return None
        
        try:
            session_data = await self._session_store.get(session_id)
            if session_data and "messages" in session_data:
                logger.info(f"📥 Loaded {len(session_data['messages'])} messages for session {session_id}")
                return session_data["messages"]
            logger.debug(f"No existing conversation found for session {session_id}")
            return None
        except Exception as e:
            # Graceful degradation: log warning and continue with fresh conversation
            logger.warning(f"⚠️ Failed to load conversation history for session {session_id}: {e}")
            return None
    
    async def _save_conversation_history(self, session_id: str, messages: list) -> bool:
        """
        Save conversation history to the session store.
        
        Validates:
        - Requirement 8.3: WHEN a chat response is generated, THE AI_Agent SHALL
          persist updated conversation history to the Session_Store
        - Requirement 8.6: WHEN the Session_Store is unavailable, THE Backend_Service
          SHALL gracefully degrade by starting a new conversation rather than failing
        
        Args:
            session_id: Unique identifier for the conversation session.
            messages: List of conversation messages to persist.
            
        Returns:
            True if saved successfully, False otherwise.
        """
        if not await self._ensure_session_store_connected():
            logger.debug(f"Session store unavailable, conversation not persisted for session {session_id}")
            return False
        
        try:
            session_data = {
                "session_id": session_id,
                "messages": messages,
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "message_count": len(messages)
            }
            await self._session_store.set(session_id, session_data)
            logger.info(f"📤 Saved {len(messages)} messages for session {session_id}")
            return True
        except Exception as e:
            # Graceful degradation: log warning but don't fail the request
            logger.warning(f"⚠️ Failed to save conversation history for session {session_id}: {e}")
            return False
    
    async def _clear_session(self, session_id: str) -> bool:
        """
        Clear conversation history from the session store.
        
        Args:
            session_id: Unique identifier for the conversation session.
            
        Returns:
            True if cleared successfully, False otherwise.
        """
        if not await self._ensure_session_store_connected():
            return False
        
        try:
            await self._session_store.delete(session_id)
            logger.info(f"🗑️ Cleared session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"⚠️ Failed to clear session {session_id}: {e}")
            return False
    
    def _handle_circuit_breaker_exception(self, exc: CircuitOpenException) -> dict:
        """
        Handle a circuit breaker exception by returning an appropriate error response.
        
        Validates:
        - Requirement 2.5: Return specific error code indicating AI service unavailability
        - Requirement 3.2: Return service unavailable response immediately when circuit is open
        
        Args:
            exc: The CircuitOpenException that was raised
            
        Returns:
            dict: Error response with type "error" and appropriate message
        """
        time_until_retry = None
        if exc.time_until_retry:
            time_until_retry = int(exc.time_until_retry.total_seconds())
        
        error_message = f"❌ AI service temporarily unavailable. Circuit breaker '{exc.circuit_name}' is open."
        if time_until_retry:
            error_message += f" Please retry in {time_until_retry} seconds."
        
        return {
            "type": "error",
            "content": error_message,
            "error_code": ErrorCode.CIRCUIT_OPEN.value,
            "details": {
                "circuit_name": exc.circuit_name,
                "time_until_retry_seconds": time_until_retry,
                "service": "gemini_api"
            }
        }
    
    def _handle_gemini_api_error(self, error: Exception) -> dict:
        """
        Handle a Gemini API error by returning an appropriate error response.
        
        Validates:
        - Requirement 2.5: Return specific error code indicating AI service unavailability
        
        Args:
            error: The exception that was raised
            
        Returns:
            dict: Error response with type "error" and appropriate message
        """
        logger.error(f"Gemini API error: {error}")
        return {
            "type": "error",
            "content": f"❌ AI service error: {str(error)}",
            "error_code": ErrorCode.AI_SERVICE_UNAVAILABLE.value,
            "details": {
                "error": str(error),
                "service": "gemini_api"
            }
        }

    def setup_gemini_credentials(self):
        """Setup Gemini credentials using the service account file from configuration"""
        try:
            # Check if running in Cloud Run (has GOOGLE_APPLICATION_CREDENTIALS set)
            if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
                logger.info("✅ Using Cloud Run service account credentials")
                os.environ['GOOGLE_CLOUD_PROJECT'] = self.settings.google_cloud_project
                return
            
            # Local development - use credentials path from configuration if provided
            credentials_path = self.settings.google_application_credentials
            
            if credentials_path and os.path.exists(credentials_path):
                os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = credentials_path
                os.environ['GOOGLE_CLOUD_PROJECT'] = self.settings.google_cloud_project
                logger.info(f"✅ Gemini credentials configured from: {credentials_path}")
            else:
                logger.warning("⚠️ No service account file found, using default credentials")
                os.environ['GOOGLE_CLOUD_PROJECT'] = self.settings.google_cloud_project
                
        except Exception as e:
            logger.error(f"Failed to setup Gemini credentials: {e}")
            # Don't raise - let it try with default credentials
            os.environ['GOOGLE_CLOUD_PROJECT'] = self.settings.google_cloud_project

    def clear_memory(self, session_id: Optional[str] = None):
        """
        Clear the agent's conversation memory.
        
        If a session_id is provided and session store is available,
        also clears the persisted session data.
        
        Args:
            session_id: Optional session identifier to clear from store.
        """
        try:
            # Clear Strands agent's message history
            self.agent.messages = []
            logger.info("✅ Agent memory cleared")
            
            # If session_id provided, also clear from session store
            if session_id:
                import asyncio
                try:
                    # Try to get the running event loop
                    loop = asyncio.get_running_loop()
                    # Schedule the coroutine to run
                    asyncio.create_task(self._clear_session(session_id))
                except RuntimeError:
                    # No running event loop, create one
                    asyncio.run(self._clear_session(session_id))
        except Exception as e:
            logger.error(f"Failed to clear agent memory: {e}")

    async def chat_streaming(self, message: str, mode: str = "chat", session_id: Optional[str] = None) -> AsyncGenerator[dict, None]:
        """
        Asynchronous streaming chat method with circuit breaker protection, 
        retry logic, and session persistence.
        
        Validates:
        - Requirement 3.5: Implement circuit breakers for Gemini API
        - Requirement 2.5: Return specific error code indicating AI service unavailability
        - Requirement 3.4: Retry with exponential backoff
        - Requirement 5.4: Record custom metrics for AI response times
        - Requirement 8.2: Load conversation history from Session_Store
        - Requirement 8.3: Persist updated conversation history to Session_Store
        - Requirement 8.6: Gracefully degrade when Session_Store is unavailable
        
        Args:
            message: The user's message to process.
            mode: Chat mode - "chat" or "agent".
            session_id: Optional session identifier for conversation persistence.
        """
        max_retries = 3
        retry_count = 0
        start_time = time.time()
        
        # Load conversation history from session store if session_id provided
        # Requirement 8.2: Load conversation history using session identifier
        if session_id:
            try:
                stored_messages = await self._load_conversation_history(session_id)
                if stored_messages:
                    # Restore conversation history to agent
                    self.agent.messages = stored_messages
                    logger.info(f"📥 Restored {len(stored_messages)} messages from session {session_id}")
            except Exception as e:
                # Graceful degradation: continue with fresh conversation
                logger.warning(f"⚠️ Could not restore session {session_id}: {e}")
        
        # Check circuit breaker state before attempting
        if self._circuit_breaker.state.value == "open":
            if not self._circuit_breaker._should_attempt_reset():
                # Circuit is open and not ready to retry
                error_response = self._handle_circuit_breaker_exception(
                    CircuitOpenException(
                        self._circuit_breaker.name,
                        self._circuit_breaker._get_time_until_retry()
                    )
                )
                yield error_response
                return
        
        while retry_count < max_retries:
            try:
                # Add mode context to the message
                message_with_context = f"[Mode: {mode.upper()}] {message}"
                
                # Track if we got any response
                got_response = False
                first_token_time = None
                
                # Wrap the streaming call with circuit breaker tracking
                async def _stream_with_tracking():
                    nonlocal got_response, first_token_time
                    async for event in self.agent.stream_async(message_with_context):
                        if not got_response:
                            first_token_time = time.time()
                        got_response = True
                        yield event
                
                async for event in _stream_with_tracking():
                    yield event
                
                # If we got here without exception, record success
                if got_response:
                    self._circuit_breaker._on_success()
                    
                    # Record AI response time metrics (Requirement 5.4)
                    telemetry = _get_telemetry_service()
                    if telemetry:
                        total_duration_ms = (time.time() - start_time) * 1000
                        telemetry.record_metric(
                            name="ai_response_time_ms",
                            value=total_duration_ms,
                            tags={"mode": mode, "success": "true"}
                        )
                        if first_token_time:
                            time_to_first_token_ms = (first_token_time - start_time) * 1000
                            telemetry.record_metric(
                                name="ai_time_to_first_token_ms",
                                value=time_to_first_token_ms,
                                tags={"mode": mode}
                            )
                    
                    # Persist updated conversation history to session store
                    # Requirement 8.3: Persist updated conversation history
                    if session_id:
                        try:
                            await self._save_conversation_history(session_id, self.agent.messages)
                        except Exception as e:
                            # Graceful degradation: log but don't fail the response
                            logger.warning(f"⚠️ Could not persist session {session_id}: {e}")
                
                return
                
            except CircuitOpenException as e:
                # Circuit breaker is open
                error_response = self._handle_circuit_breaker_exception(e)
                yield error_response
                return
                
            except Exception as e:
                retry_count += 1
                error_msg = str(e)
                
                # Check if it's a connection error (retryable)
                is_connection_error = any(keyword in error_msg.lower() for keyword in [
                    'connection closed', 'connection error', 'timeout', 'unavailable',
                    'service unavailable', 'rate limit', 'quota'
                ])
                
                if is_connection_error:
                    # Record failure in circuit breaker
                    self._circuit_breaker._on_failure()
                    
                    # Check if circuit is now open
                    if self._circuit_breaker.state.value == "open":
                        error_response = self._handle_circuit_breaker_exception(
                            CircuitOpenException(
                                self._circuit_breaker.name,
                                self._circuit_breaker._get_time_until_retry()
                            )
                        )
                        yield error_response
                        return
                    
                    if retry_count < max_retries:
                        logger.warning(f"Connection error (attempt {retry_count}/{max_retries}): {error_msg}")
                        yield {
                            "type": "status",
                            "content": f"🔄 Connection interrupted, retrying... (attempt {retry_count}/{max_retries})"
                        }
                        
                        # Wait a bit before retrying (exponential backoff)
                        import asyncio
                        await asyncio.sleep(1 * retry_count)
                        continue
                    else:
                        # Max retries reached - record failure metrics
                        telemetry = _get_telemetry_service()
                        if telemetry:
                            total_duration_ms = (time.time() - start_time) * 1000
                            telemetry.record_metric(
                                name="ai_response_time_ms",
                                value=total_duration_ms,
                                tags={"mode": mode, "success": "false", "error_type": "connection"}
                            )
                        
                        logger.error(f"Error in streaming chat (final): {e}")
                        yield {
                            "type": "error", 
                            "content": f"❌ Connection failed after {max_retries} attempts. The AI service is having connectivity issues. Please try again in a moment.",
                            "error_code": ErrorCode.AI_SERVICE_UNAVAILABLE.value
                        }
                        return
                else:
                    # Non-connection error - don't retry, but record failure
                    self._circuit_breaker._on_failure()
                    
                    # Record failure metrics
                    telemetry = _get_telemetry_service()
                    if telemetry:
                        total_duration_ms = (time.time() - start_time) * 1000
                        telemetry.record_metric(
                            name="ai_response_time_ms",
                            value=total_duration_ms,
                            tags={"mode": mode, "success": "false", "error_type": "other"}
                        )
                    
                    logger.error(f"Error in streaming chat: {e}")
                    yield self._handle_gemini_api_error(e)
                    return

    async def chat_fallback(self, message: str, mode: str = "chat", session_id: Optional[str] = None) -> str:
        """
        Non-streaming fallback method with circuit breaker protection and session persistence.
        
        Validates:
        - Requirement 3.5: Implement circuit breakers for Gemini API
        - Requirement 2.5: Return specific error code indicating AI service unavailability
        - Requirement 5.4: Record custom metrics for AI response times
        - Requirement 8.2: Load conversation history from Session_Store
        - Requirement 8.3: Persist updated conversation history to Session_Store
        - Requirement 8.6: Gracefully degrade when Session_Store is unavailable
        
        Args:
            message: The user's message to process.
            mode: Chat mode - "chat" or "agent".
            session_id: Optional session identifier for conversation persistence.
        """
        start_time = time.time()
        
        # Load conversation history from session store if session_id provided
        # Requirement 8.2: Load conversation history using session identifier
        if session_id:
            try:
                stored_messages = await self._load_conversation_history(session_id)
                if stored_messages:
                    # Restore conversation history to agent
                    self.agent.messages = stored_messages
                    logger.info(f"📥 Restored {len(stored_messages)} messages from session {session_id}")
            except Exception as e:
                # Graceful degradation: continue with fresh conversation
                logger.warning(f"⚠️ Could not restore session {session_id}: {e}")
        
        try:
            # Check circuit breaker state before attempting
            if self._circuit_breaker.state.value == "open":
                if not self._circuit_breaker._should_attempt_reset():
                    # Circuit is open and not ready to retry
                    time_until_retry = self._circuit_breaker._get_time_until_retry()
                    retry_msg = ""
                    if time_until_retry:
                        retry_msg = f" Please retry in {int(time_until_retry.total_seconds())} seconds."
                    return f"❌ AI service temporarily unavailable. Circuit breaker is open.{retry_msg}"
            
            logger.info("🔄 Using non-streaming fallback mode")
            message_with_context = f"[Mode: {mode.upper()}] {message}"
            
            # Use non-streaming completion
            response = await self.agent.run_async(message_with_context)
            
            # Record success in circuit breaker
            self._circuit_breaker._on_success()
            
            # Record AI response time metrics (Requirement 5.4)
            telemetry = _get_telemetry_service()
            if telemetry:
                total_duration_ms = (time.time() - start_time) * 1000
                telemetry.record_metric(
                    name="ai_response_time_ms",
                    value=total_duration_ms,
                    tags={"mode": mode, "success": "true", "method": "fallback"}
                )
            
            # Persist updated conversation history to session store
            # Requirement 8.3: Persist updated conversation history
            if session_id:
                try:
                    await self._save_conversation_history(session_id, self.agent.messages)
                except Exception as e:
                    # Graceful degradation: log but don't fail the response
                    logger.warning(f"⚠️ Could not persist session {session_id}: {e}")
            
            return response
            
        except CircuitOpenException as e:
            time_until_retry = ""
            if e.time_until_retry:
                time_until_retry = f" Please retry in {int(e.time_until_retry.total_seconds())} seconds."
            return f"❌ AI service temporarily unavailable. Circuit breaker '{e.circuit_name}' is open.{time_until_retry}"
            
        except Exception as e:
            # Record failure in circuit breaker
            self._circuit_breaker._on_failure()
            
            # Record failure metrics
            telemetry = _get_telemetry_service()
            if telemetry:
                total_duration_ms = (time.time() - start_time) * 1000
                telemetry.record_metric(
                    name="ai_response_time_ms",
                    value=total_duration_ms,
                    tags={"mode": mode, "success": "false", "method": "fallback"}
                )
            
            logger.error(f"Error in fallback chat: {e}")
            return f"❌ I'm having trouble connecting to the AI service right now. However, all the data tools are working fine. Please try again in a moment."