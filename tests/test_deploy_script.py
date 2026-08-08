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
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh"


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
