# AWS deployment strategy — ECS Fargate, Aurora PostgreSQL, ElastiCache Redis

This is a target architecture for *this* application, not a reference
architecture. Every recommendation below is either forced by something verifiable
in the code or is called out as an open decision. Where the codebase makes a
common AWS pattern unsafe, that is stated rather than worked around silently.

Read [docs/deploy-runbook.md](deploy-runbook.md) first — it is the ordered
procedure, and this document is what that procedure runs on.

## The four facts that determine the shape

Everything else follows from these.

1. **The API can now run more than one process — this changed.** Every
   background job still *starts* in every process, but leadership is now
   elected: `persistence/leader_election.py` holds one Postgres advisory lock
   for the "run the periodic jobs" role, and `run_periodic` skips the cycle on a
   follower. All 13 periodic sweeps and every autonomous agent (via
   `AutonomousAgentBase._run_loop`) are gated on it, and the outbox relay keeps
   its own separate lock.

   Verified with two real backend processes against one Postgres: both derived
   the same lock objid, one logged `This process is now the sweep leader`, the
   other `Sweep leader held elsewhere — standing by`, both served `/health/live`
   200 and `/api/orders` 401, and killing the leader moved leadership to the
   standby within one verify interval while it kept serving.

   Leadership is a single *role*, not one lock per job, and that is forced by
   arithmetic: a session-scoped lock needs a connection that lives as long as
   the loop, and ~34 singleton jobs against `database_pool_size` 10 +
   `database_max_overflow` 5 would exhaust the pool. So background work is not
   spread across replicas — one replica runs all of it. Spreading it needs
   transaction-scoped locks per job, which is a larger change and is not
   required for safe deploys.
2. **Scale out, not up.** The Elasticsearch client is the *synchronous*
   `elasticsearch.Elasticsearch`, and `index_document` / `search_documents` call
   it directly inside `async def` with no `asyncio.to_thread`. Every ES round
   trip blocks the event loop. That is the mechanism behind the measured ceiling
   (8× concurrency bought 1.8× throughput), and it means extra vCPU on one
   process buys almost nothing — a second process buys a second event loop.
   `uvicorn --workers N` is also viable now that fact 1 is fixed: each worker is
   a process, and only one of them wins the sweep lock. More tasks is still
   preferable to more workers per task, because it also buys AZ redundancy.
3. **Postgres holds three guarantees nothing else can.** Invoice numbering,
   idempotency-key uniqueness, and the credit-check `SELECT ... FOR UPDATE`.
   Startup refuses staging/production without `DATABASE_URL` for exactly this
   reason. Aurora is therefore load-bearing, not a cache.
4. **Elasticsearch is not just a projection.** Three indices —
   `customer_tanks`, `truck_compartments`, `fuel_stations` — have no Postgres
   source and cannot be rebuilt from one. See
   [docs/backup-and-restore.md](backup-and-restore.md).

## Service topology

```
Internet
  │
  ├── CloudFront + S3 ──────────────► Next.js dispatcher UI (out of scope, see below)
  │
  └── ALB (public subnets, ACM cert)
        │  HTTP :443 → target group :8080, health check /health/live
        ▼
      ECS Fargate service "runsheet-api"   desiredCount >= 2  (private subnets)
        │
        ├──► Aurora PostgreSQL (writer endpoint only)      private subnets
        ├──► ElastiCache Redis (cluster mode DISABLED)     private subnets
        ├──► Elastic Cloud (HTTPS, via NAT)                internet egress
        ├──► SuperTokens managed core (HTTPS, via NAT)     internet egress
        └──► KMS · S3 · Textract (VPC endpoints or NAT)

      ECS RunTask, one-shot, same image:
        · alembic upgrade head            (deploy step, never in the entrypoint)
        · scripts.es_only_backup export   (scheduled, EventBridge Scheduler)
```

Two AZs minimum for the ALB, the Aurora subnet group, and now the API service
itself.

### Deployment: a normal rolling update

Leader election removed the stop-then-start requirement. Use the ECS defaults —
`minimumHealthyPercent: 100`, `maximumPercent: 200` — so the new task starts and
passes its health check before the old one drains. **No downtime window.**

