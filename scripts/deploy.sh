#!/usr/bin/env bash
#
# BRAINS — provision + deploy to GCP. Idempotent: safe to re-run.
#
# Every command here was checked against current docs (2026) before being
# written; the ones with a history of changing carry a note saying what was
# verified and where. Notable corrections found during that check:
#   - Cloud Run CPU flag is --no-cpu-throttling (there is no --cpu-always-allocated).
#   - PostgreSQL 16+ DEFAULTS to Cloud SQL Enterprise Plus edition, which
#     rejects shared-core tiers. --edition=ENTERPRISE is mandatory, not cosmetic.
#   - --set-secrets REPLACES all secrets on the service; --update-secrets merges.
#   - `gcloud tasks queues create` and `scheduler jobs create` fall back to an
#     App Engine app's location if --location is omitted. We have no App Engine
#     app, so omitting it fails. Always pass it.
#
# Usage:
#   ./scripts/deploy.sh              # everything, including the migration
#   ./scripts/deploy.sh infra        # same, minus the migration
#   ./scripts/deploy.sh migrate      # db/schema.sql against Cloud SQL
#   ./scripts/deploy.sh seed         # db/seed.sql against Cloud SQL (demo data)
#   ./scripts/deploy.sh keys         # mint prod API keys (prints raw keys ONCE)
#   ./scripts/deploy.sh url          # print the service URL
#
# usage() below is the same list. Keep the three in step — see the comment on
# the case statement in main() for why that is called out twice.
#
set -euo pipefail

# ============================================================================
# CONFIGURATION — every value that varies lives here.
# ============================================================================
PROJECT_ID="brains-abhishek-2026"
REGION="asia-south1"                  # Mumbai
SERVICE_NAME="brains-api"

# --- Artifact Registry ---
AR_REPO="brains"
IMAGE_NAME="brains-api"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}:${IMAGE_TAG}"

# --- Cloud SQL ---
SQL_INSTANCE="brains-pg"
SQL_DB="brains"
SQL_USER="brains"
SQL_VERSION="POSTGRES_16"
# db-f1-micro: 1 shared vCPU / 0.6 GB / ~$9.20/mo in Mumbai. The smallest tier
# that exists, and pgvector has no documented minimum (checked: no such
# statement in Cloud SQL docs). Our knowledge base is 7 chunks — an HNSW build
# over that is trivial, so the usual "0.6 GB is too small for HNSW" concern
# (which is about large index builds needing maintenance_work_mem) does not
# apply at this size. Explicitly NOT SLA-covered and not for production:
#   "The db-f1-micro and db-g1-small machine types aren't included in the Cloud
#    SQL SLA ... designed to provide low-cost test and development instances
#    only."  — docs.cloud.google.com/sql/docs/postgres/instance-settings
# Step up to db-g1-small (1.7 GB, ~$30.66/mo) if the KB grows.
SQL_TIER="db-f1-micro"
SQL_EDITION="ENTERPRISE"              # REQUIRED on PG16+: default is ENTERPRISE_PLUS
SQL_STORAGE_SIZE="10GB"
SQL_STORAGE_TYPE="SSD"
SQL_CONNECTION_NAME="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
# Local port for the Cloud SQL Auth Proxy. Shared by `keys`, `migrate` and
# `seed` — every subcommand that needs a real connection to the production
# database goes through the proxy, and none of them touch authorized networks.
# Deliberately not 5432 — that is docker-compose's local Postgres.
PROXY_PORT=5433

# --- Service accounts (dedicated, least privilege; never the default compute SA) ---
RUN_SA="brains-run"                   # the API's own identity
RUN_SA_EMAIL="${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
TASKS_SA="brains-tasks"               # signs the OIDC token on queued tasks
TASKS_SA_EMAIL="${TASKS_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHED_SA="brains-scheduler"           # calls the sweep on a cron
SCHED_SA_EMAIL="${SCHED_SA}@${PROJECT_ID}.iam.gserviceaccount.com"

# --- Secrets (Secret Manager ids) ---
SECRET_ANTHROPIC="anthropic-api-key"
SECRET_VOYAGE="voyage-api-key"
SECRET_DB_PASSWORD="brains-db-password"

