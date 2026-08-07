#!/usr/bin/env bash
#
# Provision (and tear down) the Runsheet STAGING environment on AWS.
#
# This is not infrastructure-as-code, and it is not pretending to be. There is no
# Terraform or CDK in this repository; the AWS deployment strategy
# (docs/aws-deployment-strategy.md) says expressing the target as code is a
# separate piece of work. This script is the deliberate middle ground: every
# billable resource staging depends on is created here, by name, in one file that
# is committed — so the environment is inspectable and removable even though it
# is not declarative.
#
#   ./scripts/staging_aws.sh plan       # print what would be created + cost
#   ./scripts/staging_aws.sh up         # create everything (idempotent)
#   ./scripts/staging_aws.sh deploy     # build + push image, roll the service
#   ./scripts/staging_aws.sh migrate    # alembic upgrade head as a one-shot task
#   ./scripts/staging_aws.sh verify     # readiness + auth + TLS + redis
#   ./scripts/staging_aws.sh status     # what exists right now
#   ./scripts/staging_aws.sh frontend-env  # env vars to paste into Vercel
#   ./scripts/staging_aws.sh down       # DESTROY everything this script made
#
# Pass DOMAIN to get TLS. Without it the ALB serves plaintext HTTP on its own DNS
# name and an HTTPS frontend cannot call it at all:
#
#   DOMAIN=runsheetops.com ./scripts/staging_aws.sh up
#
# Idempotent: every step checks for the resource before creating it, so a re-run
# after a failure continues rather than duplicating. Safe to run repeatedly.
#
# ---------------------------------------------------------------------------
# Shape, and why it is not the shape in docs/aws-deployment-strategy.md
# ---------------------------------------------------------------------------
# That document describes the PRODUCTION target: Aurora PostgreSQL, ElastiCache
# Redis, private subnets behind NAT, ALB with ACM. This is staging, provisioned
# for cost and speed, and it departs in TWO places on purpose:
#
#   * RDS db.t4g.micro, single-AZ, instead of Aurora Serverless v2. No failover.
#   * Tasks run in PUBLIC subnets with assignPublicIp=ENABLED, so there is no NAT
#     gateway (~$32/month plus data processing). The task security group allows no
#     inbound except from the ALB. The document's target is private subnets.
#
# Two things that look like departures and are not:
#
#   * Redis is ELASTICACHE, in the exact shape the document specifies: a replication
#     group with CLUSTER MODE DISABLED (all six consumers build the client with
#     redis.asyncio.from_url, which is the plain client — a cluster-mode cluster
#     answers MOVED redirects that only RedisCluster follows, so it would fail at
#     runtime, per key, not at startup), two nodes across AZs with automatic
#     failover, encryption in transit and at rest, an AUTH token, and REDIS_URL
#     pointing at the PRIMARY endpoint so it follows a failover. Because the URL
#     carries the token it is a Secrets Manager secret, not a plain environment
#     variable.
#
#     Redis previously ran as a per-task SIDECAR here. That was a data-loss shape,
#     not a cheap substitution: the sidecar is wiped on every task replacement and
#     scheduling/services/job_id_generator.py does INCR on scheduling:job_id_counter,
#     so a wipe restarted job ids at 1 and JobService's index_document(
#     JOBS_CURRENT_INDEX, job_id, doc) — an upsert keyed on job_id — silently
#     overwrote the existing JOB_1. Full detail at the container definition below.
#   * SuperTokens is the MANAGED SaaS core, reached over HTTPS. Not a sidecar.
#     This script briefly ran a self-hosted supertokens-postgresql container against
#     the same RDS instance, which contradicted a resolved architectural decision:
#     .kiro/specs/supertokens-auth-migration says "Deployment is the SuperTokens
#     managed SaaS core — there are no container/docker tasks for the core". It also
#     caused three separate deploy failures on its own (a guessed image reference, a
#     wget health check on an image that ships only curl, and a second libpq-form
#     database secret) and made staging unrepresentative of production's auth
#     topology, which is the one thing staging is for.
#
# TLS is CONDITIONAL ON $DOMAIN, and everything downstream of it follows.
#
# With DOMAIN set: a Route 53 hosted zone, an ACM certificate for api.$DOMAIN
# validated by a CNAME this script writes itself, a :443 listener on a TLS 1.2+
# policy, :80 permanently redirecting to it, and an ALIAS record. The task
# definition's SUPERTOKENS_API_DOMAIN / SUPERTOKENS_WEBSITE_DOMAIN / CORS_ORIGINS all
# derive from the same two origin helpers, so the scheme cannot drift between them.
#
# Without it: plaintext HTTP on the ALB's own DNS name, session cookies in the clear,
# and — the part that surprises people — a browser on an HTTPS page (Vercel) cannot
# call the API at all. Mixed-content blocking kills every fetch and every ws://
# socket, so the UI loads and then does nothing.
#
# The frontend host (app.$DOMAIN) is NOT managed here. Vercel issues its own
# certificate and owns that record.
#
# Everything is tagged Project=runsheet,Environment=staging. `down` finds
# resources by name, not by tag, but the tags make an orphan obvious in the
# console next to the unrelated "cleanup" project that shares this account.

set -euo pipefail

# AWS CLI v2 pipes output through a pager when stdout is a TTY, so the first
# command that printed JSON blocked on ``less`` forever and the script appeared to
# hang after "==> ECR repository" with nothing created. Disabled here rather than
# per-call so no future command can reintroduce it.
export AWS_PAGER=""

# The region travels in the environment, and there is deliberately NO ``aws()``
# shell-function wrapper to add ``--region``.
#
# There was one, and it broke every error guard in this script. ``set -e`` suppresses
# errexit for a command that is part of an ``A && B || C`` list — but when A is a
# SHELL FUNCTION, the failing command inside the function body triggers errexit there
# first, and the whole script dies before the ``||`` at the call site can run. So
# ``aws ... || true`` and ``aws ... && ok || ok`` were both inert: an already-existing
# security-group rule (exit 254, InvalidPermission.Duplicate) killed the run instead
# of being tolerated. It cost two silent partial provisions, each dying at a different
# step depending on which resource already existed. Calling the real binary restores
# the intended behaviour.
export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-2}"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AWS_REGION="${AWS_REGION:-us-east-2}"
PROJECT="runsheet"
ENV_NAME="staging"
PREFIX="${PROJECT}-${ENV_NAME}"

VPC_ID="${VPC_ID:-vpc-04f074f40e472a5f7}"
SUBNETS="subnet-0c38cce5324421875 subnet-0a42bb2359d333bb4 subnet-0d3dd3842c442bbbf"
SUBNETS_CSV="$(echo "$SUBNETS" | tr ' ' ',')"

ECR_REPO="${PREFIX}-backend"
CLUSTER="${PREFIX}"
SERVICE="${PREFIX}-api"
TASK_FAMILY="${PREFIX}-api"
LOG_GROUP="/ecs/${PREFIX}"

DB_ID="${PREFIX}-pg"
DB_NAME="runsheet"
DB_USER="runsheet"
DB_CLASS="db.t4g.micro"
DB_ENGINE_VERSION="16.14"
DB_STORAGE_GB="20"
DB_SUBNET_GROUP="${PREFIX}-db-subnets"

#: ElastiCache Redis, cluster mode DISABLED. See the header: this is the one shape
#: docs/aws-deployment-strategy.md makes non-negotiable, because every consumer uses
#: the plain redis.asyncio client and a cluster-mode cluster would fail per-key at
#: runtime rather than at startup.
#:
#: Two nodes (one primary, one replica) so --automatic-failover-enabled and
#: --multi-az-enabled are legal; the cache subnet group spans three AZs.
REDIS_ID="${PREFIX}-redis"
REDIS_NODE_TYPE="cache.t4g.micro"
REDIS_ENGINE_VERSION="7.1"
REDIS_SUBNET_GROUP="${PREFIX}-redis-subnets"
SG_REDIS="${PREFIX}-redis-sg"

#: The Next.js dispatcher UI, as a second ECS service behind the SAME load balancer.
#:
#: Host-based routing rather than a second ALB: api.$DOMAIN falls through to the
#: default action and app.$DOMAIN matches a host-header rule, which saves the ~$17
#: a month a second ALB would cost and keeps both hosts on one certificate story.
#:
#: The UI is NOT static. S3 + CloudFront was the intended cheap answer and does not
#: work here — verified, see runsheet/Dockerfile for the build output. Eleven routes
#: are server-rendered and there is no generateStaticParams anywhere, so it runs as
#: a Node server from Next's standalone output.
UI_ECR_REPO="${PREFIX}-ui"
UI_SERVICE="${PREFIX}-ui"
UI_TASK_FAMILY="${PREFIX}-ui"
UI_TG_NAME="${PREFIX}-ui-tg"
UI_LOG_GROUP="/ecs/${PREFIX}-ui"
SG_UI="${PREFIX}-ui-sg"
UI_PORT="3000"
UI_CPU="512"
UI_MEM="1024"

ALB_NAME="${PREFIX}-alb"
TG_NAME="${PREFIX}-tg"
SG_ALB="${PREFIX}-alb-sg"
SG_TASK="${PREFIX}-task-sg"
SG_DB="${PREFIX}-db-sg"

#: Custom domain, and the whole TLS story is conditional on it. Unset, this script
#: behaves exactly as it did before: HTTP on the ALB's own DNS name, no certificate,
#: no Route 53. Set, it additionally creates the hosted zone, an ACM certificate
#: validated by DNS, a :443 listener, a :80 -> :443 redirect, and an ALIAS record.
#:
#:   DOMAIN=runsheetops.com ./scripts/staging_aws.sh up
#:
#: The domain REGISTRATION is deliberately not automated. register-domain writes
#: real registrant contact details into the ICANN WHOIS record and bills a
#: non-refundable year, so it is a human decision made once, not a step in an
#: idempotent provisioning script that people re-run after failures.
#: DOMAIN is what the HOSTS hang off; ZONE_DOMAIN is the REGISTRABLE domain that owns
#: the Route 53 hosted zone. They are usually the same and deliberately separable:
#:
#:   DOMAIN=staging.runsheetops.com  ->  api.staging.runsheetops.com
#:                                       app.staging.runsheetops.com
#:                                       records written into the runsheetops.com zone
#:
#: Without this split, pointing DOMAIN at a subdomain would look for a hosted zone
#: named staging.runsheetops.com, not find one, create it, and then need NS records
#: delegated from the parent before ACM validation could ever succeed. One zone for
#: the registrable domain avoids that entirely.
#:
#: Staging deliberately does NOT sit on api./app. directly. Those are production's
#: names, and an environment whose own header says not to put real customer data in it
#: should not be the thing answering on them.
DOMAIN="${DOMAIN:-}"
#: Last two labels of DOMAIN. Override for a multi-part public suffix (.co.uk etc.),
#: where the registrable domain is three labels rather than two.
ZONE_DOMAIN="${ZONE_DOMAIN:-$(echo "${DOMAIN}" | awk -F. 'NF>1{print $(NF-1)"."$NF}')}"
API_HOST="${DOMAIN:+api.${DOMAIN}}"
APP_HOST="${DOMAIN:+app.${DOMAIN}}"

SECRET_DB="${PREFIX}/database-url"
SECRET_GEMINI="${PREFIX}/gemini-api-key"
SECRET_ST="${PREFIX}/supertokens-api-key"
#: REDIS_URL is a SECRET, not a plain environment variable, because TLS + AUTH puts
#: the auth token inside the URL: rediss://:<token>@<primary-endpoint>:6379/0
SECRET_REDIS="${PREFIX}/redis-url"

#: SuperTokens Cloud connection details for staging. Supplied by the environment so
#: a staging-specific core can be used without editing this file:
#:
#:   ST_URI=https://st-stg-xxxx.aws.supertokens.io \
#:   ST_KEY=... ./scripts/staging_aws.sh up
#:
#: When unset these fall back to the DEVELOPMENT core in .env.development, which
#: works but means staging and development share one identity store — the same users
#: and sessions in both. ``up`` warns loudly when it does that.
ST_URI="${ST_URI:-}"
ST_KEY="${ST_KEY:-}"

