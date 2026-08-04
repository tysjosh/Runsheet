# Deploy runbook — Runsheet backend

Ordered procedure for putting a build into staging or production. Every step is
either a command you can run or a check you can read; nothing here is "verify it
looks right".

Before this existed the only deployment artifacts in the repo were a `Procfile`
and a local-Postgres `docker-compose.yml`, so a deploy meant somebody's shell
history.

## 0. Preconditions

The backend **refuses to start** outside development without all of these. That
is deliberate — each one silently breaks a correctness guarantee if absent, so
the failure was moved to startup where it is visible.

| Variable | Why it is required |
|---|---|
| `ELASTIC_ENDPOINT`, `ELASTIC_API_KEY` | Search and every projection read |
| `DATABASE_URL` | Invoice numbering, idempotency uniqueness, credit-check row lock. A dormant persistence layer silently loses all three |
| `REDIS_URL` | Session store (when `SESSION_STORE_TYPE=redis`) |
| `SUPERTOKENS_CONNECTION_URI`, `SUPERTOKENS_API_KEY` | Session verification. `main.py` fails closed at import if unset, in every environment |
| `CORS_ORIGINS` | Production rejects any `localhost` / `127.0.0.1` origin |
| `COMMERCE_DUAL_WRITE_POSTGRES=true` | Required when `COMMERCE_BACKBONE_ENABLED=true`; invoice numbering is gated on it |

Not enforced at startup but required for the surfaces that read them — check
against what you are enabling:

| Variable | Surface that degrades quietly without it |
|---|---|
| `VOICE_API_KEY_SALT` | Dinee voice Surface B key verification (defaults to an empty salt) |

`Runsheet-backend/.env.example` is the full annotated template. Supply values as
environment variables, not as a mounted `.env` file: `.dockerignore` excludes
every `.env.*` except the placeholder template, so a mounted file is the only way
a credential reaches a layer.

## 1. Build the image

```sh
cd Runsheet-backend
docker build --platform linux/amd64 -t runsheet-backend:"$(git rev-parse --short HEAD)" .
```

Tag with the commit SHA, never `latest`. A rollback needs a name that still
means the same bytes tomorrow.

The image is Python 3.11 to match CI and the development venv; `requirements.txt`
is fully pinned or major-bounded so a rebuild of the same SHA resolves the same
tree. Two stages, so no compiler ships. Runs as uid 10001.

## 2. Snapshot the database — **required before any migration**

```sh
pg_dump --format=custom --no-owner --file "runsheet-$(date -u +%Y%m%dT%H%M%SZ).dump" "$DATABASE_URL"
```

Verify the dump is readable before continuing. A dump you have not listed is a
hope, not a backup:

```sh
pg_restore --list runsheet-*.dump | head
```

> ⚠️ **The migration chain contains a destructive revision.**
> `0007_drop_shipments_current` **drops the `shipments_current` table**. It is
> irreversible without this dump. Shipments now live only in Elasticsearch.

## 3. Apply migrations — once, from one place

```sh
docker run --rm \
  -e ENVIRONMENT="$ENVIRONMENT" \
  -e DATABASE_URL="$DATABASE_URL" \
  -e ELASTIC_ENDPOINT="$ELASTIC_ENDPOINT" -e ELASTIC_API_KEY="$ELASTIC_API_KEY" \
  -e SUPERTOKENS_CONNECTION_URI="$SUPERTOKENS_CONNECTION_URI" \
  -e SUPERTOKENS_API_KEY="$SUPERTOKENS_API_KEY" \
  -e REDIS_URL="$REDIS_URL" -e CORS_ORIGINS="$CORS_ORIGINS" \
  runsheet-backend:"$SHA" alembic upgrade head
```

Run this as a discrete job, not from an application container's entrypoint: N
replicas starting together would race the same migration, and one of these
revisions drops a table.

Confirm the chain is at head and linear:

```sh
docker run --rm -e ... runsheet-backend:"$SHA" python -m scripts.check_migrations
```

## 4. Deploy the application

Start the image with the environment from step 0. `PORT` defaults to 8080.

Point the platform's readiness gate at **`/health/ready`**, not `/health/live`
and not `/health`:

- `/health/ready` — checks Elasticsearch and returns **503** when a dependency
  is unreachable. This is the rollout gate.
- `/health/live` — liveness only. Returns 200 with every datastore down, which
  is correct for restart decisions and useless for shifting traffic.
- `/health`, `/api/health` — banner endpoints. Do not gate on them.

The image also carries a `HEALTHCHECK` against `/health/ready` with a 45s
start period; first readiness locally is ~20s.

## 5. Verify before shifting traffic

```sh
curl -fsS "$BASE/health/ready"            # 200, elasticsearch healthy
curl -o /dev/null -w '%{http_code}\n' "$BASE/api/orders"   # 401 — auth is enforced
```

A `200` on `/api/orders` without a session means the tenant guard is not
enforcing and the deploy must be rolled back.

Then exercise one authenticated read and one write. The invoice path is the one
worth checking by hand, because its failure mode is a silently defective record:
finalize a draft invoice and confirm the response carries an `invoice_number`. A
`503 COMMERCE_INVOICE_NUMBERING_UNAVAILABLE` means Postgres is reachable but the
counter is not — the invoice is left in `draft`, which is the safe outcome, and
the deploy is not healthy.

## 6. Rollback

```sh
# Application: redeploy the previous SHA. The image is immutable, so this is
# sufficient whenever the schema did not change.
docker run ... runsheet-backend:"$PREVIOUS_SHA"
```

If a migration must be undone, restore the step-2 dump. Do **not** rely on
`alembic downgrade` past `0007_drop_shipments_current`: the table is dropped and
the rows are gone, so the downgrade recreates an empty table and the data is only
in the dump.

## Not covered here

- **Infrastructure provisioning.** There is no Terraform/CDK/Kubernetes in this
  repo; the compute, managed Postgres, Elasticsearch and Redis are provisioned
  out of band.
- **The Next.js dispatcher UI and the Expo driver app.** Both build from their
  own directories; the driver app has `eas.json` for EAS builds. Neither has a
  release pipeline in CI.
- **Backup schedule and restore drills.** Step 2 is a pre-migration snapshot,
  not a backup policy. Nobody has yet restored this database from a dump.
- **Load characteristics.** No load test has been run against this stack, so the
  replica count and the outbox relay's drain rate under load are unmeasured.
