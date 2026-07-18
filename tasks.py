"""Dispatching the agent loop: Cloud Tasks in the cloud, in-process locally.

WHY NOT A BackgroundTask
------------------------
FastAPI's BackgroundTasks runs the loop after the response is returned. On Cloud
Run that is a broken design, not merely a slow one. Cloud Run's default billing
mode is request-based, and (verified — Cloud Run "Billing settings", docs.cloud.
google.com/run/docs/configuring/billing-settings):

    "Cloud Run instances are only charged when they process requests, when they
     start, and when they shut down."
    "Selecting instance-based billing allocates CPU even outside of request
     processing, letting you execute short-lived background tasks and other
     asynchronous processing work after returning responses."

i.e. CPU is throttled to ~nothing the moment we return the 202, and the instance
may be shut down entirely. A 30-60s agent loop started after the response would
be throttled, then killed. Every decision would strand in 'processing' — the
exact failure this system exists to catch, reintroduced by the platform.

The two ways out, and why this one:

  - `--no-cpu-throttling` (instance-based billing): keeps CPU on outside
    requests, but bills the whole instance lifecycle rather than request time.
    Combined with the min-instances=1 it effectively pushes you toward, that is
    a permanently-billed instance (~$15/mo) to run a job that takes a minute a
    few times a day. It buys back the broken design at the price of the thing
    that made Cloud Run worth choosing: scale to zero.

  - Cloud Tasks (this): the work is a real queued job. The request that enqueues
    it returns in milliseconds; a SECOND request delivers the task, and that
    request has its own full CPU allocation and its own timeout. Scale to zero
    survives, because an idle service with no tasks has no instances. Retries
    with backoff come free, which a BackgroundTask never had.

So the async model isn't a Cloud Run workaround — it is what the work always
was. The BackgroundTask was borrowing a request's CPU to run a job.

LOCAL / TEST FALLBACK — AND WHY IT FAILS CLOSED
-----------------------------------------------
`enqueue_process` runs the loop in-process when emulation is EXPLICITLY opted
into, so the CLI, the tests and `docker compose up` need no GCP at all.

The opt-in is `TASKS_EMULATED=1` and nothing else. An earlier version also
inferred emulation from "TASKS_QUEUE is unset", which was a real hole: emulation
disables the OIDC check on /internal/process, so a MISSING env var — not a
decision, an absence — turned a public endpoint that spends Anthropic credits
into an open door. `--set-env-vars` REPLACES the whole env block (same as
--set-secrets), so one incomplete redeploy could have dropped TASKS_QUEUE and
silently unauthenticated the internals, with no error anywhere. That is the same
bug class the rest of this system exists to close: identity granted by absence
rather than by presence. Config that is missing must fail, not default to trust.

So, three rules, in order of who catches what:

  1. `emulated()` is False whenever K_SERVICE is set. Cloud Run always sets it,
     so a deployed service CANNOT emulate — not even if TASKS_EMULATED=1 leaked
     into the image or the env. Belt.
  2. `assert_sane_config()` refuses to start at all if a deployed service is
     configured to emulate, or is missing the config that would let it enqueue.
     Braces: crash loudly at boot rather than serve something half-wired.
  3. Absence is never a fallback. If TASKS_QUEUE is missing where it matters,
     that is a fatal error, not a quiet in-process path.

Rule 1 uses K_SERVICE to detect "deployed", which does read the environment —
but only to REFUSE, never to grant. A test cannot be tricked into unsafe
behaviour by where it runs; the worst K_SERVICE can do is make something fail.
"""

import json
import logging
import os
import threading

import config  # noqa: F401  — loads .env before we read os.environ

logger = logging.getLogger("brains-tasks")
if not logger.handlers:
    _h = logging.StreamHandler()  # stderr
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# --- Configuration -----------------------------------------------------------
# Required in any deployed context; absence is a startup error, never a fallback.
TASKS_QUEUE = os.environ.get("TASKS_QUEUE")                    # queue id
TASKS_LOCATION = os.environ.get("TASKS_LOCATION", "asia-south1")
TASKS_PROJECT = os.environ.get("TASKS_PROJECT")
TASKS_SERVICE_ACCOUNT = os.environ.get("TASKS_SERVICE_ACCOUNT")  # OIDC signer
SERVICE_URL = os.environ.get("SERVICE_URL")                    # this service's base URL