The overlap that used to be the unsafe state is now safe by construction: during
a rolling deploy the old and new tasks both run, exactly one of them holds the
sweep lock, and the other skips every cycle. When the old task stops, its lock
connection closes and the new task picks up leadership within one verify interval
(5 s default).

Two consequences worth stating plainly:

- **A deploy briefly stops the periodic jobs**, for up to one verify interval
  while leadership moves. Every sweep is idempotent and interval-driven, and the
  shortest interval is 60 s, so a 5 s gap is not observable. It is not a missed
  run, just a later one.
- **Autoscaling the API service is now safe.** It was not before. Scale on ALB
  request count or target-response-time rather than CPU, because ES calls block
  the event loop (fact 2) and CPU under-reports load.

### ALB health check: `/health/live`, not `/health/ready`

This differs from the deploy runbook on purpose, and it matters more with several
tasks, not less.

`/health/ready` returns **503 when Elasticsearch is unreachable**. Wired into the
target group, an Elastic Cloud blip marks *every* task unhealthy at once, because
they all share the same dependency. ECS kills them, each replacement checks the
same unreachable cluster and is also killed — a dependency wobble becomes a
fleet-wide crash loop. Replicas do not help here; they all fail together.
`/health/live` returns 200 with every datastore down, which is the correct signal
for "should this process be restarted".

So: **target group → `/health/live`**. Keep `/health/ready` as the deploy gate,
checked by the deploy script after the task is running and before it is declared
good (runbook step 5). Set the container `healthCheck` in the task definition
explicitly rather than relying on the image's `HEALTHCHECK` instruction.

Also set: target group `deregistration_delay` 30 s, ALB `idle_timeout` above the
WebSocket heartbeat interval (there are WebSocket routes in
`bootstrap/websockets.py`; the default 60 s will cut idle sockets), and
`healthCheckGracePeriodSeconds` ≥ 60 so the first readiness pass is not counted
as a failure.

## Aurora PostgreSQL

**Writer endpoint only.** There is a single `database_url` setting and no reader
URL anywhere in the code, so a reader endpoint cannot be used without a code
change. Provisioning replicas is still worth it for failover speed — just do not
expect read offload.

Aurora Serverless v2 fits the measured load well (bursty, modest connection
count). Provisioned instances are equally fine; the choice is cost shape, not
correctness.

### Connections

`database_pool_size` 10 + `database_max_overflow` 5 = **15 per process**, plus the
migration job's own short-lived pool. **Two of those 15 are held permanently**:
one by the outbox relay's lock session and one by the sweep leader's, both for
the same reason — a session-scoped advisory lock has to be held by a connection
that lives as long as the loop. Both are idle-in-transaction by design.

Budget `15 × task count` and check it against the instance's `max_connections`
before scaling out. At 4 tasks that is 60, which is comfortable; it is the number
to watch if the service is autoscaled aggressively. This is also the constraint
that forced one role lock instead of one lock per job — see
`persistence/leader_election.py`.

### Do not put RDS Proxy in front of this

Two reasons, both concrete:

- The relay holds a **session-scoped** `pg_try_advisory_lock`. RDS Proxy
  multiplexes client connections onto backend connections and pins a connection
  as soon as it sees session state it cannot safely share. So either the proxy
  pins (and you have paid for a proxy that no longer multiplexes) or the lock is
  not where the relay thinks it is.
- The credit check uses `SELECT ... FOR UPDATE` inside a transaction. That is
  transaction-scoped and fine, but it is another source of pinning.

With ≤16 connections there is nothing for a proxy to solve.

### Failover behaviour, and what already handles it

- **Stale pooled connections: already handled.** `pool_pre_ping=True` is set in
  `persistence/database.py`, so a connection killed by a failover is detected and
  replaced on checkout rather than surfacing as a random error.
- **The relay's advisory lock: now handled.** The lock is granted on a
  long-lived session, while `drain_once` runs on a *different* session. A
  failover kills the lock-holding connection, Postgres releases the lock, and a
  loop that checked once at startup would keep projecting while a second
  instance legitimately took over — the two-relay hazard arriving by the back
  door. `run_forever` now re-verifies ownership before every drain by comparing
  `pg_backend_pid()` against the pid the lock was granted on, and re-contends
  (or stands down) when it changes. Verified against real Postgres by
  `pg_terminate_backend`-ing the lock backend while another connection held the
  lock: the relay drained 0 times in the following 1.5 s and logged the loss at
  WARNING.

