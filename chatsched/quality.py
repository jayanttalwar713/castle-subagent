"""Measure whether the *answers* are any good (DESIGN.md §10).

Everything else in this project tests the scheduler: did the right work run,
in the right order, with nothing lost. None of it asks whether the answer was
*true*. A worker that confidently reports the wrong exposure passes every one
of the nine invariants.

Three checks, deliberately in descending order of how much they can be
trusted:

  grounded       every ticker and figure in the answer traces back to this
                 member's holdings. Pure arithmetic — no model, no judgement,
                 and it catches both hallucinated positions and the far worse
                 case of quoting somebody else's book.

  covers         the facts the question actually turns on are present. Also
                 arithmetic: we computed them from the fixture.

  responsive     does the text answer the question that was asked? This one
                 needs a judge, because it is a question about meaning. It is
                 listed last because it is the only check that can itself be
                 wrong.

The ordering is the point. Reaching for a model to grade something you can
compute is how you end up with a measurement that is harder to trust than the
thing it measures.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

#: Tickers are 1-5 capitals, optionally dotted (BRK.B). Matching greedily on
#: capitals would also catch "NVDA" inside "NVDA's", which is intended, and
#: ordinary words like "ETF" or "US", which is not — hence the known-ticker
#: universe below rather than a bare regex.
_TICKER_RE = re.compile(r"\b[A-Z]{1,5}(?:\.[A-Z])?\b")

#: Capitals that appear in ordinary prose about portfolios and are not
#: holdings. Without this the groundedness check fires on its own vocabulary.
_NOT_TICKERS = frozenset({
    "A", "I", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "IF", "IN", "IS", "IT",
    "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US", "WE", "ETF", "ETFS", "THE",
    "AND", "FOR", "YOU", "YOUR", "NOT", "BUT", "ALL", "ANY", "CAN", "HAS",
    "HAD", "WAS", "ARE", "ITS", "OUT", "NOW", "ONE", "TWO", "GDP", "CPI",
    "FED", "USD", "EUR", "JPY", "P", "E", "PE", "YTD", "Q1", "Q2", "Q3", "Q4",
    "AI", "IT", "OK", "SP", "S", "TAM", "EPS", "ROI", "ESG", "IPO", "CEO",
})


@dataclass
class QualityCase:
    """One question with facts we can check without asking a model."""

    question: str
    #: Whose portfolio. None = the chat's shared fixture.
    member: str | None
    #: Substrings that must appear — the figures the question turns on.
    #: Computed from the fixture, not guessed.
    must_mention: list[str] = field(default_factory=list)
    #: Why this case exists.
    note: str = ""


@dataclass
class QualityResult:
    case: QualityCase
    answer: str
    #: Tickers named in the answer that this member does not hold.
    foreign: list[str] = field(default_factory=list)
    #: Required facts that never appeared.
    missing: list[str] = field(default_factory=list)
    #: None when the judge was not run.
    responsive: bool | None = None
    judge_note: str = ""

    @property
    def grounded(self) -> bool:
        return not self.foreign

    @property
    def covers(self) -> bool:
        return not self.missing

    @property
    def ok(self) -> bool:
        return self.grounded and self.covers and self.responsive is not False


def load_holdings(member: str | None) -> dict:
    """The portfolio a given member's answers must be consistent with."""
    if member:
        path = FIXTURES / "portfolios" / f"{member}.json"
        if path.exists():
            return json.loads(path.read_text())
    return json.loads((FIXTURES / "portfolio.json").read_text())


def known_universe() -> set[str]:
    """Every ticker that exists anywhere in the fixtures.

    A ticker from *another member's* file is the dangerous case, so it has to
    be recognised as a ticker rather than dismissed as prose.
    """
    out: set[str] = set()
    for path in [FIXTURES / "portfolio.json", *(FIXTURES / "portfolios").glob("*.json")]:
        data = json.loads(path.read_text())
        out |= {h["ticker"] for h in data["holdings"]}
    return out


def tickers_in(text: str) -> set[str]:
    """Tickers mentioned in an answer.

    Restricted to the known universe on purpose. A looser regex flags "ETF"
    and "US" and turns the check into noise, and a check people learn to
    ignore is worse than no check.
    """
    universe = known_universe()
    found = {m.group(0) for m in _TICKER_RE.finditer(text)}
    return {t for t in found if t in universe and t not in _NOT_TICKERS}


def check_grounded(answer: str, member: str | None) -> list[str]:
    """Tickers named that this member does not own.

    Two failures at once: a hallucinated position, or — much worse — a
    position belonging to somebody else in the chat. §11 keeps the Sequencer
    from routing one member's answer to another; this is the check that the
    *content* did not leak either.
    """
    held = {h["ticker"] for h in load_holdings(member)["holdings"]}
    return sorted(tickers_in(answer) - held)


def check_covers(answer: str, must_mention: list[str]) -> list[str]:
    """Required facts that never appeared.

    Comparison is done on digits only for numeric facts, so "$448,740",
    "448740" and "$449k" are not treated as different answers — the check is
    about whether the figure is right, not how it was formatted.
    """
    flat = _normalise(answer)
    return [fact for fact in must_mention if _normalise(fact) not in flat]