#: Env vars without which a deployed service cannot enqueue or verify a task.
REQUIRED_IN_CLOUD = ("TASKS_QUEUE", "TASKS_PROJECT", "TASKS_SERVICE_ACCOUNT",
                     "SERVICE_URL")

# The queue's max_attempts, mirrored here so the handler can tell when Cloud
# Tasks is on its LAST try and the row must be parked in needs_review rather
# than retried into silence. Kept in sync with scripts/deploy.sh.
TASKS_MAX_ATTEMPTS = int(os.environ.get("TASKS_MAX_ATTEMPTS", "5"))


def on_cloud_run() -> bool:
    """True when running as a Cloud Run service.

    K_SERVICE is set by the Cloud Run runtime on every instance and is not
    something a request can influence. Read at CALL time, not bound at import,
    so it stays testable — and because a value captured at import is a value
    frozen before anyone could have set it.
    """
    return bool(os.environ.get("K_SERVICE"))


def emulated() -> bool:
    """True only when emulation was EXPLICITLY asked for, and we are not deployed.

    Two things this deliberately is not:

      - It is not inferred from missing config. `TASKS_QUEUE` being unset used to
        imply emulation, which meant a dropped env var silently disabled the OIDC
        check on /internal/process. Absence is not consent.
      - It is not overridable in the cloud. K_SERVICE wins over TASKS_EMULATED,
        so even if the flag reaches a deployed instance — leaked in an image, set
        by a tired operator, copied from a local .env — the answer is still False
        and the internals still demand a real OIDC token.

    The startup assertion below turns that second case into a crash rather than a
    silent correction, because a deployed service asking to emulate is a mistake
    someone needs to hear about, not a thing to quietly fix at runtime.
    """
    if on_cloud_run():
        return False
    return os.environ.get("TASKS_EMULATED") == "1"


class ConfigError(RuntimeError):
    """Fatal misconfiguration. Raised at startup; the service must not serve."""


def assert_sane_config() -> None:
    """Refuse to start in a state where the internals would be unguarded.

    Called at import of api.main, so a bad deploy dies at boot and the revision
    never takes traffic — Cloud Run keeps the previous revision serving when a
    new one fails its health check, which is exactly the behaviour we want from a
    config mistake. Crashing at boot is the loudest, cheapest failure available;
    the alternative is a healthy-looking service with its front door open.
    """
    if not on_cloud_run():
        return  # local: nothing to assert, emulation is a legitimate choice

    if os.environ.get("TASKS_EMULATED") == "1":
        raise ConfigError(
            "TASKS_EMULATED=1 is set on a deployed service (K_SERVICE="
            f"{os.environ.get('K_SERVICE')!r}). Emulation skips OIDC verification "
            "on /internal/process, which would leave a public endpoint that spends "
            "Anthropic credits open to anyone. Refusing to start."
        )

    missing = [name for name in REQUIRED_IN_CLOUD if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            f"missing required config on a deployed service: {', '.join(missing)}. "
            "These are not optional and there is no fallback: without them the "
            "service cannot enqueue work or verify that a task really came from "
            "Cloud Tasks. Refusing to start."
        )


def process_audience() -> str:
    """The OIDC audience for /internal/process tasks.

    Set EXPLICITLY to the service base URL rather than left to default. Verified
    (OidcToken reference): "Audience to be used when generating OIDC token. If
    not specified, the URI specified in target will be used." Defaulting would
    make the audience the full task URL including its path — the verifier's
    expected value would then be a URL built two different ways in two places,
    and any drift between them fails closed at 3am instead of loudly at deploy.
    One constant, both sides.
    """
    return SERVICE_URL or "http://localhost"