# --- Cloud Tasks ---
TASKS_QUEUE="brains-process"
# Retry policy. The loop itself never fails a task (decisions.process files
# needs_review instead of raising), so these retries exist for infrastructure
# failure: DB unreachable, instance evicted mid-run. 5 attempts over ~10 minutes
# of backoff outlasts a rolling deploy or a brief DB blip. When they ARE
# exhausted, /internal/process parks the row in needs_review rather than letting
# it sit in 'processing' — and sweep_stuck is the backstop under that.
TASKS_MAX_ATTEMPTS=5
TASKS_MIN_BACKOFF="10s"
TASKS_MAX_BACKOFF="300s"
TASKS_MAX_DOUBLINGS=4
TASKS_MAX_CONCURRENT=10               # bounds concurrent agent loops => Anthropic spend
TASKS_MAX_DISPATCHES_PER_SEC=5

# --- Cloud Run ---
RUN_MIN_INSTANCES=0                   # scale to zero: the entire cost argument
RUN_MAX_INSTANCES=5                   # a ceiling on runaway spend
# One request per instance. The agent loop is a 30-60s CPU+network job; packing
# several onto one instance would have them contend for the same CPU and make
# every loop slower. Cloud Run scales out instead.
RUN_CONCURRENCY=1
RUN_TIMEOUT="300s"                    # 5m: a 30-60s loop with headroom for retries
RUN_MEMORY="1Gi"
RUN_CPU=1

# --- Cloud Scheduler ---
SCHED_JOB="brains-sweep"
SCHED_CRON="*/15 * * * *"             # every 15 min; matches STUCK_AFTER_MINUTES
SCHED_TZ="Etc/UTC"

# ============================================================================
GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
log()  { echo "${GREEN}==>${NC} $*"; }
warn() { echo "${YELLOW}[!]${NC} $*"; }

# `gcloud ... describe >/dev/null 2>&1` is the idempotency test throughout:
# create only when absent, so a re-run is a no-op rather than an error.
exists() { "$@" >/dev/null 2>&1; }

require_gcloud() {
  command -v gcloud >/dev/null || { echo "gcloud not on PATH"; exit 1; }
  gcloud config set project "${PROJECT_ID}" >/dev/null 2>&1
}

enable_apis() {
  log "Enabling required APIs (idempotent)"
  # Enable EXPLICITLY rather than letting gcloud offer to do it. When an API is
  # missing, `gcloud run deploy` raises an interactive
  #   "API [x] not enabled ... Would you like to enable and retry? (y/N)"
  # and under --quiet that takes the default (no) and the deploy dies with a
  # spectacularly unhelpful "Aborted by user" — which says nothing about which
  # API, and is what happens when a human runs this from CI.
  #
  # sql-component is the easy one to miss: sqladmin.googleapis.com covers
  # managing instances, but --add-cloudsql-instances on Cloud Run needs
  # sql-component too, and having one enabled does not enable the other.
  gcloud services enable \
    run.googleapis.com \
    sqladmin.googleapis.com \
    sql-component.googleapis.com \
    cloudtasks.googleapis.com \
    cloudscheduler.googleapis.com \
    secretmanager.googleapis.com \
    artifactregistry.googleapis.com \
    iamcredentials.googleapis.com \
    --quiet
}

# ---------------------------------------------------------------------------
setup_registry() {
  log "Artifact Registry: ${AR_REPO}"
  if exists gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}"; then
    echo "    exists"
  else
    gcloud artifacts repositories create "${AR_REPO}" \
      --repository-format=docker \
      --location="${REGION}" \
      --description="BRAINS container images"
  fi
  gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
}

build_push() {
  log "Build + push ${IMAGE_URI}"
  # --platform is not optional here: this repo is developed on arm64 Macs and
  # Cloud Run is amd64. Without it the image builds fine, pushes fine, and the
  # container fails to start with a bare "exec format error".
  docker build --platform linux/amd64 -t "${IMAGE_URI}" .
  docker push "${IMAGE_URI}"
}