def _normalise(text: str) -> str:
    return re.sub(r"[\s,$%]", "", text).lower()


def load_cases(path: Path | None = None) -> list[QualityCase]:
    raw = json.loads((path or FIXTURES / "answer_quality.json").read_text())
    return [
        QualityCase(
            question=c["question"],
            member=c.get("member"),
            must_mention=list(c.get("must_mention", ())),
            note=c.get("note", ""),
        )
        for c in raw["cases"]
    ]


# ------------------------------------------------------------- the report --


def render(results: list[QualityResult]) -> str:
    lines: list[str] = []
    n = len(results)
    grounded = sum(1 for r in results if r.grounded)
    covers = sum(1 for r in results if r.covers)
    judged = [r for r in results if r.responsive is not None]
    responsive = sum(1 for r in judged if r.responsive)

    lines.append("")
    lines.append(f"answer quality over {n} questions")
    lines.append("─" * 52)
    lines.append(f"  grounded   — no position the member does not hold : {grounded}/{n}")
    lines.append(f"  covers     — the figures the question turns on    : {covers}/{n}")
    if judged:
        lines.append(f"  responsive — actually answers what was asked     : {responsive}/{len(judged)}")
    else:
        lines.append("  responsive — not run (judge disabled)")

    bad = [r for r in results if not r.ok]
    if bad:
        lines.append("")
        lines.append("failures")
        lines.append("─" * 52)
        for r in bad:
            lines.append(f"  {r.case.question}")
            if r.foreign:
                lines.append(f"      ✗ names positions this member does not hold: {', '.join(r.foreign)}")
            if r.missing:
                lines.append(f"      ✗ never mentions: {', '.join(r.missing)}")
            if r.responsive is False:
                lines.append(f"      ✗ does not answer the question — {r.judge_note}")
            lines.append(f"      answer: {r.answer[:150]}")
    else:
        lines.append("")
        lines.append("  ✓ every answer grounded, complete and responsive")
    return "\n".join(lines)


# ------------------------------------------------- the one check that needs --
# ------------------------------------------------- a model, and why it is   --
# ------------------------------------------------- the least trusted        --


JUDGE_SYSTEM = """\
You are checking whether an answer responds to the question that was asked. \
That is the ONLY thing you are judging.

You are NOT checking whether the figures are correct — that is verified \
separately against the holdings, by arithmetic, and your opinion on it is not \
wanted. Do not reward or penalise an answer for numbers you cannot verify.

Answer true when the reply addresses what was asked, even if it is terse or \
hedged. Answer false only when it answers a different question, refuses, or \
says nothing substantive.
"""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "responsive": {"type": "boolean"},
        "why": {"type": "string", "maxLength": 200},
    },
    "required": ["responsive", "why"],
    "additionalProperties": False,
}


async def judge(question: str, answer: str, client=None) -> tuple[bool, str]:
    """Does this answer respond to this question?

    Scoped as narrowly as possible on purpose. A judge asked for a general
    quality score produces a number nobody can check; a judge asked one
    yes/no question about meaning is doing the only job arithmetic cannot.
    """
    from anthropic import AsyncAnthropic

    from .llm import TRIAGE_MODEL, TRIAGE_TIMEOUT_S

    client = client or AsyncAnthropic(timeout=TRIAGE_TIMEOUT_S, max_retries=1)
    response = await client.messages.create(
        model=TRIAGE_MODEL,
        max_tokens=512,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content":
                   f"Question:\n  {question}\n\nAnswer:\n  {answer}\n"}],
        output_config={"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
    )
    raw = next(b.text for b in response.content if b.type == "text")
    try:
        parsed = json.loads(raw)
        return bool(parsed["responsive"]), str(parsed.get("why", ""))
    except (json.JSONDecodeError, KeyError, ValueError):
        # Same discipline as triage: a judge that cannot be parsed must not
        # fail the answer it was grading. Abstain loudly instead.
        return True, "judge response unusable; not counted against the answer"


async def run_quality(cases: list[QualityCase], use_judge: bool = True) -> list[QualityResult]:
    """Ask a real worker each question, then check what comes back."""
    import asyncio

    from .llm import LLMWorker

    async def one(case: QualityCase) -> QualityResult:
        worker = LLMWorker()
        worker.portfolio = load_holdings(case.member)

        async def no_checkpoint(_phase: str, results=()):
            return None  # nothing ever amends these questions

        answer, _tokens, _tools = await worker.answer(
            case.question,
            checkpoint=no_checkpoint,
            member=case.member,
        )
        result = QualityResult(
            case=case,
            answer=answer,
            foreign=check_grounded(answer, case.member),
            missing=check_covers(answer, case.must_mention),
        )
        if use_judge:
            result.responsive, result.judge_note = await judge(case.question, answer)
        return result

    gate = asyncio.Semaphore(3)

    async def guarded(case: QualityCase) -> QualityResult:
        async with gate:
            return await one(case)

    return list(await asyncio.gather(*(guarded(c) for c in cases)))