TASK_CPU="1024"
TASK_MEM="2048"

TAGS="Key=Project,Value=${PROJECT} Key=Environment,Value=${ENV_NAME}"

# All four write to STDERR, deliberately. ``ensure_sg`` and friends return an id on
# stdout for command substitution, so a helper that logged to stdout would have its
# messages captured AS the id — which is exactly what happened: the security-group
# id came back as "  ok security group ...\nsg-0abc" and every subsequent
# --group-id was malformed.
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mFATAL\033[0m %s\n' "$*" >&2; exit 1; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
cmd_plan() {
  # Built here rather than with ${DOMAIN:+...}${DOMAIN:-...} inside the heredoc.
  # That trick printed the domain itself in the DOMAIN-set case, because
  # ${VAR:-default} expands to VAR's VALUE when set, not to nothing.
  local _plan_tls _plan_zone=""
  if [ -n "$DOMAIN" ]; then
    _plan_tls="HTTPS :443 (ACM certificate, free) + :80 -> :443 redirect"
    _plan_zone="
  Route 53 hosted zone ${DOMAIN}          ~\$0.50/month
    ACM certificate for ${API_HOST}       free"
  else
    _plan_tls="HTTP :80 only, no certificate — see the header"
  fi
  cat <<PLAN
Region ................ ${AWS_REGION}
Account ............... ${ACCOUNT_ID}
VPC ................... ${VPC_ID} (default VPC, public subnets, no NAT)

Billable resources this creates:

  RDS ${DB_CLASS} postgres ${DB_ENGINE_VERSION}, ${DB_STORAGE_GB}GB gp3, single-AZ
                                              ~\$13/month
  ElastiCache ${REDIS_NODE_TYPE} redis ${REDIS_ENGINE_VERSION}, 2 nodes,
    cluster mode disabled, multi-AZ, TLS+AUTH      ~\$23/month
  Application Load Balancer                   ~\$17/month + LCU
    ${_plan_tls}${_plan_zone}
  Fargate task, ${TASK_CPU} CPU / ${TASK_MEM} MB, 1 replica     ~\$36/month
  Secrets Manager, 4 secrets                  ~\$1.60/month
  CloudWatch Logs, ECR storage                cents
                                              ------------
                                              ~\$90/month

Not created (and why):
  NAT gateway        tasks get public IPs in public subnets instead   -\$32/mo
  Aurora             db.t4g.micro is enough for staging              -\$50/mo+
  Domain registration  NEVER automated. register-domain writes real WHOIS
                     contact details and bills a non-refundable year, so it is
                     not a step in a script people re-run after failures.
  SuperTokens core   managed SaaS (SuperTokens Cloud), not self-hosted
  Redis sidecar      REPLACED by ElastiCache above. It was ephemeral and
                     per-task, and the scheduling job-id counter lived in it.
  CloudWatch alarms  none, for ANY resource here. The strategy document lists
                     eight day-one alarms and several are log-metric filters on
                     application output (sweep leader, outbox backlog); wiring
                     two ElastiCache alarms and nothing else would misrepresent
                     the coverage. Monitoring is its own piece of work.

SuperTokens core .... ${ST_URI:-$(grep -E '^SUPERTOKENS_CONNECTION_URI=' "$(dirname "$0")/../.env.${ENV_NAME}" 2>/dev/null | cut -d= -f2- || true)}
                      ${ST_URI:+(from ST_URI)}${ST_URI:-$([ -f "$(dirname "$0")/../.env.${ENV_NAME}" ] && echo "(from .env.${ENV_NAME})" || echo "NONE FOUND — will fall back to the DEVELOPMENT core")}

Tear down with:  ./scripts/staging_aws.sh down
PLAN
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
sg_id() {
  aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$1" "Name=vpc-id,Values=${VPC_ID}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null | grep -v '^None$' || true
}

ensure_sg() {
  local name="$1" desc="$2" existing
  existing="$(sg_id "$name")"
  if [ -n "$existing" ]; then ok "security group $name ($existing)"; echo "$existing"; return; fi
  local created
  created="$(aws ec2 create-security-group --group-name "$name" --description "$desc" \
      --vpc-id "${VPC_ID}" --tag-specifications "ResourceType=security-group,Tags=[{Key=Project,Value=${PROJECT}},{Key=Environment,Value=${ENV_NAME}}]" \
      --query GroupId --output text)"
  ok "created security group $name ($created)"
  echo "$created"
}

alb_dns() {
  aws elbv2 describe-load-balancers --names "${ALB_NAME}" \
    --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null | grep -v '^None$' || true
}

db_endpoint() {
  aws rds describe-db-instances --db-instance-identifier "${DB_ID}" \
    --query 'DBInstances[0].Endpoint.Address' --output text 2>/dev/null | grep -v '^None$' || true
}

# Cluster mode is disabled, so there is exactly one node group and the address to
# use is its PrimaryEndpoint. Reading the primary (not a node's own endpoint, and not
# the reader endpoint) is what makes REDIS_URL survive a failover: ElastiCache
# repoints this DNS name at the promoted replica.
redis_primary() {
  aws elasticache describe-replication-groups --replication-group-id "${REDIS_ID}" \
    --query 'ReplicationGroups[0].NodeGroups[0].PrimaryEndpoint.Address' \
    --output text 2>/dev/null | grep -v '^None$' || true
}

redis_status() {
  aws elasticache describe-replication-groups --replication-group-id "${REDIS_ID}" \
    --query 'ReplicationGroups[0].Status' --output text 2>/dev/null | grep -v '^None$' || true
}

# ---------------------------------------------------------------------------
# DNS and TLS
#
# The public origins. Every consumer of these — the task definition, `verify`, and
# the Vercel env vars printed by `frontend-env` — reads them from here rather than
# rebuilding "http://$alb" locally, which is how the scheme got hard-coded to http
# in four places the first time round.
# ---------------------------------------------------------------------------
api_origin() { if [ -n "$DOMAIN" ]; then echo "https://${API_HOST}"; else echo "http://$(alb_dns)"; fi; }
app_origin() { if [ -n "$DOMAIN" ]; then echo "https://${APP_HOST}"; else echo "http://$(alb_dns)"; fi; }

#: The zone for ZONE_DOMAIN, not DOMAIN. api.staging.runsheetops.com is a record
#: inside the runsheetops.com zone, not a zone of its own.
zone_id() {
  aws route53 list-hosted-zones-by-name --dns-name "${ZONE_DOMAIN}." \
    --query "HostedZones[?Name=='${ZONE_DOMAIN}.'].Id | [0]" --output text 2>/dev/null \
    | grep -v '^None$' | sed 's|/hostedzone/||' || true
}

#: The certificate for API_HOST, whatever its status. FAILED ones are excluded
#: deliberately: this account already contains a FAILED cleanupng.com certificate
#: whose DNS validation never completed, and reusing a FAILED certificate can never
#: succeed — ACM will not retry validation on it, so it has to be replaced.
cert_arn() {
  aws acm list-certificates --includes keyTypes=RSA_2048 \
    --query "CertificateSummaryList[?DomainName=='${API_HOST}'].CertificateArn | [0]" \
    --output text 2>/dev/null | grep -v '^None$' || true
}

cert_status() {
  aws acm describe-certificate --certificate-arn "$1" \
    --query 'Certificate.Status' --output text 2>/dev/null || true
}

#: Certificate for an arbitrary host in this domain. Used for APP_HOST; API_HOST has
#: its own path above because it is also the listener's DEFAULT certificate.
cert_arn_for() {
  aws acm list-certificates \
    --query "CertificateSummaryList[?DomainName=='$1'].CertificateArn | [0]" \
    --output text 2>/dev/null | grep -v '^None$' || true
}

alb_arn() {
  aws elbv2 describe-load-balancers --names "${ALB_NAME}" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null | grep -v '^None$' || true
}

https_listener_arn() {
  aws elbv2 describe-listeners --load-balancer-arn "$(alb_arn)" \
    --query 'Listeners[?Port==`443`].ListenerArn | [0]' --output text 2>/dev/null | grep -v '^None$' || true
}

secret_value() {
  aws secretsmanager get-secret-value --secret-id "$1" --query SecretString --output text 2>/dev/null || true
}

ensure_secret() {
  local name="$1" value="$2"
  if aws secretsmanager describe-secret --secret-id "$name" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value --secret-id "$name" --secret-string "$value" >/dev/null
    ok "secret $name (updated)"
  else
    aws secretsmanager create-secret --name "$name" --secret-string "$value" \
      --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" >/dev/null
    ok "secret $name (created)"
  fi
}

secret_arn() {
  aws secretsmanager describe-secret --secret-id "$1" --query ARN --output text
}

#: Resolve the SuperTokens Cloud core, setting ST_URI and ST_KEY.
#:
#: Both ``up`` (which stores the API key as a secret) and ``deploy`` (which bakes the
#: connection URI into the task definition) need these, so it lives in one place
#: rather than being resolved twice and drifting.
#:
#: The API key is a REAL managed-core credential and is never generated. An earlier
#: version minted a random one, which only made sense while a self-hosted core was in
#: the task — against SuperTokens Cloud it would have failed every session
#: verification, and the app would have looked broken for an unrelated reason.
resolve_supertokens() {
  #: Resolution order: explicit env vars, then .env.<ENV_NAME>, then .env.development.
  #:
  #: The middle step is the one that matters and was missing. This used to read
  #: .env.development directly, so staging silently shared development's identity
  #: store — same users, same sessions — even once a staging core existed. Reading
  #: .env.staging first makes the per-environment file the natural place to put it,
  #: and the development fallback stays only so a fresh environment can still boot.
  local env_file="$(dirname "$0")/../.env.${ENV_NAME}"
  local dev_file="$(dirname "$0")/../.env.development"

  #: Braces are load-bearing for the reader, not the shell: || and && bind equally and
  #: left to right, so the unbraced form already means "(missing either) and file
  #: exists". Spelling it out stops the next person from "fixing" it into
  #: "missing URI, or (missing key and file exists)".
  if { [ -z "$ST_URI" ] || [ -z "$ST_KEY" ]; } && [ -f "$env_file" ]; then
    ST_URI="${ST_URI:-$(grep -E '^SUPERTOKENS_CONNECTION_URI=' "$env_file" 2>/dev/null | cut -d= -f2-)}"
    ST_KEY="${ST_KEY:-$(grep -E '^SUPERTOKENS_API_KEY=' "$env_file" 2>/dev/null | cut -d= -f2-)}"
    [ -n "$ST_URI" ] && [ -n "$ST_KEY" ] && ok "supertokens core from .env.${ENV_NAME}"
  fi

  if [ -z "$ST_URI" ] || [ -z "$ST_KEY" ]; then
    [ -f "$dev_file" ] || die "no ST_URI/ST_KEY, no .env.${ENV_NAME}, no .env.development"
    ST_URI="${ST_URI:-$(grep -E '^SUPERTOKENS_CONNECTION_URI=' "$dev_file" | cut -d= -f2-)}"
    ST_KEY="${ST_KEY:-$(grep -E '^SUPERTOKENS_API_KEY=' "$dev_file" | cut -d= -f2-)}"
    warn "using the DEVELOPMENT SuperTokens core — ${ENV_NAME} shares development's"
    warn "identity store (same users, same sessions). Put a ${ENV_NAME}-specific core"
    warn "in .env.${ENV_NAME}, or pass ST_URI and ST_KEY."
  fi
  [ -n "$ST_URI" ] || die "no SuperTokens connection URI (set ST_URI)"
  [ -n "$ST_KEY" ] || die "no SuperTokens API key (set ST_KEY)"
  export ST_URI ST_KEY
}

# ---------------------------------------------------------------------------
# up: networking, database, load balancer, roles, secrets
# ---------------------------------------------------------------------------
cmd_up() {
  log "ECR repository"
  if aws ecr describe-repositories --repository-names "${ECR_REPO}" >/dev/null 2>&1; then
    ok "ecr ${ECR_REPO}"
  else
    aws ecr create-repository --repository-name "${ECR_REPO}" \
      --image-scanning-configuration scanOnPush=true \
      --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" >/dev/null
    ok "created ecr ${ECR_REPO}"
  fi

  log "Security groups"
  local alb_sg task_sg db_sg redis_sg
  alb_sg="$(ensure_sg "${SG_ALB}"  "Runsheet staging ALB: public HTTP in")"
  task_sg="$(ensure_sg "${SG_TASK}" "Runsheet staging Fargate tasks")"
  db_sg="$(ensure_sg "${SG_DB}"   "Runsheet staging RDS: from tasks only")"
  redis_sg="$(ensure_sg "${SG_REDIS}" "Runsheet staging ElastiCache: from tasks only")"

  # ALB accepts public HTTP. No :443 — there is no certificate. See the header.
  aws ec2 authorize-security-group-ingress --group-id "$alb_sg" \
    --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null 2>&1 \
    && ok "alb-sg :80 from world" || ok "alb-sg :80 already allowed"

  # The task port is reachable ONLY from the ALB. This is what keeps a
  # public-subnet task with a public IP from being directly addressable.
  aws ec2 authorize-security-group-ingress --group-id "$task_sg" \
    --protocol tcp --port 8080 --source-group "$alb_sg" >/dev/null 2>&1 \
    && ok "task-sg :8080 from alb-sg only" || ok "task-sg :8080 already allowed"

  aws ec2 authorize-security-group-ingress --group-id "$db_sg" \
    --protocol tcp --port 5432 --source-group "$task_sg" >/dev/null 2>&1 \
    && ok "db-sg :5432 from task-sg only" || ok "db-sg :5432 already allowed"

  # The UI container port, reachable only from the ALB — same containment as the API.
  # The UI needs no inbound access from anything else and no outbound AWS access:
  # browsers call the API directly, so nothing server-side talks to it.
  local ui_sg; ui_sg="$(ensure_sg "${SG_UI}" "Runsheet staging UI tasks")"
  aws ec2 authorize-security-group-ingress --group-id "$ui_sg" \
    --protocol tcp --port "${UI_PORT}" --source-group "$alb_sg" >/dev/null 2>&1 \
    && ok "ui-sg :${UI_PORT} from alb-sg only" || ok "ui-sg :${UI_PORT} already allowed"

  # ElastiCache is reachable only from the tasks. It sits in the same public subnets
  # as the tasks (the documented departure), so the security group is the whole of
  # its network isolation — there is no private subnet behind it. TLS and the AUTH
  # token are the second and third layers.
  aws ec2 authorize-security-group-ingress --group-id "$redis_sg" \
    --protocol tcp --port 6379 --source-group "$task_sg" >/dev/null 2>&1 \
    && ok "redis-sg :6379 from task-sg only" || ok "redis-sg :6379 already allowed"

  log "RDS subnet group"
  if aws rds describe-db-subnet-groups --db-subnet-group-name "${DB_SUBNET_GROUP}" >/dev/null 2>&1; then
    ok "db subnet group ${DB_SUBNET_GROUP}"
  else
    aws rds create-db-subnet-group --db-subnet-group-name "${DB_SUBNET_GROUP}" \
      --db-subnet-group-description "Runsheet staging" --subnet-ids ${SUBNETS} \
      --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" >/dev/null
    ok "created db subnet group"
  fi

  log "RDS instance (this is the slow step, ~5-8 minutes)"
  if aws rds describe-db-instances --db-instance-identifier "${DB_ID}" >/dev/null 2>&1; then
    ok "rds ${DB_ID} exists"
  else
    # Generated here and stored only in Secrets Manager. Never echoed, never a
    # command-line argument to anything but this call.
    local db_password
    db_password="$(python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(32)))')"
    aws rds create-db-instance \
      --db-instance-identifier "${DB_ID}" \
      --db-instance-class "${DB_CLASS}" \
      --engine postgres --engine-version "${DB_ENGINE_VERSION}" \
      --master-username "${DB_USER}" --master-user-password "$db_password" \
      --db-name "${DB_NAME}" \
      --allocated-storage "${DB_STORAGE_GB}" --storage-type gp3 --storage-encrypted \
      --db-subnet-group-name "${DB_SUBNET_GROUP}" \
      --vpc-security-group-ids "$db_sg" \
      --no-publicly-accessible \
      --backup-retention-period 1 \
      --no-multi-az \
      --no-auto-minor-version-upgrade \
      --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" >/dev/null
    ok "creating rds ${DB_ID}"
    # Stash the password immediately; the DATABASE_URL secret is finalised once
    # the endpoint is known, below.
    ensure_secret "${SECRET_DB}-password" "$db_password"
  fi

  log "ElastiCache subnet group"
  if aws elasticache describe-cache-subnet-groups --cache-subnet-group-name "${REDIS_SUBNET_GROUP}" >/dev/null 2>&1; then
    ok "cache subnet group ${REDIS_SUBNET_GROUP}"
  else
    aws elasticache create-cache-subnet-group --cache-subnet-group-name "${REDIS_SUBNET_GROUP}" \
      --cache-subnet-group-description "Runsheet staging" --subnet-ids ${SUBNETS} \
      --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" >/dev/null
    ok "created cache subnet group (3 AZs — multi-AZ needs at least 2)"
  fi

  # Started here, BEFORE the RDS waiter, so the two ~7 minute creations overlap
  # instead of running end to end. Both are waited on below.
  log "ElastiCache replication group (~7-10 minutes, starts in parallel with RDS)"
  if [ -n "$(redis_status)" ]; then
    ok "elasticache ${REDIS_ID} exists ($(redis_status))"
  else
    # AUTH token constraints: 16-128 printable characters, and ElastiCache rejects
    # '/', '"', '@' and spaces. Restricting to alphanumerics satisfies that AND
    # keeps the token safe to embed in a URL without percent-encoding, which matters
    # because from_url would otherwise mis-parse it.
    local auth_token
    auth_token="$(python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(40)))')"
    # NO --cluster-enabled. That is the whole point: all six consumers construct the
    # client with redis.asyncio.from_url, and only RedisCluster follows the MOVED
    # redirects a cluster-mode cluster returns.
    aws elasticache create-replication-group \
      --replication-group-id "${REDIS_ID}" \
      --replication-group-description "Runsheet staging Redis, cluster mode disabled" \
      --engine redis --engine-version "${REDIS_ENGINE_VERSION}" \
      --cache-node-type "${REDIS_NODE_TYPE}" \
      --num-cache-clusters 2 \
      --automatic-failover-enabled --multi-az-enabled \
      --transit-encryption-enabled --at-rest-encryption-enabled \
      --auth-token "$auth_token" \
      --cache-subnet-group-name "${REDIS_SUBNET_GROUP}" \
      --security-group-ids "$redis_sg" \
      --snapshot-retention-limit 1 \
      --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" >/dev/null
    ok "creating elasticache ${REDIS_ID}"
    # Stashed on its own the way the RDS password is: the REDIS_URL secret cannot be
    # written until the primary endpoint exists, and a re-run after a failure between
    # those two points must be able to rebuild the URL rather than rotate the token.
    ensure_secret "${SECRET_REDIS}-token" "$auth_token"
  fi

  log "Waiting for RDS to become available"
  aws rds wait db-instance-available --db-instance-identifier "${DB_ID}"
  local endpoint; endpoint="$(db_endpoint)"
  ok "rds endpoint ${endpoint}"

  log "Waiting for ElastiCache to become available"
  # Polled rather than ``aws elasticache wait replication-group-available``, whose
  # default budget is 40 attempts at 15s = 10 minutes. A two-node group with
  # transit encryption regularly takes 8-11, so the waiter is a coin flip, and under
  # ``set -e`` a timed-out waiter kills the whole run at the point where the group is
  # nearly ready — leaving a billable cluster and no REDIS_URL secret. 20 minutes.
  local waited=0
  while [ "$(redis_status)" != "available" ]; do
    [ "$waited" -ge 1200 ] && die "elasticache ${REDIS_ID} still $(redis_status) after 20 minutes"
    sleep 20; waited=$((waited + 20))
  done
  local redis_host; redis_host="$(redis_primary)"
  [ -n "$redis_host" ] || die "elasticache ${REDIS_ID} has no primary endpoint"
  ok "redis primary ${redis_host}"

  log "Secrets"
  local db_password
  db_password="$(secret_value "${SECRET_DB}-password")"
  [ -n "$db_password" ] || die "no stored RDS password; delete ${DB_ID} and re-run up"
  ensure_secret "${SECRET_DB}" \
    "postgresql+psycopg://${DB_USER}:${db_password}@${endpoint}:5432/${DB_NAME}"

  # rediss:// — two s. from_url reads the scheme as "TLS" and the empty username with
  # a password as the AUTH token, so TLS and AUTH need no application change at all.
  # The port is the primary endpoint's, and the /0 database index is preserved
  # because a bare host with no path would leave consumers on a different db than
  # development.
  local redis_token
  redis_token="$(secret_value "${SECRET_REDIS}-token")"
  [ -n "$redis_token" ] || die "no stored Redis AUTH token; delete ${REDIS_ID} and re-run up"
  ensure_secret "${SECRET_REDIS}" "rediss://:${redis_token}@${redis_host}:6379/0"

  # Reused from development rather than newly issued: staging needs *a* valid
  # Gemini credential to start (settings refuses staging without one) and issuing
  # a second key is a console round-trip. Rotate independently when staging
  # becomes long-lived.
  local gemini
  gemini="$(grep -E '^GEMINI_API_KEY=' "$(dirname "$0")/../.env.development" | cut -d= -f2- || true)"
  [ -n "$gemini" ] || die "GEMINI_API_KEY not found in .env.development"
  ensure_secret "${SECRET_GEMINI}" "$gemini"

  resolve_supertokens
  ensure_secret "${SECRET_ST}" "$ST_KEY"
  ok "supertokens core ${ST_URI}"

  log "CloudWatch log group"
  aws logs create-log-group --log-group-name "${LOG_GROUP}" >/dev/null 2>&1 \
    && ok "created ${LOG_GROUP}" || ok "${LOG_GROUP} exists"
  aws logs put-retention-policy --log-group-name "${LOG_GROUP}" --retention-in-days 14 >/dev/null
  ok "retention 14 days"

  log "IAM roles"
  ensure_execution_role
  ensure_task_role

  log "Load balancer"
  local alb_arn tg_arn
  if [ -n "$(alb_dns)" ]; then
    alb_arn="$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
    ok "alb ${ALB_NAME}"
  else
    alb_arn="$(aws elbv2 create-load-balancer --name "${ALB_NAME}" \
        --subnets ${SUBNETS} --security-groups "$alb_sg" --scheme internet-facing --type application \
        --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" \
        --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
    ok "created alb"
  fi

  if aws elbv2 describe-target-groups --names "${TG_NAME}" >/dev/null 2>&1; then
    tg_arn="$(aws elbv2 describe-target-groups --names "${TG_NAME}" --query 'TargetGroups[0].TargetGroupArn' --output text)"
    ok "target group ${TG_NAME}"
  else
    # /health/live, NOT /health/ready — deliberately, and this is the one place
    # this script follows docs/aws-deployment-strategy.md exactly. /health/ready
    # returns 503 when the database is unreachable, so wiring it here means an
    # RDS blip marks every task unhealthy at once, ECS replaces them, each
    # replacement checks the same unreachable database, and a dependency wobble
    # becomes a fleet-wide crash loop. /health/live answers "should this process
    # be restarted"; /health/ready stays the deploy gate, checked by `verify`.
    tg_arn="$(aws elbv2 create-target-group --name "${TG_NAME}" \
        --protocol HTTP --port 8080 --vpc-id "${VPC_ID}" --target-type ip \
        --health-check-path /health/live --health-check-interval-seconds 30 \
        --healthy-threshold-count 2 --unhealthy-threshold-count 5 \
        --query 'TargetGroups[0].TargetGroupArn' --output text)"
    aws elbv2 modify-target-group-attributes --target-group-arn "$tg_arn" \
      --attributes Key=deregistration_delay.timeout_seconds,Value=30 >/dev/null
    ok "created target group (health check /health/live)"
  fi

  if [ -z "$(aws elbv2 describe-listeners --load-balancer-arn "$alb_arn" --query 'Listeners[?Port==`80`].ListenerArn' --output text)" ]; then
    aws elbv2 create-listener --load-balancer-arn "$alb_arn" --protocol HTTP --port 80 \
      --default-actions "Type=forward,TargetGroupArn=$tg_arn" >/dev/null
    ok "created listener :80"
  else
    ok "listener :80"
  fi

  if [ -n "$DOMAIN" ]; then
    log "DNS and TLS for ${DOMAIN}"
    ensure_hosted_zone >/dev/null
    local cert; cert="$(ensure_certificate)"
    ensure_https_listener "$alb_arn" "$tg_arn" "$cert"
    ensure_dns_records

    log "UI routing for ${APP_HOST}"
    ensure_ui_certificate
    local ui_tg; ui_tg="$(ensure_ui_target_group)"
    ensure_ui_listener_rule "$ui_tg"
    ensure_ui_dns
    warn "next: ./scripts/staging_aws.sh deploy-ui"
  else
    warn "no DOMAIN set — the ALB serves plaintext HTTP on its own DNS name."
    warn "Session cookies cross the internet in cleartext and an HTTPS frontend"
    warn "(Vercel) cannot call this origin at all: browsers block mixed content."
  fi

  log "ECS cluster"
  if [ "$(aws ecs describe-clusters --clusters "${CLUSTER}" --query 'clusters[0].status' --output text 2>/dev/null)" = "ACTIVE" ]; then
    ok "cluster ${CLUSTER}"
  else
    aws ecs create-cluster --cluster-name "${CLUSTER}" \
      --tags "key=Project,value=${PROJECT}" "key=Environment,value=${ENV_NAME}" >/dev/null
    ok "created cluster ${CLUSTER}"
  fi

  echo
  ok "infrastructure ready — ALB http://$(alb_dns)"
  warn "next: ./scripts/staging_aws.sh deploy"
}

# ---------------------------------------------------------------------------
# TLS: hosted zone, ACM certificate, :443 listener, ALIAS record
# ---------------------------------------------------------------------------

#: The hosted zone. Route 53 creates one automatically when a domain is registered
#: THROUGH Route 53, so this usually just finds it. It is still created here when
#: absent so a domain registered elsewhere and delegated to Route 53 also works.
ensure_hosted_zone() {
  local existing; existing="$(zone_id)"
  if [ -n "$existing" ]; then ok "hosted zone ${ZONE_DOMAIN} (${existing})"; echo "$existing"; return; fi
  local created
  created="$(aws route53 create-hosted-zone --name "${ZONE_DOMAIN}" \
      --caller-reference "${PREFIX}-$(date +%s)" \
      --hosted-zone-config "Comment=Runsheet ${ENV_NAME}" \
      --query 'HostedZone.Id' --output text | sed 's|/hostedzone/||')"
  ok "created hosted zone ${created}"
  warn "if ${ZONE_DOMAIN} is registered elsewhere, delegate it to these name servers:"
  aws route53 get-hosted-zone --id "$created" --query 'DelegationSet.NameServers' --output text >&2
  echo "$created"
}

#: Request the certificate and complete DNS validation without a human in the loop.
#:
#: This is the step that makes Route 53 worth its price premium over a cheaper
#: registrar: ACM publishes the validation CNAME it wants, and because the zone is in
#: the same account we can write it ourselves. With DNS anywhere else this becomes
#: "copy this record, paste it in your registrar, tell me when it is live".
ensure_certificate() {
  local arn; arn="$(cert_arn)"
  if [ -n "$arn" ] && [ "$(cert_status "$arn")" = "FAILED" ]; then
    warn "existing certificate for ${API_HOST} is FAILED; requesting a fresh one"
    arn=""
  fi
  if [ -z "$arn" ]; then
    arn="$(aws acm request-certificate --domain-name "${API_HOST}" \
        --validation-method DNS --key-algorithm RSA_2048 \
        --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" \
        --query CertificateArn --output text)"
    ok "requested certificate for ${API_HOST}"
  else
    ok "certificate exists ($(cert_status "$arn"))"
  fi

  if [ "$(cert_status "$arn")" = "ISSUED" ]; then echo "$arn"; return; fi

  # The validation record does not appear on the certificate immediately after
  # request-certificate returns; polling for it beats a fixed sleep.
  local name value waited=0
  while :; do
    name="$(aws acm describe-certificate --certificate-arn "$arn" \
        --query 'Certificate.DomainValidationOptions[0].ResourceRecord.Name' --output text 2>/dev/null | grep -v '^None$' || true)"
    value="$(aws acm describe-certificate --certificate-arn "$arn" \
        --query 'Certificate.DomainValidationOptions[0].ResourceRecord.Value' --output text 2>/dev/null | grep -v '^None$' || true)"
    [ -n "$name" ] && [ -n "$value" ] && break
    [ "$waited" -ge 60 ] && die "ACM never published a validation record for ${API_HOST}"
    sleep 5; waited=$((waited + 5))
  done

  local zone; zone="$(zone_id)"
  # UPSERT, not CREATE, so a re-run after a partial failure is not an error.
  aws route53 change-resource-record-sets --hosted-zone-id "$zone" --change-batch "$(printf '{
    "Changes":[{"Action":"UPSERT","ResourceRecordSet":{
      "Name":"%s","Type":"CNAME","TTL":300,"ResourceRecords":[{"Value":"%s"}]}}]}' "$name" "$value")" >/dev/null
  ok "wrote ACM validation CNAME"

  log "Waiting for the certificate to be issued (usually 1-3 minutes)"
  aws acm wait certificate-validated --certificate-arn "$arn" \
    || die "certificate not issued; check DNS delegation for ${DOMAIN}"
  ok "certificate ISSUED"
  echo "$arn"
}