# ---------------------------------------------------------------------------
setup_service_accounts() {
  log "Service accounts (dedicated, least privilege)"
  for pair in "${RUN_SA}:BRAINS Cloud Run runtime" \
              "${TASKS_SA}:BRAINS Cloud Tasks OIDC signer" \
              "${SCHED_SA}:BRAINS Cloud Scheduler"; do
    sa="${pair%%:*}"; desc="${pair#*:}"
    if exists gcloud iam service-accounts describe \
        "${sa}@${PROJECT_ID}.iam.gserviceaccount.com"; then
      echo "    ${sa} exists"
    else
      gcloud iam service-accounts create "${sa}" --display-name="${desc}"
    fi
  done

  log "IAM: only what each account needs"
  # The API: reach the DB, read its secrets, enqueue its own work. Nothing else.
  # Note NO roles/run.invoker for the runtime SA and no project-wide editor —
  # the default compute SA (which we are deliberately not using) is an Editor on
  # the whole project, which is why it is the wrong identity for anything real.
  grant "${RUN_SA_EMAIL}" roles/cloudsql.client
  grant "${RUN_SA_EMAIL}" roles/secretmanager.secretAccessor
  grant "${RUN_SA_EMAIL}" roles/cloudtasks.enqueuer

  # cloudtasks.enqueuer lets brains-run PUT a task on the queue. It does NOT let
  # it mint a task whose OIDC token is signed AS brains-tasks — that is
  # impersonation, and it needs iam.serviceAccounts.actAs on the impersonated
  # account. Without this the enqueue fails at runtime with:
  #   PermissionDenied: 403 ... lacks IAM permission "iam.serviceAccounts.actAs"
  # which surfaces as a 500 on /decisions/trigger, i.e. only when a real request
  # arrives. Nothing at deploy time catches it.
  #
  # Granted ON THE brains-tasks SERVICE ACCOUNT, not project-wide: this is
  # permission to act as exactly one identity, not as any service account in the
  # project. roles/iam.serviceAccountUser at the project level would be the lazy
  # version and would let brains-run impersonate every SA here, including the
  # scheduler's.
  log "IAM: brains-run may act as brains-tasks (scoped to that one SA)"
  gcloud iam service-accounts add-iam-policy-binding "${TASKS_SA_EMAIL}" \
    --member="serviceAccount:${RUN_SA_EMAIL}" \
    --role=roles/iam.serviceAccountUser \
    --quiet >/dev/null
  echo "    roles/iam.serviceAccountUser on ${TASKS_SA} -> ${RUN_SA}"

  # The tasks SA only needs to be invocable-as; it is granted run.invoker at the
  # service level below, not project-wide.
  # The scheduler SA likewise gets run.invoker on the one service.
}

grant() {  # grant <member> <role> — idempotent by nature of add-iam-policy-binding
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:$1" --role="$2" \
    --condition=None --quiet >/dev/null
  echo "    $2 -> $1"
}

# ---------------------------------------------------------------------------
setup_secrets() {
  log "Secret Manager"
  # Values are read from the local .env / environment and piped in. They are
  # never passed as --set-env-vars and never written to a file: an env var on
  # the service shows up in `gcloud run services describe` and in the console;
  # a mounted secret does not.
  create_secret "${SECRET_ANTHROPIC}" "${ANTHROPIC_API_KEY:-}"
  create_secret "${SECRET_VOYAGE}" "${VOYAGE_API_KEY:-}"

  if exists gcloud secrets describe "${SECRET_DB_PASSWORD}"; then
    echo "    ${SECRET_DB_PASSWORD} exists"
  else
    # Generated here and never printed: nothing needs to see it. The API reads
    # it from Secret Manager; a human who needs it can read it the same way.
    log "generating a random DB password"
    python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" \
      | gcloud secrets create "${SECRET_DB_PASSWORD}" \
          --replication-policy=automatic --data-file=-
  fi
}

create_secret() {  # create_secret <id> <value>
  local id="$1" value="$2"
  if exists gcloud secrets describe "${id}"; then
    echo "    ${id} exists"
    return
  fi
  [ -n "${value}" ] || { echo "ERROR: ${id} has no value; set it in .env"; exit 1; }
  printf '%s' "${value}" | gcloud secrets create "${id}" \
    --replication-policy=automatic --data-file=-
  echo "    ${id} created"
}

