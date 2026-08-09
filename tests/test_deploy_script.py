"""The deploy script's documented subcommands must exist.

This file exists because the same bug happened three times: `keys` was
documented with no branch, `migrate`/`infra` were documented as different while
sharing one branch that ran neither, and `all` claimed a completeness it did not
have. Every instance was quiet — the docs promised a capability the dispatch did
not have, and it surfaced only when the thing you thought ran turned out not to
have run.

A comment asking the next person to keep three lists in step is a request. This
is the same request, enforced.
"""

import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh"
REPO = DEPLOY.parent.parent

#: Printed by the stub `uv` below. Its presence in a run's output means the
#: subcommand got all the way past start_proxy to the thing that talks to the
#: database — which, in these tests, is exactly what must never happen.
REACHED_DB = "REACHED_THE_DATABASE"


@pytest.fixture(scope="module")
def script() -> str:
    return DEPLOY.read_text()


def _usage_subcommands(script: str) -> set[str]:
    """The names in `usage: $0 [all|infra|...]`."""
    m = re.search(r"usage: \$0 \[([^\]]+)\]", script)
    assert m, "the usage() heredoc no longer has a `usage: $0 [...]` line"
    return set(m.group(1).split("|"))


def _case_branches(script: str) -> set[str]:
    """The labels of the dispatch's case statement, minus the `*` fallback."""
    m = re.search(r'case "\$\{1:-all\}" in\n(.*?)\n  esac', script, re.DOTALL)
    assert m, "the dispatch's case statement could not be located"

    branches: set[str] = set()
    for line in m.group(1).splitlines():
        # A branch label is a token at the start of an indented line, ending in
        # ')'. Body lines are indented further or do not match.
        label = re.match(r"^    ([a-z|]+)\)", line)
        if label:
            branches.update(label.group(1).split("|"))
    assert branches, "no case branches were parsed — the regex has drifted"
    return branches


def test_every_documented_subcommand_has_a_branch(script):
    documented = _usage_subcommands(script)
    implemented = _case_branches(script)

    missing = documented - implemented
    assert not missing, (
        f"usage() documents {sorted(missing)} with no branch in the case "
        "statement — a documented command that falls through to exit 1"
    )


def test_every_branch_is_documented(script):
    documented = _usage_subcommands(script)
    implemented = _case_branches(script)

    undocumented = implemented - documented
    assert not undocumented, (
        f"the case statement implements {sorted(undocumented)} which usage() "
        "does not mention — a capability nobody can discover"
    )


def test_the_header_comment_lists_the_same_subcommands(script):
    """The third list. It drifted too, so it is checked too."""
    header = script.split("set -euo pipefail")[0]
    documented = _usage_subcommands(script)

    for name in documented:
        assert re.search(rf"\./scripts/deploy\.sh\s+{name}\b", header) or (
            name == "all" and "./scripts/deploy.sh  " in header
        ), f"the header Usage comment does not mention {name!r}"


def test_all_is_not_merely_infra(script):
    """The exact drift that made `all` a lie: one shared branch for both."""
    assert not re.search(r"^    all\|infra\)", script, re.MULTILINE), (
        "`all` and `infra` share a branch again — they are documented as "
        "different things and must therefore do different things"
    )
    m = re.search(r"^    all\)\n(.*?)^      ;;", script, re.DOTALL | re.MULTILINE)
    assert m, "no `all)` branch found"
    body = m.group(1)
    assert "infra" in body and "migrate" in body, (
        "`all` must run the infrastructure AND the migration — that is the "
        "only thing distinguishing it from `infra`"
    )


def test_infra_does_not_migrate(script):
    m = re.search(r"^    infra\)\n(.*?)^      ;;", script, re.DOTALL | re.MULTILINE)
    assert m, "no `infra)` branch found"
    assert "migrate" not in m.group(1).replace("run '$0 migrate'", ""), (
        "`infra` is documented as the no-migration path and must not migrate"
    )


def test_the_proxy_is_the_only_route_to_cloud_sql(script):
    """`gcloud sql connect` authorizes this machine's IP; the proxy does not.

    setup_sql() keeps the authorized networks list empty so the public IP
    answers to nobody. A subcommand that reaches for `gcloud sql connect` breaks
    that invariant, and breaks it at the worst time: the IP is authorized BEFORE
    the client is exec'd, so a missing psql leaves the instance exposed with
    nothing still running to clean up.
    """
    # Comments are stripped first: the script explains at length WHY it does not
    # use `gcloud sql connect`, and that explanation must not trip its own guard.
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert "gcloud sql connect" not in code, (
        "gcloud sql connect is back — it authorizes this machine's IP on the "
        "production instance; use start_proxy instead"
    )