### Parameter group notes

- `idle_in_transaction_session_timeout`: leave at `0`. The relay's lock session
  is idle-in-transaction **by design** — that is how a session-scoped advisory
  lock is held. A non-zero value reaps it periodically. The relay now recovers,
  but it will churn for no reason. (It is `READ COMMITTED`, so this idle session
  does not hold back `xmin` and does not block vacuum.)
- Enable **Performance Insights** and slow-query logging. The write path is ~10×
  a read and carries the pricing hook, the credit check, and the outbox insert;
  when it regresses, this is where you see it.

### Backups

Enable **PITR** (backup retention ≥ 7 days). It gives a far lower RPO than
periodic dumps and is the right primary mechanism. Two caveats that matter
operationally:

- An Aurora PITR restore creates a **new cluster with a new endpoint**. It is
  not an in-place undo, so recovering means a config change and a restart, not
  just a restore.
- `scripts/backup_restore.sh` needs `pg_dump`/`pg_restore`, and the runtime
  image **does not contain `postgresql-client`** — only `curl` was added. So the
  runbook's pre-migration dump cannot be executed as an ECS RunTask with this
  image as built. Either add `postgresql-client` to the runtime stage, or take an
  **Aurora manual snapshot** before the migration job (which needs no client at
  all) and keep the dump path for the in-place restore case.

Take a manual snapshot before every migration regardless. The chain contains
`0007_drop_shipments_current`, which drops a table.

## ElastiCache Redis

**Cluster mode disabled.** This is not a preference. All four Redis consumers —
`session/redis_store.py`, `ops/services/feature_flags.py`,
`ops/ingestion/idempotency.py`, and the fuel-ops migration script — construct the
client with `redis.asyncio.from_url(...)`. That is the plain client. A
cluster-mode-enabled ElastiCache cluster answers `MOVED` redirects that only
`RedisCluster` follows, so cluster mode would fail at runtime, per-key, not at
startup.

Multi-AZ with automatic failover on a replication group is the right shape:
one primary, one replica, cluster mode off. Point `REDIS_URL` at the **primary
endpoint** so it follows a failover.

TLS and AUTH need no code change — `from_url` parses both:

```
REDIS_URL=rediss://:<auth-token>@<primary-endpoint>:6379/0
```

Enable encryption in transit and at rest, and set an AUTH token. Note the URL
therefore contains a credential, so it belongs in Secrets Manager, not in a
plain task-definition environment variable.

Two things to know about how Redis is used here:

- Each consumer opens its **own connection** with no shared pool and no retry
  configuration. A failover surfaces as errors on in-flight commands.
- The session store is the AI-agent conversation memory. Losing Redis loses
  conversation context and trips `SESSION_STORE_UNAVAILABLE` (503); it does not
  lose business records.

## Elasticsearch: stay on Elastic Cloud

The client is `elasticsearch==8.11.0`, connecting with `api_key` and using ILM
(`setup_ilm_policies`, `apply_ilm_policies_to_indices`) plus strict mappings
validated at boot. **Amazon OpenSearch is not a drop-in for this.** The 8.x
client refuses to talk to OpenSearch, ILM is Elastic-specific (OpenSearch has
ISM), and OpenSearch Serverless additionally requires SigV4 request signing that
nothing in `services/elasticsearch_service.py` does.

So: keep Elastic Cloud, reachable over the internet through NAT (or over an
AWS PrivateLink Elastic deployment if you want to avoid NAT egress). Migrating to
OpenSearch is a project — client swap, ILM→ISM rewrite, SigV4 auth, and
re-validating every strict mapping — and it should be a deliberate decision, not
a side effect of moving to AWS.

**The three ES-only indices no longer need their own backup.** Migration
`0008_fuel_asset_tables` gave `customer_tanks`, `truck_compartments` and
`fuel_stations` Postgres tables, so an Aurora snapshot plus
`rebuild_from_postgres --all` covers them and `ES_ONLY_INDICES` is empty. The
scheduled `es_only_backup export` task is no longer required; the script refuses
its default scope rather than writing an empty manifest that looks like a
successful backup.

