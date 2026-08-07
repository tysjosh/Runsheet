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
#   ./scripts/staging_aws.sh verify     # readiness + auth enforcement
#   ./scripts/staging_aws.sh status     # what exists right now
#   ./scripts/staging_aws.sh down       # DESTROY everything this script made
#
# Idempotent: every step checks for the resource before creating it, so a re-run
# after a failure continues rather than duplicating. Safe to run repeatedly.
#
# ---------------------------------------------------------------------------
# Shape, and why it is not the shape in docs/aws-deployment-strategy.md
# ---------------------------------------------------------------------------
# That document describes the PRODUCTION target: Aurora PostgreSQL, ElastiCache
# Redis, private subnets behind NAT, ALB with ACM. This is staging, provisioned
# for cost and speed, and it departs in four places on purpose:
#
#   * RDS db.t4g.micro, single-AZ, instead of Aurora Serverless v2. No failover.
#   * Redis runs as a SIDECAR container in the task, not ElastiCache. Losing it
#     loses AI-agent conversation memory and nothing else — no business records —
#     which is an acceptable staging trade for ~$11/month and one less service.
#   * SuperTokens core runs as a SIDECAR too, against the same RDS instance, so
#     staging gets its own identity store rather than borrowing development's.
#   * Tasks run in PUBLIC subnets with assignPublicIp=ENABLED, so there is no NAT
#     gateway (~$32/month plus data processing). The task security group allows no
#     inbound except from the ALB.
#
# NO TLS. There is no Route 53 zone for this project and no ACM certificate, so
# the listener is HTTP on the ALB's own DNS name. Session cookies therefore cross
# the internet in cleartext. Do not put real customer data in this environment.
# Fixing it needs a domain: create the zone, request a cert, then add a :443
# listener and set SUPERTOKENS_WEBSITE_DOMAIN / CORS_ORIGINS to the https origin.
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

ALB_NAME="${PREFIX}-alb"
TG_NAME="${PREFIX}-tg"
SG_ALB="${PREFIX}-alb-sg"
SG_TASK="${PREFIX}-task-sg"
SG_DB="${PREFIX}-db-sg"

SECRET_DB="${PREFIX}/database-url"
SECRET_GEMINI="${PREFIX}/gemini-api-key"
SECRET_ST="${PREFIX}/supertokens-api-key"
#: The same database, in libpq form, for the SuperTokens core. Two secrets rather
#: than one because ECS injects a secret verbatim and cannot rewrite the scheme.
SECRET_ST_DB="${PREFIX}/supertokens-db-uri"

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
  cat <<PLAN
Region ................ ${AWS_REGION}
Account ............... ${ACCOUNT_ID}
VPC ................... ${VPC_ID} (default VPC, public subnets, no NAT)

Billable resources this creates:

  RDS ${DB_CLASS} postgres ${DB_ENGINE_VERSION}, ${DB_STORAGE_GB}GB gp3, single-AZ
                                              ~\$13/month
  Application Load Balancer (HTTP :80 only)   ~\$17/month + LCU
  Fargate task, ${TASK_CPU} CPU / ${TASK_MEM} MB, 1 replica     ~\$36/month
  Secrets Manager, 3 secrets                  ~\$1.20/month
  CloudWatch Logs, ECR storage                cents
                                              ------------
                                              ~\$67/month