# ---------------------------------------------------------------------------
setup_sql() {
  log "Cloud SQL: ${SQL_INSTANCE} (${SQL_TIER}, ${SQL_VERSION}, ${SQL_EDITION})"
  if exists gcloud sql instances describe "${SQL_INSTANCE}"; then
    echo "    exists"
  else
    warn "creating a Cloud SQL instance takes ~10 minutes"
    # No --authorized-networks: the list stays empty, so nothing can reach the
    # DB over its public IP. The only way in is the Cloud SQL connector, which
    # authenticates with IAM and encrypts without us managing certs. Verified:
    # docs.cloud.google.com/sql/docs/postgres/connect-overview — connectors are
    # for "connect[ing] to a Cloud SQL instance using a public IP address
    # without having to configure authorized networks".
    #
    # Private IP was the alternative and is rejected on cost: it needs Direct
    # VPC egress or a Serverless VPC Access connector, and the connector is
    # billed as always-on e2-micro VMs. Public IP + zero authorized networks +
    # the connector is equally closed and free.
    gcloud sql instances create "${SQL_INSTANCE}" \
      --database-version="${SQL_VERSION}" \
      --region="${REGION}" \
      --edition="${SQL_EDITION}" \
      --tier="${SQL_TIER}" \
      --storage-type="${SQL_STORAGE_TYPE}" \
      --storage-size="${SQL_STORAGE_SIZE}" \
      --no-backup \
      --quiet
  fi

  log "database + user"
  exists gcloud sql databases describe "${SQL_DB}" --instance="${SQL_INSTANCE}" \
    && echo "    db exists" \
    || gcloud sql databases create "${SQL_DB}" --instance="${SQL_INSTANCE}"

  local db_password
  db_password="$(gcloud secrets versions access latest --secret="${SECRET_DB_PASSWORD}")"
  if gcloud sql users list --instance="${SQL_INSTANCE}" --format="value(name)" \
       | grep -qx "${SQL_USER}"; then
    echo "    user exists"
  else
    gcloud sql users create "${SQL_USER}" --instance="${SQL_INSTANCE}" \
      --password="${db_password}"
  fi
}

# ---------------------------------------------------------------------------
setup_tasks() {
  log "Cloud Tasks queue: ${TASKS_QUEUE}"
  # --location is mandatory in practice: without it gcloud looks for an App
  # Engine app's location, and this project has no App Engine app.
  if exists gcloud tasks queues describe "${TASKS_QUEUE}" --location="${REGION}"; then
    echo "    exists — updating retry config"
    gcloud tasks queues update "${TASKS_QUEUE}" --location="${REGION}" \
      --max-attempts="${TASKS_MAX_ATTEMPTS}" \
      --min-backoff="${TASKS_MIN_BACKOFF}" \
      --max-backoff="${TASKS_MAX_BACKOFF}" \
      --max-doublings="${TASKS_MAX_DOUBLINGS}" \
      --max-concurrent-dispatches="${TASKS_MAX_CONCURRENT}" \
      --max-dispatches-per-second="${TASKS_MAX_DISPATCHES_PER_SEC}" --quiet
  else
    gcloud tasks queues create "${TASKS_QUEUE}" --location="${REGION}" \
      --max-attempts="${TASKS_MAX_ATTEMPTS}" \
      --min-backoff="${TASKS_MIN_BACKOFF}" \
      --max-backoff="${TASKS_MAX_BACKOFF}" \
      --max-doublings="${TASKS_MAX_DOUBLINGS}" \
      --max-concurrent-dispatches="${TASKS_MAX_CONCURRENT}" \
      --max-dispatches-per-second="${TASKS_MAX_DISPATCHES_PER_SEC}"
  fi
}

