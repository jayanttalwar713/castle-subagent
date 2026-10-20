"""The answer-quality checks, driven by canned answers.

Every other test in this suite verifies that the right work ran. These are
the only ones that ask whether the answer was *true* — a worker that
confidently reports the wrong exposure passes all nine invariants.

No model calls: the checks are fed answers chosen to break them. A check that
has never been watched to fail is indistinguishable from one that always
returns true, which is the same argument `--no-version-gate` makes for the
staleness invariant.
"""

from __future__ import annotations

import json

import pytest

from chatsched.quality import (
    QualityCase,
    QualityResult,
    check_covers,
    check_grounded,
    known_universe,
    load_cases,
    render,
    tickers_in,
)

# Ana & Cara's household holds NVDA, TSM, QQQ, AAPL, XOM, TLT.
# Dev holds VTI, BRK.B, JNJ, GLD — and no semiconductors at all.

GOOD = ("No — QQQ is your largest line at $449k. On look-through NVDA is "
        "your biggest single name at about $259k.")
LEAK = "Your NVDA position is $221k and TSM adds another $254k."


# ------------------------------------------------------- groundedness --


def test_a_correct_answer_is_grounded():
    assert check_grounded(GOOD, None) == []


def test_quoting_another_members_holdings_is_caught():
    """The disclosure case. §11 stops the Sequencer routing Ana's *answer* to
    Dev; this is the check that Ana's *positions* did not appear in his
    answer either. Dev holds no semiconductors."""
    assert check_grounded(LEAK, "dev") == ["NVDA", "TSM"]


def test_a_hallucinated_position_is_caught():
    """Same mechanism, different failure: a ticker the member does not own,
    whoever it belongs to."""
    assert "GLD" in check_grounded("You hold GLD for protection.", None)


def test_ordinary_portfolio_prose_is_not_flagged():
    """A check that fires on its own vocabulary gets ignored, and an ignored
    check is worse than no check."""
    text = ("Your US equity ETF exposure is fine. The P/E is high and YTD "
            "performance is strong, but AI names dominate. No action needed.")
    assert check_grounded(text, None) == []


def test_company_names_without_tickers_are_not_claimed_as_holdings():
    """Dev's real answer describes NVIDIA look-through *inside VTI*, which is
    true and not a leak. Only an explicit ticker asserts ownership."""
    text = "VTI holds roughly 1.6% NVIDIA and 0.9% Apple by weight."
    assert check_grounded(text, "dev") == []


# ------------------------------------------------------------ coverage --


def test_a_missing_figure_is_caught():
    assert check_covers("Your book looks fine.", ["QQQ", "NVDA"]) == ["QQQ", "NVDA"]


def test_formatting_does_not_count_as_a_different_answer():
    """$448,740 and 448740 are the same fact. The check is about the figure,
    not the comma."""
    assert check_covers("Your largest line is $448,740.", ["448740"]) == []
    assert check_covers("You hold 48,200 in cash.", ["48,200"]) == []


def test_coverage_is_case_insensitive():
    assert check_covers("your largest line is qqq", ["QQQ"]) == []


# ------------------------------------------------------- ticker finding --


def test_the_universe_spans_every_members_fixture():
    """A ticker from another member's file has to be recognised as a ticker,
    or the leak it represents is invisible."""
    universe = known_universe()
    assert {"NVDA", "TSM", "QQQ"} <= universe, "the shared household"
    assert {"VTI", "GLD", "BRK.B"} <= universe, "Dev's book"


def test_tickers_are_found_next_to_punctuation():
    assert tickers_in("NVDA's weight, TSM; and (QQQ).") == {"NVDA", "TSM", "QQQ"}


# ------------------------------------------------------------- the fixture --


def test_the_cases_load_and_include_the_disclosure_trap():
    cases = load_cases()
    assert len(cases) >= 5
    dev = [c for c in cases if c.member == "dev"]
    assert dev, "the cross-member case is the point of the set"
    assert all(c.note for c in cases), "a case without a rationale is a guess"


def test_every_required_fact_is_traceable_to_the_fixture():
    """`must_mention` is generated from portfolio.json, so it cannot drift
    away from the holdings it describes. This asserts that stays true."""
    import pathlib

    from chatsched.quality import FIXTURES

    port = json.loads((FIXTURES / "portfolio.json").read_text())
    held = {h["ticker"] for h in port["holdings"]}
    cash = f"{port['cash_usd']:,}"

    for case in load_cases():
        for fact in case.must_mention:
            assert fact in held or fact == cash, (
                f"{fact!r} is not a holding or the cash balance — "
                "it was typed by hand rather than computed"
            )


# -------------------------------------------------------------- reporting --


def _result(**kw) -> QualityResult:
    case = QualityCase(question="Is NVDA still my biggest position?",
                       member=None, must_mention=["QQQ"], note="x")
    return QualityResult(case=case, answer=kw.pop("answer", GOOD), **kw)


def test_the_report_names_what_went_wrong():
    out = render([_result(answer=LEAK, foreign=["NVDA", "TSM"])])
    assert "does not hold" in out and "NVDA" in out


def test_a_clean_run_says_so():
    out = render([_result(responsive=True)])
    assert "grounded, complete and responsive" in out


def test_an_unjudged_run_does_not_claim_a_responsive_score():
    """With --no-judge the arithmetic still runs, but the report must not
    imply a check that never happened."""
    out = render([_result()])
    assert "not run (judge disabled)" in out


@pytest.mark.parametrize("kw,ok", [
    ({"responsive": True}, True),
    ({"foreign": ["GLD"], "responsive": True}, False),
    ({"missing": ["QQQ"], "responsive": True}, False),
    ({"responsive": False}, False),
    ({}, True),   # judge not run — absence of evidence is not a failure
])
def test_overall_verdict(kw, ok):
    assert _result(**kw).ok is ok
