# ADR 005: Elasticsearch as Primary Data Layer

## Status

Accepted

## Context

The Runsheet logistics platform manages operational data across multiple domains: shipment tracking, rider management, fleet/vehicle locations, fuel monitoring, job scheduling, cargo tracking, and AI agent activity logs. The platform needs to support:

- **Full-text search**: Operators search shipments by tracking number, rider name, destination, and other text fields.
- **Real-time aggregations**: Dashboards display live counts, status breakdowns, and SLA compliance metrics across thousands of records.
- **Time-series data**: Shipment events, fuel consumption records, location updates, and agent activity logs are time-stamped and queried by time range.
- **Multi-tenant isolation**: All data is scoped by tenant ID, and queries must efficiently filter by tenant.
- **High write throughput**: Webhook ingestion from external systems (Dinee) produces bursts of shipment and rider updates that must be indexed quickly.
- **Flexible schema evolution**: New fields are added to shipment and rider documents as the platform evolves, without requiring downtime or migrations.

Two primary approaches were considered:

1. **Relational database (PostgreSQL)**: Traditional ACID-compliant storage with structured schemas, joins, and transactions. Well-suited for transactional workloads with complex relationships.

2. **Elasticsearch as primary data store**: Document-oriented storage with built-in full-text search, aggregation framework, and near-real-time indexing. Well-suited for search-heavy, analytics-heavy workloads with flexible schemas.

## Decision

We chose Elasticsearch as the primary data store for all operational data. The platform uses dedicated indices per domain and entity type:

**Ops domain indices:**
- `shipments_current` — Current state of all shipments (upserted on each status change)
- `shipment_events` — Immutable event log of all shipment state transitions
- `riders_current` — Current state of all riders
- `ops_poison_queue` — Failed ingestion events for manual review

**Fuel domain indices:**
- `fuel_entries` — Fuel consumption and refill records
- `fuel_alerts` — Fuel threshold alerts and anomaly detections

**Scheduling domain indices:**
- `jobs` — Job definitions and status
- `cargo` — Cargo records linked to jobs

**Agent domain indices:**
- `agent_activity_log` — AI agent action logs, mutation decisions, and orchestration events
- `agent_memory` — Conversation context and agent memory entries
- `agent_feedback` — User feedback on agent responses

Key design choices:

- **One index per entity type**: Rather than a single large index, each entity type has its own index with a strict mapping. This allows independent index lifecycle management (ILM), retention policies, and performance tuning.
- **Upsert pattern for current state**: Indices like `shipments_current` use document upserts keyed by entity ID. Each update replaces the previous document, maintaining a single current-state view per entity.
- **Append-only event indices**: Indices like `shipment_events` and `agent_activity_log` are append-only, providing an immutable audit trail.
- **Tenant ID as a required field**: Every document includes a `tenant_id` field, and all queries inject a tenant filter. This is enforced at the service layer rather than through Elasticsearch's security features.
- **Circuit breaker protection**: The shared `ElasticsearchService` wraps the Elasticsearch client with circuit breaker logic to handle cluster unavailability gracefully.
- **Serverless compatibility**: Index creation logic detects whether the cluster is serverless (Elastic Cloud Serverless) and strips incompatible settings (e.g., number of shards, ILM policies) automatically.

The `ElasticsearchService` class provides the shared connection and error handling, while domain-specific services (`OpsElasticsearchService`, `FuelService`, etc.) implement domain logic on top of it.

## Consequences

### Positive

- **Search performance**: Full-text search across shipments, riders, and jobs is a core user workflow. Elasticsearch provides sub-second search with relevance scoring out of the box, without requiring a separate search index.
- **Aggregation capabilities**: Real-time dashboards use Elasticsearch aggregations (terms, date histograms, percentiles) to compute metrics without pre-computation or materialized views.
- **Schema flexibility**: Adding new fields to documents does not require schema migrations or downtime. Elasticsearch's dynamic mapping handles new fields automatically, while explicit mappings enforce types for known fields.
- **Near-real-time indexing**: Documents are searchable within 1 second of indexing (configurable refresh interval), which is sufficient for the platform's real-time monitoring use cases.
- **Horizontal scalability**: Elasticsearch clusters scale horizontally by adding nodes. Index sharding distributes data and query load across the cluster.
- **Built-in ILM**: Index Lifecycle Management policies handle automatic rollover, retention, and deletion of time-series data (e.g., keeping shipment events for 90 days).

### Negative

- **No ACID transactions**: Elasticsearch does not support multi-document transactions. Operations that need to update multiple documents atomically (e.g., reassigning a rider and updating the shipment) must handle partial failures at the application level.
- **No relational joins**: Cross-entity queries (e.g., "find all shipments for riders in zone X") require either denormalization (storing rider data in shipment documents) or application-level joins (multiple queries). The platform uses denormalization where needed.
- **Eventual consistency**: Elasticsearch's near-real-time model means a document may not be immediately searchable after indexing. The platform handles this with explicit refresh calls where immediate consistency is required.
- **Operational complexity**: Running an Elasticsearch cluster requires monitoring cluster health, shard allocation, disk usage, and JVM heap. The platform mitigates this by using Elastic Cloud (managed service) rather than self-hosted clusters.
- **Vendor coupling**: The data layer is tightly coupled to Elasticsearch's query DSL and indexing API. Migrating to a different data store would require rewriting all service-layer code. The domain-specific service classes (`OpsElasticsearchService`, `FuelService`) encapsulate this coupling, but the migration effort would still be significant.
- **Cost at scale**: Elasticsearch clusters with high write throughput and large data volumes can be expensive, particularly on managed cloud services. Index lifecycle policies and data retention limits help control costs.
