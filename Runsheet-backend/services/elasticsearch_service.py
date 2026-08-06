"""
Elasticsearch service for Runsheet Logistics Platform
Handles all Elasticsearch operations including index management and data operations

Validates:
- Requirement 3.5: Implement circuit breakers for Elasticsearch
- Requirement 2.4: Return specific error code indicating database unavailability
- Requirement 7.1: Implement index lifecycle management policies for data tiering
"""

import os
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from elasticsearch import Elasticsearch
from dotenv import load_dotenv
from config.settings import get_settings, Environment
from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitOpenException
from errors.codes import ErrorCode
from errors.exceptions import AppException, elasticsearch_unavailable, circuit_open
from services.time_utils import utcnow

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)


# Strict-mapped event-stream indices that carry their own domain timestamps
# (e.g. event_timestamp / ingested_at) and therefore MUST NOT receive the
# auto-stamped created_at/updated_at — doing so trips a
# strict_dynamic_mapping_exception. Every OTHER strict index written via
# index_document is auto-stamped, so its mapping must declare created_at and
# updated_at (enforced by tests/unit/test_mapping_timestamp_contract.py).
TIMESTAMP_SKIP_INDICES = frozenset(
    {"job_events", "shipment_events", "fuel_order_events"}
)


# Out-of-order protection for current-state documents, used by
# ``ElasticsearchService.upsert_if_newer``. Two byte-identical copies of this
# lived in ``fuel/order_repository.py`` and ``ops/services/ops_es_service.py``,
# each reaching past the facade to ``client.update``; they are here once so both
# call sites go through the facade and so the Postgres document store can answer
# the same call.
#
# ``isBefore || isEqual`` is deliberate: an event whose timestamp EQUALS the
# stored one is discarded. At-least-once delivery makes an equal timestamp the
# common case for a redelivery, and applying it would overwrite whatever a later
# event had already written.
_UPSERT_IF_NEWER_SCRIPT = """
    if (ctx._source.containsKey('last_event_timestamp') && ctx._source.last_event_timestamp != null) {
        ZonedDateTime existing = ZonedDateTime.parse(ctx._source.last_event_timestamp);
        ZonedDateTime incoming = ZonedDateTime.parse(params.last_event_timestamp);
        if (incoming.isBefore(existing) || incoming.isEqual(existing)) {
            ctx.op = 'noop';
            return;
        }
    }
    for (entry in params.entrySet()) {
        ctx._source[entry.getKey()] = entry.getValue();
    }
""".strip()