def enqueue_process(decision_id: int, email: str, *, org_id: int, role: str) -> str:
    """Dispatch the loop for one decision. Returns how it was dispatched.

    In the cloud: creates a Cloud Task that POSTs to /internal/process with an
    OIDC token. Returns immediately — the caller's 202 does not wait for the
    loop.

    Locally: runs the loop on a background thread, which is safe here because
    nothing throttles our CPU.
    """
    payload = {"decision_id": decision_id, "email": email, "org_id": org_id,
               "role": role}

    if emulated():
        _run_in_thread(payload)
        return "in-process"

    return _create_cloud_task(payload)


def _run_in_thread(payload: dict) -> None:
    """Local path: run the loop off-thread so the request still returns fast."""
    import decisions

    def run():
        try:
            decisions.process(
                payload["decision_id"], payload["email"],
                org_id=payload["org_id"], role=payload["role"],
            )
        except BaseException:  # noqa: BLE001 — a thread dying silently is the bug
            logger.exception("in-process task failed for decision %s",
                             payload["decision_id"])

    threading.Thread(target=run, daemon=True,
                     name=f"process-{payload['decision_id']}").start()
    logger.info("decision %s dispatched in-process (Cloud Tasks not configured)",
                payload["decision_id"])


# --- Outbound webhook delivery -----------------------------------------------
#
# Same queue, same OIDC signer, same retry policy as /internal/process. A second
# queue would let webhook retries be tuned separately, which is a real argument
# once one noisy receiver can starve the agent loop of dispatch slots — but it is
# a second queue, a second set of variables in deploy.sh and a second thing to
# keep in sync, for a workload of a few deliveries per decision. One queue now;
# the split is a variable block away if delivery volume ever justifies it.

def enqueue_delivery(delivery_id: int, *, org_id: int) -> str:
    """Dispatch ONE webhook delivery. Returns how it was dispatched.

    The payload is deliberately just the delivery id, not the event body. The
    body is rebuilt from the database inside the handler, so a task sitting in
    the queue is not a copy of a decision record waiting to go stale, and the
    task itself carries nothing a queue reader would care about.
    """
    payload = {"delivery_id": delivery_id, "org_id": org_id}

    if emulated():
        _deliver_in_thread(payload)
        return "in-process"

    return _create_cloud_task(payload, path="/internal/deliver",
                              label=f"delivery {delivery_id}")


def _deliver_in_thread(payload: dict) -> None:
    """Local path: POST off-thread so the caller is not held by a receiver."""
    import webhooks

    def run():
        try:
            webhooks.deliver(payload["delivery_id"], attempt=0,
                             is_final_attempt=True)
        except BaseException:  # noqa: BLE001 — a thread dying silently is the bug
            logger.exception("in-process delivery failed for delivery %s",
                             payload["delivery_id"])

    threading.Thread(target=run, daemon=True,
                     name=f"deliver-{payload['delivery_id']}").start()
    logger.info("delivery %s dispatched in-process", payload["delivery_id"])


def _create_cloud_task(payload: dict, *, path: str = "/internal/process",
                       label: str | None = None) -> str:
    """Cloud path: enqueue an HTTP task carrying an OIDC token.

    Verified against docs.cloud.google.com/tasks/docs/samples/
    cloud-tasks-create-http-task-with-token (google-cloud-tasks 2.23.0): the
    surface is typed objects (Task/HttpRequest/OidcToken) wrapped in a
    CreateTaskRequest, not dicts.

    `path` is a parameter because both internal handlers — the agent loop and
    webhook delivery — want the identical task shape and differ only in where
    they land. The AUDIENCE deliberately does not vary with the path: it stays
    the service base URL, computed by `process_audience()` in the one place, so
    the signer and the verifier cannot drift apart per-endpoint.
    """
    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(TASKS_PROJECT, TASKS_LOCATION, TASKS_QUEUE)

    task = tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=f"{SERVICE_URL}{path}",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload).encode(),
            oidc_token=tasks_v2.OidcToken(
                service_account_email=TASKS_SERVICE_ACCOUNT,
                audience=process_audience(),
            ),
        ),
    )
    created = client.create_task(
        tasks_v2.CreateTaskRequest(parent=parent, task=task)
    )
    logger.info("%s enqueued as %s",
                label or f"decision {payload.get('decision_id')}", created.name)
    return created.name
