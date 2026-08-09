"""Session-wide test configuration.

The suite splits in two: tests that need a local Postgres and tests that do not.
The DB half is guarded, so a bare `uv run pytest` on a machine with no database
SKIPS those rather than failing them — a database that isn't running is an
environment condition, not a broken guarantee, and a red suite that means "you
didn't start Docker" trains people to ignore red suites.

What it did not do was say so. The run reported a number well short of the full
suite with nothing on screen explaining the gap, which reads like tests silently
went missing. This adds the lines that close it: how many were skipped, why, and
the command that gets the rest.

Two conditions are reported separately because they have different fixes. Most
skips just want Postgres. Four also want an embedded knowledge base, which costs
a VOYAGE_API_KEY and an ingest run — so `docker compose up -d` alone does not
bring them back, and a summary that implied otherwise would send a reader
looking for a bug that isn't there.

Nothing prints when neither applies, so a normal full run stays quiet.
"""

#: Substrings of the reason strings the suite's own skip guards use. Matched
#: against what the run actually reported rather than re-probing the database:
#: a probe now could disagree with the probe each module made at import time,
#: and the summary should describe the run that happened.
DB_SKIP_REASON = "needs Postgres"
INGEST_SKIP_REASON = "VOYAGE_API_KEY"

_collected = 0


def pytest_collection_modifyitems(session, config, items):
    """Remember the collected total, so the hint can name the full number."""
    global _collected
    _collected = len(items)


def _skip_reason(report) -> str:
    """The human-readable reason off a skip report.

    pytest hands skips over as a (path, lineno, "Skipped: <reason>") tuple, but
    not for every skip kind, so fall back to the string form rather than
    unpacking blind.
    """
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    reasons = [
        _skip_reason(report)
        for report in terminalreporter.stats.get("skipped", [])
    ]
    db_skipped = sum(1 for reason in reasons if DB_SKIP_REASON in reason)
    ingest_skipped = sum(1 for reason in reasons if INGEST_SKIP_REASON in reason)

    if not (db_skipped or ingest_skipped):
        return

    header = "Postgres not reachable" if db_skipped else "Local dependencies"
    terminalreporter.write_sep("=", header, yellow=True)

    if db_skipped:
        terminalreporter.write_line(
            f"{db_skipped} DB tests skipped. Run `docker compose up -d`, then "
            f"re-run for the full {_collected}."
        )
    if ingest_skipped:
        terminalreporter.write_line(
            f"{ingest_skipped} of those also need an embedded knowledge base: "
            f"`uv run python ingest.py` with VOYAGE_API_KEY set."
        )