#: :443 forwarding to the target group, and :80 permanently redirecting to it.
#:
#: The redirect matters more than it looks. Without it the plaintext listener keeps
#: serving the API, so a stale client, an old bookmark or a copy-pasted curl keeps
#: working over HTTP and nobody notices the cleartext path is still open.
ensure_https_listener() {
  local alb_arn="$1" tg_arn="$2" cert="$3"
  # An EXISTING listener keeps whatever default certificate it was created with, so a
  # host rename would leave api.<old-domain> as the default and the new host would be
  # served the wrong certificate. modify-listener re-points it; it is a no-op when the
  # certificate is already correct.
  local existing_listener
  existing_listener="$(aws elbv2 describe-listeners --load-balancer-arn "$alb_arn" --query 'Listeners[?Port==`443`].ListenerArn | [0]' --output text 2>/dev/null | grep -v '^None$' || true)"
  if [ -n "$existing_listener" ]; then
    local current_default
    current_default="$(aws elbv2 describe-listeners --listener-arns "$existing_listener" --query 'Listeners[0].Certificates[0].CertificateArn' --output text)"
    if [ "$current_default" != "$cert" ]; then
      aws elbv2 modify-listener --listener-arn "$existing_listener" \
        --certificates "CertificateArn=$cert" >/dev/null
      ok "default certificate re-pointed to ${API_HOST}"
    fi
  fi
  if [ -z "$(aws elbv2 describe-listeners --load-balancer-arn "$alb_arn" --query 'Listeners[?Port==`443`].ListenerArn' --output text)" ]; then
    aws elbv2 create-listener --load-balancer-arn "$alb_arn" \
      --protocol HTTPS --port 443 --certificates "CertificateArn=$cert" \
      --ssl-policy ELBSecurityPolicy-TLS13-1-2-2021-06 \
      --default-actions "Type=forward,TargetGroupArn=$tg_arn" >/dev/null
    ok "created listener :443 (TLS 1.2+ policy)"
  else
    ok "listener :443"
  fi

  local http_arn
  http_arn="$(aws elbv2 describe-listeners --load-balancer-arn "$alb_arn" --query 'Listeners[?Port==`80`].ListenerArn' --output text)"
  aws elbv2 modify-listener --listener-arn "$http_arn" \
    --default-actions 'Type=redirect,RedirectConfig={Protocol=HTTPS,Port=443,StatusCode=HTTP_301}' >/dev/null
  ok ":80 now redirects to :443 (301)"

  # The ALB security group only ever allowed :80. Without this the new listener is
  # unreachable and every request times out rather than failing loudly.
  local alb_sg; alb_sg="$(sg_id "${SG_ALB}")"
  aws ec2 authorize-security-group-ingress --group-id "$alb_sg" \
    --protocol tcp --port 443 --cidr 0.0.0.0/0 >/dev/null 2>&1 \
    && ok "alb-sg :443 from world" || ok "alb-sg :443 already allowed"
}

