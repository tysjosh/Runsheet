"""
Logistics AI Agent with circuit breaker protection for Gemini API calls
and external session store integration for stateless operation.

Validates:
- Requirement 3.5: Implement circuit breakers for Gemini API
- Requirement 2.5: Return specific error code indicating AI service unavailability
- Requirement 5.4: Record custom metrics for AI response times
- Requirement 7.6: Orchestrator receives user requests and delegates to specialists
- Requirement 7.7: Orchestrator coordinates multiple specialist agents
- Requirement 8.2: Load conversation history from Session_Store using session identifier
- Requirement 8.3: Persist updated conversation history to Session_Store
- Requirement 8.6: Gracefully degrade when Session_Store is unavailable
"""

import asyncio
import os
import logging
import time
from typing import AsyncGenerator, Optional, Any
from datetime import datetime
from strands import Agent
# The model itself is built by Agents.model_provider.build_agent_model, which
# owns provider selection and credential resolution for every agent entry point.
from dotenv import load_dotenv
from .tools import ALL_TOOLS
from .tools._tenant_context import set_current_tenant
from .tools.scheduling_tools import JOB_STATUS_VALUES, JOB_TYPE_VALUES
from .tools.search_tools import FLEET_ASSET_STATUSES
from config.settings import get_settings
from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitOpenException
from errors.exceptions import ai_service_unavailable, circuit_open
from .llm_errors import (
    AI_SERVICE_UNAVAILABLE,
    MAX_ATTEMPTS,
    AgentServiceError,
    ChatEvent,
    call_with_llm_retry,
    classify_llm_exception,
    error_event,
    error_event_for,
    retry_delay,
    safe_message_for,
    status_event,
    to_agent_service_error,
)

# Load environment variables
load_dotenv()

# Disable OpenTelemetry to avoid context errors — controlled by env var
if os.environ.get('DISABLE_OTEL_IN_AGENT', 'true').lower() == 'true':
    os.environ.setdefault('OTEL_SDK_DISABLED', 'true')
    os.environ.setdefault('OTEL_PYTHON_DISABLED', 'true')
    os.environ.setdefault('OTEL_EXPORTER_OTLP_ENDPOINT', '')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Suppress OpenTelemetry warnings and errors
logging.getLogger('opentelemetry').setLevel(logging.CRITICAL)
logging.getLogger('opentelemetry.context').setLevel(logging.CRITICAL)

# Module-level orchestrator reference for multi-agent routing.
# When configured, chat requests are routed through the AgentOrchestrator
# which delegates to specialist agents. When None, the legacy direct
# Strands agent invocation is used as a fallback.
# Validates: Requirements 7.6, 7.7
_orchestrator = None


def _require_tenant_id(tenant_id: Optional[str]) -> str:
    if not tenant_id:
        raise ValueError(
            "tenant_id is required for AI agent execution; refusing to use a "
            "hardcoded/default tenant."
        )
    return tenant_id


