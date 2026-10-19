"""The eval's own arithmetic.

These matter more than they look. The first version of `destructive_error`
counted only CANCEL and IGNORE and so reported zero destroyed questions on a
run that had destroyed two — it missed NEW→AMEND, which is the exact failure
§4 is built around. A measurement that is quietly wrong is worse than no
measurement, because it gets believed.

Pure arithmetic over canned outcomes: no model calls.
"""

from __future__ import annotations

import pytest

from chatsched.evaluate import Case, Outcome, Report, load_cases
from chatsched.model import Relationship


def case(expected: Relationship, hard: bool = False) -> Case:
    return Case(context=[], author="ana", member=None, text="…",
                expected=expected, expected_target=None, hard=hard, note="")


def outcome(expected: Relationship, predicted: Relationship,
            confidence: float = 0.9, used: Relationship | None = None,
            hard: bool = False) -> Outcome:
    return Outcome(case=case(expected, hard), predicted=predicted,
                   used=used or predicted, confidence=confidence,
                   target=None, fell_back=False, reasoning="")


# ------------------------------------------------- which way is an error --


@pytest.mark.parametrize(
    "expected,predicted",
    [
        (Relationship.NEW, Relationship.AMEND),       # §4's headline case
        (Relationship.NEW, Relationship.CANCEL),
        (Relationship.NEW, Relationship.IGNORE),
        (Relationship.REFERENCE, Relationship.IGNORE),
        (Relationship.DUPLICATE, Relationship.AMEND),
    ],
)
def test_losing_work_counts_as_destructive(expected, predicted):
    o = outcome(expected, predicted)
    assert o.destructive_error
    assert not o.redundant_error


@pytest.mark.parametrize(
    "expected,predicted",
    [
        (Relationship.CANCEL, Relationship.NEW),
        (Relationship.IGNORE, Relationship.NEW),
        (Relationship.AMEND, Relationship.NEW),
        (Relationship.DUPLICATE, Relationship.NEW),
    ],
)
def test_doing_unnecessary_work_counts_as_redundant(expected, predicted):
    o = outcome(expected, predicted)
    assert o.redundant_error
    assert not o.destructive_error


def test_new_to_amend_is_not_missed():
    """The regression that motivated these tests: this case was silently
    counted as fine, so the eval reported zero destroyed questions on a run
    that had destroyed two."""
    assert outcome(Relationship.NEW, Relationship.AMEND).destructive_error


def test_a_correct_prediction_is_neither():
    o = outcome(Relationship.AMEND, Relationship.AMEND)
    assert not o.destructive_error and not o.redundant_error


# ------------------------------------------------------------- the sweep --


def test_the_sweep_trades_destroyed_questions_for_redundant_ones():
    """Raising the threshold should only ever move errors from the
    unrecoverable column to the recoverable one."""
    report = Report(outcomes=[
        outcome(Relationship.NEW, Relationship.CANCEL, confidence=0.80),
        outcome(Relationship.NEW, Relationship.AMEND, confidence=0.60),
        outcome(Relationship.CANCEL, Relationship.CANCEL, confidence=0.70),
    ])
    rows = {r["threshold"]: r for r in report.sweep([0.0, 0.65, 0.85])}

    assert rows[0.0]["destroyed"] == 2 and rows[0.0]["redundant"] == 0
    # 0.65 downgrades the 0.60 AMEND to NEW: one destroyed error removed.
    assert rows[0.65]["destroyed"] == 1
    # 0.85 downgrades everything. The two mistakes become correct NEWs; the
    # only remaining error is the *correct* CANCEL we threw away.
    assert rows[0.85]["destroyed"] == 0 and rows[0.85]["redundant"] == 1

    destroyed = [rows[t]["destroyed"] for t in (0.0, 0.65, 0.85)]
    assert destroyed == sorted(destroyed, reverse=True), "must be monotonic"


def test_the_sweep_covers_amend_not_just_cancel_and_ignore():
    """model.DESTRUCTIVE gates CANCEL and IGNORE. The sweep has to be wider,
    or it cannot price the NEW→AMEND error at all."""
    assert Relationship.AMEND in Report.LOSES_WORK


# --------------------------------------------- does confidence tell us? --


def test_confidence_separation_reports_the_gap_and_the_worst_case():
    report = Report(outcomes=[
        outcome(Relationship.NEW, Relationship.NEW, confidence=1.0),
        outcome(Relationship.NEW, Relationship.NEW, confidence=0.9),
        outcome(Relationship.NEW, Relationship.AMEND, confidence=0.8),
    ])
    sep = report.confidence_separation()

    assert sep["correct_mean"] == pytest.approx(0.95)
    assert sep["incorrect_mean"] == pytest.approx(0.8)
    assert sep["gap"] == pytest.approx(0.15)
    # The lowest-confidence mistake is what any threshold has to sit below.
    assert sep["incorrect_min"] == pytest.approx(0.8)


# ------------------------------------------------- accuracy and labelling --


def test_hard_cases_are_scored_separately():
    """A good score on the clear-cut cases must not be able to hide a bad one
    on the genuinely ambiguous messages."""
    report = Report(outcomes=[
        outcome(Relationship.NEW, Relationship.NEW),
        outcome(Relationship.NEW, Relationship.AMEND, hard=True),
    ])
    assert report.accuracy() == (1, 2)
    assert report.accuracy(only_hard=False) == (1, 1)
    assert report.accuracy(only_hard=True) == (0, 1)


def test_per_run_spread_is_reported_when_repeating():
    runs = [
        [outcome(Relationship.NEW, Relationship.NEW)],
        [outcome(Relationship.NEW, Relationship.AMEND)],
    ]
    report = Report(outcomes=[o for run in runs for o in run], runs=runs)
    assert report.per_run_accuracy() == [1.0, 0.0]


# ------------------------------------------------------------- the fixture --


def test_the_labelled_set_is_loadable_and_covers_every_relationship():
    cases = load_cases()
    assert len(cases) >= 30
    covered = {c.expected for c in cases}
    assert covered == set(Relationship), f"unexercised: {set(Relationship) - covered}"


def test_the_labelled_set_keeps_real_ambiguity_in_it():
    """Easy cases inflate the headline and teach you nothing. The point of
    the set is the messages a human would hesitate over."""
    cases = load_cases()
    hard = [c for c in cases if c.hard]
    assert len(hard) >= 8
    assert all(c.note for c in hard if c.expected is Relationship.IGNORE), (
        "an ambiguous label needs its rationale written down, or it is "
        "indistinguishable from a mistake"
    )