If you still want a whole-cluster Elasticsearch export (reasonable while the
ES → Postgres migration is in flight), schedule `es_only_backup export --all` as
an EventBridge Scheduler → ECS RunTask. One gap remains: the script writes to a
local `--out-dir` and has no S3 support, so the task must copy the directory to S3
afterwards (`boto3` is available in the image; the AWS CLI is not).

## Configuration and secrets

Startup refuses staging/production without these, so they are not optional:

| Secrets Manager | Why |
|---|---|
| `DATABASE_URL` | Contains the Aurora password. `postgresql+psycopg://…@<writer-endpoint>:5432/runsheet` |
| `REDIS_URL` | Contains the AUTH token when TLS/AUTH is on |
| `ELASTIC_API_KEY` | Elastic Cloud credential |
| `SUPERTOKENS_API_KEY` | Managed-core credential |
| `VOICE_API_KEY_SALT` | Not enforced at startup; defaults to `""` and silently derives every stored key hash from an empty salt |
| `GEMINI_API_KEY` | The agent stack's LLM credential. Startup refuses staging/production without it unless `AGENT_LLM_PROVIDER=vertex_ai`, which needs Google ADC — on Fargate that means mounting a GCP service-account key, so the API key is the simpler path here |

| Plain task-definition environment | Value |
|---|---|
| `ENVIRONMENT` | `production` / `staging` |
| `ELASTIC_ENDPOINT` | Elastic Cloud URL |
| `SUPERTOKENS_CONNECTION_URI` | Managed core URL |
| `SUPERTOKENS_API_DOMAIN` / `SUPERTOKENS_WEBSITE_DOMAIN` | Public backend and frontend origins |
| `CORS_ORIGINS` | JSON array. Production **rejects** any `localhost` origin |
| `COMMERCE_DUAL_WRITE_POSTGRES` | `true` whenever `COMMERCE_BACKBONE_ENABLED=true`; startup refuses the combination otherwise |
| `PORT` | `8080` (the image default) |
| `LOG_LEVEL`, `OTEL_ENDPOINT`, `OTEL_SERVICE_NAME` | See observability |

Inject secrets with the task definition's `secrets` block (ECS resolves them to
environment variables at start), not as a mounted `.env` file. `.dockerignore`
excludes every `.env.*` except the template, so a mounted file is the only way a
credential reaches a layer.

### Three AWS-backed surfaces that are wired by environment variables only

`bootstrap/agents.py::_resolve_fuel_ops_settings` reads these straight from the
process environment. They are **not** pydantic settings and **not** in
`.env.example`, and each service is silently skipped when its variable is
missing — logged at INFO, no startup failure:

| Variable | What does not register without it |
|---|---|
| `FUEL_OPS_S3_BUCKET` + `FUEL_OPS_S3_REGION` (or `AWS_REGION`) | `FileStorageService` — proof-of-delivery object storage and presigned URLs |
| `FUEL_OPS_KMS_KEY_ID` | `TenantCredentialsVault` — KMS envelope encryption for per-tenant integration credentials |
| both of the above | `MeterTicketOCRService` — Textract meter-ticket OCR (requires file storage) |

If any of these surfaces is meant to be live, set the variables **and** grant the
task role the matching permissions. This is also the one place where moving to
AWS simplifies things: on Fargate the task role replaces static AWS keys
entirely.

### External data providers the agents read

Each of these has a finished adapter. Without its credential the consuming agent
still runs — on no external data — and says so only through a fallback
annotation on its output, so they are worth setting deliberately rather than
discovering later:

| Variable | Consumer, and what it falls back to |
|---|---|
| `OPENWEATHER_API_KEY` or `NOAA_CDO_TOKEN` | Degree-days for the propane / heating-oil consumption models. Absent, forecasts compute without HDD and carry `weather_fallback: true`. Also populates `weather_observations`, which is what the compliance K-factor service calibrates against |
| `OPIS_API_KEY` / `OPIS_API_SECRET` | Live terminal rack prices. Absent, sourcing falls to a tenant-uploaded CSV whose upload path is not built, so every candidate returns `no_price_available` |
| `MAPBOX_ACCESS_TOKEN` / `HERE_API_KEY` / `GOOGLE_MAPS_API_KEY` | Traffic-aware routing — and the credential alone is not enough: the per-tenant `overlay.traffic_aware_routing` flag is seeded **disabled** and `overlay.traffic_provider:{tenant}` must be set. Absent, Haversine at 40 km/h with `traffic_fallback: true` |

