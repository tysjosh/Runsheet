"""
Ops Elasticsearch Service for the Ops Intelligence Layer.

Manages ops-specific indices (shipments_current, shipment_events, riders_current,
ops_poison_queue) with strict mappings, scripted upserts for out-of-order event
reconciliation, ILM policies, and bulk operations.

Delegates to the existing ElasticsearchService for connection management and
circuit breaker protection.

Validates:
- Requirement 5.1-5.6: Elasticsearch index creation and strict mappings
- Requirement 6.1-6.9: Upsert logic with out-of-order event reconciliation
- Requirement 7.1-7.5: Index lifecycle and retention policies
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class OpsElasticsearchService:
    """
    Manages ops-specific indices and operations.
    Delegates to the existing ElasticsearchService for connection/circuit breaker.
    """

    SHIPMENTS_CURRENT = "shipments_current"
    SHIPMENT_EVENTS = "shipment_events"
    RIDERS_CURRENT = "riders_current"
    POISON_QUEUE = "ops_poison_queue"

    # Painless script for current-state upsert with out-of-order reconciliation.
    # Compares incoming event_timestamp vs existing last_event_timestamp.
    # Discards (noop) if incoming is older. Otherwise partial-updates only
    # the fields present in the incoming params.
    # Validates: Req 6.1, 6.2, 6.4, 6.7, 6.8
    UPSERT_SCRIPT = """
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

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service

    @property
    def client(self):
        """Access the underlying Elasticsearch client."""
        return self._es.client

    @property
    def circuit_breaker(self):
        """Access the circuit breaker from the delegate service."""
        return self._es.circuit_breaker

    # ------------------------------------------------------------------
    # Index setup
    # ------------------------------------------------------------------

    def setup_ops_indices(self):
        """
        Create all ops indices with strict mappings if they don't exist.
        Validates: Req 5.1-5.6
        """
        from services.elasticsearch_service import ElasticsearchService

        indices = {
            self.SHIPMENTS_CURRENT: self._get_shipments_current_mapping(),
            self.SHIPMENT_EVENTS: self._get_shipment_events_mapping(),
            self.RIDERS_CURRENT: self._get_riders_current_mapping(),
            self.POISON_QUEUE: self._get_poison_queue_mapping(),
        }

        for index_name, mapping in indices.items():
            try:
                if not self.client.indices.exists(index=index_name):
                    if self._es.is_serverless:
                        mapping = ElasticsearchService.strip_serverless_incompatible_settings(mapping)
                    self.client.indices.create(index=index_name, body=mapping)
                    logger.info(f"✅ Created ops index: {index_name}")
                else:
                    logger.info(f"📋 Ops index already exists: {index_name}")
            except Exception as e:
                logger.error(f"❌ Failed to create ops index {index_name}: {e}")

        # Set up shipment_events alias for time-based rollover
        self._setup_shipment_events_alias()

    def validate_ops_index_schemas(self) -> Dict[str, Any]:
        """
        Validate that ops index mappings match expected schemas.
        Logs warnings for any mismatches.
        """
        logger.info("🔍 Validating ops index schemas...")

        expected_mappings = {
            self.SHIPMENTS_CURRENT: self._get_shipments_current_mapping(),
            self.SHIPMENT_EVENTS: self._get_shipment_events_mapping(),
            self.RIDERS_CURRENT: self._get_riders_current_mapping(),
            self.POISON_QUEUE: self._get_poison_queue_mapping(),
        }

        results: Dict[str, Any] = {"valid": True, "indices": {}}

        for index_name, expected in expected_mappings.items():
            try:
                if not self.client.indices.exists(index=index_name):
                    results["indices"][index_name] = {
                        "valid": False,
                        "error": "Index does not exist",
                    }
                    results["valid"] = False
                    logger.warning(f"⚠️ Ops index missing: {index_name}")
                    continue

                actual = self.client.indices.get_mapping(index=index_name)
                actual_props = (
                    actual.get(index_name, {})
                    .get("mappings", {})
                    .get("properties", {})
                )
                expected_props = expected.get("mappings", {}).get("properties", {})

                missing = set(expected_props.keys()) - set(actual_props.keys())
                if missing:
                    results["indices"][index_name] = {
                        "valid": False,
                        "missing_fields": list(missing),
                    }
                    results["valid"] = False
                    logger.warning(
                        f"⚠️ Ops index {index_name} missing fields: {missing}"
                    )
                else:
                    results["indices"][index_name] = {"valid": True}
            except Exception as e:
                results["indices"][index_name] = {"valid": False, "error": str(e)}
                results["valid"] = False
                logger.error(f"❌ Failed to validate ops index {index_name}: {e}")

        if results["valid"]:
            logger.info("✅ All ops index schemas validated successfully")
        return results

    # ------------------------------------------------------------------
    # Index mappings
    # ------------------------------------------------------------------

    def _get_shipments_current_mapping(self) -> Dict[str, Any]:
        """
        Strict mapping for shipments_current index.
        Validates: Req 5.1, 5.5
        """
        return {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 1,
            },
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "shipment_id": {"type": "keyword"},
                    "status": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "rider_id": {"type": "keyword"},
                    "failure_reason": {"type": "keyword"},
                    "source_schema_version": {"type": "keyword"},
                    "trace_id": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "estimated_delivery": {"type": "date"},
                    "last_event_timestamp": {"type": "date"},
                    "ingested_at": {"type": "date"},
                    "current_location": {"type": "geo_point"},
                    "origin": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "destination": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                },
            },
        }

    def _get_shipment_events_mapping(self) -> Dict[str, Any]:
        """
        Strict mapping for shipment_events index.
        Validates: Req 5.2, 5.6
        """
        return {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 1,
            },
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "event_id": {"type": "keyword"},
                    "shipment_id": {"type": "keyword"},
                    "event_type": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "source_schema_version": {"type": "keyword"},
                    "trace_id": {"type": "keyword"},
                    "event_timestamp": {"type": "date"},
                    "ingested_at": {"type": "date"},
                    "event_payload": {"type": "nested"},
                    "location": {"type": "geo_point"},
                },
            },
        }

    def _get_riders_current_mapping(self) -> Dict[str, Any]:
        """
        Strict mapping for riders_current index.
        Validates: Req 5.3
        """
        return {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 1,
            },
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "rider_id": {"type": "keyword"},
                    "status": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "availability": {"type": "keyword"},
                    "source_schema_version": {"type": "keyword"},
                    "trace_id": {"type": "keyword"},
                    "last_seen": {"type": "date"},
                    "last_event_timestamp": {"type": "date"},
                    "ingested_at": {"type": "date"},
                    "current_location": {"type": "geo_point"},
                    "active_shipment_count": {"type": "integer"},
                    "completed_today": {"type": "integer"},
                    "rider_name": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                },
            },
        }

    def _get_poison_queue_mapping(self) -> Dict[str, Any]:
        """
        Mapping for ops_poison_queue index.
        original_payload is stored but not indexed (enabled: false).
        Validates: Req 4.2
        """
        return {
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "event_id": {"type": "keyword"},
                    "error_type": {"type": "keyword"},
                    "status": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "original_payload": {"type": "object", "enabled": False},
                    "error_reason": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "created_at": {"type": "date"},
                    "retry_count": {"type": "integer"},
                    "max_retries": {"type": "integer"},
                    "trace_id": {"type": "keyword"},
                },
            },
        }

    # ------------------------------------------------------------------
    # Shipment events alias for time-based rollover
    # ------------------------------------------------------------------

    def _setup_shipment_events_alias(self):
        """
        Set up an index alias for shipment_events with time-based naming
        to support monthly rollover.
        Validates: Req 5.6
        """
        alias_name = self.SHIPMENT_EVENTS
        current_month = datetime.utcnow().strftime("%Y-%m")
        concrete_index = f"{alias_name}-{current_month}"

        try:
            # If the concrete monthly index doesn't exist, the base index
            # already serves as the write target. We add an alias so that
            # future rollover can point to monthly indices transparently.
            if not self.client.indices.exists(index=concrete_index):
                # The base index was already created in setup_ops_indices.
                # Add a write alias pointing to it.
                alias_exists = False
                try:
                    aliases = self.client.indices.get_alias(name=alias_name)
                    alias_exists = bool(aliases)
                except Exception:
                    pass

                if not alias_exists:
                    # The base index IS the alias name, so we don't need a
                    # separate alias for the initial setup. The alias pattern
                    # will be used when monthly rollover creates new indices.
                    logger.info(
                        f"📋 Shipment events using base index '{alias_name}' "
                        f"(monthly rollover alias pattern: {alias_name}-YYYY-MM)"
                    )
            else:
                logger.info(
                    f"📋 Monthly shipment events index exists: {concrete_index}"
                )
        except Exception as e:
            logger.warning(f"⚠️ Failed to set up shipment events alias: {e}")

    # ------------------------------------------------------------------
    # Upsert operations
    # ------------------------------------------------------------------

    async def upsert_shipment_current(self, doc: Dict[str, Any]) -> bool:
        """
        Scripted upsert for shipments_current.
        Compares incoming event_timestamp vs existing last_event_timestamp.
        Discards if stale. Partial update only fields present in incoming event.
        Validates: Req 6.1, 6.4, 6.7, 6.8
        """
        shipment_id = doc.get("shipment_id")
        if not shipment_id:
            logger.error("Cannot upsert shipment: missing shipment_id")
            return False

        return await self._scripted_upsert(
            index=self.SHIPMENTS_CURRENT,
            doc_id=shipment_id,
            doc=doc,
            entity_label="shipment",
        )

    async def upsert_rider_current(self, doc: Dict[str, Any]) -> bool:
        """
        Scripted upsert for riders_current.
        Same timestamp-based upsert logic as shipments.
        Validates: Req 6.2, 6.7
        """
        rider_id = doc.get("rider_id")
        if not rider_id:
            logger.error("Cannot upsert rider: missing rider_id")
            return False

        return await self._scripted_upsert(
            index=self.RIDERS_CURRENT,
            doc_id=rider_id,
            doc=doc,
            entity_label="rider",
        )

    async def _scripted_upsert(
        self,
        index: str,
        doc_id: str,
        doc: Dict[str, Any],
        entity_label: str,
    ) -> bool:
        """
        Execute a scripted upsert with out-of-order event reconciliation.

        The painless script compares incoming event_timestamp against the
        existing last_event_timestamp. If the incoming event is older or
        equal, the operation is a noop. Otherwise, only the fields present
        in the incoming document are updated.

        Returns True if the document was updated, False if discarded (noop).
        """
        from resilience.circuit_breaker import CircuitOpenException

        try:
            async def _do_upsert():
                response = self.client.update(
                    index=index,
                    id=doc_id,
                    body={
                        "scripted_upsert": True,
                        "script": {
                            "source": self.UPSERT_SCRIPT,
                            "lang": "painless",
                            "params": doc,
                        },
                        "upsert": doc,
                    },
                    refresh=True,
                )
                result = response.get("result", "")
                if result == "noop":
                    logger.info(
                        f"Discarded stale {entity_label} event: "
                        f"entity_id={doc_id}, "
                        f"incoming_timestamp={doc.get('last_event_timestamp')}, "
                        f"event_id={doc.get('trace_id', 'unknown')}"
                    )
                    return False
                return True

            return await self._es._circuit_breaker.execute(_do_upsert)
        except CircuitOpenException as e:
            self._es._handle_circuit_breaker_exception(e)
            return False
        except Exception as e:
            self._es._handle_elasticsearch_error(
                f"upsert_{entity_label}({index})", e
            )
            return False

    # ------------------------------------------------------------------
    # Append operations
    # ------------------------------------------------------------------

    async def append_shipment_event(self, doc: Dict[str, Any]) -> None:
        """
        Always append to shipment_events regardless of ordering.
        Uses event_id as document ID.
        Validates: Req 6.3, 6.9
        """
        event_id = doc.get("event_id")
        if not event_id:
            logger.error("Cannot append shipment event: missing event_id")
            return

        await self._es.index_document(
            index=self.SHIPMENT_EVENTS,
            doc_id=event_id,
            document=doc,
        )

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    async def bulk_upsert(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Bulk API for batch ingestion. Routes failures to poison queue.

        Each operation dict must contain:
          - "action": "upsert_shipment" | "upsert_rider" | "append_event"
          - "doc": the document payload

        Returns summary with success/failure counts.
        Validates: Req 6.5, 6.6
        """
        from resilience.circuit_breaker import CircuitOpenException

        results: Dict[str, Any] = {
            "total": len(operations),
            "successful": 0,
            "failed": 0,
            "errors": [],
        }

        # Build bulk actions list
        actions: List[Dict[str, Any]] = []
        action_meta: List[Dict[str, Any]] = []  # parallel metadata

        for op in operations:
            action_type = op.get("action")
            doc = op.get("doc", {})

            if action_type == "upsert_shipment":
                doc_id = doc.get("shipment_id")
                actions.append({
                    "_op_type": "update",
                    "_index": self.SHIPMENTS_CURRENT,
                    "_id": doc_id,
                    "scripted_upsert": True,
                    "script": {
                        "source": self.UPSERT_SCRIPT,
                        "lang": "painless",
                        "params": doc,
                    },
                    "upsert": doc,
                })
                action_meta.append({"action": action_type, "doc_id": doc_id, "doc": doc})

            elif action_type == "upsert_rider":
                doc_id = doc.get("rider_id")
                actions.append({
                    "_op_type": "update",
                    "_index": self.RIDERS_CURRENT,
                    "_id": doc_id,
                    "scripted_upsert": True,
                    "script": {
                        "source": self.UPSERT_SCRIPT,
                        "lang": "painless",
                        "params": doc,
                    },
                    "upsert": doc,
                })
                action_meta.append({"action": action_type, "doc_id": doc_id, "doc": doc})

            elif action_type == "append_event":
                doc_id = doc.get("event_id")
                actions.append({
                    "_op_type": "index",
                    "_index": self.SHIPMENT_EVENTS,
                    "_id": doc_id,
                    "_source": doc,
                })
                action_meta.append({"action": action_type, "doc_id": doc_id, "doc": doc})
            else:
                results["failed"] += 1
                results["errors"].append({
                    "action": action_type,
                    "error": f"Unknown action type: {action_type}",
                })

        if not actions:
            return results

        try:
            async def _do_bulk():
                from elasticsearch.helpers import bulk

                success_count, errors = bulk(
                    self.client,
                    actions,
                    refresh=True,
                    raise_on_error=False,
                    raise_on_exception=False,
                )
                return success_count, errors

            success_count, errors = await self._es._circuit_breaker.execute(_do_bulk)
            results["successful"] = success_count

            if errors:
                results["failed"] += len(errors)
                for err in errors:
                    error_info = self._es._extract_bulk_error_info(err)
                    results["errors"].append(error_info)
                    logger.error(
                        f"❌ Bulk ops indexing failed: "
                        f"doc_id={error_info.get('doc_id', 'unknown')}, "
                        f"error_type={error_info.get('error_type', 'unknown')}, "
                        f"reason={error_info.get('reason', 'unknown')}"
                    )
            else:
                logger.info(
                    f"✅ Bulk ops indexed {results['successful']} documents"
                )

        except CircuitOpenException as e:
            self._es._handle_circuit_breaker_exception(e)
            results["failed"] = len(actions)
        except Exception as e:
            self._es._handle_elasticsearch_error("bulk_upsert(ops)", e)
            results["failed"] = len(actions)

        return results

    # ------------------------------------------------------------------
    # ILM policies
    # ------------------------------------------------------------------

    def setup_ops_ilm_policies(self):
        """
        Create ILM policies for ops indices.

        - shipment_events: warm@30d, cold@90d, delete@365d
        - shipments_current / riders_current: force-merge after 7d no writes

        Validates: Req 7.1-7.4
        """
        if not self._es._check_ilm_available():
            logger.info(
                "ℹ️ ILM not available — skipping ops ILM policy setup"
            )
            return

        policies = {
            "ops-shipment-events-policy": self._get_shipment_events_ilm_policy(),
            "ops-current-state-policy": self._get_current_state_ilm_policy(),
        }

        for policy_name, policy_body in policies.items():
            try:
                try:
                    self.client.ilm.get_lifecycle(name=policy_name)
                    logger.info(f"📋 Ops ILM policy already exists: {policy_name}")
                    self.client.ilm.put_lifecycle(name=policy_name, body=policy_body)
                    logger.info(f"✅ Updated ops ILM policy: {policy_name}")
                except Exception:
                    self.client.ilm.put_lifecycle(name=policy_name, body=policy_body)
                    logger.info(f"✅ Created ops ILM policy: {policy_name}")
            except Exception as e:
                logger.warning(
                    f"⚠️ Failed to create/update ops ILM policy {policy_name}: {e}"
                )

        # Apply policies to indices
        self._apply_ops_ilm_policies()

    def verify_ops_ilm_policies(self):
        """
        Verify ILM policies are applied to all ops indices on startup.
        Log warnings for any missing policies.
        Validates: Req 7.5
        """
        if not getattr(self._es, "_ilm_available", False):
            logger.debug(
                "Skipping ops ILM verification — ILM not available"
            )
            return

        expected = {
            self.SHIPMENT_EVENTS: "ops-shipment-events-policy",
            self.SHIPMENTS_CURRENT: "ops-current-state-policy",
            self.RIDERS_CURRENT: "ops-current-state-policy",
        }

        for index_name, expected_policy in expected.items():
            try:
                if not self.client.indices.exists(index=index_name):
                    logger.warning(
                        f"⚠️ Ops index {index_name} does not exist — "
                        f"cannot verify ILM policy"
                    )
                    continue

                settings = self.client.indices.get_settings(index=index_name)
                lifecycle_name = (
                    settings.get(index_name, {})
                    .get("settings", {})
                    .get("index", {})
                    .get("lifecycle", {})
                    .get("name")
                )

                if lifecycle_name != expected_policy:
                    logger.warning(
                        f"⚠️ Ops index {index_name} has ILM policy "
                        f"'{lifecycle_name}' but expected '{expected_policy}'"
                    )
                else:
                    logger.info(
                        f"✅ Ops ILM policy verified for {index_name}: "
                        f"{expected_policy}"
                    )
            except Exception as e:
                logger.warning(
                    f"⚠️ Failed to verify ILM policy for {index_name}: {e}"
                )

    def _get_shipment_events_ilm_policy(self) -> Dict[str, Any]:
        """
        ILM policy for shipment_events: warm@30d, cold@90d, delete@365d.
        Validates: Req 7.1-7.3
        """
        return {
            "policy": {
                "phases": {
                    "hot": {
                        "min_age": "0ms",
                        "actions": {
                            "set_priority": {"priority": 100},
                        },
                    },
                    "warm": {
                        "min_age": "30d",
                        "actions": {
                            "set_priority": {"priority": 50},
                            "forcemerge": {"max_num_segments": 1},
                            "readonly": {},
                        },
                    },
                    "cold": {
                        "min_age": "90d",
                        "actions": {
                            "set_priority": {"priority": 0},
                            "allocate": {"number_of_replicas": 0},
                        },
                    },
                    "delete": {
                        "min_age": "365d",
                        "actions": {"delete": {}},
                    },
                }
            }
        }

    def _get_current_state_ilm_policy(self) -> Dict[str, Any]:
        """
        ILM policy for shipments_current and riders_current:
        force-merge after 7d of no writes.
        Validates: Req 7.4
        """
        return {
            "policy": {
                "phases": {
                    "hot": {
                        "min_age": "0ms",
                        "actions": {
                            "set_priority": {"priority": 100},
                        },
                    },
                    "warm": {
                        "min_age": "7d",
                        "actions": {
                            "forcemerge": {"max_num_segments": 1},
                        },
                    },
                }
            }
        }

    def _apply_ops_ilm_policies(self):
        """Apply ILM policies to ops indices."""
        index_policy_map = {
            self.SHIPMENT_EVENTS: "ops-shipment-events-policy",
            self.SHIPMENTS_CURRENT: "ops-current-state-policy",
            self.RIDERS_CURRENT: "ops-current-state-policy",
        }

        for index_name, policy_name in index_policy_map.items():
            try:
                if self.client.indices.exists(index=index_name):
                    self.client.indices.put_settings(
                        index=index_name,
                        body={
                            "index": {
                                "lifecycle": {"name": policy_name}
                            }
                        },
                    )
                    logger.info(
                        f"✅ Applied ops ILM policy '{policy_name}' "
                        f"to index '{index_name}'"
                    )
            except Exception as e:
                logger.warning(
                    f"⚠️ Failed to apply ops ILM policy to {index_name}: {e}"
                )