# ---------------------------------------------------------------------------
deploy_run() {
  log "Cloud Run: ${SERVICE_NAME}"

  # Two-pass deploy. The service needs its own URL (SERVICE_URL) so it can mint
  # tasks addressed to itself with a matching OIDC audience — but the URL does
  # not exist until the service does. Deploy, read the URL, deploy again with it.
  # (Cloud Run URLs are deterministic enough to guess, but a guessed audience
  # that silently mismatches fails at 3am, not at deploy.)
  local service_url=""
  if exists gcloud run services describe "${SERVICE_NAME}" --region="${REGION}"; then
    service_url="$(gcloud run services describe "${SERVICE_NAME}" \
      --region="${REGION}" --format='value(status.url)')"
  fi

  deploy_revision "${service_url}"

  if [ -z "${service_url}" ]; then
    service_url="$(gcloud run services describe "${SERVICE_NAME}" \
      --region="${REGION}" --format='value(status.url)')"
    log "first deploy done; re-deploying with SERVICE_URL=${service_url}"
    deploy_revision "${service_url}"
  fi

  SERVICE_URL="${service_url}"
  log "service URL: ${SERVICE_URL}"

  # Public access, as its own step (see the note in deploy_revision). Idempotent:
  # re-binding an existing member is a no-op.
  log "granting public invoke (the API's own X-API-Key auth is the real gate)"
  gcloud run services add-iam-policy-binding "${SERVICE_NAME}" --region="${REGION}" \
    --member=allUsers --role=roles/run.invoker --quiet >/dev/null

  # The tasks SA must be able to invoke the service it posts to. Granted on the
  # SERVICE, not the project: this account has no business invoking anything else.
  log "run.invoker for the tasks + scheduler service accounts (service-scoped)"
  gcloud run services add-iam-policy-binding "${SERVICE_NAME}" --region="${REGION}" \
    --member="serviceAccount:${TASKS_SA_EMAIL}" --role=roles/run.invoker --quiet >/dev/null
  gcloud run services add-iam-policy-binding "${SERVICE_NAME}" --region="${REGION}" \
    --member="serviceAccount:${SCHED_SA_EMAIL}" --role=roles/run.invoker --quiet >/dev/null
}

deploy_revision() {
  local service_url="$1"

  # DATABASE_URL via the Cloud SQL unix socket. db.py already reads DATABASE_URL
  # and needs no change: locally it points at docker (host:port), here at the
  # socket the Cloud Run runtime mounts. Verified socket path
  # (docs.cloud.google.com/sql/docs/postgres/connect-run): /cloudsql/<connection
  # name>, and "The PostgreSQL standard requires a .s.PGSQL.5432 suffix" — psycopg
  # appends that itself given host=/cloudsql/...  The Auth Proxy is built into
  # the runtime; no sidecar.
  #
  # The password is NOT in this URL — it arrives as PGPASSWORD from Secret
  # Manager, so nothing secret is in the service's env var list.
  local database_url="postgresql://${SQL_USER}@/${SQL_DB}?host=/cloudsql/${SQL_CONNECTION_NAME}"

  # NOTE on public access: we do NOT pass --allow-unauthenticated here. That
  # flag makes `gcloud run deploy` raise an interactive confirmation, and under
  # --quiet the confirmation takes its safe default (no) and the whole deploy
  # dies with "Aborted by user" — a scripted deploy must not depend on a prompt.
  # The public binding is applied as its own explicit, idempotent step below
  # (add-iam-policy-binding allUsers), which is the same end state and says out
  # loud that this service is internet-facing.
  #
  # Why public at all: the API authenticates with its own X-API-Key scheme, and
  # Cloud Run's IAM invoker check is per-SERVICE, not per-path — there is no way
  # to leave /decisions/* open while IAM guards /internal/*. So /internal/*
  # defends itself at the application layer (auth: signature + audience + issuer
  # SA). The tradeoff is named in the README: the alternative is a second Cloud
  # Run service, --no-allow-unauthenticated, for internals only.
  gcloud run deploy "${SERVICE_NAME}" \
    --image="${IMAGE_URI}" \
    --region="${REGION}" \
    --platform=managed \
    --service-account="${RUN_SA_EMAIL}" \
    --min-instances="${RUN_MIN_INSTANCES}" \
    --max-instances="${RUN_MAX_INSTANCES}" \
    --concurrency="${RUN_CONCURRENCY}" \
    --timeout="${RUN_TIMEOUT}" \
    --memory="${RUN_MEMORY}" \
    --cpu="${RUN_CPU}" \
    --add-cloudsql-instances="${SQL_CONNECTION_NAME}" \
    --set-env-vars="DATABASE_URL=${database_url},TASKS_QUEUE=${TASKS_QUEUE},TASKS_PROJECT=${PROJECT_ID},TASKS_LOCATION=${REGION},TASKS_SERVICE_ACCOUNT=${TASKS_SA_EMAIL},SCHEDULER_SERVICE_ACCOUNT=${SCHED_SA_EMAIL},TASKS_MAX_ATTEMPTS=${TASKS_MAX_ATTEMPTS},SERVICE_URL=${service_url}" \
    --set-secrets="ANTHROPIC_API_KEY=${SECRET_ANTHROPIC}:latest,VOYAGE_API_KEY=${SECRET_VOYAGE}:latest,PGPASSWORD=${SECRET_DB_PASSWORD}:latest" \
    --quiet
}

