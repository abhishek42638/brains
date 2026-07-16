"""Unit tests for the pure scoring rules in rules.py.

No DB and no monkeypatching: every test passes plain values to a pure function
and asserts on the returned (points, reason) or band string.
"""

import pytest

from rules import (
    band_for,
    score_from_revenue,
    score_from_size,
    score_from_source,
    score_from_title,
)


# --------------------------------------------------------------------------- #
# band_for — boundaries are now testable at exact scores                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "score, band",
    [
        (80, "hot"),
        (79, "warm"),
        (50, "warm"),
        (49, "cold"),
        (0, "cold"),
        (100, "hot"),
    ],
)
def test_band_for(score, band):
    assert band_for(score) == band


# --------------------------------------------------------------------------- #
# score_from_title — true positives, new keywords, and the 3 false positives   #
# --------------------------------------------------------------------------- #


def test_title_true_positive():
    points, reason = score_from_title("VP of Sales")
    assert points == 30
    assert "senior title" in reason


@pytest.mark.parametrize(
    "title",
    ["CEO", "CFO", "President", "Partner", "Principal", "Chairman"],
)
def test_title_newly_added_keywords_score_senior(title):
    points, _ = score_from_title(title)
    assert points == 30


@pytest.mark.parametrize(
    "title",
    ["Head Cashier", "Assistant to the VP", "confounder"],
)
def test_title_false_positives_are_not_senior(title):
    # These used to score +30 via naive substring matching. Now they must not.
    points, _ = score_from_title(title)
    assert points == 0


def test_title_none_is_not_senior():
    points, _ = score_from_title(None)
    assert points == 0


def test_title_empty_is_not_senior():
    points, _ = score_from_title("")
    assert points == 0


# --------------------------------------------------------------------------- #
# score_from_size — bands, missing company, and the None-collapse fix          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "employees, points",
    [
        (500, 25),
        (501, 25),
        (50, 15),
        (499, 15),
        (49, 0),
        (0, 0),
    ],
)
def test_size_bands(employees, points):
    pts, _ = score_from_size(employees, has_company=True)
    assert pts == points


def test_size_no_company():
    pts, reason = score_from_size(None, has_company=False)
    assert pts == 0
    assert "no company" in reason


def test_size_null_employee_count_with_company_does_not_claim_no_company():
    # The None-collapse bug: a company WITH a NULL employee_count previously
    # reported "no company on lead". It must not anymore.
    pts, reason = score_from_size(None, has_company=True)
    assert pts == 0
    assert "no company" not in reason


# --------------------------------------------------------------------------- #
# score_from_revenue — bands, missing company, NULL revenue                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "revenue, points",
    [
        (100_000_000, 25),
        (250_000_000, 25),
        (10_000_000, 15),
        (99_999_999, 15),
        (9_999_999, 0),
        (0, 0),
    ],
)
def test_revenue_bands(revenue, points):
    pts, _ = score_from_revenue(revenue, has_company=True)
    assert pts == points


def test_revenue_no_company():
    pts, reason = score_from_revenue(None, has_company=False)
    assert pts == 0
    assert "no company" in reason


def test_revenue_null_with_company_does_not_claim_no_company():
    pts, reason = score_from_revenue(None, has_company=True)
    assert pts == 0
    assert "no company" not in reason


# --------------------------------------------------------------------------- #
# score_from_source                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "source, points",
    [
        ("webinar", 20),
        ("website_form", 20),
        ("cold_list", 0),
        ("referral", 0),
        (None, 0),
    ],
)
def test_source(source, points):
    pts, _ = score_from_source(source)
    assert pts == points