def configure_orchestrator(orchestrator) -> None:
    """Wire the AgentOrchestrator for multi-agent request routing.

    Called during application lifespan startup after the orchestrator
    and all specialist agents have been initialised. Once configured,
    ``LogisticsAgent.chat_streaming`` will route requests through the
    orchestrator instead of invoking the Strands agent directly.

    Args:
        orchestrator: An ``AgentOrchestrator`` instance.

    Validates: Requirements 7.6, 7.7
    """
    global _orchestrator
    _orchestrator = orchestrator
    logger.info("✅ Orchestrator configured for multi-agent routing")


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

        # Model + credential resolution lives in one place (see
        # Agents/model_provider.py). This used to read GEMINI_API_KEY with an
        # empty-string default and hardcode a ``gemini/`` model id, so an unset
        # key produced a model that authenticated with nothing and failed on
        # every request rather than at startup.
        from Agents.model_provider import build_agent_model

        gemini_model = build_agent_model(self.settings)
        
        # Initialize Strands Agent with the Gemini model
        self.agent = Agent(
            model=gemini_model,
            system_prompt=f"""You are a Fuel Distribution Operations AI Assistant. You help dispatchers and operations managers run a fuel delivery business — managing orders, tracking drivers, optimizing routes, and monitoring tank levels.

            **YOU HAVE ACCESS TO LIVE DATA!** You can search and analyze real fleet, order, driver, and fuel data using your tools.

            **How you work:**
            - Answer questions using real data from your tools
            - ALWAYS announce your actions: "Let me search for [topic]..." BEFORE using tools
            - Use semantic search to find relevant information
            - Provide insights based on actual data
            - When asked for reports or analysis, use multiple tools systematically and present findings in structured markdown
            - Be conversational, helpful, and transparent about what you found

            **Fuel Order Domain:**
            The platform manages fuel delivery orders. Each order represents a customer requesting fuel delivered to their tank:
            - **order**: A fuel delivery request with a unique order_id (e.g. ord_abc123)
            - **customer**: The account receiving fuel, identified by customer_id and customer_name
            - **tank**: The customer's storage tank (customer_tank_id), with capacity and current level
            - **product_code**: The fuel product being delivered (DIESEL_2, GASOLINE_REG, KEROSENE, PROPANE, etc.)
            - **gallons_requested**: Volume of fuel to deliver (or fill_to_full for tank-top orders)
            - **delivery_window**: The time window (delivery_window_start / delivery_window_end) within which delivery must occur
            - **call_type**: How the order originated — will_call (customer called), auto_fill (forecaster triggered), keep_full (standing top-off), one_off (single delivery)
            - **intake_channel**: How the order entered the system — voice, web_portal, dispatcher, csv, edi, api_partner, legacy

            **Order Statuses:** placed → confirmed → scheduled → dispatched → in_transit → delivered (terminal) | failed (terminal) | cancelled (terminal) | on_hold (can return to placed)

            **Supported Asset Types:**
            The platform tracks multiple logistics asset types:
            - **vehicle**: truck, fuel_truck, personnel_vehicle
            - **vessel**: boat, barge
            - **equipment**: crane, forklift
            - **container**: cargo_container, ISO_tank

            **Available Tools:**
            - `search_fleet_data(query, asset_type=None, status=None)` - Search assets using semantic search. Accepts an optional `asset_type` parameter to filter by type (e.g. "vehicle", "vessel", "equipment", "container") and an optional `status` ({', '.join(FLEET_ASSET_STATUSES)}).
            - `search_orders(status, customer_id, driver_id, call_type, product_code, start_date, end_date, intake_channel)` - Search fuel orders by status, customer, driver, call type, product, date range, or intake channel
            - `search_drivers(status, availability, hazmat_endorsement)` - Search drivers by status, availability, and qualifications
            - `get_order_events(order_id)` - Get the full event timeline for a specific fuel order
            - `get_orders_metrics(metric_type, bucket, start_date, end_date, intake_channel)` - Get aggregated order metrics (orders, drivers, sla, failures)
            - `search_support_tickets(query)` - Search support tickets using semantic search
            - `search_inventory(query)` - Search inventory items using semantic search
            - `get_inventory_summary()` - Get all inventory items organized by status
            - `get_fleet_summary()` - Get current fleet status overview with per-type breakdowns
            - `get_analytics_overview()` - Get performance metrics and KPIs
            - `get_performance_insights()` - Get actionable performance insights
            - `find_truck_by_id(truck_id)` - Find any asset by ID or plate number (works for all asset types, not just trucks)
            - `get_all_locations()` - Get all depots, warehouses, and stations
            - `generate_operations_report()` - Generate comprehensive operations status report
            - `generate_performance_report()` - Generate detailed performance analysis report
            - `generate_incident_analysis(issue)` - Analyze incidents across multiple data sources

            **Legacy Ops Tools:**
            - `get_ops_metrics(metric_type, bucket, start_date, end_date, tenant_id)` - Get aggregated operational metrics
            - `generate_sla_report(start_date, end_date, tenant_id)` - Generate SLA violations report
            - `generate_failure_report(start_date, end_date, tenant_id, intake_channel=None)` - Generate failure root-cause analysis report. Filter by intake_channel (voice, web_portal, dispatcher, csv, edi, api_partner, legacy) to compare failure rates across channels.
            - `generate_driver_productivity_report(start_date, end_date, tenant_id)` - Generate driver productivity report

            **IMPORTANT - Read-Only Guardrail:**
            All ops intelligence tools are strictly read-only. You must NEVER modify order, driver, or event data.
            If you identify an action that should be taken (e.g., reassign a driver, cancel an order),
            present the suggestion to the user as a recommendation but do NOT execute it. The user must perform
            mutations through the dedicated UI action endpoints.

            **Fuel Monitoring Tools (read-only):**
            - `search_fuel_stations(query, fuel_type=None, status=None)` - Search fuel stations by name, type, location, or stock status. Filter by canonical fuel_type (DIESEL_2, GASOLINE_REG, KEROSENE, PROPANE, DEF) and status (normal, low, critical, empty).
            - `get_fuel_summary()` - Get network-wide fuel summary including total capacity, current stock, daily consumption, average days until empty, and station counts by status.
            - `get_fuel_consumption_history(station_id=None, asset_id=None, days=7)` - Get fuel consumption events for a specific station or asset over a date range.
            - `generate_fuel_report(days=7)` - Generate a comprehensive markdown fuel operations report covering stock levels, consumption trends, alert history, and refill recommendations.

            **IMPORTANT - Fuel Tools Read-Only Guardrail:**
            All fuel monitoring tools are strictly read-only. You must NEVER modify fuel stock levels or station configuration.
            If you identify a fuel action that should be taken (e.g., schedule a refill, update a threshold),
            present the suggestion to the user as a recommendation but do NOT execute it.

            **Scheduling & Dispatch Tools (read-only):**
            - `search_jobs(job_type=None, status=None, asset=None, origin=None, destination=None, start_date=None, end_date=None, tenant_id=None)` - Search logistics jobs by type, status, asset, location, or time range using the authenticated tenant context. Job types: {', '.join(JOB_TYPE_VALUES)}. Statuses: {', '.join(JOB_STATUS_VALUES)}.
            - `get_job_details(job_id, tenant_id=None)` - Get full details of a job including event history and cargo manifest using the authenticated tenant context.
            - `find_available_assets(asset_type=None, start_time_range=None, end_time_range=None, tenant_id=None)` - Find assets not assigned to active jobs within a time window using the authenticated tenant context. Filter by asset_type: vehicle, vessel, equipment, container.
            - `get_scheduling_summary(tenant_id=None)` - Get summary of active jobs, delayed jobs, available assets, and upcoming scheduled jobs using the authenticated tenant context.
            - `generate_dispatch_report(days=7, tenant_id=None, intake_channel=None)` - Generate a markdown dispatch report with completion rates, delay analysis, asset utilization, and recommendations using the authenticated tenant context. Filter by intake_channel (voice, web_portal, dispatcher, csv, edi, api_partner, legacy) to see channel-specific dispatch metrics.

            **IMPORTANT - Scheduling Tools Read-Only Guardrail:**
            All scheduling tools are strictly read-only. You must NEVER modify job data, assignments, or status.
            If you identify a scheduling action that should be taken (e.g., assign an asset, start a job, cancel a job),
            present the suggestion to the user as a recommendation but do NOT execute it. The user must perform
            mutations through the scheduling API or dashboard UI.

            **Your Expertise Areas:**
            - Fuel order management and delivery tracking
            - Driver dispatch and utilization
            - Route optimization and delivery planning
            - Tank monitoring and auto-fill forecasting
            - Fleet tracking and vehicle management
            - SLA compliance and delivery window management
            - Support ticket analysis
            - Fuel distribution performance analytics

            **Your Personality:**
            - Professional fuel distribution operations expert with access to live data
            - Always explain what you're searching for before using tools
            - Provide actionable insights based on real information
            - Clear communicator who builds trust through transparency

            **Example Interactions:**
            User: "Show me all pending orders"
            You: "Let me search for placed orders..." [calls search_orders(status="placed")]
            You: "I found [X] orders in placed status. Here's the breakdown: [results and analysis]"

            User: "What orders does customer CUST_123 have?"
            You: "Let me look up orders for that customer..." [calls search_orders(customer_id="CUST_123")]
            You: "I found [X] orders for customer CUST_123: [results with statuses and delivery windows]"

            User: "Show me available drivers with HAZMAT"
            You: "Let me search for available HAZMAT-endorsed drivers..." [calls search_drivers(status="active", hazmat_endorsement=true)]
            You: "I found [X] available HAZMAT drivers: [results with active order counts]"

            User: "What happened with order ord_abc123?"
            You: "Let me pull the event timeline for that order..." [calls get_order_events(order_id="ord_abc123")]
            You: "Here's the full history for order ord_abc123: [timeline from placed through delivery]"

            User: "How many orders came in via voice today?"
            You: "Let me check today's voice channel intake..." [calls get_orders_metrics(metric_type="orders", intake_channel="voice")]
            You: "Here's the voice channel intake summary: [counts by status and call type]"

            User: "Show me delayed trucks"
            You: "Let me search for delayed vehicles in our fleet..." [calls get_fleet_summary]
            You: "I found [X] delayed trucks. Here's the breakdown: [results and analysis]"

            User: "Find available trucks for tomorrow"
            You: "Let me check which trucks are available..." [calls find_available_assets(asset_type="vehicle")]
            You: "I found [X] available trucks: [results with locations]"

            User: "Generate a dispatch report for the last week"
            You: "Let me generate a comprehensive dispatch report..." [calls generate_dispatch_report(days=7)]
            You: "Here's the dispatch report: [completion rates, delays, asset utilization, recommendations]"

            Always announce your tool usage and explain the results clearly.""",
            tools=ALL_TOOLS,
            # The default PrintingCallbackHandler echoes every answer and
            # tool call to stdout (CloudWatch on staging, F13).
            callback_handler=None,
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
    
    @staticmethod
    def _session_key(tenant_id: Optional[str], session_id: Optional[str]) -> Optional[str]:
        """Session-store key for one conversation, scoped to its tenant.

        The store used to be keyed on the client-supplied ``session_id``
        alone, so tenant B sending tenant A's session id loaded A's history
        (staging finding F1). ``tenant_id`` comes from the verified
        ``TenantContext`` and leads the key, so a client-chosen session id
        cannot reach another tenant's entry. Returns ``None`` when either
        part is missing; callers then skip the store entirely.
        """
        if not tenant_id or not session_id:
            return None
        return f"{tenant_id}:{session_id}"

    async def _load_conversation_history(
        self, session_id: str, tenant_id: Optional[str] = None
    ) -> Optional[list]:
        """
        Load conversation history from the session store.
        
        Validates:
        - Requirement 8.2: WHEN a chat request is received, THE AI_Agent SHALL load
          conversation history from the Session_Store using a session identifier
        - Requirement 8.6: WHEN the Session_Store is unavailable, THE Backend_Service
          SHALL gracefully degrade by starting a new conversation rather than failing
        
        Args:
            session_id: Unique identifier for the conversation session.
            tenant_id: Verified tenant of the caller; scopes the store key.
            
        Returns:
            List of conversation messages if found, None otherwise.
        """
        key = self._session_key(tenant_id, session_id)
        if key is None:
            return None

        if not await self._ensure_session_store_connected():
            logger.debug(f"Session store unavailable, starting fresh conversation for session {session_id}")
            return None
        
        try:
            session_data = await self._session_store.get(key)
            if session_data and "messages" in session_data:
                logger.info(f"📥 Loaded {len(session_data['messages'])} messages for session {session_id}")
                return session_data["messages"]
            logger.debug(f"No existing conversation found for session {session_id}")
            return None
        except Exception as e:
            # Graceful degradation: log warning and continue with fresh conversation
            logger.warning(f"⚠️ Failed to load conversation history for session {session_id}: {e}")
            return None
    
    async def _save_conversation_history(
        self, session_id: str, messages: list, tenant_id: Optional[str] = None
    ) -> bool:
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
            tenant_id: Verified tenant of the caller; scopes the store key.
            
        Returns:
            True if saved successfully, False otherwise.
        """
        key = self._session_key(tenant_id, session_id)
        if key is None:
            return False

        if not await self._ensure_session_store_connected():
            logger.debug(f"Session store unavailable, conversation not persisted for session {session_id}")
            return False
        
        try:
            session_data = {
                "session_id": session_id,
                "tenant_id": tenant_id,
                "messages": messages,
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "message_count": len(messages)
            }
            await self._session_store.set(key, session_data)
            logger.info(f"📤 Saved {len(messages)} messages for session {session_id}")
            return True
        except Exception as e:
            # Graceful degradation: log warning but don't fail the request
            logger.warning(f"⚠️ Failed to save conversation history for session {session_id}: {e}")
            return False
    
    async def _clear_session(self, session_id: str, tenant_id: Optional[str] = None) -> bool:
        """
        Clear conversation history from the session store.
        
        Args:
            session_id: Unique identifier for the conversation session.
            tenant_id: Verified tenant of the caller; scopes the store key.
            
        Returns:
            True if cleared successfully, False otherwise.
        """
        key = self._session_key(tenant_id, session_id)
        if key is None:
            return False

        if not await self._ensure_session_store_connected():
            return False
        
        try:
            await self._session_store.delete(key)
            logger.info(f"🗑️ Cleared session {session_id}")
            return True
        except Exception as e:
            logger.warning(f"⚠️ Failed to clear session {session_id}: {e}")
            return False
    
    @staticmethod
    def _circuit_retry_after(exc: CircuitOpenException) -> Optional[int]:
        if exc.time_until_retry:
            return max(1, int(exc.time_until_retry.total_seconds()))
        return None

    def _handle_circuit_breaker_exception(
        self, exc: CircuitOpenException, request_id: Optional[str] = None
    ) -> ChatEvent:
        """
        Error event for an open circuit breaker.
        
        Validates:
        - Requirement 2.5: Return specific error code indicating AI service unavailability
        - Requirement 3.2: Return service unavailable response immediately when circuit is open
        
        Args:
            exc: The CircuitOpenException that was raised
            request_id: Correlates the event with server logs.
            
        Returns:
            ChatEvent: ``error`` event with ``AI_SERVICE_UNAVAILABLE`` and
            ``retry_after_seconds`` when known.
        """
        logger.warning(
            "AI circuit '%s' open (request_id=%s)", exc.circuit_name, request_id
        )
        retry_after = self._circuit_retry_after(exc)
        return error_event(
            AI_SERVICE_UNAVAILABLE,
            safe_message_for(AI_SERVICE_UNAVAILABLE, retry_after),
            request_id,
            retry_after,
        )
    
    def _handle_gemini_api_error(
        self, error: Exception, request_id: Optional[str] = None
    ) -> ChatEvent:
        """
        Error event for a failed LLM call. Full detail is logged server-side;
        the event carries only the code and a safe message (F3), never
        ``str(error)``.
        
        Validates:
        - Requirement 2.5: Return specific error code indicating AI service unavailability
        
        Args:
            error: The exception that was raised
            request_id: Correlates the event with server logs.
            
        Returns:
            ChatEvent: ``error`` event
        """
        logger.error("AI service error (request_id=%s)", request_id, exc_info=error)
        return error_event_for(to_agent_service_error(error), request_id)

    def setup_gemini_credentials(self):
        """Setup Gemini credentials. Skips Vertex AI setup when GEMINI_API_KEY is set."""
        # If using Gemini API key directly, skip Vertex AI credential setup
        if os.environ.get('GEMINI_API_KEY'):
            logger.info("✅ Using Gemini API key (skipping Vertex AI credentials)")
            return

        try:
            if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
                logger.info("✅ Using Cloud Run service account credentials")
                os.environ['GOOGLE_CLOUD_PROJECT'] = self.settings.google_cloud_project
                return
            
            credentials_path = self.settings.google_application_credentials
            
            if credentials_path and os.path.exists(credentials_path):
                os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = credentials_path
                os.environ['GOOGLE_CLOUD_PROJECT'] = self.settings.google_cloud_project
                logger.info(f"✅ Gemini credentials configured from: {credentials_path}")
            else:
                logger.warning("⚠️ No service account file found, using default credentials")
                os.environ['GOOGLE_CLOUD_PROJECT'] = self.settings.google_cloud_project
                
        except Exception:
            logger.exception("Failed to setup Gemini credentials")
            os.environ['GOOGLE_CLOUD_PROJECT'] = self.settings.google_cloud_project

    async def clear_memory(
        self, session_id: Optional[str] = None, tenant_id: Optional[str] = None
    ) -> bool:
        """
        Clear the agent's conversation memory.
        
        If a session_id is provided and session store is available,
        also clears the persisted session data for (tenant_id, session_id).
        The store delete is awaited: it used to be fire-and-forget via
        ``create_task``, so ``/api/chat/clear`` returned before anything was
        cleared and the next turn could still load the old history (F1).
        
        Args:
            session_id: Optional session identifier to clear from store.
            tenant_id: Verified tenant of the caller; scopes the store key.

        Returns:
            True if cleared (or nothing persisted to clear), False otherwise.
        """
        try:
            # Clear Strands agent's message history
            self.agent.messages = []
            logger.info("✅ Agent memory cleared")
            
            # If session_id provided, also clear from session store
            if session_id:
                return await self._clear_session(session_id, tenant_id)
            return True
        except Exception:
            logger.exception("Failed to clear agent memory")
            return False

    async def chat_streaming(
        self,
        message: str,
        mode: str = "chat",
        session_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Asynchronous streaming chat method with circuit breaker protection,
        retry logic, session persistence, tenant scoping, and orchestrator
        routing.

        When an ``AgentOrchestrator`` has been configured via
        ``configure_orchestrator``, requests are routed through
        ``route_stream`` and its normalized ``ChatEvent``s are yielded as
        they arrive (status, tool, text, error, done).

        When no orchestrator is available the method falls back to the
        legacy direct Strands agent invocation with full circuit breaker
        and retry support. That path yields raw Strands event dicts plus
        ``ChatEvent`` status/error events. The tenant ContextVar is bound for
        the duration of the streaming generator so every ES-reading tool runs
        tenant-scoped.

        Provider failures never reach the client as text: they are retried
        per ``Agents.llm_errors`` and reported as one ``error`` event with a
        safe message and ``request_id`` (F3).
        
        Validates:
        - Requirement 3.5: Implement circuit breakers for Gemini API
        - Requirement 2.5: Return specific error code indicating AI service unavailability
        - Requirement 3.4: Retry with exponential backoff
        - Requirement 5.4: Record custom metrics for AI response times
        - Requirement 7.6: Route requests through the orchestrator
        - Requirement 7.7: Coordinate multiple specialist agents
        - Requirement 8.2: Load conversation history from Session_Store
        - Requirement 8.3: Persist updated conversation history to Session_Store
        - Requirement 8.6: Gracefully degrade when Session_Store is unavailable
        - Requirements 9.2, 9.4: Enforce tenant scoping on every ES read

        Args:
            message: The user's message to process.
            mode: Chat mode - "chat" or "agent".
            session_id: Optional session identifier for conversation persistence.
            tenant_id: Optional tenant identifier for data scoping. When provided
                it is bound to the tool ContextVar so every ES-reading tool in
                the legacy fallback runs tenant-scoped. The orchestrator path
                passes tenant_id through its own API.
            request_id: The HTTP request id, echoed in error events so a user
                can quote it and operators can find the server-side log.
        """
        start_time = time.time()
        
        # Load conversation history from session store if session_id provided
        # Requirement 8.2: Load conversation history using session identifier
        if session_id:
            try:
                stored_messages = await self._load_conversation_history(session_id, tenant_id)
                if stored_messages:
                    # Restore conversation history to agent
                    self.agent.messages = stored_messages
                    logger.info(f"📥 Restored {len(stored_messages)} messages from session {session_id}")
            except Exception as e:
                # Graceful degradation: continue with fresh conversation
                logger.warning(f"⚠️ Could not restore session {session_id}: {e}")
        
        # ------------------------------------------------------------------
        # Orchestrator routing (Requirements 7.6, 7.7)
        # ``route_stream`` yields normalized ChatEvents incrementally. Fall
        # back to the legacy agent only when it fails unexpectedly before
        # yielding anything; an AgentServiceError is already a user-safe
        # outcome and falling back would just spend more model calls.
        # ------------------------------------------------------------------
        if _orchestrator is not None:
            yielded_any = False
            saw_error = False
            try:
                logger.info("🔀 Routing request through AgentOrchestrator")
                # Tenant id comes from the caller (injected by the /api/chat
                # handler from the authenticated ``TenantContext``).
                effective_tenant_id = _require_tenant_id(tenant_id)
                async for event in _orchestrator.route_stream(
                    user_message=message,
                    tenant_id=effective_tenant_id,
                    session_id=session_id,
                    request_id=request_id,
                ):
                    yielded_any = True
                    # A partial error (one specialist failed, the rest of
                    # the answer stands) is not a failed response (N4).
                    if event.get("type") == "error" and not event.get("partial"):
                        saw_error = True
                    yield event
            except Exception as e:
                if yielded_any or isinstance(e, AgentServiceError):
                    logger.error(
                        "Orchestrator stream failed (request_id=%s)",
                        request_id,
                        exc_info=e,
                    )
                    self._record_response_metric(
                        start_time, mode, success=False, method="orchestrator"
                    )
                    if not saw_error:
                        yield error_event_for(to_agent_service_error(e), request_id)
                    return
                logger.warning(
                    "⚠️ Orchestrator routing failed, falling back to direct agent "
                    "(request_id=%s)",
                    request_id,
                    exc_info=e,
                )
                self._record_response_metric(
                    start_time, mode, success=False, method="orchestrator"
                )
                # Fall through to direct agent invocation below
            else:
                # Record AI response time metrics (Requirement 5.4)
                self._record_response_metric(
                    start_time, mode, success=not saw_error, method="orchestrator"
                )
                return
        
        # ------------------------------------------------------------------
        # Direct agent invocation (legacy fallback)
        # Used when no orchestrator is configured or when orchestrator
        # routing fails. Bind the tenant ContextVar for the duration of
        # the streaming generator so any ES-reading tool the LLM invokes
        # is tenant-scoped.
        # ------------------------------------------------------------------

        # Check circuit breaker state before attempting
        if self._circuit_breaker.state.value == "open":
            if not self._circuit_breaker._should_attempt_reset():
                # Circuit is open and not ready to retry
                yield self._handle_circuit_breaker_exception(
                    CircuitOpenException(
                        self._circuit_breaker.name,
                        self._circuit_breaker._get_time_until_retry()
                    ),
                    request_id,
                )
                return
        
        attempt = 0
        while True:
            attempt += 1
            # Track if we got any response
            got_response = False
            first_token_time = None
            try:
                # Send message to agent (mode prefix removed — single unified mode)
                message_to_send = message
                
                # Wrap the streaming call with circuit breaker tracking and
                # with the tenant ContextVar bound so tools see the caller's
                # tenant.
                effective_tenant_id = _require_tenant_id(tenant_id)

                async def _stream_with_tracking():
                    nonlocal got_response, first_token_time
                    with set_current_tenant(effective_tenant_id):
                        async for event in self.agent.stream_async(message_to_send):
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
                    self._record_response_metric(start_time, mode, success=True)
                    telemetry = _get_telemetry_service()
                    if telemetry and first_token_time:
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
                            await self._save_conversation_history(session_id, self.agent.messages, tenant_id)
                        except Exception as e:
                            # Graceful degradation: log but don't fail the response
                            logger.warning(f"⚠️ Could not persist session {session_id}: {e}")
                
                return
                
            except CircuitOpenException as e:
                # Circuit breaker is open
                yield self._handle_circuit_breaker_exception(e, request_id)
                return
                
            except Exception as e:
                failure = classify_llm_exception(e)
                self._circuit_breaker._on_failure()
                logger.warning(
                    "Legacy chat failed (attempt %d/%d, code=%s, request_id=%s)",
                    attempt, MAX_ATTEMPTS, failure.code, request_id,
                    exc_info=e,
                )

                # Check if circuit is now open
                if self._circuit_breaker.state.value == "open":
                    yield self._handle_circuit_breaker_exception(
                        CircuitOpenException(
                            self._circuit_breaker.name,
                            self._circuit_breaker._get_time_until_retry()
                        ),
                        request_id,
                    )
                    return

                # Retrying after streamed output would repeat it.
                delay = None if got_response else retry_delay(failure, attempt)
                if delay is not None:
                    yield status_event("retrying", attempt=attempt + 1)
                    await asyncio.sleep(delay)
                    continue

                self._record_response_metric(
                    start_time, mode, success=False, error_type=failure.code
                )
                yield self._handle_gemini_api_error(e, request_id)
                return

    def _record_response_metric(
        self,
        start_time: float,
        mode: str,
        *,
        success: bool,
        method: Optional[str] = None,
        error_type: Optional[str] = None,
    ) -> None:
        """Record ``ai_response_time_ms`` (Requirement 5.4)."""
        telemetry = _get_telemetry_service()
        if not telemetry:
            return
        tags = {"mode": mode, "success": "true" if success else "false"}
        if method:
            tags["method"] = method
        if error_type:
            tags["error_type"] = error_type
        telemetry.record_metric(
            name="ai_response_time_ms",
            value=(time.time() - start_time) * 1000,
            tags=tags,
        )

    async def chat_fallback(
        self,
        message: str,
        mode: str = "chat",
        session_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> str:
        """
        Non-streaming fallback method with circuit breaker protection, session
        persistence, and tenant scoping.

        The LLM call gets the bounded retry from ``Agents.llm_errors``. A
        final failure, or an open circuit, raises ``AgentServiceError`` (safe
        message only) so the endpoint can answer 429/503 instead of a 200
        whose text is an error (F3).

        Validates:
        - Requirement 3.5: Implement circuit breakers for Gemini API
        - Requirement 2.5: Return specific error code indicating AI service unavailability
        - Requirement 5.4: Record custom metrics for AI response times
        - Requirement 8.2: Load conversation history from Session_Store
        - Requirement 8.3: Persist updated conversation history to Session_Store
        - Requirement 8.6: Gracefully degrade when Session_Store is unavailable
        - Requirements 9.2, 9.4: Enforce tenant scoping on every ES read
        
        Args:
            message: The user's message to process.
            mode: Chat mode - "chat" or "agent".
            session_id: Optional session identifier for conversation persistence.
            tenant_id: Optional tenant identifier for data scoping. Bound to the
                tool ContextVar for the duration of the run.

        Raises:
            AgentServiceError: the AI service failed after retries, or its
                circuit breaker is open.
        """
        start_time = time.time()
        
        # Load conversation history from session store if session_id provided
        # Requirement 8.2: Load conversation history using session identifier
        if session_id:
            try:
                stored_messages = await self._load_conversation_history(session_id, tenant_id)
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
                    raise CircuitOpenException(
                        self._circuit_breaker.name,
                        self._circuit_breaker._get_time_until_retry(),
                    )
            
            logger.info("🔄 Using non-streaming fallback mode")
            
            # Use non-streaming completion. Bind the tenant ContextVar for the
            # duration of the call so ES-reading tools are tenant-scoped.
            # NB: the Strands ``Agent`` exposes ``invoke_async`` (not
            # ``run_async``) for a single non-streaming turn; it returns an
            # ``AgentResult`` whose ``__str__`` yields the concatenated text.
            effective_tenant_id = _require_tenant_id(tenant_id)

            async def _invoke():
                with set_current_tenant(effective_tenant_id):
                    return await self.agent.invoke_async(message)

            agent_result = await call_with_llm_retry(
                _invoke, describe="Fallback chat"
            )
            response = str(agent_result)
            
            # Record success in circuit breaker
            self._circuit_breaker._on_success()
            
            # Record AI response time metrics (Requirement 5.4)
            self._record_response_metric(start_time, mode, success=True, method="fallback")
            
            # Persist updated conversation history to session store
            # Requirement 8.3: Persist updated conversation history
            if session_id:
                try:
                    await self._save_conversation_history(session_id, self.agent.messages, tenant_id)
                except Exception as e:
                    # Graceful degradation: log but don't fail the response
                    logger.warning(f"⚠️ Could not persist session {session_id}: {e}")
            
            return response
            
        except CircuitOpenException as e:
            logger.warning("AI circuit '%s' open on fallback chat", e.circuit_name)
            retry_after = self._circuit_retry_after(e)
            raise AgentServiceError(
                AI_SERVICE_UNAVAILABLE, retry_after_seconds=retry_after
            ) from e
            
        except AgentServiceError:
            # Already logged with full detail by call_with_llm_retry. One
            # failed request counts once toward the circuit, as before.
            self._circuit_breaker._on_failure()
            self._record_response_metric(start_time, mode, success=False, method="fallback")
            raise