# ---------------------------------------------------------------------------
setup_scheduler() {
  log "Cloud Scheduler: ${SCHED_JOB} (${SCHED_CRON})"
  # The sweep is the backstop for rows abandoned in 'processing' by a worker
  # that died so hard no handler ran (SIGKILL, OOM, eviction). Authenticated
  # with OIDC like any other caller — a cron endpoint that anyone can hit is a
  # free way to make the DB do work.
  local uri="${SERVICE_URL}/internal/sweep"
  if exists gcloud scheduler jobs describe "${SCHED_JOB}" --location="${REGION}"; then
    gcloud scheduler jobs update http "${SCHED_JOB}" --location="${REGION}" \
      --schedule="${SCHED_CRON}" --time-zone="${SCHED_TZ}" --uri="${uri}" \
      --http-method=POST \
      --oidc-service-account-email="${SCHED_SA_EMAIL}" \
      --oidc-token-audience="${SERVICE_URL}" --quiet
  else
    gcloud scheduler jobs create http "${SCHED_JOB}" --location="${REGION}" \
      --schedule="${SCHED_CRON}" --time-zone="${SCHED_TZ}" --uri="${uri}" \
      --http-method=POST \
      --oidc-service-account-email="${SCHED_SA_EMAIL}" \
      --oidc-token-audience="${SERVICE_URL}" --quiet
  fi
}

# ---------------------------------------------------------------------------
# Everything that stands the service up, with no data migration in it. `all`
# calls this and then migrate(); `infra` calls it alone. One definition, so the
# two can no longer claim to differ while doing the same thing.
infra() {
  enable_apis
  setup_registry
  build_push
  setup_service_accounts
  setup_secrets
  setup_sql
  setup_tasks
  deploy_run
  setup_scheduler
}

# ---------------------------------------------------------------------------
# The Cloud SQL Auth Proxy, shared by every subcommand that needs a real
# connection to the production database.
#
# WHY NOT `gcloud sql connect`, which migrate() used to use: it works by adding
# this machine's public IP to the instance's authorized networks for a few
# minutes, then shelling out to psql. That breaks the invariant setup_sql()
# rests on — the authorized networks list stays EMPTY, so the public IP answers
# to nobody — and it breaks it at the worst possible moment, because if psql is
# not installed the command fails AFTER the IP has been authorized and there is
# nothing left running to remove it. It also silently requires a postgres client
# that this repo otherwise has no need for.
#
# The proxy needs no authorized networks at all: it authenticates with IAM and
# reuses this machine's own credentials. It does need Application Default
# Credentials, which are NOT the same as `gcloud auth login` — see the error
# message below, which has cost enough time to be worth spelling out.
PROXY_PID=""

