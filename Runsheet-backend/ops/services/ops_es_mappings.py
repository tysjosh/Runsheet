"""Strict mappings for the four ops indices.

These were four methods on :class:`ops.services.ops_es_service.OpsElasticsearchService`,
used only by ``setup_ops_indices`` — which Phase 6 deleted along with every other
index-creation path, because there are no indices to create once the document store
is one Postgres table.

The declarations survive the deletion for a reason that is easy to miss: they are
the *schema*, and ``persistence/document_field_policy.py`` reads declared mappings
to work out which fields must stay unqueryable. Registering this module there is a
net improvement, because ``ops_poison_queue.original_payload`` is declared
``enabled: false`` — Elasticsearch stores it and cannot filter on it — and it holds
the raw payload of a failed ingestion, which can be anything an upstream system
sent. Until now the ops mappings were not in that registry at all, so a jsonb
column made that payload freely queryable.

Validates: Req 5.1-5.6 (the mappings themselves are unchanged; they are moved, not
rewritten, and the move was done by importing the old methods and serialising their
output rather than by copying text).
"""

from typing import Any, Dict

__all__ = ["OPS_INDEX_MAPPINGS"]

#: ``index name -> mapping body``, in the shape the other ``*_es_mappings`` modules
#: use so the field-policy registry can read it uniformly.
OPS_INDEX_MAPPINGS: Dict[str, Dict[str, Any]] = {
    "shipments_current": {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 1
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "shipment_id": {
                    "type": "keyword"
                },
                "status": {
                    "type": "keyword"
                },
                "priority": {
                    "type": "keyword"
                },
                "tenant_id": {
                    "type": "keyword"
                },
                "rider_id": {
                    "type": "keyword"
                },
                "failure_reason": {
                    "type": "keyword"
                },
                "source_schema_version": {
                    "type": "keyword"
                },
                "trace_id": {
                    "type": "keyword"
                },
                "created_at": {
                    "type": "date"
                },
                "updated_at": {
                    "type": "date"
                },
                "estimated_delivery": {
                    "type": "date"
                },
                "last_event_timestamp": {
                    "type": "date"
                },
                "ingested_at": {
                    "type": "date"
                },
                "current_location": {
                    "type": "geo_point"
                },
                "origin": {
                    "type": "text",
                    "fields": {
                        "keyword": {
                            "type": "keyword"
                        }
                    }
                },
                "destination": {
                    "type": "text",
                    "fields": {
                        "keyword": {
                            "type": "keyword"
                        }
                    }
                }
            }
        }
    },
    "shipment_events": {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 1
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "event_id": {
                    "type": "keyword"
                },
                "shipment_id": {
                    "type": "keyword"
                },
                "event_type": {
                    "type": "keyword"
                },
                "tenant_id": {
                    "type": "keyword"
                },
                "source_schema_version": {
                    "type": "keyword"
                },
                "trace_id": {
                    "type": "keyword"
                },
                "event_timestamp": {
                    "type": "date"
                },
                "ingested_at": {
                    "type": "date"
                },
                "event_payload": {
                    "type": "nested"
                },
                "location": {
                    "type": "geo_point"
                }
            }
        }
    },
    "riders_current": {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 1
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "rider_id": {
                    "type": "keyword"
                },
                "status": {
                    "type": "keyword"
                },
                "tenant_id": {
                    "type": "keyword"
                },
                "availability": {
                    "type": "keyword"
                },
                "source_schema_version": {
                    "type": "keyword"
                },
                "trace_id": {
                    "type": "keyword"
                },
                "last_seen": {
                    "type": "date"
                },
                "last_event_timestamp": {
                    "type": "date"
                },
                "ingested_at": {
                    "type": "date"
                },
                "current_location": {
                    "type": "geo_point"
                },
                "active_shipment_count": {
                    "type": "integer"
                },
                "completed_today": {
                    "type": "integer"
                },
                "rider_name": {
                    "type": "text",
                    "fields": {
                        "keyword": {
                            "type": "keyword"
                        }
                    }
                }
            }
        }
    },
    "ops_poison_queue": {
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "event_id": {
                    "type": "keyword"
                },
                "error_type": {
                    "type": "keyword"
                },
                "status": {
                    "type": "keyword"
                },
                "tenant_id": {
                    "type": "keyword"
                },
                "original_payload": {
                    "type": "object",
                    "enabled": False
                },
                "error_reason": {
                    "type": "text",
                    "fields": {
                        "keyword": {
                            "type": "keyword"
                        }
                    }
                },
                "created_at": {
                    "type": "date"
                },
                "retry_count": {
                    "type": "integer"
                },
                "max_retries": {
                    "type": "integer"
                },
                "trace_id": {
                    "type": "keyword"
                }
            }
        }
    }
}