#: ALIAS, not CNAME. An ALB's addresses change, and an alias tracks them; alias
#: queries to an AWS target are also not billed as DNS queries.
ensure_dns_records() {
  local zone; zone="$(zone_id)"
  local alb_dns_name alb_zone
  alb_dns_name="$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" --query 'LoadBalancers[0].DNSName' --output text)"
  alb_zone="$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" --query 'LoadBalancers[0].CanonicalHostedZoneId' --output text)"
  aws route53 change-resource-record-sets --hosted-zone-id "$zone" --change-batch "$(printf '{
    "Changes":[{"Action":"UPSERT","ResourceRecordSet":{
      "Name":"%s","Type":"A","AliasTarget":{
        "HostedZoneId":"%s","DNSName":"%s","EvaluateTargetHealth":false}}}]}' \
    "$API_HOST" "$alb_zone" "$alb_dns_name")" >/dev/null
  ok "${API_HOST} -> ALIAS ${alb_dns_name}"
  warn "point ${APP_HOST} at Vercel yourself — Vercel issues its own certificate and"
  warn "will tell you which CNAME to add. This script does not manage that record."
}

# ---------------------------------------------------------------------------
# UI: certificate, target group, host-header rule, DNS
# ---------------------------------------------------------------------------

#: A SECOND certificate on the same :443 listener, attached via SNI.
#:
#: A SAN certificate covering both hosts would also work, but would mean reissuing
#: and re-attaching the API's certificate to add the UI — a change to a working
#: production path for the sake of a host that does not exist yet. Two certificates
#: on one listener keeps the two lifecycles independent.
ensure_ui_certificate() {
  local arn; arn="$(cert_arn_for "${APP_HOST}")"
  if [ -n "$arn" ] && [ "$(cert_status "$arn")" = "FAILED" ]; then
    warn "existing certificate for ${APP_HOST} is FAILED; requesting a fresh one"
    arn=""
  fi
  if [ -z "$arn" ]; then
    arn="$(aws acm request-certificate --domain-name "${APP_HOST}" \
        --validation-method DNS --key-algorithm RSA_2048 \
        --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" \
        --query CertificateArn --output text)"
    ok "requested certificate for ${APP_HOST}"
  else
    ok "ui certificate exists ($(cert_status "$arn"))"
  fi

  if [ "$(cert_status "$arn")" != "ISSUED" ]; then
    local name value waited=0
    while :; do
      name="$(aws acm describe-certificate --certificate-arn "$arn" \
          --query 'Certificate.DomainValidationOptions[0].ResourceRecord.Name' --output text 2>/dev/null | grep -v '^None$' || true)"
      value="$(aws acm describe-certificate --certificate-arn "$arn" \
          --query 'Certificate.DomainValidationOptions[0].ResourceRecord.Value' --output text 2>/dev/null | grep -v '^None$' || true)"
      [ -n "$name" ] && [ -n "$value" ] && break
      [ "$waited" -ge 60 ] && die "ACM never published a validation record for ${APP_HOST}"
      sleep 5; waited=$((waited + 5))
    done
    aws route53 change-resource-record-sets --hosted-zone-id "$(zone_id)" --change-batch "$(printf '{
      "Changes":[{"Action":"UPSERT","ResourceRecordSet":{
        "Name":"%s","Type":"CNAME","TTL":300,"ResourceRecords":[{"Value":"%s"}]}}]}' "$name" "$value")" >/dev/null
    ok "wrote ACM validation CNAME for ${APP_HOST}"
    log "Waiting for the UI certificate to be issued"
    aws acm wait certificate-validated --certificate-arn "$arn" || die "ui certificate not issued"
    ok "ui certificate ISSUED"
  fi

  # Idempotent: add-listener-certificates is a no-op when already attached.
  aws elbv2 add-listener-certificates --listener-arn "$(https_listener_arn)" \
    --certificates "CertificateArn=$arn" >/dev/null
  ok "attached ${APP_HOST} certificate to the :443 listener (SNI)"
}