Not created (and why):
  NAT gateway        tasks get public IPs in public subnets instead   -\$32/mo
  ElastiCache Redis  runs as a sidecar in the task                    -\$11/mo
  Aurora             db.t4g.micro is enough for staging              -\$50/mo+
  ACM certificate    no domain exists, so the listener is HTTP        (see header)

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
  local alb_sg task_sg db_sg
  alb_sg="$(ensure_sg "${SG_ALB}"  "Runsheet staging ALB: public HTTP in")"
  task_sg="$(ensure_sg "${SG_TASK}" "Runsheet staging Fargate tasks")"
  db_sg="$(ensure_sg "${SG_DB}"   "Runsheet staging RDS: from tasks only")"

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

  log "Waiting for RDS to become available"
  aws rds wait db-instance-available --db-instance-identifier "${DB_ID}"
  local endpoint; endpoint="$(db_endpoint)"
  ok "rds endpoint ${endpoint}"

  log "Secrets"
  local db_password
  db_password="$(secret_value "${SECRET_DB}-password")"
  [ -n "$db_password" ] || die "no stored RDS password; delete ${DB_ID} and re-run up"
  ensure_secret "${SECRET_DB}" \
    "postgresql+psycopg://${DB_USER}:${db_password}@${endpoint}:5432/${DB_NAME}"
  ensure_secret "${SECRET_ST_DB}" \
    "postgresql://${DB_USER}:${db_password}@${endpoint}:5432/${DB_NAME}"

  # Reused from development rather than newly issued: staging needs *a* valid
  # Gemini credential to start (settings refuses staging without one) and issuing
  # a second key is a console round-trip. Rotate independently when staging
  # becomes long-lived.
  local gemini
  gemini="$(grep -E '^GEMINI_API_KEY=' "$(dirname "$0")/../.env.development" | cut -d= -f2- || true)"
  [ -n "$gemini" ] || die "GEMINI_API_KEY not found in .env.development"
  ensure_secret "${SECRET_GEMINI}" "$gemini"

  # SuperTokens core runs as a sidecar, so this is the key the app and the core
  # agree on, not a managed-service credential. Generated, not borrowed.
  if [ -z "$(secret_value "${SECRET_ST}")" ]; then
    ensure_secret "${SECRET_ST}" \
      "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  else
    ok "secret ${SECRET_ST} (kept)"
  fi

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
    ok "created listener :80 (HTTP — no certificate available)"
  else
    ok "listener :80"
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
  # start, and it is scoped to these three ARNs rather than secretsmanager:*.
  local doc
  doc="$(printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":["%s","%s","%s","%s"]}]}' \
      "$(secret_arn "${SECRET_DB}")" "$(secret_arn "${SECRET_GEMINI}")" \
      "$(secret_arn "${SECRET_ST}")" "$(secret_arn "${SECRET_ST_DB}")")"
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
  local image="$1" alb="$2"
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

image, alb, exec_arn, task_arn = sys.argv[1:5]
prefix = os.environ["PREFIX"]
log_group = os.environ["LOG_GROUP"]
region = os.environ["AWS_REGION"]
secret_db = os.environ["SECRET_DB_ARN"]
secret_gemini = os.environ["SECRET_GEMINI_ARN"]
secret_st = os.environ["SECRET_ST_ARN"]
secret_st_db = os.environ["SECRET_ST_DB_ARN"]

def logs(stream):
    return {
        "logDriver": "awslogs",
        "options": {
            "awslogs-group": log_group,
            "awslogs-region": region,
            "awslogs-stream-prefix": stream,
        },
    }