class ElasticsearchService:
    """
    Elasticsearch service with circuit breaker protection.
    
    All Elasticsearch operations are wrapped with a circuit breaker to prevent
    cascading failures when the database is unavailable.
    
    Validates:
    - Requirement 3.5: Implement circuit breakers for Elasticsearch
    - Requirement 2.4: Return specific error code indicating database unavailability
    """
    
    def __init__(self):
        self.client = None
        self.settings = get_settings()
        self._serverless: bool | None = None
        
        # Initialize separate circuit breakers for read and write operations
        # so that agent write failures don't block user read queries
        self._circuit_breaker = CircuitBreaker(
            name="elasticsearch_write",
            config=CircuitBreakerConfig(
                failure_threshold=10,
            )
        )
        self._read_circuit_breaker = CircuitBreaker(
            name="elasticsearch_read",
            config=CircuitBreakerConfig(
                failure_threshold=15,
            )
        )
        
        # Lazily constructed on first use so importing this module does not pull
        # in the persistence layer, and so a deployment on the legacy path never
        # touches it at all.
        self._document_store = None

        self.connect()

    # ------------------------------------------------------------------
    # Postgres document-store delegation (migration Phase 4)
    # ------------------------------------------------------------------
    #
    # The nine async document methods below check ``_pg_store()`` first. When the
    # ``document_store_backend`` setting is ``postgres`` they hand off to
    # :class:`persistence.document_store.PostgresDocumentStore`, which returns the
    # same response shapes — so none of the 684 Elasticsearch call sites change,
    # and a rollback is flipping one environment variable.
    #
    # The delegation lives here rather than in a wrapper class because ``client``
    # is also used directly in 20 files (index management, ILM, scripted upserts).
    # Those are a separate migration item; putting the switch inside the methods
    # keeps the object identity, the circuit breakers and the raw client all
    # exactly where they are while the document plane moves.

    def _pg_store(self):
        """The Postgres document store, or ``None`` when on the legacy path."""
        try:
            if not get_settings().document_store_is_postgres:
                return None
        except Exception:  # noqa: BLE001 — a settings hiccup must not break reads
            return None
        if self._document_store is None:
            from persistence.document_store import PostgresDocumentStore

            self._document_store = PostgresDocumentStore()
            logger.info(
                "Document operations are served from PostgreSQL "
                "(document_store_backend=postgres)"
            )
        return self._document_store

    def _is_retired_index(self, index: str) -> bool:
        """True when ``index`` has been retired (migrated to Postgres + dropped).

        Writes to a retired index (direct index/update/delete AND outbox-relay
        projection, since the relay calls ``index_document``) are skipped so a
        dropped index is not silently recreated with ES dynamic mappings.
        Read from current settings each call so the list can be flipped without
        restarting (and tests can monkeypatch it). Reversible: remove the index
        from ``retired_es_indices`` to resume projecting to it.
        """
        try:
            retired = get_settings().retired_es_indices
        except Exception:  # noqa: BLE001 — never let a settings hiccup block ES
            return False
        return index in (retired or [])

    def connect(self):
        """Initialize Elasticsearch connection"""
        # Skip actual connection in test environment - tests should mock ES
        if self.settings.environment == Environment.TEST:
            logger.info("⏭️  Skipping Elasticsearch connection in test environment")
            return
            
        try:
            api_key = self.settings.elastic_api_key.strip('"')
            endpoint = self.settings.elastic_endpoint.strip('"')
            
            if not api_key or not endpoint:
                raise ValueError("ELASTIC_API_KEY and ELASTIC_ENDPOINT must be set in configuration")
            
            self.client = Elasticsearch(
                endpoint,
                api_key=api_key,
                verify_certs=True,
                request_timeout=30
            )
            
            # Test connection
            if self.client.ping():
                logger.info("✅ Connected to Elasticsearch successfully")
                # Set up ILM policies before creating indices
                self.setup_ilm_policies()
                self.setup_indices()
                # Apply ILM policies to existing indices
                self.apply_ilm_policies_to_indices()
                # Validate index schemas match expected mappings
                self.validate_index_schemas()
            else:
                raise ConnectionError("Failed to ping Elasticsearch")
                
        except Exception:
            logger.exception("Failed to connect to Elasticsearch")
            raise
    
    def _check_ilm_available(self) -> bool:
        """
        Check if ILM (Index Lifecycle Management) is available on this Elasticsearch cluster.
        
        ILM requires specific license tiers (Basic+ for some features, Platinum for others).
        This method detects availability to avoid errors on clusters without ILM support.
        
        Returns:
            True if ILM is available, False otherwise
        """
        try:
            # Try to list ILM policies - this will fail if ILM is not available
            self.client.ilm.get_lifecycle()
            return True
        except Exception as e:
            error_str = str(e).lower()
            # Check for common indicators that ILM is not available
            if "no handler found" in error_str or "unknown setting" in error_str or "ilm" in error_str:
                logger.info("ℹ️ ILM (Index Lifecycle Management) is not available on this Elasticsearch cluster. "
                          "This is normal for serverless or basic tier deployments. Skipping ILM configuration.")
                return False
            # For other errors, assume ILM might be available but there's a different issue
            logger.debug(f"ILM availability check encountered error: {e}")
            return False

    @property
    def is_serverless(self) -> bool:
        """Detect whether the connected Elasticsearch cluster is running in serverless mode.

        The result is cached after the first call so subsequent checks are free.
        Detection works by attempting to read ILM policies — serverless clusters
        reject this with a 400 / "no handler found" error.
        """
        if self._serverless is None:
            self._serverless = not self._check_ilm_available()
        return self._serverless

    @staticmethod
    def strip_serverless_incompatible_settings(mapping: dict) -> dict:
        """Return a copy of *mapping* with shard/replica settings removed.

        Elastic Cloud Serverless does not allow ``number_of_shards`` or
        ``number_of_replicas`` in index creation requests.  Call this before
        ``indices.create`` when running against a serverless cluster.
        """
        import copy
        mapping = copy.deepcopy(mapping)
        settings = mapping.get("settings", {})
        settings.pop("number_of_shards", None)
        settings.pop("number_of_replicas", None)
        if not settings:
            mapping.pop("settings", None)
        return mapping

    def setup_ilm_policies(self):
        """
        Set up Index Lifecycle Management (ILM) policies for data tiering.
        
        Creates ILM policies that move old data to warm/cold tiers after 30 days.
        Gracefully skips if ILM is not available on the cluster.
        
        Validates:
        - Requirement 7.1: Implement index lifecycle management policies that move 
          old data to warm/cold tiers after 30 days
        """
        # Check if ILM is available before attempting to create policies
        if not self._check_ilm_available():
            self._ilm_available = False
            return
        
        self._ilm_available = True
        
        # Define ILM policies for different data types
        ilm_policies = {
            "runsheet-standard-policy": self._get_standard_ilm_policy(),
            "runsheet-analytics-policy": self._get_analytics_ilm_policy(),
            "runsheet-logs-policy": self._get_logs_ilm_policy(),
        }
        
        for policy_name, policy_body in ilm_policies.items():
            try:
                # Check if policy already exists
                try:
                    existing_policy = self.client.ilm.get_lifecycle(name=policy_name)
                    logger.info(f"📋 ILM policy already exists: {policy_name}")
                    # Update the policy if it exists
                    self.client.ilm.put_lifecycle(name=policy_name, body=policy_body)
                    logger.info(f"✅ Updated ILM policy: {policy_name}")
                except Exception:
                    # Policy doesn't exist, create it
                    self.client.ilm.put_lifecycle(name=policy_name, body=policy_body)
                    logger.info(f"✅ Created ILM policy: {policy_name}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to create/update ILM policy {policy_name}: {e}")
                # Continue with other policies even if one fails
    
    def _get_standard_ilm_policy(self) -> Dict[str, Any]:
        """
        Get the standard ILM policy for operational data (trucks, inventory, etc.).
        
        Policy phases:
        - Hot: Active data, optimized for indexing and search
        - Warm: Data older than 30 days, read-only, optimized for search
        - Cold: Data older than 90 days, minimal resources
        - Delete: Data older than 365 days (optional, can be disabled)
        
        Validates:
        - Requirement 7.1: Move old data to warm/cold tiers after 30 days
        
        Returns:
            Dict containing the ILM policy configuration
        """
        return {
            "policy": {
                "phases": {
                    "hot": {
                        "min_age": "0ms",
                        "actions": {
                            "rollover": {
                                "max_age": "30d",
                                "max_primary_shard_size": "50gb"
                            },
                            "set_priority": {
                                "priority": 100
                            }
                        }
                    },
                    "warm": {
                        "min_age": "30d",
                        "actions": {
                            "set_priority": {
                                "priority": 50
                            },
                            "shrink": {
                                "number_of_shards": 1
                            },
                            "forcemerge": {
                                "max_num_segments": 1
                            },
                            "readonly": {}
                        }
                    },
                    "cold": {
                        "min_age": "90d",
                        "actions": {
                            "set_priority": {
                                "priority": 0
                            },
                            "allocate": {
                                "number_of_replicas": 0
                            }
                        }
                    }
                }
            }
        }
    
    def _get_analytics_ilm_policy(self) -> Dict[str, Any]:
        """
        Get the ILM policy for analytics data.
        
        Analytics data has a longer retention period and different tiering strategy:
        - Hot: Active data for real-time analytics
        - Warm: Data older than 30 days, still queryable for historical analysis
        - Cold: Data older than 180 days, archived for compliance
        
        Validates:
        - Requirement 7.1: Move old data to warm/cold tiers after 30 days
        
        Returns:
            Dict containing the ILM policy configuration
        """
        return {
            "policy": {
                "phases": {
                    "hot": {
                        "min_age": "0ms",
                        "actions": {
                            "rollover": {
                                "max_age": "30d",
                                "max_primary_shard_size": "50gb"
                            },
                            "set_priority": {
                                "priority": 100
                            }
                        }
                    },
                    "warm": {
                        "min_age": "30d",
                        "actions": {
                            "set_priority": {
                                "priority": 50
                            },
                            "forcemerge": {
                                "max_num_segments": 1
                            },
                            "readonly": {}
                        }
                    },
                    "cold": {
                        "min_age": "180d",
                        "actions": {
                            "set_priority": {
                                "priority": 0
                            },
                            "allocate": {
                                "number_of_replicas": 0
                            }
                        }
                    }
                }
            }
        }
    
    def _get_logs_ilm_policy(self) -> Dict[str, Any]:
        """
        Get the ILM policy for log data.
        
        Log data has shorter retention and aggressive tiering:
        - Hot: Recent logs for active debugging
        - Warm: Logs older than 7 days
        - Cold: Logs older than 30 days
        - Delete: Logs older than 90 days
        
        Validates:
        - Requirement 7.1: Move old data to warm/cold tiers after 30 days
        
        Returns:
            Dict containing the ILM policy configuration
        """
        return {
            "policy": {
                "phases": {
                    "hot": {
                        "min_age": "0ms",
                        "actions": {
                            "rollover": {
                                "max_age": "7d",
                                "max_primary_shard_size": "30gb"
                            },
                            "set_priority": {
                                "priority": 100
                            }
                        }
                    },
                    "warm": {
                        "min_age": "7d",
                        "actions": {
                            "set_priority": {
                                "priority": 50
                            },
                            "shrink": {
                                "number_of_shards": 1
                            },
                            "forcemerge": {
                                "max_num_segments": 1
                            },
                            "readonly": {}
                        }
                    },
                    "cold": {
                        "min_age": "30d",
                        "actions": {
                            "set_priority": {
                                "priority": 0
                            },
                            "allocate": {
                                "number_of_replicas": 0
                            }
                        }
                    },
                    "delete": {
                        "min_age": "90d",
                        "actions": {
                            "delete": {}
                        }
                    }
                }
            }
        }
    
    def apply_ilm_policies_to_indices(self):
        """
        Apply ILM policies to existing indices.
        
        Maps indices to their appropriate ILM policies:
        - trucks, inventory, support_tickets, locations -> standard policy
        - analytics_events -> analytics policy
        
        Skips if ILM is not available on the cluster.
        
        Validates:
        - Requirement 7.1: Implement index lifecycle management policies
        """
        # Skip if ILM is not available
        if not getattr(self, '_ilm_available', False):
            logger.debug("Skipping ILM policy application - ILM not available on this cluster")
            return
        
        # Define index to policy mapping
        index_policy_mapping = {
            "trucks": "runsheet-standard-policy",
            "inventory": "runsheet-standard-policy",
            "support_tickets": "runsheet-standard-policy",
            "locations": "runsheet-standard-policy",
            "analytics_events": "runsheet-analytics-policy",
        }
        
        for index_name, policy_name in index_policy_mapping.items():
            try:
                # Check if index exists
                if self.client.indices.exists(index=index_name):
                    # Apply ILM policy to the index
                    self.client.indices.put_settings(
                        index=index_name,
                        body={
                            "index": {
                                "lifecycle": {
                                    "name": policy_name
                                }
                            }
                        }
                    )
                    logger.info(f"✅ Applied ILM policy '{policy_name}' to index '{index_name}'")
            except Exception as e:
                logger.warning(f"⚠️ Failed to apply ILM policy to {index_name}: {e}")
                # Continue with other indices even if one fails
    
    def get_ilm_policy_status(self, index_name: str) -> Optional[Dict[str, Any]]:
        """
        Get the ILM status for a specific index.
        
        Args:
            index_name: Name of the index to check
            
        Returns:
            Dict containing ILM status information, or None if not available
            
        Validates:
        - Requirement 7.1: Index lifecycle management policies
        """
        try:
            response = self.client.ilm.explain_lifecycle(index=index_name)
            if "indices" in response and index_name in response["indices"]:
                return response["indices"][index_name]
            return None
        except Exception as e:
            logger.warning(f"⚠️ Failed to get ILM status for {index_name}: {e}")
            return None
    
    def get_all_ilm_policies(self) -> Dict[str, Any]:
        """
        Get all ILM policies configured in the cluster.
        
        Returns:
            Dict containing all ILM policies
            
        Validates:
        - Requirement 7.1: Index lifecycle management policies
        """
        try:
            return self.client.ilm.get_lifecycle()
        except Exception as e:
            logger.warning(f"⚠️ Failed to get ILM policies: {e}")
            return {}
    
    def update_ilm_policy(self, policy_name: str, policy_body: Dict[str, Any]) -> bool:
        """
        Update an existing ILM policy.
        
        Args:
            policy_name: Name of the policy to update
            policy_body: New policy configuration
            
        Returns:
            True if update was successful, False otherwise
            
        Validates:
        - Requirement 7.1: Index lifecycle management policies
        """
        try:
            self.client.ilm.put_lifecycle(name=policy_name, body=policy_body)
            logger.info(f"✅ Updated ILM policy: {policy_name}")
            return True
        except Exception:
            logger.exception("Failed to update ILM policy %s", policy_name)
            return False
    
    def remove_ilm_policy_from_index(self, index_name: str) -> bool:
        """
        Remove ILM policy from an index.
        
        Args:
            index_name: Name of the index
            
        Returns:
            True if removal was successful, False otherwise
            
        Validates:
        - Requirement 7.1: Index lifecycle management policies
        """
        try:
            self.client.indices.put_settings(
                index=index_name,
                body={
                    "index": {
                        "lifecycle": {
                            "name": None
                        }
                    }
                }
            )
            logger.info(f"✅ Removed ILM policy from index: {index_name}")
            return True
        except Exception:
            logger.exception("Failed to remove ILM policy from %s", index_name)
            return False
    
    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Get the circuit breaker instance for external access."""
        return self._circuit_breaker
    
    def _handle_circuit_breaker_exception(self, exc: CircuitOpenException) -> None:
        """
        Handle a circuit breaker exception by raising an appropriate AppException.
        
        Validates:
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirement 3.2: Return service unavailable response immediately when circuit is open
        
        Args:
            exc: The CircuitOpenException that was raised
            
        Raises:
            AppException: With CIRCUIT_OPEN error code
        """
        time_until_retry = None
        if exc.time_until_retry:
            time_until_retry = int(exc.time_until_retry.total_seconds())
        
        raise circuit_open(
            message=f"Elasticsearch service temporarily unavailable. Circuit breaker '{exc.circuit_name}' is open.",
            details={
                "circuit_name": exc.circuit_name,
                "time_until_retry_seconds": time_until_retry,
                "service": "elasticsearch"
            }
        )
    
    def _handle_elasticsearch_error(self, operation: str, error: Exception) -> None:
        """
        Handle an Elasticsearch error by raising an appropriate AppException.
        
        Validates:
        - Requirement 2.4: Return specific error code indicating database unavailability
        
        Args:
            operation: The operation that failed (e.g., "search", "index")
            error: The exception that was raised
            
        Raises:
            AppException: With ELASTICSEARCH_UNAVAILABLE error code
        """
        logger.error("Elasticsearch %s failed: %s", operation, error)
        raise elasticsearch_unavailable(
            message=f"Database operation failed: {operation}",
            details={
                "operation": operation,
                "error": str(error)
            }
        )
    

    def setup_indices(self):
        """Create indices with proper mappings if they don't exist, and update mappings for existing indices."""
        indices = {
            "trucks": self._get_trucks_mapping(),
            "locations": self._get_locations_mapping(),
            "inventory": self._get_inventory_mapping(),
            "support_tickets": self._get_support_tickets_mapping(),
            "analytics_events": self._get_analytics_mapping(),
            "import_sessions": self._get_import_sessions_mapping(),
            "import_sessions_active": self._get_active_import_sessions_mapping(),
        }

        for index_name, mapping in indices.items():
            try:
                if not self.client.indices.exists(index=index_name):
                    self.client.indices.create(
                        index=index_name,
                        body=mapping
                    )
                    logger.info(f"✅ Created index: {index_name}")
                else:
                    logger.info(f"📋 Index already exists: {index_name}")
                    # Update mapping with any new fields (existing fields are unchanged)
                    self._update_index_mapping(index_name, mapping)
            except Exception:
                logger.exception("Failed to create index %s", index_name)

        # Create 'assets' alias pointing to 'trucks' index for multi-asset support
        try:
            alias_exists = self.client.indices.exists_alias(name="assets")
            if not alias_exists:
                self.client.indices.put_alias(index="trucks", name="assets")
                logger.info("✅ Created alias: assets → trucks")
            else:
                logger.info("📋 Alias already exists: assets → trucks")
        except Exception as e:
            logger.warning(f"⚠️ Failed to create 'assets' alias pointing to 'trucks': {e}")
    def _update_index_mapping(self, index_name: str, expected_mapping: Dict[str, Any]):
        """
        Update an existing index mapping with any new fields from the expected mapping.
        Elasticsearch allows adding new fields to an existing mapping via PUT mapping.
        Existing fields are not modified.
        """
        try:
            current_mapping = self.client.indices.get_mapping(index=index_name)
            current_props = (
                current_mapping.get(index_name, {})
                .get("mappings", {})
                .get("properties", {})
            )
            expected_props = expected_mapping.get("mappings", {}).get("properties", {})

            # Find fields that exist in expected but not in current
            missing_fields = {
                k: v for k, v in expected_props.items() if k not in current_props
            }

            # Find existing fields that need sub-field updates (e.g., adding .keyword)
            subfield_updates = {}
            for field_name, expected_def in expected_props.items():
                if field_name not in current_props:
                    continue
                expected_fields = expected_def.get("fields", {})
                current_fields = current_props[field_name].get("fields", {})
                if expected_fields and expected_fields != current_fields:
                    new_subfields = {
                        k: v for k, v in expected_fields.items() if k not in current_fields
                    }
                    if new_subfields:
                        subfield_updates[field_name] = {
                            "type": current_props[field_name].get("type", "text"),
                            "fields": new_subfields,
                        }

            updates = {**missing_fields, **subfield_updates}

            if updates:
                logger.info(
                    f"📝 Updating index '{index_name}' with {len(updates)} field update(s): "
                    f"{list(updates.keys())}"
                )
                self.client.indices.put_mapping(
                    index=index_name,
                    body={"properties": updates},
                )
                logger.info(f"✅ Updated mapping for index: {index_name}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to update mapping for index '{index_name}': {e}")

    def validate_index_schemas(self) -> Dict[str, Any]:
        """
        Validate that index mappings match expected schemas and log warnings for mismatches.
        
        This method compares the actual Elasticsearch index mappings against the expected
        schemas defined in the mapping methods. Any mismatches are logged as warnings.
        
        Validates:
        - Requirement 7.3: WHEN the Backend_Service starts, THE Elasticsearch_Client SHALL 
          verify index mappings match expected schemas and log warnings for mismatches
        
        Returns:
            Dict containing validation results with structure:
            {
                "valid": bool,
                "indices": {
                    "index_name": {
                        "valid": bool,
                        "mismatches": [list of mismatch descriptions]
                    }
                }
            }
        """
        logger.info("🔍 Validating index schemas...")
        
        # Get expected mappings for all indices
        expected_mappings = {
            "trucks": self._get_trucks_mapping(),
            "locations": self._get_locations_mapping(),
            "inventory": self._get_inventory_mapping(),
            "support_tickets": self._get_support_tickets_mapping(),
            "analytics_events": self._get_analytics_mapping()
        }
        
        validation_results = {
            "valid": True,
            "indices": {}
        }
        
        for index_name, expected_mapping in expected_mappings.items():
            index_result = self._validate_single_index_schema(index_name, expected_mapping)
            validation_results["indices"][index_name] = index_result
            
            if not index_result["valid"]:
                validation_results["valid"] = False
        
        # Log summary
        if validation_results["valid"]:
            logger.info("✅ All index schemas validated successfully")
        else:
            invalid_indices = [
                name for name, result in validation_results["indices"].items() 
                if not result["valid"]
            ]
            logger.warning(f"⚠️ Schema validation completed with mismatches in indices: {invalid_indices}")
        
        return validation_results
    
    def _validate_single_index_schema(self, index_name: str, expected_mapping: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate a single index's mapping against expected schema.
        
        Args:
            index_name: Name of the index to validate
            expected_mapping: Expected mapping configuration
            
        Returns:
            Dict with validation result:
            {
                "valid": bool,
                "mismatches": [list of mismatch descriptions],
                "missing_fields": [list of missing field names],
                "type_mismatches": [list of type mismatch descriptions],
                "extra_fields": [list of unexpected field names]
            }
            
        Validates:
        - Requirement 7.3: Verify index mappings match expected schemas
        """
        result = {
            "valid": True,
            "mismatches": [],
            "missing_fields": [],
            "type_mismatches": [],
            "extra_fields": []
        }
        
        try:
            # Check if index exists
            if not self.client.indices.exists(index=index_name):
                result["valid"] = False
                result["mismatches"].append(f"Index '{index_name}' does not exist")
                logger.warning(f"⚠️ Schema validation: Index '{index_name}' does not exist")
                return result
            
            # Get actual mapping from Elasticsearch
            actual_mapping_response = self.client.indices.get_mapping(index=index_name)
            actual_mapping = actual_mapping_response.get(index_name, {}).get("mappings", {})
            
            # Get expected properties
            expected_properties = expected_mapping.get("mappings", {}).get("properties", {})
            actual_properties = actual_mapping.get("properties", {})
            
            # Compare properties
            self._compare_properties(
                expected_properties, 
                actual_properties, 
                result, 
                index_name,
                path=""
            )
            
            # tenant_id is not just another field: every read is scoped with
            # ``{"term": {"tenant_id": ...}}`` (inject_tenant_filter), and a term
            # query against analyzed text matches only when a produced token
            # equals the whole term. A tenant id containing a hyphen is split by
            # the standard analyzer, so an index that inferred ``text`` here
            # serves ZERO rows to every tenant while looking perfectly healthy.
            # Say so at ERROR, separately from the generic type-mismatch list,
            # because the fix is a reindex rather than a mapping update.
            actual_tenant = actual_properties.get("tenant_id")
            if actual_tenant is not None and actual_tenant.get("type") != "keyword":
                result["valid"] = False
                detail = (
                    f"tenant_id is mapped as '{actual_tenant.get('type')}', not "
                    f"'keyword' — every tenant-scoped term filter on "
                    f"'{index_name}' will match nothing. The index needs a "
                    f"reindex; a put-mapping cannot change a field's type."
                )
                result["mismatches"].append(detail)
                logger.error(f"❌ Schema validation [{index_name}]: {detail}")

            # Log warnings for any mismatches
            if result["missing_fields"]:
                logger.warning(
                    f"⚠️ Schema validation [{index_name}]: Missing fields: {result['missing_fields']}"
                )
            
            if result["type_mismatches"]:
                for mismatch in result["type_mismatches"]:
                    logger.warning(f"⚠️ Schema validation [{index_name}]: {mismatch}")
            
            if result["extra_fields"]:
                logger.info(
                    f"ℹ️ Schema validation [{index_name}]: Extra fields in actual mapping "
                    f"(may be auto-generated): {result['extra_fields']}"
                )
            
            if result["valid"]:
                logger.info(f"✅ Schema validation [{index_name}]: Mapping matches expected schema")
            
        except Exception as e:
            result["valid"] = False
            result["mismatches"].append(f"Failed to validate index '{index_name}': {str(e)}")
            logger.exception("Schema validation [%s]: failed to validate", index_name)
        
        return result
    
    def _compare_properties(
        self, 
        expected: Dict[str, Any], 
        actual: Dict[str, Any], 
        result: Dict[str, Any],
        index_name: str,
        path: str = ""
    ) -> None:
        """
        Recursively compare expected and actual property mappings.
        
        Args:
            expected: Expected properties mapping
            actual: Actual properties mapping from Elasticsearch
            result: Result dict to update with mismatches
            index_name: Name of the index being validated
            path: Current path in the property hierarchy (for nested fields)
            
        Validates:
        - Requirement 7.3: Verify index mappings match expected schemas
        """
        # Check for missing fields in actual mapping
        for field_name, expected_config in expected.items():
            full_path = f"{path}.{field_name}" if path else field_name
            
            if field_name not in actual:
                result["valid"] = False
                result["missing_fields"].append(full_path)
                result["mismatches"].append(f"Missing field: {full_path}")
                continue
            
            actual_config = actual[field_name]
            
            # Compare field types
            expected_type = expected_config.get("type")
            actual_type = actual_config.get("type")
            
            # Handle nested properties (objects without explicit type)
            if "properties" in expected_config:
                # This is an object type with nested properties
                if "properties" not in actual_config:
                    result["valid"] = False
                    result["type_mismatches"].append(
                        f"Field '{full_path}': Expected object with properties, "
                        f"but actual has no nested properties"
                    )
                    result["mismatches"].append(
                        f"Type mismatch at '{full_path}': expected object, got {actual_type}"
                    )
                else:
                    # Recursively compare nested properties
                    self._compare_properties(
                        expected_config["properties"],
                        actual_config.get("properties", {}),
                        result,
                        index_name,
                        full_path
                    )
            elif expected_type:
                # Compare explicit types
                if actual_type and expected_type != actual_type:
                    # Some type variations are acceptable (e.g., semantic_text might be stored differently)
                    if not self._is_compatible_type(expected_type, actual_type):
                        result["valid"] = False
                        result["type_mismatches"].append(
                            f"Field '{full_path}': Expected type '{expected_type}', "
                            f"but actual type is '{actual_type}'"
                        )
                        result["mismatches"].append(
                            f"Type mismatch at '{full_path}': expected {expected_type}, got {actual_type}"
                        )
        
        # Check for extra fields in actual mapping (informational, not a validation failure)
        for field_name in actual:
            full_path = f"{path}.{field_name}" if path else field_name
            if field_name not in expected:
                result["extra_fields"].append(full_path)
    
    def _is_compatible_type(self, expected_type: str, actual_type: str) -> bool:
        """
        Check if two Elasticsearch field types are compatible.
        
        Some type variations are acceptable due to Elasticsearch's type inference
        or plugin-specific types.
        
        Args:
            expected_type: The expected field type
            actual_type: The actual field type from Elasticsearch
            
        Returns:
            True if types are compatible, False otherwise
            
        Validates:
        - Requirement 7.3: Verify index mappings match expected schemas
        """
        # Define compatible type pairs
        compatible_types = {
            # ``("semantic_text", "text")`` used to be declared compatible here.
            # No mapping declares ``semantic_text`` any more (see the note above
            # _get_locations_mapping), so the pair is unreachable — but it was
            # also actively harmful while it lasted: it made this validator
            # report "compatible" for exactly the indices whose declared mapping
            # had been rejected and replaced by a dynamic one, which is how the
            # dead tenant filters went unreported at every startup.
            # long and integer are often interchangeable
            ("integer", "long"): True,
            ("long", "integer"): True,
            # float and double are often interchangeable
            ("float", "double"): True,
            ("double", "float"): True,
        }
        
        return compatible_types.get((expected_type, actual_type), False)
    
    def get_index_mapping(self, index_name: str) -> Optional[Dict[str, Any]]:
        """
        Get the current mapping for a specific index.
        
        Args:
            index_name: Name of the index
            
        Returns:
            Dict containing the index mapping, or None if index doesn't exist
            
        Validates:
        - Requirement 7.3: Verify index mappings match expected schemas
        """
        try:
            if not self.client.indices.exists(index=index_name):
                return None
            
            response = self.client.indices.get_mapping(index=index_name)
            return response.get(index_name, {}).get("mappings", {})
        except Exception:
            logger.exception("Failed to get mapping for index %s", index_name)
            return None
    
    def get_schema_validation_summary(self) -> Dict[str, Any]:
        """
        Get a summary of schema validation status for all indices.
        
        Returns:
            Dict containing validation summary with counts and details
            
        Validates:
        - Requirement 7.3: Verify index mappings match expected schemas
        """
        validation_results = self.validate_index_schemas()
        
        total_indices = len(validation_results["indices"])
        valid_indices = sum(
            1 for result in validation_results["indices"].values() 
            if result["valid"]
        )
        invalid_indices = total_indices - valid_indices
        
        total_mismatches = sum(
            len(result["mismatches"]) 
            for result in validation_results["indices"].values()
        )
        
        return {
            "overall_valid": validation_results["valid"],
            "total_indices": total_indices,
            "valid_indices": valid_indices,
            "invalid_indices": invalid_indices,
            "total_mismatches": total_mismatches,
            "details": validation_results["indices"]
        }
    
    def _get_trucks_mapping(self):
        """Get mapping for trucks index"""
        return {
            "mappings": {
                "dynamic": False,
                "properties": {
                    "truck_id": {"type": "keyword"},
                    "plate_number": {"type": "keyword"},
                    "driver_id": {"type": "keyword"},
                    "driver_name": {"type": "text"},
                    "current_location": {
                        "properties": {
                            "id": {"type": "keyword"},
                            "name": {"type": "text"},
                            "type": {"type": "keyword"},
                            "coordinates": {"type": "geo_point"},
                            "address": {"type": "text"}
                        }
                    },
                    "destination": {
                        "properties": {
                            "id": {"type": "keyword"},
                            "name": {"type": "text"},
                            "type": {"type": "keyword"},
                            "coordinates": {"type": "geo_point"},
                            "address": {"type": "text"}
                        }
                    },
                    "route": {
                        "properties": {
                            "id": {"type": "keyword"},
                            "distance": {"type": "float"},
                            "estimated_duration": {"type": "integer"},
                            "actual_duration": {"type": "integer"}
                        }
                    },
                    "status": {"type": "keyword"},
                    # Operational state, distinct from the movement ``status``
                    # above: ``out_of_service`` is written by
                    # Inspection_Service when a driver reports a defect of that
                    # severity (driver-mobile-app R8.5), and tracking updates
                    # that move ``status`` must not clear it.
                    "operational_state": {"type": "keyword"},
                    "estimated_arrival": {"type": "date"},
                    "last_update": {"type": "date"},
                    "cargo": {
                        "properties": {
                            "type": {"type": "keyword"},
                            "weight": {"type": "float"},
                            "volume": {"type": "float"},
                            # ``semantic_text`` until it was found to be
                            # load-bearing in the worst way — see the note above
                            # _get_locations_mapping. The only consumer,
                            # ``semantic_search``, issues a plain multi_match,
                            # which behaves identically on ``text``.
                            "description": {
                                "type": "text",
                                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                            },
                            "priority": {"type": "keyword"}
                        }
                    },
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    # Core asset classification
                    "asset_type": {"type": "keyword"},
                    "asset_subtype": {"type": "keyword"},
                    "asset_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    # Vessel-specific fields
                    "vessel_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "imo_number": {"type": "keyword"},
                    "port_of_registry": {"type": "keyword"},
                    "draft_meters": {"type": "float"},
                    "vessel_capacity_tonnes": {"type": "float"},
                    # Equipment-specific fields
                    "equipment_model": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "lifting_capacity_tonnes": {"type": "float"},
                    "operational_radius_meters": {"type": "float"},
                    # Container-specific fields
                    "container_number": {"type": "keyword"},
                    "container_size": {"type": "keyword"},
                    "seal_number": {"type": "keyword"},
                    "contents_description": {"type": "text"},
                    "weight_tonnes": {"type": "float"},
                    # Fuel monitoring fields
                    "fuel_level_pct": {"type": "float"},
                    # Depot assignment (cross-module-entity-linkage Req 10.1/10.2).
                    # Nullable/additive: an asset's home/operating depot. Used to
                    # resolve the asset → depot reference and to enumerate the
                    # assets assigned to a depot.
                    "assigned_depot_id": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                }
            }
        }
    
    # ------------------------------------------------------------------
    # Why these four mappings no longer declare ``semantic_text``
    # ------------------------------------------------------------------
    #
    # ``semantic_text`` needs Elasticsearch 8.15+ AND an inference endpoint.
    # Nothing in this codebase ever creates one, and the sole consumer of these
    # fields — :meth:`semantic_search` — issues a plain ``multi_match``, which is
    # lexical and behaves identically on ``text``. So the type bought nothing
    # even where it is supported.
    #
    # What it cost was severe and silent. On a cluster without the type,
    # ``indices.create`` fails with ``No handler for type [semantic_text]``;
    # :meth:`setup_indices` logs that and moves on, so the first write creates
    # the index by DYNAMIC mapping instead. Dynamic mapping infers analyzed
    # ``text`` for every string — including ``tenant_id`` — and
    # ``inject_tenant_filter`` scopes every read with
    # ``{"term": {"tenant_id": ...}}``. A term query against analyzed text
    # matches only if a produced token equals the whole term, and the standard
    # analyzer splits ``demo-tenant`` into ``demo`` + ``tenant``. So every
    # tenant-scoped read returned zero rows from a populated index: ``trucks``
    # held 10 documents and matched 0, ``locations`` 4 and matched 0, and
    # ``support_tickets`` was never created at all.
    #
    # Existing indices keep the mapping they were created with, so a cluster
    # that already went down this path needs a reindex — a put-mapping cannot
    # change a field's type. See ``.kiro/runbooks`` for the repair.

    def _get_locations_mapping(self):
        """Get mapping for locations index"""
        return {
            "mappings": {
                "properties": {
                    "location_id": {"type": "keyword"},
                    "name": {"type": "text"},
                    "type": {"type": "keyword"},
                    "coordinates": {"type": "geo_point"},
                    "address": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                    },
                    "region": {"type": "keyword"},
                    # Declared so it is never inferred as analyzed text: reads go
                    # through inject_tenant_filter, which uses a term query.
                    "tenant_id": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"}
                }
            }
        }
    
    def _get_inventory_mapping(self):
        """Get mapping for inventory index"""
        return {
            "mappings": {
                "properties": {
                    "item_id": {"type": "keyword"},
                    "name": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                    },
                    "category": {"type": "keyword"},
                    "quantity": {"type": "integer"},
                    "unit": {"type": "keyword"},
                    "location": {"type": "text"},
                    "status": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "last_updated": {"type": "date"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"}
                }
            }
        }
    
    def _get_support_tickets_mapping(self):
        """Get mapping for support tickets index"""
        return {
            "mappings": {
                "properties": {
                    "ticket_id": {"type": "keyword"},
                    "customer": {"type": "text"},
                    "customer_id": {"type": "keyword"},
                    "issue": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                    },
                    "description": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                    },
                    "priority": {"type": "keyword"},
                    "status": {"type": "keyword"},
                    "assigned_to": {"type": "keyword"},
                    "related_order": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "resolved_at": {"type": "date"}
                }
            }
        }
    
    def _get_analytics_mapping(self):
        """Get mapping for analytics events index"""
        return {
            "mappings": {
                "properties": {
                    "event_id": {"type": "keyword"},
                    "event_type": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "timestamp": {"type": "date"},
                    "truck_id": {"type": "keyword"},
                    "order_id": {"type": "keyword"},
                    "region": {"type": "keyword"},
                    "route_name": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}}
                    },
                    "route_id": {"type": "keyword"},
                    "delay_cause": {"type": "keyword"},
                    "metrics": {
                        "properties": {
                            # Performance metrics
                            "delivery_performance_pct": {"type": "float"},
                            "average_delay_minutes": {"type": "float"},
                            "fleet_utilization_pct": {"type": "float"},
                            "customer_satisfaction": {"type": "float"},
                            "on_time_percentage": {"type": "float"},
                            
                            # Delivery metrics
                            "delivery_time_minutes": {"type": "integer"},
                            "delay_minutes": {"type": "integer"},
                            "distance_km": {"type": "float"},
                            "fuel_consumed_liters": {"type": "float"},
                            "customer_rating": {"type": "float"},
                            
                            # Count metrics
                            "total_deliveries": {"type": "integer"},
                            "on_time_deliveries": {"type": "integer"},
                            "active_trucks": {"type": "integer"},
                            "completed_trips": {"type": "integer"},
                            "delay_incidents": {"type": "integer"},
                            "incident_count": {"type": "integer"},
                            
                            # Performance analysis
                            "performance_pct": {"type": "float"},
                            "avg_delivery_time": {"type": "float"},
                            "percentage": {"type": "float"},
                            "avg_delay_minutes": {"type": "float"},
                            
                            # Planning metrics
                            "planned_distance_km": {"type": "float"},
                            "estimated_duration_minutes": {"type": "integer"},
                            "expected_delay_duration": {"type": "integer"}
                        }
                    },
                    "created_at": {"type": "date"}
                }
            }
        }

    def _get_import_sessions_mapping(self):
        """Get mapping for import sessions index"""
        return {
            "mappings": {
                "properties": {
                    "session_id": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "data_type": {"type": "keyword"},
                    "source_type": {"type": "keyword"},
                    "source_name": {
                        "type": "text",
                        "fields": {
                            "keyword": {"type": "keyword"}
                        }
                    },
                    "total_records": {"type": "integer"},
                    "imported_records": {"type": "integer"},
                    "skipped_records": {"type": "integer"},
                    "error_count": {"type": "integer"},
                    "status": {"type": "keyword"},
                    "errors": {"type": "text"},
                    "field_mapping": {"type": "object", "enabled": False},
                    "created_at": {"type": "date"},
                    "completed_at": {"type": "date"},
                    "duration_seconds": {"type": "float"}
                }
            }
        }

    def _get_active_import_sessions_mapping(self):
        """Get mapping for durable in-progress import sessions."""
        return {
            "mappings": {
                "properties": {
                    "session_id": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "data_type": {"type": "keyword"},
                    "source_type": {"type": "keyword"},
                    "source_name": {"type": "text"},
                    "rows_json": {"type": "text", "index": False},
                    "columns": {"type": "keyword"},
                    "sample_rows": {"type": "object", "enabled": False},
                    "total_rows": {"type": "integer"},
                    "suggested_mapping": {"type": "object", "enabled": False},
                    "field_mapping": {"type": "object", "enabled": False},
                    "validation_result": {"type": "object", "enabled": False},
                    "status": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                }
            }
        }

    # CRUD Operations
    async def index_document(self, index: str, doc_id: str, document: Dict[Any, Any]):
        """
        Index a single document with circuit breaker protection.
        
        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        """
        if self._is_retired_index(index):
            return {"result": "skipped_retired_index"}
        store = self._pg_store()
        if store is not None:
            return await store.index_document(index, doc_id, document)
        try:
            async def _do_index():
                # Only inject timestamps for indices that have these fields in
                # their mapping. Strict-mapped event-stream indices carry their
                # own domain timestamps and reject the auto-stamped fields, so
                # they are excluded via the module-level TIMESTAMP_SKIP_INDICES.
                if index not in TIMESTAMP_SKIP_INDICES:
                    document["updated_at"] = utcnow().isoformat()
                    if "created_at" not in document:
                        document["created_at"] = utcnow().isoformat()
                
                response = self.client.index(
                    index=index,
                    id=doc_id,
                    body=document,
                    refresh=False
                )
                return response
            
            return await self._circuit_breaker.execute(_do_index)
        except CircuitOpenException as e:
            self._handle_circuit_breaker_exception(e)
        except Exception as e:
            self._handle_elasticsearch_error(f"index_document({index})", e)

    async def update_document(self, index: str, doc_id: str, partial_doc: Dict[Any, Any]):
        """
        Partially update a document using the ES _update API with circuit breaker protection.

        Only the fields present in *partial_doc* are merged into the existing document.
        """
        if self._is_retired_index(index):
            return {"result": "skipped_retired_index"}
        store = self._pg_store()
        if store is not None:
            from persistence.document_store import DocumentNotFound

            try:
                return await store.update_document(index, doc_id, partial_doc)
            except DocumentNotFound as exc:
                # The ES client raises NotFoundError here, which callers already
                # handle; re-raise through the same error path so behaviour on
                # both backends is identical.
                self._handle_elasticsearch_error(
                    f"update_document({index}, {doc_id})", exc
                )
        try:
            async def _do_update():
                partial_doc["updated_at"] = utcnow().isoformat()
                response = self.client.update(
                    index=index,
                    id=doc_id,
                    body={"doc": partial_doc},
                    refresh=True,
                )
                return response

            return await self._circuit_breaker.execute(_do_update)
        except CircuitOpenException as e:
            self._handle_circuit_breaker_exception(e)
        except Exception as e:
            self._handle_elasticsearch_error(f"update_document({index}, {doc_id})", e)

    
    async def upsert_if_newer(
        self,
        index: str,
        doc_id: str,
        document: Dict[str, Any],
        *,
        timestamp_field: str = "last_event_timestamp",
    ) -> bool:
        """Upsert unless the stored ``timestamp_field`` is newer or equal.

        Out-of-order protection for current-state documents: at-least-once
        delivery means an event can arrive after a later one has already been
        applied, and a plain last-write-wins upsert would move the document
        backwards.

        Two identical copies of a painless ``scripted_upsert`` used to implement
        this — one in ``fuel/order_repository.py``, one in
        ``ops/services/ops_es_service.py`` — each reaching past this facade to
        ``client.update``. They are the same script character for character, so
        they belong here once, and putting them here is what lets the Postgres
        document store answer the same call: it does the comparison under a
        ``SELECT … FOR UPDATE`` instead, which cannot lose a concurrent write and
        needs no retry loop.

        Returns ``True`` when the document was written, ``False`` when the event
        was discarded as stale.
        """
        if self._is_retired_index(index):
            return False

        store = self._pg_store()
        if store is not None:
            return await store.upsert_if_newer(
                index, doc_id, document, timestamp_field=timestamp_field
            )

        try:
            async def _do_upsert():
                response = self.client.update(
                    index=index,
                    id=doc_id,
                    body={
                        "scripted_upsert": True,
                        "script": {
                            "source": _UPSERT_IF_NEWER_SCRIPT,
                            "lang": "painless",
                            "params": document,
                        },
                        "upsert": document,
                    },
                    refresh=True,
                )
                if response.get("result", "") != "noop":
                    return True

                # A "noop" has two causes and they need opposite handling:
                #
                #   (a) a genuine stale-event discard — the document exists and
                #       the incoming timestamp is older-or-equal;
                #   (b) a serverless-Elasticsearch quirk where ``scripted_upsert``
                #       reports "noop" AND fails to materialise the ``upsert``
                #       body on a FRESH insert.
                #
                # Case (b) silently dropped every new order, and since reads are
                # served from Postgres the dispatcher got a 404 immediately after
                # a 201. The document's absence distinguishes them.
                #
                # Neither case exists on the Postgres path: there is no split
                # between "run the script" and "apply the upsert".
                if self.client.exists(index=index, id=doc_id):
                    logger.info(
                        "upsert_if_newer(%s): discarded stale event for %s "
                        "(incoming %s=%s)",
                        index, doc_id, timestamp_field,
                        document.get(timestamp_field),
                    )
                    return False
                self.client.index(index=index, id=doc_id, body=document, refresh=True)
                logger.info(
                    "upsert_if_newer(%s): scripted_upsert no-op'd a fresh insert "
                    "for %s; indexed directly (serverless-ES fallback)",
                    index, doc_id,
                )
                return True

            return await self._circuit_breaker.execute(_do_upsert)
        except CircuitOpenException as e:
            self._handle_circuit_breaker_exception(e)
        except Exception as e:
            self._handle_elasticsearch_error(f"upsert_if_newer({index}, {doc_id})", e)

    async def atomic_update(
        self,
        index: str,
        doc_id: str,
        transform,
        *,
        upsert: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.01,
    ):
        """Read-modify-write one document, safely under concurrency.

        One call replacing two hand-rolled patterns that each appeared in several
        places and each reached past this facade to the raw client:

        * ``if_seq_no`` / ``if_primary_term`` optimistic concurrency with a
          retry-on-409 loop (``fuel/compartment_state_models.py``,
          ``Agents/approval_queue_service.py``);
        * painless scripts that increment counters
          (``fuel/driver_repository.py``).

        ``transform`` is called with a copy of the stored document and returns the
        new document, or ``None`` to leave it unchanged. ``None`` is the direct
        equivalent of painless ``ctx.op = 'noop'``.

        Returns ``(document, applied)``.

        The two backends reach the same guarantee by different means, and the
        difference is worth stating: Elasticsearch retries a compare-and-set and
        can eventually give up, so ``max_retries`` and the backoff exist and
        :class:`AppException` surfaces persistent contention. Postgres takes a row
        lock, so a concurrent writer waits instead of colliding — nothing is lost
        and nothing has to retry. Verified: with the lock removed, ten concurrent
        increments produce three.
        """
        store = self._pg_store()
        if store is not None:
            return await store.atomic_update(
                index, doc_id, transform, upsert=upsert
            )

        import asyncio
        import random

        for attempt in range(max(max_retries, 1)):
            try:
                current = self.client.get(index=index, id=doc_id)
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "status_code", None) == 404 or "notfound" in type(exc).__name__.lower():
                    if upsert is None:
                        return (None, False)
                    document = dict(upsert)
                    document.setdefault("created_at", utcnow().isoformat())
                    document["updated_at"] = utcnow().isoformat()
                    self.client.index(
                        index=index, id=doc_id, body=document, refresh=True
                    )
                    return (document, True)
                raise

            source = dict(current.get("_source") or {})
            updated = transform(dict(source))
            if updated is None:
                return (source, False)
            updated["updated_at"] = utcnow().isoformat()
            try:
                self.client.index(
                    index=index,
                    id=doc_id,
                    body=updated,
                    if_seq_no=current.get("_seq_no"),
                    if_primary_term=current.get("_primary_term"),
                    refresh=True,
                )
                return (updated, True)
            except Exception as exc:  # noqa: BLE001
                conflict = (
                    getattr(exc, "status_code", None) == 409
                    or "conflict" in type(exc).__name__.lower()
                    or "version_conflict" in str(exc).lower()
                )
                if not conflict:
                    raise
                logger.info(
                    "atomic_update(%s, %s): version conflict on attempt %d/%d",
                    index, doc_id, attempt + 1, max_retries,
                )
                await asyncio.sleep(
                    backoff_base_seconds * (2 ** attempt) * random.uniform(0.5, 1.5)
                )

        raise elasticsearch_unavailable(
            message=(
                f"concurrent modification of {doc_id!r} in {index!r} after "
                f"{max_retries} attempts"
            ),
            details={"index": index, "doc_id": doc_id, "attempts": max_retries},
        )

    #: Ceiling on how many documents one :meth:`update_by_query` call will touch
    #: on the Elasticsearch branch. The branch fans the transform out over the
    #: matched ids one document at a time, so an unbounded match set would be an
    #: unbounded number of round trips. Exceeding it raises rather than applying
    #: a prefix of the change: a silently partial ``update_by_query`` leaves the
    #: index in a state no caller asked for and no caller can detect.
    UPDATE_BY_QUERY_MAX_DOCS: int = 5_000

    async def update_by_query(
        self,
        index: str,
        query: Dict[str, Any],
        transform,
    ) -> int:
        """Apply ``transform`` to every document matching ``query``; return the count.

        The facade equivalent of ``_update_by_query``, which
        ``fuel/driver_repository.py`` used with a painless script to reset
        denormalised driver counters.

        ``transform`` is a Python callable on both backends, and the
        Elasticsearch branch deliberately pays for that: rather than translate the
        change into painless it searches for the matching ids and calls
        :meth:`atomic_update` on each. A painless twin would mean the same rule
        written twice in two languages, drifting apart with nothing to catch it —
        and the ES branch is the one being deleted, so the duplication would be
        paid permanently to optimise the path with the shorter life.

        The row-locking difference from :meth:`atomic_update` carries over: on
        Postgres every matched row is locked for one transaction, so a concurrent
        write to a matched document cannot be lost. Elasticsearch resolves the
        query first and then updates each hit, so concurrent writers race per
        document.

        Returns the number of documents actually changed — a transform that
        returns ``None`` for a hit is not counted.
        """
        if self._is_retired_index(index):
            return 0

        store = self._pg_store()
        if store is not None:
            return await store.update_by_query(index, query, transform)

        # ``_source: false`` because the ids are all this needs; ``atomic_update``
        # re-reads each document under its own version assertion, and using a body
        # fetched before that read would reintroduce the lost update this exists
        # to avoid.
        response = await self.search_documents(
            index,
            {"query": query, "_source": False},
            size=self.UPDATE_BY_QUERY_MAX_DOCS + 1,
        )
        hits = ((response or {}).get("hits") or {}).get("hits") or []
        if len(hits) > self.UPDATE_BY_QUERY_MAX_DOCS:
            raise elasticsearch_unavailable(
                message=(
                    f"update_by_query on {index!r} matched more than "
                    f"{self.UPDATE_BY_QUERY_MAX_DOCS} documents; refusing to "
                    "apply a partial update"
                ),
                details={"index": index, "limit": self.UPDATE_BY_QUERY_MAX_DOCS},
            )

        changed = 0
        for hit in hits:
            _doc, applied = await self.atomic_update(index, hit["_id"], transform)
            if applied:
                changed += 1
        return changed

    async def bulk_index_documents(self, index: str, documents: List[Dict[Any, Any]]) -> Dict[str, Any]:
        """
        Bulk index multiple documents with circuit breaker protection and partial failure handling.
        
        This method handles partial failures in bulk operations by:
        - Continuing to process successful documents even when some fail
        - Logging detailed information about failed documents
        - Returning a result indicating partial success with counts
        
        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirement 7.6: WHEN bulk indexing operations fail partially, THE Elasticsearch_Client 
          SHALL log failed documents and continue processing successful ones
        
        Args:
            index: The name of the Elasticsearch index
            documents: List of documents to index
            
        Returns:
            Dict containing:
            - success: bool indicating if all documents were indexed successfully
            - total: total number of documents attempted
            - successful: count of successfully indexed documents
            - failed: count of failed documents
            - errors: list of error details for failed documents
        """
        store = self._pg_store()
        if store is not None:
            return await store.bulk_index_documents(index, documents)
        try:
            async def _do_bulk_index():
                from elasticsearch.helpers import bulk, BulkIndexError
                
                actions = []
                doc_id_map = {}  # Map action index to document info for error reporting
                
                for idx, doc in enumerate(documents):
                    doc["updated_at"] = utcnow().isoformat()
                    if "created_at" not in doc:
                        doc["created_at"] = utcnow().isoformat()
                    
                    # Map index names to correct ID fields
                    id_field_map = {
                        "trucks": "truck_id",
                        "inventory": "item_id", 
                        "support_tickets": "ticket_id",
                        "locations": "location_id",
                        "analytics_events": "event_id"
                    }
                    
                    # Get the correct ID field for this index
                    id_field = id_field_map.get(index, f"{index[:-1]}_id")
                    doc_id = doc.get("id") or doc.get(id_field)
                    
                    if not doc_id:
                        logger.warning(f"No ID found for document in {index} index. Available fields: {list(doc.keys())}")
                    
                    action = {
                        "_index": index,
                        "_id": doc_id,
                        "_source": doc
                    }
                    actions.append(action)
                    doc_id_map[idx] = {"doc_id": doc_id, "index": index}
                
                # Initialize result structure
                result = {
                    "success": True,
                    "total": len(documents),
                    "successful": 0,
                    "failed": 0,
                    "errors": []
                }
                
                try:
                    # Use raise_on_error=False to handle partial failures
                    # This allows us to continue processing even when some documents fail
                    success_count, errors = bulk(
                        self.client, 
                        actions, 
                        refresh=True,
                        raise_on_error=False,
                        raise_on_exception=False
                    )
                    
                    result["successful"] = success_count
                    
                    # Process any errors that occurred
                    if errors:
                        result["success"] = False
                        result["failed"] = len(errors)
                        
                        for error in errors:
                            # Extract error details from the bulk response
                            error_info = self._extract_bulk_error_info(error)
                            result["errors"].append(error_info)
                            
                            # Log each failed document with details
                            # Validates Requirement 7.6: log failed documents
                            logger.error(
                                f"❌ Bulk indexing failed for document in '{index}': "
                                f"doc_id={error_info.get('doc_id', 'unknown')}, "
                                f"error_type={error_info.get('error_type', 'unknown')}, "
                                f"reason={error_info.get('reason', 'unknown')}"
                            )
                        
                        # Log summary of partial failure
                        logger.warning(
                            f"⚠️ Bulk indexing to '{index}' completed with partial failures: "
                            f"{result['successful']}/{result['total']} documents indexed successfully, "
                            f"{result['failed']} documents failed"
                        )
                    else:
                        logger.info(f"✅ Bulk indexed {result['successful']} documents to {index}")
                    
                    return result
                    
                except BulkIndexError as e:
                    # Handle BulkIndexError which contains details about failed documents
                    # This exception is raised when raise_on_error=True (not our case, but handle defensively)
                    result["success"] = False
                    result["failed"] = len(e.errors)
                    result["successful"] = result["total"] - result["failed"]
                    
                    for error in e.errors:
                        error_info = self._extract_bulk_error_info(error)
                        result["errors"].append(error_info)
                        
                        # Log each failed document
                        logger.error(
                            f"❌ Bulk indexing failed for document in '{index}': "
                            f"doc_id={error_info.get('doc_id', 'unknown')}, "
                            f"error_type={error_info.get('error_type', 'unknown')}, "
                            f"reason={error_info.get('reason', 'unknown')}"
                        )
                    
                    logger.warning(
                        f"⚠️ Bulk indexing to '{index}' completed with partial failures: "
                        f"{result['successful']}/{result['total']} documents indexed successfully, "
                        f"{result['failed']} documents failed"
                    )
                    
                    return result
            
            return await self._circuit_breaker.execute(_do_bulk_index)
        except CircuitOpenException as e:
            self._handle_circuit_breaker_exception(e)
        except Exception as e:
            self._handle_elasticsearch_error(f"bulk_index_documents({index})", e)
    
    def _extract_bulk_error_info(self, error: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract detailed error information from a bulk operation error.
        
        This method parses the error structure returned by Elasticsearch bulk operations
        and extracts relevant information for logging and reporting.
        
        Validates:
        - Requirement 7.6: Log failed documents with details
        
        Args:
            error: The error dict from Elasticsearch bulk response
            
        Returns:
            Dict containing:
            - doc_id: The document ID that failed
            - index: The target index
            - error_type: The type of error (e.g., 'mapper_parsing_exception')
            - reason: Human-readable error reason
            - caused_by: Additional cause information if available
        """
        error_info = {
            "doc_id": None,
            "index": None,
            "error_type": None,
            "reason": None,
            "caused_by": None
        }
        
        try:
            # The error structure can vary based on the operation type (index, create, update, delete)
            # Common structure: {'index': {'_index': '...', '_id': '...', 'error': {...}, 'status': 400}}
            for operation_type in ['index', 'create', 'update', 'delete']:
                if operation_type in error:
                    op_result = error[operation_type]
                    error_info["doc_id"] = op_result.get("_id")
                    error_info["index"] = op_result.get("_index")
                    
                    if "error" in op_result:
                        error_detail = op_result["error"]
                        error_info["error_type"] = error_detail.get("type")
                        error_info["reason"] = error_detail.get("reason")
                        
                        # Extract caused_by if present (nested error details)
                        if "caused_by" in error_detail:
                            caused_by = error_detail["caused_by"]
                            error_info["caused_by"] = {
                                "type": caused_by.get("type"),
                                "reason": caused_by.get("reason")
                            }
                    break
            
            # If we couldn't parse the standard structure, store the raw error
            if error_info["error_type"] is None and error_info["reason"] is None:
                error_info["reason"] = str(error)
                
        except Exception as parse_error:
            # If parsing fails, store what we can
            logger.warning(f"Failed to parse bulk error details: {parse_error}")
            error_info["reason"] = str(error)
        
        return error_info
    
    async def search_documents(self, index: str, query: Dict[Any, Any], size: int = 100, request_timeout: int = 10):
        """
        Search documents in an index with circuit breaker protection.
        
        Args:
            index: The Elasticsearch index to search.
            query: The query body.
            size: Maximum number of results to return.
            request_timeout: Per-call timeout in seconds (default 10s).
                Prevents a single slow aggregation from blocking the
                ASGI event loop for the full connection-level 30s.
        
        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        """
        store = self._pg_store()
        if store is not None:
            return await store.search_documents(
                index, query, size, request_timeout
            )
        try:
            async def _do_search():
                # Add size to query body if not already present
                if "size" not in query:
                    query["size"] = size
                
                response = self.client.search(
                    index=index,
                    body=query,
                    request_timeout=request_timeout,
                )
                return response
            
            return await self._read_circuit_breaker.execute(_do_search)
        except CircuitOpenException as e:
            self._handle_circuit_breaker_exception(e)
        except Exception as e:
            self._handle_elasticsearch_error(f"search_documents({index})", e)
    
    async def multi_search(
        self,
        searches: List[Dict[str, Any]],
        request_timeout: int = 10,
    ) -> Dict[str, Any]:
        """Run several search bodies in ONE round trip via the `_msearch` API.

        The point of this method is the round-trip count: N independent
        `terms`-filtered lookups cost one network hop instead of N. It is what
        lets a read model collapse an N+1 fan-out into a fixed budget (see
        `DriverWorkService`, which resolves compartment prior grades and stop
        coordinates in a single call).

        Args:
            searches: One entry per search body, each
                ``{"index": <index name>, "query": <query body>}``. A missing
                index is tolerated per body (``ignore_unavailable``), so a
                deployment that has not created an optional index gets an empty
                result rather than a failed request.
            request_timeout: Per-call timeout in seconds.

        Returns:
            The raw `_msearch` response, ``{"responses": [<search response>, ...]}``
            in request order. An empty ``searches`` list returns
            ``{"responses": []}`` without touching the cluster.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        """
        if not searches:
            return {"responses": []}

        store = self._pg_store()
        if store is not None:
            return await store.multi_search(searches, request_timeout)

        try:
            async def _do_multi_search():
                body: List[Dict[str, Any]] = []
                for entry in searches:
                    header = {
                        "index": entry["index"],
                        "ignore_unavailable": True,
                    }
                    body.append(header)
                    body.append(dict(entry.get("query") or {}))
                return self.client.msearch(
                    body=body,
                    request_timeout=request_timeout,
                )

            return await self._read_circuit_breaker.execute(_do_multi_search)
        except CircuitOpenException as e:
            self._handle_circuit_breaker_exception(e)
        except Exception as e:
            indices = ",".join(str(entry.get("index")) for entry in searches)
            self._handle_elasticsearch_error(f"multi_search({indices})", e)

    async def get_document(self, index: str, doc_id: str):
        """
        Get a single document by ID with circuit breaker protection.

        Returns the document ``_source`` dict, or ``None`` when the document
        does not exist. A missing document is an expected outcome for
        idempotency / existence checks (e.g. the weather-alert ingester
        checking whether an alert was already persisted), so a 404 is NOT
        logged at ERROR — it quietly returns ``None``. Genuine ES failures
        (auth, connection, 5xx) still raise through ``_handle_elasticsearch_error``.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        """
        store = self._pg_store()
        if store is not None:
            return await store.get_document(index, doc_id)
        try:
            async def _do_get():
                response = self.client.get(index=index, id=doc_id)
                return response["_source"]
            
            return await self._read_circuit_breaker.execute(_do_get)
        except CircuitOpenException as e:
            self._handle_circuit_breaker_exception(e)
        except Exception as e:
            # NotFoundError (404) means the doc doesn't exist — return None
            # without logging at ERROR. This is an expected path for
            # existence/idempotency probes.
            if getattr(e, "status_code", None) == 404:
                return None
            self._handle_elasticsearch_error(f"get_document({index}, {doc_id})", e)
    
    async def delete_document(self, index: str, doc_id: str) -> bool:
        """
        Delete a single document by ID with circuit breaker protection.

        Returns True if the document was deleted, False if not found.
        """
        if self._is_retired_index(index):
            return False
        store = self._pg_store()
        if store is not None:
            return await store.delete_document(index, doc_id)
        try:
            async def _do_delete():
                self.client.delete(index=index, id=doc_id, refresh="wait_for")
                return True

            return await self._read_circuit_breaker.execute(_do_delete)
        except CircuitOpenException as e:
            self._handle_circuit_breaker_exception(e)
        except Exception as e:
            # NotFoundError means the doc doesn't exist — return False
            if hasattr(e, 'status_code') and e.status_code == 404:
                return False
            self._handle_elasticsearch_error(f"delete_document({index}, {doc_id})", e)

    async def get_all_documents(self, index: str, size: int = 1000):
        """
        Get all documents from an index with circuit breaker protection.
        
        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        """
        try:
            query = {
                "query": {"match_all": {}},
                "sort": [{"created_at": {"order": "desc"}}]
            }
            response = await self.search_documents(index, query, size)
            return [hit["_source"] for hit in response["hits"]["hits"]]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error(f"get_all_documents({index})", e)
    
    async def semantic_search(self, tenant_id: str, index: str, text: str, fields: List[str], size: int = 10):
        """
        Full-text search across ``fields``, with circuit breaker protection.

        The name is historical and the docstring used to claim "semantic search
        using semantic_text fields". It never did: the query below is a plain
        ``multi_match``, which is lexical, and no inference endpoint is
        configured anywhere in the codebase. The four mappings that declared
        ``semantic_text`` for these fields have been changed to ``text`` (see the
        note above ``_get_locations_mapping``) precisely because this method
        behaves identically either way — while the type made index creation fail
        outright on any cluster that does not support it.

        The query is scoped to the supplied tenant: every request is wrapped
        with a ``{"term": {"tenant_id": tenant_id}}`` filter so a caller
        cannot see documents from another tenant even if the index is shared.
        This is required because ``/api/search`` and every AI tool that calls
        ``semantic_search`` runs on behalf of an authenticated tenant and
        must not leak cross-tenant rows.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("semantic_search requires a tenant_id")
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {
                                "multi_match": {
                                    "query": text,
                                    "fields": fields,
                                    "type": "best_fields",
                                }
                            }
                        ],
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                    }
                }
            }
            response = await self.search_documents(index, query, size)
            return [hit["_source"] for hit in response["hits"]["hits"]]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error(f"semantic_search({index})", e)
    
    # Analytics-specific methods
    async def get_time_series_data(self, tenant_id: str, event_type: str, metric_field: str, time_range: str = "7d"):
        """
        Get time-series data for analytics charts with circuit breaker protection.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("get_time_series_data requires a tenant_id")
        try:
            # Calculate date range
            from datetime import timedelta
            now = utcnow()

            if time_range == "24h":
                start_time = now - timedelta(hours=24)
                interval = "1h"
            elif time_range == "7d":
                start_time = now - timedelta(days=7)
                interval = "1d"
            elif time_range == "30d":
                start_time = now - timedelta(days=30)
                interval = "1d"
            else:  # 90d
                start_time = now - timedelta(days=90)
                interval = "1d"

            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"event_type": event_type}},
                            {"range": {"timestamp": {"gte": start_time.isoformat()}}}
                        ],
                        "filter": [
                            {"term": {"tenant_id": tenant_id}},
                        ],
                    }
                },
                "aggs": {
                    "time_series": {
                        "date_histogram": {
                            "field": "timestamp",
                            "fixed_interval": interval,
                            "min_doc_count": 0
                        },
                        "aggs": {
                            "avg_metric": {
                                "avg": {"field": f"metrics.{metric_field}"}
                            }
                        }
                    }
                },
                "size": 0
            }
            
            response = await self.search_documents("analytics_events", query)
            buckets = response["aggregations"]["time_series"]["buckets"]
            
            return [
                {
                    "timestamp": bucket["key_as_string"],
                    "value": round(bucket["avg_metric"]["value"] or 0, 2)
                }
                for bucket in buckets
            ]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error("get_time_series_data", e)
    
    async def get_route_performance_data(self, tenant_id: str):
        """
        Get route performance aggregation with circuit breaker protection.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("get_route_performance_data requires a tenant_id")
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [{"term": {"event_type": "route_performance"}}],
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                    }
                },
                "aggs": {
                    "routes": {
                        "terms": {"field": "route_name.keyword", "size": 10},
                        "aggs": {
                            "avg_performance": {
                                "avg": {"field": "metrics.performance_pct"}
                            }
                        }
                    }
                },
                "size": 0
            }
            
            response = await self.search_documents("analytics_events", query)
            buckets = response["aggregations"]["routes"]["buckets"]
            
            return [
                {
                    "name": bucket["key"],
                    "performance": round(bucket["avg_performance"]["value"] or 0, 1)
                }
                for bucket in buckets
            ]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error("get_route_performance_data", e)
    
    async def get_delay_causes_data(self, tenant_id: str):
        """
        Get delay causes aggregation with circuit breaker protection.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("get_delay_causes_data requires a tenant_id")
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [{"term": {"event_type": "delay_cause_analysis"}}],
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                    }
                },
                "aggs": {
                    "causes": {
                        "terms": {"field": "delay_cause", "size": 10},
                        "aggs": {
                            "avg_percentage": {
                                "avg": {"field": "metrics.percentage"}
                            }
                        }
                    }
                },
                "size": 0
            }
            
            response = await self.search_documents("analytics_events", query)
            buckets = response["aggregations"]["causes"]["buckets"]
            
            return [
                {
                    "name": bucket["key"],
                    "percentage": round(bucket["avg_percentage"]["value"] or 0, 1)
                }
                for bucket in buckets
            ]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error("get_delay_causes_data", e)
    
    async def get_regional_performance_data(self, tenant_id: str):
        """
        Get regional performance aggregation with circuit breaker protection.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("get_regional_performance_data requires a tenant_id")
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [{"term": {"event_type": "regional_performance"}}],
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                    }
                },
                "aggs": {
                    "regions": {
                        "terms": {"field": "region", "size": 10},
                        "aggs": {
                            "avg_on_time": {
                                "avg": {"field": "metrics.on_time_percentage"}
                            }
                        }
                    }
                },
                "size": 0
            }
            
            response = await self.search_documents("analytics_events", query)
            buckets = response["aggregations"]["regions"]["buckets"]
            
            return [
                {
                    "name": bucket["key"],
                    "onTimePercentage": round(bucket["avg_on_time"]["value"] or 0, 1)
                }
                for bucket in buckets
            ]
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error("get_regional_performance_data", e)
    
    async def get_current_metrics(self, tenant_id: str):
        """
        Get current performance metrics with circuit breaker protection.

        Validates:
        - Requirement 3.5: Implement circuit breakers for Elasticsearch
        - Requirement 2.4: Return specific error code indicating database unavailability
        - Requirements 9.2, 9.4: Enforce tenant scoping on ES reads
        """
        if not tenant_id:
            raise ValueError("get_current_metrics requires a tenant_id")
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [{"term": {"event_type": "daily_performance"}}],
                        "filter": [{"term": {"tenant_id": tenant_id}}],
                    }
                },
                "sort": [{"timestamp": {"order": "desc"}}],
                "size": 1
            }
            
            response = await self.search_documents("analytics_events", query)
            if response["hits"]["hits"]:
                latest = response["hits"]["hits"][0]["_source"]["metrics"]
                return {
                    "delivery_performance": {
                        "title": "Delivery Performance",
                        "value": f"{latest.get('delivery_performance_pct', 87.5)}%",
                        "change": "+2.3%",
                        "trend": "up"
                    },
                    "average_delay": {
                        "title": "Average Delay", 
                        "value": f"{latest.get('average_delay_minutes', 144)/60:.1f} hrs",
                        "change": "-0.8 hrs",
                        "trend": "down"
                    },
                    "fleet_utilization": {
                        "title": "Fleet Utilization",
                        "value": f"{latest.get('fleet_utilization_pct', 92)}%",
                        "change": "+5%",
                        "trend": "up"
                    },
                    "customer_satisfaction": {
                        "title": "Customer Satisfaction",
                        "value": f"{latest.get('customer_satisfaction', 4.2)}/5",
                        "change": "+0.1",
                        "trend": "up"
                    }
                }
            else:
                raise Exception("No analytics data found")
        except AppException:
            # Re-raise AppExceptions (already handled by search_documents)
            raise
        except Exception as e:
            self._handle_elasticsearch_error("get_current_metrics", e)

# Global instance
elasticsearch_service = ElasticsearchService()