ensure_ui_target_group() {
  local tg
  if aws elbv2 describe-target-groups --names "${UI_TG_NAME}" >/dev/null 2>&1; then
    tg="$(aws elbv2 describe-target-groups --names "${UI_TG_NAME}" --query 'TargetGroups[0].TargetGroupArn' --output text)"
    ok "ui target group ${UI_TG_NAME}"
  else
    # "/" rather than a dedicated health path: the app has no health route, and the
    # root page is statically prerendered so it answers without touching the API.
    # A check that reached the API would mark the UI unhealthy whenever the API was
    # down and replace tasks for no reason — the same trap as /health/ready on the
    # API's own target group.
    tg="$(aws elbv2 create-target-group --name "${UI_TG_NAME}" \
        --protocol HTTP --port "${UI_PORT}" --vpc-id "${VPC_ID}" --target-type ip \
        --health-check-path / --health-check-interval-seconds 30 \
        --healthy-threshold-count 2 --unhealthy-threshold-count 5 \
        --matcher HttpCode=200 \
        --query 'TargetGroups[0].TargetGroupArn' --output text)"
    aws elbv2 modify-target-group-attributes --target-group-arn "$tg" \
      --attributes Key=deregistration_delay.timeout_seconds,Value=30 >/dev/null
    ok "created ui target group (health check /)"
  fi
  echo "$tg"
}

#: Route app.$DOMAIN to the UI. api.$DOMAIN needs no rule: it falls through to the
#: listener's default action, which already forwards to the API target group.
ensure_ui_listener_rule() {
  local tg="$1" listener; listener="$(https_listener_arn)"
  # Matched on PRIORITY, not on the host value. Matching on the host meant a rename
  # found nothing, tried to create a second rule at the same priority, and failed with
  # PriorityInUse — leaving the old host still routed to the UI.
  local existing
  existing="$(aws elbv2 describe-rules --listener-arn "$listener" \
      --query "Rules[?Priority=='10'].RuleArn | [0]" \
      --output text 2>/dev/null | grep -v '^None$' || true)"
  if [ -n "$existing" ]; then
    aws elbv2 modify-rule --rule-arn "$existing" \
      --conditions "Field=host-header,Values=${APP_HOST}" \
      --actions "Type=forward,TargetGroupArn=$tg" >/dev/null
    ok "ui host rule -> ${APP_HOST} (updated)"
  else
    aws elbv2 create-rule --listener-arn "$listener" --priority 10 \
      --conditions "Field=host-header,Values=${APP_HOST}" \
      --actions "Type=forward,TargetGroupArn=$tg" >/dev/null
    ok "created ui host rule: ${APP_HOST} -> ui target group"
  fi
}

ensure_ui_dns() {
  local zone alb_dns_name alb_zone
  zone="$(zone_id)"
  alb_dns_name="$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" --query 'LoadBalancers[0].DNSName' --output text)"
  alb_zone="$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" --query 'LoadBalancers[0].CanonicalHostedZoneId' --output text)"
  aws route53 change-resource-record-sets --hosted-zone-id "$zone" --change-batch "$(printf '{
    "Changes":[{"Action":"UPSERT","ResourceRecordSet":{
      "Name":"%s","Type":"A","AliasTarget":{
        "HostedZoneId":"%s","DNSName":"%s","EvaluateTargetHealth":false}}}]}' \
    "$APP_HOST" "$alb_zone" "$alb_dns_name")" >/dev/null
  ok "${APP_HOST} -> ALIAS ${alb_dns_name}"
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------
ensure_execution_role() {
  local role="${PREFIX}-execution"
  if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
    ok "role $role"
  else
    aws iam create-role --role-name "$role" \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
      --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" >/dev/null
    aws iam attach-role-policy --role-name "$role" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy >/dev/null
    ok "created role $role"
  fi
  # Least privilege on the secrets: the execution role resolves them at task
  # start, and it is scoped to these four ARNs rather than secretsmanager:*.
  # REDIS_URL is one of them because it carries the ElastiCache AUTH token.
  local doc
  doc="$(printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":["%s","%s","%s","%s"]}]}' \
      "$(secret_arn "${SECRET_DB}")" "$(secret_arn "${SECRET_GEMINI}")" \
      "$(secret_arn "${SECRET_ST}")" "$(secret_arn "${SECRET_REDIS}")")"
  aws iam put-role-policy --role-name "$role" --policy-name "${PREFIX}-read-secrets" \
    --policy-document "$doc" >/dev/null
  ok "scoped secret read policy"
}

ensure_task_role() {
  local role="${PREFIX}-task"
  if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
    ok "role $role"
    return
  fi
  # Empty by design. The task needs no AWS API access in staging: the three
  # AWS-backed surfaces (S3 proof-of-delivery, KMS credentials vault, Textract
  # OCR) are read straight from the environment by bootstrap/agents.py and are
  # simply skipped when unset — logged at INFO, no startup failure. Granting
  # nothing keeps that honest rather than half-wiring them.
  aws iam create-role --role-name "$role" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" >/dev/null
  ok "created role $role (no policies — staging needs no AWS API access)"
}

# ---------------------------------------------------------------------------
# task definition
# ---------------------------------------------------------------------------
register_task_def() {
  local image="$1"
  local exec_arn task_arn
  exec_arn="$(aws iam get-role --role-name "${PREFIX}-execution" --query 'Role.Arn' --output text)"
  task_arn="$(aws iam get-role --role-name "${PREFIX}-task" --query 'Role.Arn' --output text)"

  # The generator is written to a file and then run, rather than fed to python3
  # through a heredoc inside "$( ... )". Bash scans the command substitution for its
  # closing paren and does not fully respect a nested quoted heredoc while doing so,
  # so a lone apostrophe in a Python COMMENT ("won't") made the whole script a syntax
  # error 380 lines later. Unnesting removes the class of problem rather than the
  # apostrophe.
  local gen="/tmp/${PREFIX}-taskdef-gen.py"
  cat > "$gen" <<'PY'
import json, sys, os

image, exec_arn, task_arn = sys.argv[1:4]
# Origins arrive resolved rather than as an ALB hostname to concatenate. The scheme
# used to be built here as "http://" + alb in four places, which is precisely how it
# would have silently stayed plaintext after the certificate landed.
api_origin = os.environ["API_ORIGIN"]
app_origin = os.environ["APP_ORIGIN"]
prefix = os.environ["PREFIX"]
log_group = os.environ["LOG_GROUP"]
region = os.environ["AWS_REGION"]
secret_db = os.environ["SECRET_DB_ARN"]
secret_gemini = os.environ["SECRET_GEMINI_ARN"]
secret_st = os.environ["SECRET_ST_ARN"]
secret_redis = os.environ["SECRET_REDIS_ARN"]

def logs(stream):
    return {
        "logDriver": "awslogs",
        "options": {
            "awslogs-group": log_group,
            "awslogs-region": region,
            "awslogs-stream-prefix": stream,
        },
    }

# ONE container. No "redis" sidecar, and no self-hosted "supertokens" — Redis is
# ElastiCache and the SuperTokens core is managed SaaS, both reached over the network.
#
# The sidecar that used to stand here was removed rather than kept as a cheaper
# staging substitution, because it was not a substitution. It was EPHEMERAL (wiped on
# every task replacement) and PER-TASK (a second replica got a second, unrelated
# Redis), and six components depend on Redis:
#
#   session/redis_store.py                  sessions / agent conversation memory
#   ops/ingestion/idempotency.py            webhook de-duplication
#   ops/services/feature_flags.py           per-tenant Ops + overlay agent flags
#   fuel/voice/voice_submission_ledger.py   voice submission de-duplication
#   bootstrap/agents.py                     agent runtime client
#   scheduling/services/job_id_generator.py INCR on scheduling:job_id_counter
#
# The last one was a data-loss path, not a degradation. Wiping the counter restarted
# it at 1, and JobService writes with index_document(JOBS_CURRENT_INDEX, job_id, doc)
# — an upsert keyed on job_id — so the next created job SILENTLY OVERWROTE the
# existing JOB_1. Its own docstring says the counter exists "for atomicity across
# multiple backend instances", which is precisely what a per-task Redis removed.
#
# Two consequences of the move, both intentional:
#
#   * REDIS_URL is in "secrets", not "environment". It is rediss:// with the AUTH
#     token in the userinfo, so it is a credential.
#   * there is no "dependsOn" any more. It waited for the sidecar to report HEALTHY;
#     with a managed endpoint there is nothing in the task to wait for. If Redis is
#     unreachable the session store raises SESSION_STORE_UNAVAILABLE (503) on the
#     affected requests, and /health/live keeps answering, which is the correct
#     failure mode — the process is alive, one dependency is not.
containers = [
    # No self-hosted "supertokens" container. The core is SuperTokens Cloud,
    # reached over HTTPS from the task's public IP. A sidecar running
    # supertokens-postgresql against this same RDS instance stood here and was
    # wrong: .kiro/specs/supertokens-auth-migration resolves the deployment model
    # as the managed SaaS core and states there are no container tasks for it.
    {
        "name": "api",
        "image": image,
        "essential": True,
        "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
        "environment": [
            {"name": "ENVIRONMENT", "value": "staging"},
            {"name": "PORT", "value": "8080"},
            {"name": "LOG_LEVEL", "value": "INFO"},
            {"name": "SESSION_STORE_TYPE", "value": "redis"},
            # SuperTokens Cloud over HTTPS. The API key arrives as a secret below.
            {"name": "SUPERTOKENS_CONNECTION_URI", "value": os.environ["ST_URI"]},
            {"name": "SUPERTOKENS_API_DOMAIN", "value": api_origin},
            # The FRONTEND origin, which is not the API origin once the UI is on
            # Vercel. These were the same value only while both were the ALB.
            {"name": "SUPERTOKENS_WEBSITE_DOMAIN", "value": app_origin},
            # Exact-match origins: main.py passes this list straight to
            # CORSMiddleware(allow_origins=...) and never sets allow_origin_regex,
            # so Vercel PREVIEW deployments — which get a random hostname each
            # build — will fail CORS against this. Only the stable alias works.
            {"name": "CORS_ORIGINS", "value": json.dumps([app_origin])},
            # Commerce needs dual-write whenever the backbone is on; staging
            # validation refuses the combination otherwise.
            {"name": "COMMERCE_BACKBONE_ENABLED", "value": "true"},
            {"name": "COMMERCE_DUAL_WRITE_POSTGRES", "value": "true"},
            {"name": "COMMERCE_READ_FROM_POSTGRES", "value": "true"},
            # Left OFF. Development sets five retired indices, and copying that
            # list here would suppress projection for aggregates whose relational
            # tables are empty in a brand-new environment.
            {"name": "RETIRED_ES_INDICES", "value": ""},
        ],
        "secrets": [
            {"name": "DATABASE_URL", "valueFrom": secret_db},
            {"name": "GEMINI_API_KEY", "valueFrom": secret_gemini},
            {"name": "SUPERTOKENS_API_KEY", "valueFrom": secret_st},
            # Points at the ElastiCache PRIMARY endpoint, so a failover is followed
            # by DNS rather than needing a task-definition revision.
            {"name": "REDIS_URL", "valueFrom": secret_redis},
        ],
        "logConfiguration": logs("api"),
    },
]

print(json.dumps({
    "family": os.environ["TASK_FAMILY"],
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": os.environ["TASK_CPU"],
    "memory": os.environ["TASK_MEM"],
    "executionRoleArn": exec_arn,
    "taskRoleArn": task_arn,
    "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
    "containerDefinitions": containers,
}))
PY

  python3 "$gen" "$image" "$exec_arn" "$task_arn" \
    > "/tmp/${PREFIX}-taskdef.json"
  aws ecs register-task-definition --cli-input-json "file:///tmp/${PREFIX}-taskdef.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text
}

