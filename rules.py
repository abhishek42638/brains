"""Pure, deterministic scoring rules, and the registry that selects them.

No DB, no I/O, no side effects — every function maps inputs to
``(points, reason)`` (or a band string) so the rules can be unit-tested in
isolation and versioned independently of how a lead row is fetched.

The REGISTRY at the bottom is what makes a decision TYPE a first-class thing:
scoring is looked up by type rather than being the one hardcoded thing this
system knows how to do.
"""

import re

# The type every decision has unless something says otherwise. Defined here,
# next to the registry it keys into, because this is the module that decides
# what a type MEANS — everywhere else it is a string being carried around.
DEFAULT_DECISION_TYPE = "lead_qualification"

RULES_VERSION = "1.0.0"

SENIOR_TITLE_KEYWORDS = (
    "ceo",
    "cfo",
    "vp",
    "chief",
    "head",
    "director",
    "owner",
    "founder",
    "president",
    "partner",
    "principal",
    "chairman",
)

# Words that mark a support / individual-contributor role. If any appears, the
# title is not senior even when a senior keyword also appears as a whole word
# (e.g. "Assistant to the VP", "Head Cashier").
JUNIOR_TITLE_MARKERS = (
    "assistant",
    "cashier",
    "coordinator",
    "intern",
    "associate",
)


def _words(text):
    return set(re.findall(r"[a-z]+", text.lower()))


def score_from_title(title):
    title = title or ""
    words = _words(title)
    if words & set(JUNIOR_TITLE_MARKERS):
        return 0, f"title {title!r} is not senior (+0)"
    matched = next((k for k in SENIOR_TITLE_KEYWORDS if k in words), None)
    if matched:
        return 30, f"senior title: {title!r} matches {matched!r} (+30)"
    return 0, f"title {title!r} is not senior (+0)"


def score_from_size(employee_count, has_company):
    if not has_company:
        return 0, "no company on lead, so no employee-count points (+0)"
    if employee_count is None:
        return 0, "employee_count unknown (+0)"
    if employee_count >= 500:
        return 25, f"employee_count {employee_count} >= 500 (+25)"
    if employee_count >= 50:
        return 15, f"employee_count {employee_count} >= 50 (+15)"
    return 0, f"employee_count {employee_count} < 50 (+0)"


def score_from_revenue(revenue, has_company):
    if not has_company:
        return 0, "no company on lead, so no revenue points (+0)"
    if revenue is None:
        return 0, "annual_revenue_usd unknown (+0)"
    if revenue >= 100_000_000:
        return 25, f"annual_revenue_usd {revenue} >= 100M (+25)"
    if revenue >= 10_000_000:
        return 15, f"annual_revenue_usd {revenue} >= 10M (+15)"
    return 0, f"annual_revenue_usd {revenue} < 10M (+0)"


def score_from_source(source):
    if source in ("webinar", "website_form"):
        return 20, f"source {source!r} is high intent (+20)"
    return 0, f"source {source!r} is not high intent (+0)"


def band_for(score):
    if score >= 80:
        return "hot"
    if score >= 50:
        return "warm"
    return "cold"


# --- The registry: scoring, selected by decision type ------------------------

def score_lead_qualification(facts: dict) -> dict:
    """Score one lead. Returns {"score", "band", "reasons"}.

    `facts` is a plain dict — whatever fetched it is not this module's business.
    Keys read: title, employee_count, annual_revenue_usd, source, company_name
    (whose presence, not value, is what "has a company" means; a cross-tenant
    company must arrive here as absent rather than as firmographics).
    """
    has_company = facts.get("company_name") is not None

    title_pts, title_reason = score_from_title(facts.get("title"))
    size_pts, size_reason = score_from_size(facts.get("employee_count"), has_company)
    revenue_pts, revenue_reason = score_from_revenue(
        facts.get("annual_revenue_usd"), has_company
    )
    source_pts, source_reason = score_from_source(facts.get("source"))

    score = title_pts + size_pts + revenue_pts + source_pts
    return {
        "score": score,
        "band": band_for(score),
        "reasons": [title_reason, size_reason, revenue_reason, source_reason],
    }


class UnknownDecisionType(ValueError):
    """A decision type nothing in this build knows how to score."""


# Scoring, by decision type. Each entry carries its scorer AND the version of
# the rules that scorer implements, because those two travel together: a
# rules_version pulled from a module-level constant would keep reporting "1.0.0"
# for a second type whose rules had nothing to do with 1.0.0.
#
# LEAD QUALIFICATION IS THE FIRST ENTRY, NOT THE ONLY POSSIBLE ONE — AND ADDING
# A SECOND IS DELIBERATELY NOT DONE HERE. The point of this registry is the
# SEAM: a decision carries a type, policy is keyed by (org, type), and scoring
# resolves through this table. A second type would also want its own tools and
# its own prompt, which is a larger piece of work and a different one. Building
# the seam while it is free — one entry, no data to migrate — is the whole
# exercise. Populating it is for when a second type actually exists.
REGISTRY = {
    DEFAULT_DECISION_TYPE: {
        "score": score_lead_qualification,
        "rules_version": RULES_VERSION,
    },
}


def known_decision_types() -> tuple[str, ...]:
    """Every type this build can score, for validation and error messages."""
    return tuple(sorted(REGISTRY))


def entry_for(decision_type: str) -> dict:
    """The registry entry for a type. Raises UnknownDecisionType if absent.

    Raising rather than falling back to the default is the point: a type nothing
    can score must not be scored by the rules of a different type and filed as
    though the number meant something.
    """
    try:
        return REGISTRY[decision_type]
    except KeyError:
        raise UnknownDecisionType(
            f"unknown decision_type {decision_type!r}; "
            f"known: {', '.join(known_decision_types())}"
        ) from None