# Redis and SuperTokens are sidecars in the same task, so the app reaches both on
# localhost and neither needs a security group, a subnet or a managed service.
# The trade is stated in the script header: Redis here is EPHEMERAL, so a task
# replacement drops AI-agent conversation memory. No business record lives there.
containers = [
    {
        "name": "redis",
        # ECR Public mirrors Docker Hub's library, but only some tags: :7-alpine is
        # absent there while :7 is present. Verified rather than assumed. Preferring
        # the mirror over Docker Hub avoids anonymous pull-rate limits on task
        # replacement, which is a real source of "it redeployed and now it won't
        # start" in Fargate.
        "image": "public.ecr.aws/docker/library/redis:7",
        "essential": True,
        "command": ["redis-server", "--save", "", "--appendonly", "no"],
        "logConfiguration": logs("redis"),
        "healthCheck": {
            "command": ["CMD-SHELL", "redis-cli ping | grep -q PONG"],
            "interval": 10, "timeout": 3, "retries": 5, "startPeriod": 10,
        },
    },
    {
        "name": "supertokens",
        # Docker Hub, not ECR Public. ``public.ecr.aws/supertokens/...`` was a guess
        # and does not exist; the service failed to place a task seven times with
        # CannotPullContainerError before that showed up. Verified with
        # ``docker manifest inspect`` before being written here.
        #
        # Pinned by digest rather than :latest so a task replacement six months from
        # now gets the same core, and because the AWS deployment strategy makes the
        # same argument about never deploying :latest.
        "image": ("docker.io/supertokens/supertokens-postgresql@sha256:"
                  "4516ec7c00b8fb2a773012694d8ebf03b54799d59cfc974160590f16d235ce72"),
        "essential": True,
        # No "environment" block. ``API_KEYS`` arrives via "secrets" below, and ECS
        # rejects a task definition where the same name appears in both — the error
        # is explicit about it, which is how this was caught on first register.
        "secrets": [
            # The core stores its own tables in the same RDS instance, and it needs a
            # libpq URL where the app needs SQLAlchemy's ``postgresql+psycopg://``.
            # ECS cannot transform a secret, so the two forms are two secrets rather
            # than one plus a shell rewrite in the command — which is what was here
            # first, and it also overrode the entrypoint incorrectly: the real one is
            # ``docker-entrypoint.sh supertokens start``, confirmed with
            # ``docker inspect``, not the path that was guessed.
            {"name": "POSTGRESQL_CONNECTION_URI", "valueFrom": secret_st_db},
            {"name": "API_KEYS", "valueFrom": secret_st},
        ],
        "logConfiguration": logs("supertokens"),
        # curl, not wget: this image has no wget, so the probe failed on a core that
        # was up and connected. The task then sat with supertokens RUNNING/UNHEALTHY
        # and api PENDING forever on its dependsOn, which reads like a database
        # problem and was not one. Checked with docker run before changing it.
        #
        # /hello is the right endpoint rather than a TCP check: it answers only once
        # the storage layer is connected, so it distinguishes "process started" from
        # "core is usable".
        "healthCheck": {
            "command": ["CMD-SHELL",
                        "curl -fsS http://localhost:3567/hello | grep -q Hello"],
            "interval": 15, "timeout": 5, "retries": 10, "startPeriod": 60,
        },
    },
    {
        "name": "api",
        "image": image,
        "essential": True,
        "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
        # Ordered start: the app pings SuperTokens during bootstrap, and Redis is
        # the session store. Waiting for HEALTHY (not just STARTED) removes a
        # first-boot race that would otherwise show up as an unexplained restart.
        "dependsOn": [
            {"containerName": "redis", "condition": "HEALTHY"},
            {"containerName": "supertokens", "condition": "HEALTHY"},
        ],
        "environment": [
            {"name": "ENVIRONMENT", "value": "staging"},
            {"name": "PORT", "value": "8080"},
            {"name": "LOG_LEVEL", "value": "INFO"},
            {"name": "SESSION_STORE_TYPE", "value": "redis"},
            {"name": "REDIS_URL", "value": "redis://localhost:6379/0"},
            {"name": "SUPERTOKENS_CONNECTION_URI", "value": "http://localhost:3567"},
            {"name": "SUPERTOKENS_API_DOMAIN", "value": f"http://{alb}"},
            {"name": "SUPERTOKENS_WEBSITE_DOMAIN", "value": f"http://{alb}"},
            {"name": "CORS_ORIGINS", "value": json.dumps([f"http://{alb}"])},
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

  python3 "$gen" "$image" "$alb" "$exec_arn" "$task_arn" \
    > "/tmp/${PREFIX}-taskdef.json"
  aws ecs register-task-definition --cli-input-json "file:///tmp/${PREFIX}-taskdef.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text
}

# ---------------------------------------------------------------------------
# deploy: build, push, register, roll
# ---------------------------------------------------------------------------
cmd_deploy() {
  local sha alb image
  sha="$(git rev-parse --short HEAD)"
  alb="$(alb_dns)"; [ -n "$alb" ] || die "no ALB — run 'up' first"
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
  export PREFIX LOG_GROUP AWS_REGION TASK_FAMILY TASK_CPU TASK_MEM
  export SECRET_DB_ARN="$(secret_arn "${SECRET_DB}")"
  export SECRET_GEMINI_ARN="$(secret_arn "${SECRET_GEMINI}")"
  export SECRET_ST_ARN="$(secret_arn "${SECRET_ST}")"
  export SECRET_ST_DB_ARN="$(secret_arn "${SECRET_ST_DB}")"
  local td; td="$(register_task_def "$image" "$alb")"
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
  local alb; alb="$(alb_dns)"; [ -n "$alb" ] || die "no ALB"
  local base="http://${alb}"
  log "Verifying ${base}"

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

  echo
  ok "staging verified at ${base}"
}

cmd_status() {
  echo "ALB           $(alb_dns || echo '-')"
  echo "RDS           $(db_endpoint || echo '-')  $(aws rds describe-db-instances --db-instance-identifier "${DB_ID}" --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null || echo '-')"
  echo "Service       $(aws ecs describe-services --cluster "${CLUSTER}" --services "${SERVICE}" --query 'services[0].[status,desiredCount,runningCount]' --output text 2>/dev/null || echo '-')"
  echo "Task def      $(aws ecs describe-task-definition --task-definition "${TASK_FAMILY}" --query 'taskDefinition.revision' --output text 2>/dev/null || echo '-')"
  echo "Targets       $(aws elbv2 describe-target-health --target-group-arn "$(aws elbv2 describe-target-groups --names "${TG_NAME}" --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null)" --query 'TargetHealthDescriptions[].TargetHealth.State' --output text 2>/dev/null || echo '-')"
}

cmd_logs() {
  aws logs tail "${LOG_GROUP}" --since "${1:-10m}" --format short
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
  ALB ${ALB_NAME}, target group ${TG_NAME}
  Secrets ${SECRET_DB}, ${SECRET_GEMINI}, ${SECRET_ST} (+ password)
  IAM roles ${PREFIX}-execution, ${PREFIX}-task
  Security groups, subnet group, log group, ECR repo ${ECR_REPO} and its images

It does NOT touch the default VPC, its subnets, or anything belonging to the
unrelated "cleanup" project in this account.

WARNING
  printf 'Type DESTROY to proceed: '
  read -r confirm
  [ "$confirm" = "DESTROY" ] || die "aborted"

  log "Scaling the service to zero"
  aws ecs update-service --cluster "${CLUSTER}" --service "${SERVICE}" --desired-count 0 >/dev/null 2>&1 || true
  aws ecs delete-service --cluster "${CLUSTER}" --service "${SERVICE}" --force >/dev/null 2>&1 \
    && ok "service deleted" || warn "no service"

  log "Deleting the load balancer"
  local alb_arn
  alb_arn="$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
  if [ -n "$alb_arn" ] && [ "$alb_arn" != "None" ]; then
    aws elbv2 delete-load-balancer --load-balancer-arn "$alb_arn" >/dev/null
    # The ALB has to be gone before its security group can be released.
    aws elbv2 wait load-balancers-deleted --load-balancer-arns "$alb_arn" 2>/dev/null || sleep 30
    ok "alb deleted"
  fi
  local tg_arn
  tg_arn="$(aws elbv2 describe-target-groups --names "${TG_NAME}" --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
  [ -n "$tg_arn" ] && [ "$tg_arn" != "None" ] && aws elbv2 delete-target-group --target-group-arn "$tg_arn" >/dev/null && ok "target group deleted" || true

  log "Deleting the database"
  aws rds delete-db-instance --db-instance-identifier "${DB_ID}" \
    --skip-final-snapshot --delete-automated-backups >/dev/null 2>&1 \
    && ok "rds deleting" || warn "no rds"
  aws rds wait db-instance-deleted --db-instance-identifier "${DB_ID}" 2>/dev/null || true
  aws rds delete-db-subnet-group --db-subnet-group-name "${DB_SUBNET_GROUP}" >/dev/null 2>&1 \
    && ok "subnet group deleted" || true

  log "Deleting the cluster"
  aws ecs delete-cluster --cluster "${CLUSTER}" >/dev/null 2>&1 && ok "cluster deleted" || true

  log "Deregistering task definitions"
  for arn in $(aws ecs list-task-definitions --family-prefix "${TASK_FAMILY}" --query 'taskDefinitionArns[]' --output text 2>/dev/null); do
    aws ecs deregister-task-definition --task-definition "$arn" >/dev/null 2>&1 || true
  done
  ok "deregistered"

  log "Deleting security groups"
  # Ordered: the DB group references the task group, which references the ALB
  # group, so they have to go in dependency order or AWS refuses.
  for name in "${SG_DB}" "${SG_TASK}" "${SG_ALB}"; do
    local id; id="$(sg_id "$name")"
    if [ -n "$id" ]; then
      for _ in 1 2 3 4 5 6; do
        aws ec2 delete-security-group --group-id "$id" >/dev/null 2>&1 && { ok "$name deleted"; break; }
        sleep 10
      done
    fi
  done

  log "Deleting secrets"
  for s in "${SECRET_DB}" "${SECRET_DB}-password" "${SECRET_GEMINI}" "${SECRET_ST}" "${SECRET_ST_DB}"; do
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

  log "Deleting log group and ECR repository"
  aws logs delete-log-group --log-group-name "${LOG_GROUP}" >/dev/null 2>&1 && ok "log group" || true
  aws ecr delete-repository --repository-name "${ECR_REPO}" --force >/dev/null 2>&1 && ok "ecr" || true

  echo
  ok "staging destroyed"
}

case "${1:-plan}" in
  plan)    cmd_plan    ;;
  up)      cmd_up      ;;
  deploy)  cmd_deploy  ;;
  migrate) cmd_migrate ;;
  verify)  cmd_verify  ;;
  status)  cmd_status  ;;
  logs)    cmd_logs "${2:-10m}" ;;
  down)    cmd_down    ;;
  *)       die "unknown command '$1' — one of plan|up|deploy|migrate|verify|status|logs|down" ;;
esac