Veeder-Root tank telemetry, Geotab GPS, QuickBooks and Stripe are different:
they are polled per tenant by the `IntegrationScheduler` from registered
integration instances, with credentials in the KMS-backed vault. That vault needs
`FUEL_OPS_KMS_KEY_ID` in production. Geotab is the only writer of
`truck_telemetry`, so without a registered instance that index stays empty and
route planning starts every truck from its depot.

**Task role** (least privilege): `kms:GenerateDataKey` + `kms:Decrypt` on the one
CMK with an encryption-context condition on `tenant_id`; `s3:GetObject` /
`PutObject` on the PoD bucket prefix; `textract:AnalyzeDocument` /
`DetectDocumentText`. **Execution role**: ECR pull, CloudWatch Logs,
`secretsmanager:GetSecretValue` on the specific secret ARNs.

## Image and migration flow

CI already builds and checks the image (`docker-image` job: refuses leaked
`.env.*`, venv, `.git`, tests; refuses uid 0; boots against a real
Elasticsearch). Extend that job to push to ECR on the default branch, tagged with
the commit SHA — never `latest`, because a rollback needs a name that still means
the same bytes tomorrow.

Deploy order, all of it from the runbook, mapped onto AWS primitives:

1. **Snapshot.** Aurora manual snapshot. No companion Elasticsearch export is
   needed — every index is rebuildable from Postgres.
2. **Migrate.** `ecs run-task` with the new image and `alembic upgrade head`.
   One task, waited on to completion, exit code checked. Not in the app
   entrypoint — the runbook and the Dockerfile both say why.
3. **Verify the chain.** `ecs run-task … python -m scripts.check_migrations`.
4. **Deploy.** Update the service to the new task-definition revision. Default
   rolling update — no special percentages needed.
5. **Gate.** `curl /health/ready` → 200 and `curl /api/orders` → 401. A 200 on
   `/api/orders` without a session means the tenant guard is not enforcing:
   roll back. Also confirm exactly one task logged `is now the sweep leader`.
6. **Rollback.** Update the service back to the previous task-definition
   revision. Sufficient whenever the schema did not change. If a migration must
   be undone, restore the snapshot — and note that Aurora PITR/snapshot restore
   produces a new endpoint.

Migrations and the running app are **not** decoupled: no expand/contract
convention is in place and the chain already contains a destructive revision.
With a rolling deploy the old and new code now run **simultaneously** against the
migrated schema, which the stop-then-start ordering used to avoid. So the
migration must be backward-compatible with the outgoing revision for the length
of the rollout, or the rollout has to be serialised — set
`minimumHealthyPercent: 0` / `maximumPercent: 100` for that one deploy and accept
the downtime window deliberately.

**This is the one thing leader election did not fix.** It made the *sweeps* safe
to overlap; it says nothing about schema compatibility. Treat a destructive
migration as a scheduled, serialised deploy.

## Observability

`OTEL_ENDPOINT` is already read by the app (OTLP gRPC, `:4317`). Run the **ADOT
collector as a sidecar** in the same task definition and point `OTEL_ENDPOINT` at
`http://localhost:4317`; export traces to X-Ray and metrics to CloudWatch or AMP.
Traces are simply disabled when the variable is unset, so this is additive.

Logs go to CloudWatch via the `awslogs` driver. `PYTHONUNBUFFERED=1` is already
set in the image, so a crash reaches the log stream instead of dying in a pipe
buffer.

Alarms worth having on day one, chosen because each one maps to a failure this
codebase can actually produce silently:

