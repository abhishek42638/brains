"""Pure, deterministic lead-scoring rules.

No DB, no I/O, no side effects — every function maps inputs to
``(points, reason)`` (or a band string) so the rules can be unit-tested in
isolation and versioned independently of how a lead row is fetched.
"""

import re

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