# ---------------------------------------------------------------------------
# deploy-ui: build, push and roll the Next.js dispatcher UI
# ---------------------------------------------------------------------------
register_ui_task_def() {
  local image="$1"
  local exec_arn task_arn
  exec_arn="$(aws iam get-role --role-name "${PREFIX}-execution" --query 'Role.Arn' --output text)"
  task_arn="$(aws iam get-role --role-name "${PREFIX}-task" --query 'Role.Arn' --output text)"

  local gen="/tmp/${PREFIX}-ui-taskdef-gen.py"
  cat > "$gen" <<'PY'
import json, os, sys

image, exec_arn, task_arn = sys.argv[1:4]

# NO "secrets" block and NO NEXT_PUBLIC_* here, deliberately.
#
# Next inlines every NEXT_PUBLIC_* value into the JavaScript bundle at BUILD time,
# so setting them at runtime would do nothing at all — the built bundle already
# contains whatever the image was built with. Putting them here would be worse than
# useless: it would read as configuration and quietly not be.
#
# The UI also holds no credentials. It talks to the API over the public internet like
# any browser would, so it needs no database, no Redis, and no Secrets Manager
# access. The task role is the same empty one the API uses.
print(json.dumps({
    "family": os.environ["UI_TASK_FAMILY"],
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": os.environ["UI_CPU"],
    "memory": os.environ["UI_MEM"],
    "executionRoleArn": exec_arn,
    "taskRoleArn": task_arn,
    "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
    "containerDefinitions": [{
        "name": "ui",
        "image": image,
        "essential": True,
        "portMappings": [{"containerPort": int(os.environ["UI_PORT"]), "protocol": "tcp"}],
        "environment": [
            {"name": "NODE_ENV", "value": "production"},
            {"name": "PORT", "value": os.environ["UI_PORT"]},
            # server.js binds to this; without it Next listens on localhost only and
            # the ALB health check cannot reach the container at all.
            {"name": "HOSTNAME", "value": "0.0.0.0"},
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": os.environ["UI_LOG_GROUP"],
                "awslogs-region": os.environ["AWS_REGION"],
                "awslogs-stream-prefix": "ui",
            },
        },
    }],
}))
PY
  python3 "$gen" "$image" "$exec_arn" "$task_arn" > "/tmp/${PREFIX}-ui-taskdef.json"
  aws ecs register-task-definition --cli-input-json "file:///tmp/${PREFIX}-ui-taskdef.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text
}

cmd_deploy_ui() {
  [ -n "$DOMAIN" ] || die "deploy-ui needs DOMAIN: the build refuses a non-https API origin"
  local sha image api app
  sha="$(git rev-parse --short HEAD)"
  image="${REGISTRY}/${UI_ECR_REPO}:${sha}"
  api="$(api_origin)"; app="$(app_origin)"

  log "ECR repository for the UI"
  aws ecr describe-repositories --repository-names "${UI_ECR_REPO}" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "${UI_ECR_REPO}" \
         --image-scanning-configuration scanOnPush=true \
         --tags "Key=Project,Value=${PROJECT}" "Key=Environment,Value=${ENV_NAME}" >/dev/null
  ok "ecr ${UI_ECR_REPO}"

  log "CloudWatch log group for the UI"
  aws logs create-log-group --log-group-name "${UI_LOG_GROUP}" >/dev/null 2>&1 || true
  aws logs put-retention-policy --log-group-name "${UI_LOG_GROUP}" --retention-in-days 14 >/dev/null
  ok "${UI_LOG_GROUP} (14 days)"

  # The origins are BUILD ARGS, not runtime env. See runsheet/Dockerfile. This also
  # means the image is environment-specific and cannot be promoted between
  # environments by changing a task-definition variable.
  log "Building ${image} with api=${api} app=${app}"
  local maps_key
  maps_key="$(grep -E '^NEXT_PUBLIC_GOOGLE_MAPS_API_KEY=' "$(dirname "$0")/../../runsheet/.env.local" 2>/dev/null | cut -d= -f2- || true)"
  [ -n "$maps_key" ] || warn "no Maps key found in runsheet/.env.local — maps will not render"
  docker build --platform linux/amd64 \
    --build-arg "NEXT_PUBLIC_API_URL=${api}/api" \
    --build-arg "NEXT_PUBLIC_WS_URL=$(echo "$api" | sed 's|^https|wss|; s|^http|ws|')" \
    --build-arg "NEXT_PUBLIC_ST_API_DOMAIN=${api}" \
    --build-arg "NEXT_PUBLIC_ST_WEBSITE_DOMAIN=${app}" \
    --build-arg "NEXT_PUBLIC_TENANT_ID=demo-tenant" \
    --build-arg "NEXT_PUBLIC_SITE_URL=${app}" \
    --build-arg "NEXT_PUBLIC_GOOGLE_MAPS_API_KEY=${maps_key}" \
    -t "$image" "$(dirname "$0")/../../runsheet" >/dev/null
  ok "built"

  log "Pushing to ECR"
  aws ecr get-login-password | docker login --username AWS --password-stdin "${REGISTRY}" >/dev/null 2>&1
  docker push "$image" >/dev/null
  ok "pushed ${sha}"

  log "Registering the UI task definition"
  export UI_TASK_FAMILY UI_CPU UI_MEM UI_PORT UI_LOG_GROUP AWS_REGION
  local td; td="$(register_ui_task_def "$image")"
  ok "$td"

  local tg ui_sg
  tg="$(aws elbv2 describe-target-groups --names "${UI_TG_NAME}" --query 'TargetGroups[0].TargetGroupArn' --output text)"
  ui_sg="$(sg_id "${SG_UI}")"

  if [ "$(aws ecs describe-services --cluster "${CLUSTER}" --services "${UI_SERVICE}" \
          --query 'services[0].status' --output text 2>/dev/null)" = "ACTIVE" ]; then
    log "Updating the UI service"
    aws ecs update-service --cluster "${CLUSTER}" --service "${UI_SERVICE}" \
      --task-definition "$td" --force-new-deployment >/dev/null
    ok "rolling"
  else
    log "Creating the UI service"
    aws ecs create-service --cluster "${CLUSTER}" --service-name "${UI_SERVICE}" \
      --task-definition "$td" --desired-count 1 --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS_CSV}],securityGroups=[${ui_sg}],assignPublicIp=ENABLED}" \
      --load-balancers "targetGroupArn=$tg,containerName=ui,containerPort=${UI_PORT}" \
      --health-check-grace-period-seconds 90 \
      --tags "key=Project,value=${PROJECT}" "key=Environment,value=${ENV_NAME}" >/dev/null
    ok "created"
  fi

  log "Waiting for the UI service to stabilise"
  aws ecs wait services-stable --cluster "${CLUSTER}" --services "${UI_SERVICE}" \
    && ok "stable" || warn "did not stabilise — see CloudWatch ${UI_LOG_GROUP}"
}

# ---------------------------------------------------------------------------
# deploy: build, push, register, roll
# ---------------------------------------------------------------------------
cmd_deploy() {
  local sha image
  sha="$(git rev-parse --short HEAD)"
  [ -n "$(alb_dns)" ] || die "no ALB — run 'up' first"
  image="${REGISTRY}/${ECR_REPO}:${sha}"

  log "Building ${image} (linux/amd64)"
  # Tagged with the commit SHA, never :latest. A rollback needs a name that still
  # means the same bytes tomorrow.
  docker build --platform linux/amd64 -t "$image" "$(dirname "$0")/.." >/dev/null
  ok "built"

  log "Pushing to ECR"
  aws ecr get-login-password | docker login --username AWS --password-stdin "${REGISTRY}" >/dev/null 2>&1
  docker push "$image" >/dev/null
  ok "pushed ${sha}"

  log "Registering task definition"
  resolve_supertokens
  export PREFIX LOG_GROUP AWS_REGION TASK_FAMILY TASK_CPU TASK_MEM
  export SECRET_DB_ARN="$(secret_arn "${SECRET_DB}")"
  export SECRET_GEMINI_ARN="$(secret_arn "${SECRET_GEMINI}")"
  export SECRET_ST_ARN="$(secret_arn "${SECRET_ST}")"
  export SECRET_REDIS_ARN="$(secret_arn "${SECRET_REDIS}")"
  export API_ORIGIN="$(api_origin)" APP_ORIGIN="$(app_origin)"
  ok "api origin ${API_ORIGIN}"
  ok "app origin ${APP_ORIGIN}"
  local td; td="$(register_task_def "$image")"
  ok "$td"

  # Migrations run BEFORE the service is created or rolled, which is the order
  # docs/deploy-runbook.md specifies and the order this script originally got wrong.
  # Creating the service first meant the app booted against an empty schema and
  # logged ~250 psycopg UndefinedTable errors — es_documents, jobs_current, trucks,
  # accounts — from every periodic sweep until the migration landed. Nothing was
  # damaged and it self-healed, but "staging is full of errors" is a bad first
  # impression of a working deploy, and on a multi-replica service it would be a
  # thundering herd against a half-built schema.
  #
  # Still a discrete one-shot task, never the container entrypoint: N replicas
  # starting together would race the same migration, and this chain drops a table.
  cmd_migrate

  local tg_arn task_sg
  tg_arn="$(aws elbv2 describe-target-groups --names "${TG_NAME}" --query 'TargetGroups[0].TargetGroupArn' --output text)"
  task_sg="$(sg_id "${SG_TASK}")"

  if [ "$(aws ecs describe-services --cluster "${CLUSTER}" --services "${SERVICE}" \
          --query 'services[0].status' --output text 2>/dev/null)" = "ACTIVE" ]; then
    log "Updating service"
    aws ecs update-service --cluster "${CLUSTER}" --service "${SERVICE}" \
      --task-definition "$td" --force-new-deployment >/dev/null
    ok "rolling"
  else
    log "Creating service"
    aws ecs create-service --cluster "${CLUSTER}" --service-name "${SERVICE}" \
      --task-definition "$td" --desired-count 1 --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS_CSV}],securityGroups=[${task_sg}],assignPublicIp=ENABLED}" \
      --load-balancers "targetGroupArn=$tg_arn,containerName=api,containerPort=8080" \
      --health-check-grace-period-seconds 120 \
      --tags "key=Project,value=${PROJECT}" "key=Environment,value=${ENV_NAME}" >/dev/null
    ok "created"
  fi

  log "Waiting for the service to stabilise (up to ~5 min)"
  aws ecs wait services-stable --cluster "${CLUSTER}" --services "${SERVICE}" \
    && ok "stable" || warn "did not stabilise — see 'status' and CloudWatch ${LOG_GROUP}"
}

