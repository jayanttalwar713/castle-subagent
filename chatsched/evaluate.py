"""Measure the triage classifier instead of asserting things about it.

DESIGN.md §4 argues that classifier errors must be asymmetric — that getting
`NEW` wrong in the direction of `AMEND` destroys a question while the reverse
merely produces a redundant answer. The *argument* is sound. The thresholds
that implement it (0.75 and 0.50) were invented, and self-reported model
confidence is famously badly calibrated.

This module replaces the invention with a measurement:

  * per-relationship precision and recall against a labelled set
  * a confusion matrix, so you can see *which way* it is wrong
  * the destructive-error rate — how often a message that deserved an answer
    was classified as something that throws work away
  * a threshold sweep, so the numbers in `triage.py` come from data

Costs a few cents of Haiku. Run with: python -m chatsched eval
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .model import DESTRUCTIVE, Relationship
from .triage import InboundMessage

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CONCURRENCY = 6


@dataclass
class Case:
    context: list[dict]
    #: The chat that preceded this message. Triage needs it to tell a reply to
    #: another member from an instruction to the agent (§4). Stored on the
    #: context rather than the case, so it cannot be tuned per label.
    chat: list[str]
    author: str
    member: str | None
    text: str
    expected: Relationship
    expected_target: str | None
    hard: bool
    note: str


#: Predicting one of these suppresses the message instead of answering it.
#: CANCEL and IGNORE suppress it outright; AMEND and DUPLICATE suppress it by
#: folding it into a request that already exists.
SUPPRESSING = frozenset({
    Relationship.CANCEL,
    Relationship.IGNORE,
    Relationship.AMEND,
    Relationship.DUPLICATE,
})

#: Labels where the message was never going to produce an answer, so failing
#: to act on it costs nothing that belonged to the asker.
NOTHING_TO_LOSE = frozenset({Relationship.CANCEL, Relationship.IGNORE})


def loses_work(used: Relationship, expected: Relationship) -> bool:
    """Does predicting `used` when the answer was `expected` lose work?

    The single definition. It used to exist twice — once as a property and
    once inline in the threshold sweep — and the two drifted, which is how
    DUPLICATE came to be missing from one of them. A rule written down twice
    is a rule that will eventually disagree with itself.

    The rule is deliberately asymmetric, because the harm is:

      IGNORE predicted, CANCEL wanted   the cancel simply fails to happen.
                                        Work continues, the asker gets an
                                        answer they did not want, and nothing
                                        of theirs is destroyed. Redundant.

      CANCEL predicted, IGNORE wanted   chit-chat kills a live request.
                                        Destructive.

    Treating those two as the same thing over-counted destroyed questions —
    the third time this metric has been wrong in the same direction, which is
    the best argument in this file for keeping it under test.
    """
    if used is expected:
        return False
    if used is Relationship.IGNORE:
        # Dropping a message only destroys something if the message was going
        # to produce an answer. A cancel was not.
        return expected not in NOTHING_TO_LOSE
    return used in SUPPRESSING


@dataclass
class Outcome:
    case: Case
    predicted: Relationship   # before the confidence threshold is applied
    used: Relationship        # after
    confidence: float
    target: str | None
    fell_back: bool
    reasoning: str

    @property
    def raw_correct(self) -> bool:
        return self.predicted is self.case.expected

    @property
    def target_correct(self) -> bool:
        if self.case.expected_target is None:
            return True
        return self.target == self.case.expected_target

    @property
    def destructive_error(self) -> bool:
        """The prediction loses work the label would have kept.

        Four ways, all silent from the asker's point of view:
          CANCEL     kills a request that deserved an answer
          IGNORE     drops a question that was never acknowledged
          AMEND      folds a *new* question into an existing one, overwriting it
          DUPLICATE  attaches the asker to someone else's request instead of
                     creating theirs, so their question is never asked

        AMEND is §4's own headline example, so a metric counting only CANCEL
        and IGNORE misses the case the argument is built on. DUPLICATE is the
        same shape and was missed for the same reason: both *suppress a
        distinct question* by folding it into an existing one. It is the
        `two_members` scenario — Dev's "same for me please" answered with
        Ana's numbers — and the eval scored it as merely redundant while the
        scheduler had a whole fix devoted to preventing it.
        """
        return loses_work(self.used, self.case.expected)

    @property
    def disclosure_error(self) -> bool:
        """A `DUPLICATE` that would answer one member with another's figures.

        Strictly worse than losing the question: the asker does not merely go
        unanswered, they are shown holdings that are not theirs. Reported
        separately because "1 in 20 messages misread" is survivable and "1 in
        20 leaks a portfolio" is not — averaging them together hides that.

        The Sequencer refuses to cross this boundary regardless (§11), so this
        counts how often triage *proposes* it, not how often it happens.
        """
        if self.used is not Relationship.DUPLICATE:
            return False
        target = next(
            (r for r in self.case.context if r.get("id") == self.target), None
        )
        if target is None:
            return False
        return target.get("member") != self.case.member

    @property
    def redundant_error(self) -> bool:
        """The recoverable direction: work we needn't have done.

        Noisy, visible, and every question still gets answered.
        """
        if self.used is self.case.expected:
            return False
        return not self.destructive_error


@dataclass
class Report:
    outcomes: list[Outcome] = field(default_factory=list)
    #: Outcomes grouped by pass, so the spread across identical runs is
    #: visible rather than averaged away.
    runs: list[list["Outcome"]] = field(default_factory=list)

    def per_run_accuracy(self) -> list[float]:
        return [
            sum(1 for o in run if o.raw_correct) / len(run)
            for run in self.runs
            if run
        ]

    # -- accuracy ---------------------------------------------------------

    def accuracy(self, only_hard: bool | None = None) -> tuple[int, int]:
        pool = [
            o for o in self.outcomes
            if only_hard is None or o.case.hard is only_hard
        ]
        return sum(1 for o in pool if o.raw_correct), len(pool)

    def per_class(self) -> dict[Relationship, dict[str, float]]:
        out: dict[Relationship, dict[str, float]] = {}
        for rel in Relationship:
            tp = sum(1 for o in self.outcomes
                     if o.case.expected is rel and o.predicted is rel)
            fp = sum(1 for o in self.outcomes
                     if o.case.expected is not rel and o.predicted is rel)
            fn = sum(1 for o in self.outcomes
                     if o.case.expected is rel and o.predicted is not rel)
            support = tp + fn
            if not support and not fp:
                continue
            out[rel] = {
                "precision": tp / (tp + fp) if tp + fp else float("nan"),
                "recall": tp / support if support else float("nan"),
                "support": support,
            }
        return out

    def confusion(self) -> Counter:
        return Counter((o.case.expected, o.predicted) for o in self.outcomes)

    # -- the thing §4 actually claims -------------------------------------

    #: Every relationship that can lose work when predicted wrongly. Wider
    #: than model.DESTRUCTIVE, which gates CANCEL and IGNORE only — AMEND and
    #: DUPLICATE both suppress a distinct question by folding it into an
    #: existing one, which is the direction §4 is built around.
    LOSES_WORK = (Relationship.CANCEL, Relationship.IGNORE,
                  Relationship.AMEND, Relationship.DUPLICATE)

    def sweep(self, thresholds: list[float]) -> list[dict]:
        """What each threshold would have cost, if applied to everything that
        can lose work.

        Raising it trades redundant answers (noisy, recoverable) for destroyed
        questions (silent, not recoverable). §4 says to buy the first with the
        second; this says at what price — and whether the price is payable at
        all, which depends entirely on confidence separating right from wrong.
        """
        rows = []
        for cut in thresholds:
            destroyed = redundant = 0
            for o in self.outcomes:
                used = o.predicted
                if used in self.LOSES_WORK and o.confidence < cut:
                    used = Relationship.NEW
                if loses_work(used, o.case.expected):
                    destroyed += 1
                elif used is not o.case.expected:
                    redundant += 1
            rows.append({"threshold": cut, "destroyed": destroyed,
                         "redundant": redundant})
        return rows

    def confidence_separation(self) -> dict[str, float]:
        """Does confidence actually distinguish right answers from wrong ones?

        The entire §4 mitigation rests on this. If the model reports the same
        confidence whether or not it is correct, no threshold can help and the
        asymmetry has to be bought some other way.
        """
        right = [o.confidence for o in self.outcomes if o.raw_correct]
        wrong = [o.confidence for o in self.outcomes if not o.raw_correct]
        mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")  # noqa: E731
        return {
            "correct_mean": mean(right),
            "incorrect_mean": mean(wrong),
            "incorrect_min": min(wrong) if wrong else float("nan"),
            "gap": mean(right) - mean(wrong),
        }


def load_cases(path: Path | None = None) -> list[Case]:
    raw = json.loads((path or FIXTURES / "triage_eval.json").read_text())
    contexts = raw["contexts"]
    chats = raw.get("chat", {})
    return [
        Case(
            context=contexts[c["context"]],
            chat=list(chats.get(c["context"], ())),
            author=c["author"],
            member=c.get("member"),
            text=c["text"],
            expected=Relationship(c["expect"]),
            expected_target=c.get("expect_target"),
            hard=bool(c.get("hard", False)),
            note=c.get("note", ""),
        )
        for c in raw["cases"]
    ]


async def run_eval(cases: list[Case], triage=None, repeats: int = 1) -> Report:
    from .llm import LLMTriage

    triage = triage or LLMTriage()
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(index: int, case: Case) -> Outcome:
        async with gate:
            message = InboundMessage(
                msg_id=f"eval-{index}",
                author=case.author,
                text=case.text,
                at=float(index),
                member=case.member,
            )
            recent = [
                InboundMessage(
                    msg_id=f"eval-{index}-{i}",
                    author=line.split(":", 1)[0],
                    text=line.split(":", 1)[1].strip(),
                    at=float(index) - len(case.chat) + i,
                )
                for i, line in enumerate(case.chat)
                if ":" in line
            ]
            decision = await triage.aclassify(message, case.context, recent)
        record = triage.decisions[-1]
        return Outcome(
            case=case,
            predicted=Relationship(record["proposed"]),
            used=decision.relationship,
            confidence=decision.confidence,
            target=decision.target_id,
            fell_back=decision.fallback_applied,
            reasoning=decision.reasoning,
        )

    # The classifier is not deterministic, and with ~40 cases a handful of
    # errors moves the headline number several points. Repeating the whole set
    # and reporting the spread is the difference between a measurement and an
    # anecdote.
    runs: list[list[Outcome]] = []
    for _ in range(repeats):
        results = await asyncio.gather(*(one(i, c) for i, c in enumerate(cases)))
        runs.append(list(results))
    return Report(outcomes=[o for run in runs for o in run], runs=runs)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render(report: Report, show_errors: bool = True) -> str:
    lines: list[str] = []
    hit, total = report.accuracy()
    easy_hit, easy_n = report.accuracy(only_hard=False)
    hard_hit, hard_n = report.accuracy(only_hard=True)

    per_run = report.per_run_accuracy()
    lines.append("")
    lines.append(f"cases: {total}   agreement with the labels: {hit}/{total} "
                 f"({100 * hit / total:.0f}%)")
    lines.append(f"  clear-cut: {easy_hit}/{easy_n}"
                 f"   genuinely ambiguous: {hard_hit}/{hard_n}")
    if len(per_run) > 1:
        lines.append(
            f"  across {len(per_run)} identical runs: "
            f"{100 * min(per_run):.0f}% – {100 * max(per_run):.0f}%"
            "   (the classifier is not deterministic)"
        )

    lines.append("")
    lines.append(f"{'relationship':<12} {'precision':>10} {'recall':>8} {'support':>8}")
    lines.append("─" * 42)
    for rel, stats in report.per_class().items():
        lines.append(
            f"{rel.value:<12} {stats['precision']:>10.2f} "
            f"{stats['recall']:>8.2f} {stats['support']:>8.0f}"
        )

    lines.append("")
    lines.append("confusion (expected → predicted), errors only")
    lines.append("─" * 42)
    for (expected, predicted), count in sorted(
        report.confusion().items(), key=lambda kv: -kv[1]
    ):
        if expected is not predicted:
            lines.append(f"  {expected.value:<10} → {predicted.value:<10} {count}")

    destroyed = sum(1 for o in report.outcomes if o.destructive_error)
    redundant = sum(1 for o in report.outcomes if o.redundant_error)
    lines.append("")
    lines.append("error direction — the claim of §4")
    lines.append("─" * 42)
    lines.append(f"  destroyed a real question (silent, unrecoverable) : {destroyed}")
    lines.append(f"  answered something it needn't have (noisy, fine)  : {redundant}")

    disclosures = [o for o in report.outcomes if o.disclosure_error]
    lines.append(
        f"  proposed one member's answer for another (§11)    : {len(disclosures)}"
    )
    for o in disclosures:
        lines.append(f"      {o.case.author}: {o.case.text!r} → {o.target}")
    if not disclosures:
        lines.append("      (the Sequencer refuses these anyway; this counts proposals)")

    sep = report.confidence_separation()
    lines.append("")
    lines.append("does confidence separate right from wrong?")
    lines.append("─" * 42)
    lines.append(f"  mean confidence when correct   : {sep['correct_mean']:.3f}")
    lines.append(f"  mean confidence when incorrect : {sep['incorrect_mean']:.3f}")
    lines.append(f"  lowest confidence on a mistake : {sep['incorrect_min']:.3f}")
    lines.append(f"  gap                            : {sep['gap']:+.3f}")

    lines.append("")
    lines.append("threshold sweep, over everything that can lose work")
    lines.append(f"  {'threshold':>9}  {'destroyed':>9}  {'redundant':>9}")
    lines.append("─" * 42)
    for row in report.sweep([0.0, 0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 0.95]):
        lines.append(f"  {row['threshold']:>9.2f}  {row['destroyed']:>9}  "
                     f"{row['redundant']:>9}")

    if show_errors:
        wrong = [o for o in report.outcomes if not o.raw_correct]
        if wrong:
            lines.append("")
            lines.append("disagreements")
            lines.append("─" * 42)
            for o in wrong:
                flag = " [ambiguous]" if o.case.hard else ""
                lines.append(
                    f"  {o.case.text[:46]!r:<50}{flag}\n"
                    f"      labelled {o.case.expected.value}, "
                    f"predicted {o.predicted.value} (conf {o.confidence:.2f})"
                )
                if o.case.note:
                    lines.append(f"      label rationale: {o.case.note}")
    return "\n".join(lines)