# --- No subcommand may talk to a listener it did not start -------------------- #
#
# The incident these pin down: a stale cloud-sql-proxy from an earlier session
# still held PROXY_PORT. The run's own proxy lost the bind and died, the
# readiness probe connected to the LEFTOVER listener, and `migrate` reported
# "verified 12 tables present, migration complete" through a proxy it never
# started. It was harmless only because the stale proxy happened to point at the
# same instance — luck, not a property of the script.
#
# These run the real deploy.sh with gcloud/uv/cloud-sql-proxy stubbed, so they
# reach real bash logic and no cloud. `migrate` and `keys` are both checked
# because they share start_proxy, and `keys` is the one that would mint
# production credentials into whatever database answered.

pytestmark_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="needs bash"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stub_dir(tmp_path: Path, *, proxy_body: str) -> Path:
    """A PATH shadowing every external command these branches reach.

    `uv` announces itself rather than doing anything: it stands in for
    seed_keys.py and the migration's psycopg block, so a run that reaches it has
    reached the database, and the assertions can say so directly.
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    written = {
        # Answers the only two calls these paths make: `config set project` and
        # `secrets versions access` for the DB password.
        "gcloud": '#!/usr/bin/env bash\ncase "$*" in\n'
                  '  *"secrets versions access"*) echo "stub-password" ;;\n'
                  "esac\nexit 0\n",
        "cloud-sql-proxy": proxy_body,
        "uv": f'#!/usr/bin/env bash\necho "{REACHED_DB}"\nexit 0\n',
    }
    for name, body in written.items():
        path = stubs / name
        path.write_text(body)
        path.chmod(0o755)
    return stubs


def _run(subcommand: str, stubs: Path, port: int) -> subprocess.CompletedProcess:
    """Run one subcommand of a port-rewritten copy of the real deploy.sh."""
    copy = stubs.parent / "deploy.sh"
    copy.write_text(
        re.sub(r"^PROXY_PORT=\d+$", f"PROXY_PORT={port}",
               DEPLOY.read_text(), count=1, flags=re.MULTILINE)
    )
    copy.chmod(0o755)
    return subprocess.run(
        ["bash", str(copy), subcommand],
        cwd=REPO,
        env={"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(stubs.parent)},
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )


@pytestmark_bash
@pytest.mark.parametrize("subcommand", ["migrate", "keys"])
def test_a_port_bound_before_we_started_is_refused(subcommand, tmp_path):
    """The incident, reproduced: someone else already holds the port.

    The stub proxy stays alive for two seconds before failing, which is what
    makes this a real test rather than a lucky one. The pre-flight check is the
    only thing that catches it: on the first pass through the readiness loop the
    forked proxy has not yet lost its bind, so a liveness check alone still sees
    a live pid while the probe connects to the foreign listener.
    """
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        port = squatter.getsockname()[1]

        stubs = _stub_dir(
            tmp_path, proxy_body="#!/usr/bin/env bash\nsleep 2\nexit 1\n"
        )
        result = _run(subcommand, stubs, port)

    output = result.stdout + result.stderr
    assert REACHED_DB not in output, (
        f"`{subcommand}` talked to a listener it did not start — this is the "
        "stale-proxy bug: the query goes to whatever instance that port serves"
    )
    assert result.returncode != 0, (
        f"`{subcommand}` exited 0 with a port it never bound"
    )
    assert str(port) in output, "the error should name the port it refused"


@pytestmark_bash
@pytest.mark.parametrize("subcommand", ["migrate", "keys"])
def test_a_proxy_that_dies_immediately_stops_the_run(subcommand, tmp_path):
    """The other half: the port is free, but our own proxy does not survive.

    Nothing is listening here, so no connection could succeed — but the run must
    fail on the dead pid rather than spin out the full readiness timeout and
    then fail for the wrong reason.
    """
    port = _free_port()
    stubs = _stub_dir(tmp_path, proxy_body="#!/usr/bin/env bash\nexit 1\n")

    result = _run(subcommand, stubs, port)

    output = result.stdout + result.stderr
    assert REACHED_DB not in output, (
        f"`{subcommand}` proceeded to the database with a dead proxy"
    )
    assert result.returncode != 0, f"`{subcommand}` exited 0 with a dead proxy"
    assert "exited before it was ready" in output, (
        "a dead proxy should be reported as a dead proxy, not as a timeout"
    )


def test_start_proxy_checks_the_port_before_spawning(script):
    """Ordering, pinned: the pre-flight refusal must precede the spawn.

    Cheap guard on the thing the behavioural tests above prove, so that a
    refactor which moves the check after the fork is caught by something that
    names the reason rather than by a timing-dependent failure.
    """
    body = re.search(r"^start_proxy\(\) \{\n(.*?)^\}", script,
                     re.DOTALL | re.MULTILINE)
    assert body, "start_proxy() could not be located"

    preflight = body.group(1).find("/dev/tcp")
    spawn = body.group(1).find("cloud-sql-proxy --port")
    assert preflight != -1, "start_proxy no longer probes the port at all"
    assert spawn != -1, "start_proxy no longer spawns cloud-sql-proxy"
    assert preflight < spawn, (
        "start_proxy spawns the proxy before checking whether the port is "
        "already bound — the stale-proxy bug is back"
    )