start_proxy() {
  command -v cloud-sql-proxy >/dev/null || {
    echo "ERROR: cloud-sql-proxy not on PATH."
    echo "  install:  brew install cloud-sql-proxy"
    exit 1
  }
  gcloud auth application-default print-access-token >/dev/null 2>&1 || {
    echo "ERROR: no Application Default Credentials."
    echo "  fix:  gcloud auth application-default login"
    echo "  NOTE: 'gcloud auth login' is a DIFFERENT credential and is not enough."
    exit 1
  }

  cloud-sql-proxy --port "${PROXY_PORT}" "${SQL_CONNECTION_NAME}" &
  PROXY_PID=$!
  # Double quotes so the pid is expanded NOW and baked into the trap. Single
  # quotes would defer the lookup to EXIT, by which point the variable may be
  # gone, and under `set -u` the trap would die and take a successful run's
  # exit status with it.
  trap "kill ${PROXY_PID} 2>/dev/null || true" EXIT

  # The proxy binds its listener only once it is ready to serve, so a successful
  # connect IS the readiness signal. /dev/tcp is a bash builtin — no nc needed.
  # The pid check leads the loop so a dead proxy is caught before anything is
  # allowed to answer on the port.
  log "waiting for the proxy on 127.0.0.1:${PROXY_PORT}"
  local i
  for i in $(seq 1 40); do
    kill -0 "${PROXY_PID}" 2>/dev/null || { echo "ERROR: proxy exited"; exit 1; }
    (exec 3<>"/dev/tcp/127.0.0.1/${PROXY_PORT}") 2>/dev/null && break
    sleep 0.5
    [ "${i}" -lt 40 ] || { echo "ERROR: proxy did not come up"; exit 1; }
  done
}

stop_proxy() {
  [ -n "${PROXY_PID}" ] || return 0
  kill "${PROXY_PID}" 2>/dev/null || true
  wait "${PROXY_PID}" 2>/dev/null || true
  PROXY_PID=""
  trap - EXIT
}

# ---------------------------------------------------------------------------
migrate() {
  log "Applying db/schema.sql to Cloud SQL via the Auth Proxy"
  local db_password
  db_password="$(gcloud secrets versions access latest --secret="${SECRET_DB_PASSWORD}")"

  start_proxy

  # psycopg rather than psql: the proxy removes the need to authorize an IP, but
  # not the need for a client, and this repo already depends on psycopg while it
  # does not depend on a postgres CLI. `uv run` is the same entry point mint_keys
  # uses. PGPASSWORD keeps the credential out of the URL that lands in errors.
  #
  # schema.sql is CREATE ... IF NOT EXISTS throughout, so this is idempotent and
  # safe to re-run; it adds what is missing and leaves everything else alone.
  DATABASE_URL="postgresql://${SQL_USER}@127.0.0.1:${PROXY_PORT}/${SQL_DB}" \
    PGPASSWORD="${db_password}" \
    uv run python - <<'PY'
import os, re, sys
import psycopg

sql = open("db/schema.sql").read()
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()

    # VERIFY. Applying DDL without checking what landed is how a migration gets
    # reported as done when it silently did nothing — every table in the file
    # must actually be present afterwards, or this exits non-zero.
    expected = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        present = {r[0] for r in cur.fetchall()}

missing = sorted(expected - present)
if missing:
    sys.exit(f"MIGRATION VERIFY FAILED: missing tables {missing}")
print(f"verified {len(expected)} tables present: {', '.join(sorted(expected))}")
PY

  stop_proxy
  log "migration complete"
}

# ---------------------------------------------------------------------------
seed() {
  log "Applying db/seed.sql to Cloud SQL via the Auth Proxy"
  local db_password
  db_password="$(gcloud secrets versions access latest --secret="${SECRET_DB_PASSWORD}")"

  start_proxy

  # `|| true`: seed.sql is demo data, and re-running it bounces off unique
  # constraints on rows that already exist. That is an expected outcome of
  # re-seeding, not a failure of the deploy — which is exactly why this is its
  # own subcommand rather than something `migrate` drags along, where a tolerated
  # error would sit next to DDL whose errors must NOT be tolerated.
  DATABASE_URL="postgresql://${SQL_USER}@127.0.0.1:${PROXY_PORT}/${SQL_DB}" \
    PGPASSWORD="${db_password}" \
    uv run python -c "
import os, psycopg
with psycopg.connect(os.environ['DATABASE_URL']) as conn:
    with conn.cursor() as cur:
        cur.execute(open('db/seed.sql').read())
    conn.commit()
print('seed applied')
" || log "seed reported errors (expected when the demo rows already exist)"

  stop_proxy
}