# ---------------------------------------------------------------------------
# migrate: alembic upgrade head as a one-shot task
# ---------------------------------------------------------------------------
cmd_migrate() {
  # A discrete RunTask, never the application entrypoint. N replicas starting
  # together would race the same migration, and this chain contains a destructive
  # revision (0007_drop_shipments_current).
  local td task_sg arn exit_code
  td="$(aws ecs describe-task-definition --task-definition "${TASK_FAMILY}" \
        --query 'taskDefinition.taskDefinitionArn' --output text)"
  task_sg="$(sg_id "${SG_TASK}")"

  # The override carries only "command". ECS containerOverrides rejects
  # "entryPoint" outright, and the image declares no ENTRYPOINT anyway — just
  # CMD ["sh","-c","exec uvicorn ..."] — so replacing the command is sufficient.
  # (A comment cannot live INSIDE the backslash-continued command below; putting one
  # there turned --overrides into a command name and the run exited 127.)
  log "Running alembic upgrade head"
  arn="$(aws ecs run-task --cluster "${CLUSTER}" --task-definition "$td" \
      --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS_CSV}],securityGroups=[${task_sg}],assignPublicIp=ENABLED}" \
      --overrides '{"containerOverrides":[{"name":"api","command":["sh","-c","alembic upgrade head"]}]}' \
      --query 'tasks[0].taskArn' --output text)"
  ok "task ${arn##*/}"

  aws ecs wait tasks-stopped --cluster "${CLUSTER}" --tasks "$arn"
  exit_code="$(aws ecs describe-tasks --cluster "${CLUSTER}" --tasks "$arn" \
      --query 'tasks[0].containers[?name==`api`].exitCode' --output text)"
  if [ "$exit_code" = "0" ]; then
    ok "migrations applied"
  else
    warn "migration task exited ${exit_code} — logs:"
    aws logs tail "${LOG_GROUP}" --since 10m --format short 2>/dev/null | grep -i alembic | tail -20 || true
    die "migration failed"
  fi
}

# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
cmd_verify() {
  [ -n "$(alb_dns)" ] || die "no ALB"
  local base; base="$(api_origin)"
  log "Verifying ${base}"

  # With a domain there are two extra properties worth asserting, because both can
  # regress silently: the certificate has to actually cover the host the browser
  # asks for, and plaintext must not keep serving the API behind the redirect.
  if [ -n "$DOMAIN" ]; then
    curl -sS -o /dev/null -m 15 "https://${API_HOST}/health/live" \
      && ok "TLS handshake and certificate valid for ${API_HOST}" \
      || die "TLS failed for ${API_HOST} — cert, DNS or the :443 listener"
    local redirect
    redirect="$(curl -s -o /dev/null -m 10 -w '%{http_code} %{redirect_url}' "http://${API_HOST}/health/live" || true)"
    case "$redirect" in
      301*https://*) ok "http -> https redirect (${redirect})" ;;
      *) die "plaintext :80 did not redirect (got: ${redirect}) — the API is still served over HTTP" ;;
    esac
  fi

  local ready=0 code
  for _ in $(seq 1 60); do
    code="$(curl -s -o /tmp/${PREFIX}-ready -m 10 -w '%{http_code}' "${base}/health/ready" || true)"
    [ "$code" = "200" ] && { ready=1; break; }
    sleep 5
  done
  if [ "$ready" != "1" ]; then
    warn "/health/ready -> ${code}"
    cat /tmp/${PREFIX}-ready 2>/dev/null; echo
    die "not ready"
  fi
  ok "/health/ready -> 200"
  cat /tmp/${PREFIX}-ready; echo

  # The single most consequential thing that can be wrong about a running image.
  code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "${base}/api/orders" || true)"
  if [ "$code" = "401" ] || [ "$code" = "403" ]; then
    ok "/api/orders -> ${code} (auth enforced)"
  else
    die "/api/orders -> ${code}, expected 401/403 — the tenant guard is not enforcing"
  fi

  # The readiness payload must name postgres. If it ever says elasticsearch again
  # something has been reverted.
  grep -q '"name":"postgres"' /tmp/${PREFIX}-ready \
    && ok "document store reported as postgres" \
    || die "readiness does not report postgres"

  verify_redis
  verify_ui

  echo
  ok "staging verified at ${base}"
}

#: Verify the UI, if it is deployed. Skipped rather than failed when absent, so
#: `verify` stays useful on a backend-only environment.
verify_ui() {
  [ -n "$DOMAIN" ] || return 0
  if [ "$(aws ecs describe-services --cluster "${CLUSTER}" --services "${UI_SERVICE}" \
          --query 'services[0].status' --output text 2>/dev/null)" != "ACTIVE" ]; then
    warn "no UI service — skipping UI checks (run deploy-ui)"
    return 0
  fi

  log "Verifying the UI at $(app_origin)"
  local code
  code="$(curl -s -o "/tmp/${PREFIX}-ui.html" -m 20 -w '%{http_code}' "$(app_origin)/" || true)"
  [ "$code" = "200" ] && ok "${APP_HOST} -> 200" || die "${APP_HOST} -> ${code}"

  # Host-based routing actually discriminating, not both hosts hitting one service.
  # Without this the UI could be served from the API's default action (or vice
  # versa) and every other check would still pass.
  grep -qi '<!DOCTYPE html' "/tmp/${PREFIX}-ui.html" \
    && ok "served HTML, not the API" \
    || die "${APP_HOST} did not return HTML — the host rule may be routing to the API"

  # A server-rendered route, which is the thing a static host could not have done.
  code="$(curl -s -o /dev/null -m 20 -w '%{http_code}' "$(app_origin)/orders/ORD-1" || true)"
  [ "$code" = "200" ] && ok "/orders/ORD-1 -> 200 (server-rendered)" \
    || warn "/orders/ORD-1 -> ${code}"

  # The origin baked into the bundle at build time. If this says localhost the image
  # was built without the build args and every API call from the browser will fail.
  local chunk
  chunk="$(grep -oE '/_next/static/chunks/[a-zA-Z0-9._-]+\.js' "/tmp/${PREFIX}-ui.html" | head -1)"
  if [ -n "$chunk" ]; then
    if curl -s -m 20 "$(app_origin)${chunk}" | grep -q 'localhost:8080'; then
      die "the deployed bundle still points at localhost — rebuilt without build args"
    fi
    ok "no localhost origin in the served bundle"
  fi

  # The API must accept the UI's origin, or every authenticated call is a CORS
  # failure the browser reports and the server does not.
  local acao
  acao="$(curl -s -o /dev/null -m 15 -D - "$(api_origin)/api/orders" \
          -H "Origin: $(app_origin)" -w '' 2>/dev/null | grep -i '^access-control-allow-origin' | tr -d '\r' || true)"
  [ -n "$acao" ] && ok "api allows the UI origin (${acao#*: })" \
    || warn "api returned no Access-Control-Allow-Origin for $(app_origin) — check CORS_ORIGINS"
}

#: Prove ElastiCache from INSIDE the task, as a one-shot RunTask on the deployed task
#: definition.
#:
#: /health/ready cannot do this. bootstrap/core.py constructs HealthCheckService with
#: session_store=None, so the readiness payload reports postgres and nothing else —
#: Redis could be entirely unreachable and readiness would still return 200. Checking
#: it here, with the same task definition, the same execution role and the same
#: secret, is the only thing that actually exercises the path the app uses.
verify_redis() {
  local td task_sg arn exit_code
  td="$(aws ecs describe-task-definition --task-definition "${TASK_FAMILY}" \
        --query 'taskDefinition.taskDefinitionArn' --output text)"
  task_sg="$(sg_id "${SG_TASK}")"

  # Written to a file, then read, for the same reason register_task_def does it: bash
  # scans a "$( ... )" for its closing paren without fully honouring a nested quoted
  # heredoc, and an apostrophe inside the Python once broke the script 380 lines away.
  local gen="/tmp/${PREFIX}-redis-probe-gen.py"
  cat > "$gen" <<'PY'
import json

# Deliberately redis.asyncio.from_url, the plain client, because that is what all six
# consumers use. If the replication group were ever recreated with cluster mode
# enabled this probe would fail on a MOVED redirect, which is the failure the
# deployment strategy document warns is otherwise invisible until runtime.
probe = """
import asyncio, os
import redis.asyncio as redis

url = os.environ["REDIS_URL"]
assert url.startswith("rediss://"), "REDIS_URL is not TLS: " + url[:16]
assert "@" in url.split("//", 1)[1], "REDIS_URL carries no AUTH token"

async def main():
    client = redis.from_url(url, decode_responses=True)
    assert await client.ping() is True
    await client.set("staging:verify", "ok", ex=60)
    assert await client.get("staging:verify") == "ok"
    first = await client.incr("staging:verify:counter")
    second = await client.incr("staging:verify:counter")
    assert second == first + 1, (first, second)
    info = await client.info("replication")
    await client.aclose()
    print("REDIS_VERIFY_OK role=%s connected_slaves=%s endpoint=%s" % (
        info.get("role"), info.get("connected_slaves"), url.split("@", 1)[1]))

asyncio.run(main())
"""

print(json.dumps({
    "containerOverrides": [{"name": "api", "command": ["python", "-c", probe]}]
}))
PY
  python3 "$gen" > "/tmp/${PREFIX}-redis-probe.json"

  log "Probing ElastiCache from inside the task"
  arn="$(aws ecs run-task --cluster "${CLUSTER}" --task-definition "$td" \
      --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS_CSV}],securityGroups=[${task_sg}],assignPublicIp=ENABLED}" \
      --overrides "file:///tmp/${PREFIX}-redis-probe.json" \
      --query 'tasks[0].taskArn' --output text)"
  aws ecs wait tasks-stopped --cluster "${CLUSTER}" --tasks "$arn"
  exit_code="$(aws ecs describe-tasks --cluster "${CLUSTER}" --tasks "$arn" \
      --query 'tasks[0].containers[?name==`api`].exitCode' --output text)"

  local line
  line="$(aws logs tail "${LOG_GROUP}" --since 10m --format short 2>/dev/null | grep REDIS_VERIFY_OK | tail -1 || true)"
  if [ "$exit_code" = "0" ] && [ -n "$line" ]; then
    ok "elasticache reachable over TLS with AUTH"
    ok "${line#* }"
  else
    warn "redis probe exited ${exit_code}"
    aws logs tail "${LOG_GROUP}" --since 10m --format short 2>/dev/null | tail -20 || true
    die "REDIS_URL does not work from the task"
  fi

  # One container, not two. Catches a stale task definition revision still carrying
  # the sidecar, which would otherwise let the app keep talking to localhost:6379 and
  # make every check above pass for the wrong reason.
  local n
  n="$(aws ecs describe-task-definition --task-definition "${TASK_FAMILY}" \
       --query 'length(taskDefinition.containerDefinitions)' --output text)"
  [ "$n" = "1" ] && ok "task definition has 1 container (no redis sidecar)" \
    || die "task definition has ${n} containers — the redis sidecar is still there"
}

cmd_status() {
  echo "ALB           $(alb_dns || echo '-')"
  echo "RDS           $(db_endpoint || echo '-')  $(aws rds describe-db-instances --db-instance-identifier "${DB_ID}" --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null || echo '-')"
  echo "Redis         $(redis_primary || echo '-')  $(redis_status || echo '-')  $(aws elasticache describe-replication-groups --replication-group-id "${REDIS_ID}" --query 'ReplicationGroups[0].[AutomaticFailover,MultiAZ,TransitEncryptionEnabled]' --output text 2>/dev/null || echo '-')"
  echo "Service       $(aws ecs describe-services --cluster "${CLUSTER}" --services "${SERVICE}" --query 'services[0].[status,desiredCount,runningCount]' --output text 2>/dev/null || echo '-')"
  echo "Task def      $(aws ecs describe-task-definition --task-definition "${TASK_FAMILY}" --query 'taskDefinition.revision' --output text 2>/dev/null || echo '-')"
  echo "Targets       $(aws elbv2 describe-target-health --target-group-arn "$(aws elbv2 describe-target-groups --names "${TG_NAME}" --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null)" --query 'TargetHealthDescriptions[].TargetHealth.State' --output text 2>/dev/null || echo '-')"
}