| Alarm | Why it matters here |
|---|---|
| Outbox backlog (`published_at IS NULL` count) rising | The ES projection is falling behind Postgres. The relay standing down wrongly, or ES rejecting writes, both look like this and neither is otherwise visible |
| Count of tasks logging `is now the sweep leader` > 1 at once | Two leaders means the sweeps are racing. Should be impossible via the advisory lock, so this fires only if the lock key stopped being stable or two deployments share one Aurora cluster |
| No task logging `is now the sweep leader` for > 1 min | Nobody is running the periodic jobs. A wedged lock session, or every task failing election |
| Log filter on `lost the connection holding the relay lock` | Correlates with Aurora failovers and with an accidental `idle_in_transaction_session_timeout` |
| HTTP 503 with `COMMERCE_INVOICE_NUMBERING_UNAVAILABLE` | Postgres is reachable but the invoice counter is not. Invoices stay in `draft`, which is safe, but finalization is broken |
| ALB 5xx, target-group unhealthy-host count, ECS task restart count | A single restart is now survivable, but a rising restart count across tasks is the crash-loop signal |
| Aurora CPU, `DatabaseConnections`, `FreeableMemory`, failover events | — |
| ElastiCache evictions and `CurrConnections` | Evictions mean conversation memory is being dropped early |

## Sizing, from the measured baseline

The measured numbers are laptop numbers — treat the *shape* as informative and
the absolutes as meaningless (see [docs/load-test-baseline.md](load-test-baseline.md)).
The shape says: one process saturates around 150 rps mixed read/write, writes
cost ~10× reads, and throughput stops responding to concurrency well before
latency does.

Start at **1 vCPU / 2 GB** per API task, `desiredCount: 2` across two AZs, and
watch p95 rather than CPU. Because ES calls block the event loop (fact 2), CPU
will look under-used while latency climbs — CPU is the wrong signal here. Going to
2 vCPU will not help one event loop much; more tasks will.

Autoscaling is now safe and was not before. Target **ALB request count per
target** or **target response time**, not CPU. Two bounds to set deliberately:

- `maxCapacity` against Aurora `max_connections` — 15 connections per task, so
  price the ceiling rather than discovering it.
- `minCapacity: 2`, so a single task failure is never a full outage.

## Not covered, and decisions that are yours

- **No infrastructure-as-code in this repo.** There is no Terraform or CDK here.
  This document is the target; expressing it as code is a separate piece of work,
  and it should be done once, not per environment.
- **The Next.js dispatcher UI and the Expo driver app.** Neither has a release
  pipeline. The UI is a standard `next build` — CloudFront + S3, Amplify Hosting,
  or its own Fargate service — and its origin must appear in `CORS_ORIGINS` and
  `SUPERTOKENS_WEBSITE_DOMAIN`.
- **Whether to spread background work across replicas.** Today one replica runs
  all of it, because a session-scoped lock per job would exhaust the connection
  pool. If the sweeps ever become a bottleneck, the answer is transaction-scoped
  locks (`pg_try_advisory_xact_lock`) taken per cycle, which holds no permanent
  connection and lets different jobs land on different replicas. Not needed for
  safe deploys, which is why it was not built.
- **Whether a destructive migration gets a serialised deploy.** A rolling deploy
  now runs old and new code together against the migrated schema. That is a
  per-migration judgement, not a standing setting.
- **Whether to stay on Elastic Cloud long term.** Staying is the low-risk choice
  and the one this document assumes. Moving to OpenSearch is a real project;
  price it before agreeing to it.
- **Whether the ES-only snapshot window is acceptable.** Anything written to
  `customer_tanks` / `truck_compartments` / `fuel_stations` between scheduled
  exports is lost with the cluster. `truck_compartments.last_loaded_product`
  gates a cross-contamination check, so weigh that one first.
- **Multi-tenancy and multi-region.** Single region, single environment per
  account assumed throughout. Nothing here has been designed for a second
  region.
- **WAF, Shield, and rate limiting at the edge.** The app rate-limits per IP in
  process (`RATE_LIMIT_REQUESTS_PER_MINUTE`), which behind an ALB sees the
  client IP only via `X-Forwarded-For`. Whether that is trusted, and whether a
  WAF rate rule should sit in front, is an open decision.
- **Cost.** No estimate here. The shape (one small Fargate task, one Aurora
  cluster, one small Redis replication group, plus existing Elastic Cloud and
  SuperTokens subscriptions) is enough to price, but the ACU/instance sizing
  needs a load test against the real environment first.