# ---------------------------------------------------------------------------
mint_keys() {
  log "Minting production API keys against ${SQL_INSTANCE}"

  # Run LOCALLY, on purpose. The obvious alternative — a Cloud Run job running
  # the same image, which already has the Cloud SQL socket mounted — writes the
  # keys straight into Cloud Logging, where they are retained, indexed, and
  # readable by anyone holding roles/logging.viewer. That is the one thing
  # seed_keys.py exists to prevent: it prints each key once precisely because
  # `api_keys` stores only sha256(key) and nothing can recover it afterwards.
  # Printing to the operator's terminal and nowhere else keeps that property.
  # Nothing below redirects, tees, or captures stdout.
  #
  # The Auth Proxy, not `gcloud sql connect`: seed_keys.py needs psycopg, which
  # needs a real local endpoint, and authorizing our own IP would break the
  # empty-authorized-networks invariant. See start_proxy for the full argument —
  # it is shared with migrate() and seed() so there is one copy of it.
  #
  # Port 5433 rather than 5432 is a safety choice, not a courtesy:
  # docker-compose's local Postgres owns 5432, so on 5432 the proxy would lose
  # the bind, the readiness probe would happily connect to the LOCAL dev
  # database instead, and seed_keys.py would mint production keys into a docker
  # volume and print them as though they were real.
  command -v uv >/dev/null || { echo "ERROR: uv not on PATH"; exit 1; }

  local db_password
  db_password="$(gcloud secrets versions access latest --secret="${SECRET_DB_PASSWORD}")"

  start_proxy

  # DATABASE_URL and PGPASSWORD are passed as REAL environment variables, which
  # is load-bearing rather than stylistic: config.py loads .env with
  # override=False, so a real var always wins. Without this, a developer with a
  # local .env would have seed_keys.py quietly mint "production" keys into their
  # docker Postgres and print keys that authenticate against nothing.
  #
  # The password rides in PGPASSWORD, not in the URL — same split as
  # deploy_revision() and migrate(), and it keeps the credential out of the URL
  # that gets echoed around in errors.
  DATABASE_URL="postgresql://${SQL_USER}@127.0.0.1:${PROXY_PORT}/${SQL_DB}" \
    PGPASSWORD="${db_password}" \
    uv run python seed_keys.py

  stop_proxy

  # Idempotent by seed_keys.py's own rule: the two keys are matched by name and
  # skipped when present, so a re-run prints nothing and mints nothing. Rotating
  # is a deliberate act — revoke, then re-run.
}

usage() {
  cat <<EOF
usage: $0 [all|infra|migrate|seed|keys|url]

  all      infra, then migrate — a complete deploy from nothing
  infra    registry, image, SAs, secrets, SQL, tasks, run, scheduler. NO migration
  migrate  db/schema.sql against Cloud SQL, via the Auth Proxy, verified
  seed     db/seed.sql against Cloud SQL (demo data; safe to re-run)
  keys     mint production API keys (prints raw keys ONCE)
  url      print the service URL
EOF
}

# EVERY SUBCOMMAND IN usage() MUST HAVE A BRANCH HERE — THIS DRIFTED THREE TIMES.
#
#   1. `keys` was documented in the header and had no branch at all, so the
#      documented command silently fell through to usage-and-exit-1.
#   2. `migrate` and `infra` were documented as different things while sharing
#      one `all|infra)` branch that ran neither migration.
#   3. `all` claimed to be a complete deploy while being literally identical to
#      `infra`, so nobody who trusted it ever migrated.
#
# The failure mode is always the same and always quiet: the docs describe a
# capability the dispatch does not have, and you only find out when the thing
# you thought ran turns out not to have run. If you add a subcommand, add it in
# three places — the header comment, usage(), and this case statement.
main() {
  require_gcloud
  case "${1:-all}" in
    all)
      infra
      # AFTER the infrastructure exists and BEFORE anyone is told it is done:
      # the schema has to be in place for the revision that is already serving.
      # The gate fails closed on an unreadable org_settings, so a deploy that
      # ran ahead of its migration would hold every decision for a human — safe,
      # but an outage of the autonomy that is the entire product.
      migrate
      log "done. service: ${SERVICE_URL}"
      ;;
    infra)
      infra
      log "done (no migration — run '$0 migrate'). service: ${SERVICE_URL}"
      ;;
    migrate) migrate ;;
    seed)    seed ;;
    keys)    mint_keys ;;
    url)     gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" \
               --format='value(status.url)' ;;
    *)       usage; exit 1 ;;
  esac
}

main "$@"