cmd_logs() {
  aws logs tail "${LOG_GROUP}" --since "${1:-10m}" --format short
}

#: Print the frontend environment contract, derived from the same origins the task
#: definition uses, so the two cannot disagree.
#:
#: NEXT_PUBLIC_WS_URL is separate from the API url and needed explicitly: one caller
#: derives its socket url with API_BASE_URL.replace("http","ws") — which yields wss
#: correctly once the origin is https — but InvoiceDetailPage.tsx reads
#: NEXT_PUBLIC_WS_URL and falls back to ws://localhost:8080 when it is unset.
cmd_frontend_env() {
  local api app
  api="$(api_origin)"; app="$(app_origin)"
  cat <<ENV
Set these in the Vercel project (Production scope):

  NEXT_PUBLIC_API_URL=${api}/api
  NEXT_PUBLIC_WS_URL=$(echo "$api" | sed 's|^https|wss|; s|^http|ws|')
  NEXT_PUBLIC_ST_API_DOMAIN=${api}
  NEXT_PUBLIC_ST_WEBSITE_DOMAIN=${app}
  NEXT_PUBLIC_ST_API_BASE_PATH=/auth
  NEXT_PUBLIC_TENANT_ID=demo-tenant
  NEXT_PUBLIC_GOOGLE_MAPS_API_KEY=<the value from runsheet/.env.local>
  NEXT_PUBLIC_SITE_URL=${app}

The Maps key is NOT leaked in git. runsheet/.gitignore excludes .env*, the file was
never committed, and the value appears in no tracked file and nowhere in history.
Stated explicitly because an earlier version of this text claimed the opposite.

It does still become public, for a different and unavoidable reason: every
NEXT_PUBLIC_* value is inlined into the JavaScript bundle and served to the browser,
so anyone can read it off the deployed site. That is how the mechanism works, not a
mistake, and it means secrecy is not the control. A referrer restriction in the
Google Cloud console is:

  ${app}/*

NEXT_PUBLIC_SITE_URL is included because layout.tsx, robots.ts and sitemap.ts each
fall back to http://localhost:3000 when it is unset, which would otherwise publish
localhost canonical URLs and a localhost sitemap.
ENV
  if [ -z "$DOMAIN" ]; then
    echo
    warn "DOMAIN is unset, so the origins above are http:// and WILL NOT WORK from"
    warn "Vercel: an https page cannot call an http api or open a ws:// socket."
  fi
}

# ---------------------------------------------------------------------------
# down: destroy everything this script created
# ---------------------------------------------------------------------------
cmd_down() {
  # DESTRUCTIVE and it says so. The RDS instance is deleted with
  # --skip-final-snapshot, so every row in staging goes with it. That is the right
  # default for a synthetic environment and the wrong one for anything else, which
  # is why the confirmation is typed rather than a -y flag.
  cat <<WARNING
This DESTROYS the Runsheet staging environment in ${AWS_REGION}:

  ECS service ${SERVICE} and cluster ${CLUSTER}
  RDS instance ${DB_ID}  -- NO FINAL SNAPSHOT, all staging data is lost
  ElastiCache ${REDIS_ID}  -- NO FINAL SNAPSHOT, sessions and the job-id counter go
  ALB ${ALB_NAME}, target group ${TG_NAME}
  Secrets ${SECRET_DB}, ${SECRET_GEMINI}, ${SECRET_ST}, ${SECRET_REDIS} (+ password, + auth token)
  IAM roles ${PREFIX}-execution, ${PREFIX}-task
  Security groups, subnet group, log group, ECR repo ${ECR_REPO} and its images

It does NOT touch the default VPC, its subnets, or anything belonging to the
unrelated "cleanup" project in this account.

WARNING
  printf 'Type DESTROY to proceed: '
  read -r confirm
  [ "$confirm" = "DESTROY" ] || die "aborted"

  log "Scaling the services to zero"
  for svc in "${SERVICE}" "${UI_SERVICE}"; do
    aws ecs update-service --cluster "${CLUSTER}" --service "$svc" --desired-count 0 >/dev/null 2>&1 || true
    aws ecs delete-service --cluster "${CLUSTER}" --service "$svc" --force >/dev/null 2>&1 \
      && ok "$svc deleted" || warn "no $svc"
  done

  if [ -n "$DOMAIN" ]; then
    log "Deleting DNS records and the certificate"
    # Records first: Route 53 refuses to delete a zone that still has records, and
    # ACM refuses to delete a certificate still attached to a listener — which is why
    # this runs before the load balancer goes.
    local zone; zone="$(zone_id)"
    if [ -n "$zone" ]; then
      local alb_dns_name alb_zone
      alb_dns_name="$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || true)"
      alb_zone="$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" --query 'LoadBalancers[0].CanonicalHostedZoneId' --output text 2>/dev/null || true)"
      if [ -n "$alb_dns_name" ] && [ "$alb_dns_name" != "None" ]; then
        aws route53 change-resource-record-sets --hosted-zone-id "$zone" --change-batch "$(printf '{
          "Changes":[{"Action":"DELETE","ResourceRecordSet":{
            "Name":"%s","Type":"A","AliasTarget":{
              "HostedZoneId":"%s","DNSName":"%s","EvaluateTargetHealth":false}}}]}' \
          "$API_HOST" "$alb_zone" "$alb_dns_name")" >/dev/null 2>&1 \
          && ok "deleted ${API_HOST}" || true
      fi
    fi
  fi

  log "Deleting the load balancer"
  local alb_arn
  alb_arn="$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
  if [ -n "$alb_arn" ] && [ "$alb_arn" != "None" ]; then
    aws elbv2 delete-load-balancer --load-balancer-arn "$alb_arn" >/dev/null
    # The ALB has to be gone before its security group can be released.
    aws elbv2 wait load-balancers-deleted --load-balancer-arns "$alb_arn" 2>/dev/null || sleep 30
    ok "alb deleted"
  fi
  for tgn in "${TG_NAME}" "${UI_TG_NAME}"; do
    local tg_arn
    tg_arn="$(aws elbv2 describe-target-groups --names "$tgn" --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
    [ -n "$tg_arn" ] && [ "$tg_arn" != "None" ] \
      && aws elbv2 delete-target-group --target-group-arn "$tg_arn" >/dev/null 2>&1 \
      && ok "$tgn deleted" || true
  done

  log "Deleting the database"
  aws rds delete-db-instance --db-instance-identifier "${DB_ID}" \
    --skip-final-snapshot --delete-automated-backups >/dev/null 2>&1 \
    && ok "rds deleting" || warn "no rds"
  aws rds wait db-instance-deleted --db-instance-identifier "${DB_ID}" 2>/dev/null || true
  aws rds delete-db-subnet-group --db-subnet-group-name "${DB_SUBNET_GROUP}" >/dev/null 2>&1 \
    && ok "subnet group deleted" || true

  log "Deleting ElastiCache"
  # --no-retain-primary-cluster: take the whole replication group, both nodes, and do
  # not leave a standalone cache cluster behind still billing. The waiter has to run
  # before the cache subnet group and the security group can go.
  aws elasticache delete-replication-group --replication-group-id "${REDIS_ID}" \
    --no-retain-primary-cluster >/dev/null 2>&1 \
    && ok "elasticache deleting" || warn "no elasticache"
  aws elasticache wait replication-group-deleted --replication-group-id "${REDIS_ID}" 2>/dev/null || true
  aws elasticache delete-cache-subnet-group --cache-subnet-group-name "${REDIS_SUBNET_GROUP}" >/dev/null 2>&1 \
    && ok "cache subnet group deleted" || true

  log "Deleting the cluster"
  aws ecs delete-cluster --cluster "${CLUSTER}" >/dev/null 2>&1 && ok "cluster deleted" || true

  log "Deregistering task definitions"
  for fam in "${TASK_FAMILY}" "${UI_TASK_FAMILY}"; do
    for arn in $(aws ecs list-task-definitions --family-prefix "$fam" --query 'taskDefinitionArns[]' --output text 2>/dev/null); do
      aws ecs deregister-task-definition --task-definition "$arn" >/dev/null 2>&1 || true
    done
  done
  ok "deregistered"

  log "Deleting security groups"
  # Ordered: the DB and Redis groups reference the task group, which references the
  # ALB group, so they have to go in dependency order or AWS refuses.
  for name in "${SG_DB}" "${SG_REDIS}" "${SG_TASK}" "${SG_UI}" "${SG_ALB}"; do
    local id; id="$(sg_id "$name")"
    if [ -n "$id" ]; then
      for _ in 1 2 3 4 5 6; do
        aws ec2 delete-security-group --group-id "$id" >/dev/null 2>&1 && { ok "$name deleted"; break; }
        sleep 10
      done
    fi
  done

  log "Deleting secrets"
  for s in "${SECRET_DB}" "${SECRET_DB}-password" "${SECRET_GEMINI}" "${SECRET_ST}" \
           "${SECRET_REDIS}" "${SECRET_REDIS}-token"; do
    aws secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery >/dev/null 2>&1 \
      && ok "$s" || true
  done

  log "Deleting IAM roles"
  for role in "${PREFIX}-execution" "${PREFIX}-task"; do
    for p in $(aws iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
      aws iam detach-role-policy --role-name "$role" --policy-arn "$p" >/dev/null 2>&1 || true
    done
    for p in $(aws iam list-role-policies --role-name "$role" --query 'PolicyNames[]' --output text 2>/dev/null); do
      aws iam delete-role-policy --role-name "$role" --policy-name "$p" >/dev/null 2>&1 || true
    done
    aws iam delete-role --role-name "$role" >/dev/null 2>&1 && ok "$role deleted" || true
  done

  log "Deleting log groups and ECR repositories"
  for lg in "${LOG_GROUP}" "${UI_LOG_GROUP}"; do
    aws logs delete-log-group --log-group-name "$lg" >/dev/null 2>&1 && ok "$lg" || true
  done
  for repo in "${ECR_REPO}" "${UI_ECR_REPO}"; do
    aws ecr delete-repository --repository-name "$repo" --force >/dev/null 2>&1 && ok "$repo" || true
  done

  if [ -n "$DOMAIN" ]; then
    # Certificates can only go once the :443 listener that referenced them is gone
    # with the load balancer, which happened above.
    for c in "$(cert_arn)" "$(cert_arn_for "${APP_HOST}")"; do
      [ -n "$c" ] && aws acm delete-certificate --certificate-arn "$c" >/dev/null 2>&1 \
        && ok "certificate deleted" || true
    done
    local zone; zone="$(zone_id)"
    if [ -n "$zone" ]; then
      aws route53 delete-hosted-zone --id "$zone" >/dev/null 2>&1 \
        && ok "hosted zone deleted" \
        || warn "hosted zone ${zone} not deleted — it still has records (check for the ${APP_HOST} record you added for Vercel)"
    fi
    echo
    warn "THE DOMAIN REGISTRATION IS UNTOUCHED. ${DOMAIN} is still registered and"
    warn "will still auto-renew. Registrations are non-refundable and cannot be"
    warn "deleted, only left to expire or transferred; turn off auto-renew in the"
    warn "Route 53 console if you are done with it."
  fi

  echo
  ok "staging destroyed"
}

case "${1:-plan}" in
  plan)    cmd_plan    ;;
  up)      cmd_up      ;;
  deploy)  cmd_deploy  ;;
  deploy-ui) cmd_deploy_ui ;;
  migrate) cmd_migrate ;;
  verify)  cmd_verify  ;;
  status)  cmd_status  ;;
  logs)    cmd_logs "${2:-10m}" ;;
  frontend-env) cmd_frontend_env ;;
  down)    cmd_down    ;;
  *)       die "unknown command '$1' — one of plan|up|deploy|deploy-ui|migrate|verify|status|logs|frontend-env|down" ;;
esac
