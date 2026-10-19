"""The numbers that make the policy argument quantitative rather than rhetorical.

DESIGN.md §9. Three of these are correctness checks that should read zero
(staleness, misattribution, violations); the rest are costs you can trade off
against each other.
"""

from __future__ import annotations

from dataclasses import dataclass

from .delivery import Transcript
from .model import Stage
from .policies import Policy
from .sequencer import Sequencer


@dataclass
class Metrics:
    policy: str
    answers: int
    ttfa: float
    ttla: float
    head_of_line: float
    total_tokens: int
    wasted_tokens: int
    staleness: int
    misattribution: int
    sequencer_p99_us: float
    violations: int

    @property
    def wasted_pct(self) -> float:
        return 100.0 * self.wasted_tokens / self.total_tokens if self.total_tokens else 0.0


def compute(
    seq: Sequencer,
    transcript: Transcript,
    policy: Policy,
    sequencer_times: list[float],
    violations: int = 0,
) -> Metrics:
    answers = transcript.answers()
    asked_at = min((r.created_at for r in seq.requests.values()), default=0.0)

    delivered = [r for r in seq.requests.values() if r.stage is Stage.DELIVERED]

    # Time that *finished* answers spent waiting on unfinished ones. This is
    # the cost of ordering, isolated from the cost of the work itself.
    head_of_line = sum(
        (r.delivered_at or 0.0) - (r.ready_at or 0.0)
        for r in delivered
        if r.ready_at is not None and r.delivered_at is not None
    )

    return Metrics(
        policy=policy.name,
        answers=len(answers),
        ttfa=(answers[0].at - asked_at) if answers else float("nan"),
        ttla=(answers[-1].at - asked_at) if answers else float("nan"),
        head_of_line=head_of_line,
        total_tokens=sum(r.total_tokens for r in seq.requests.values()),
        wasted_tokens=sum(r.wasted_tokens for r in seq.requests.values()),
        staleness=sum(1 for rec in seq.journal if rec.stale),
        # An answer shipped with no requote while more than one request was
        # outstanding: the reader had to count positions to know what it
        # answered. FIFO ordering does not prevent this.
        misattribution=sum(
            1 for rec in seq.journal if not rec.attributed and rec.outstanding > 1
        ),
        sequencer_p99_us=_p99(sequencer_times) * 1e6,
        violations=violations,
    )


def _p99(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))]


HEADERS = [
    ("policy", "policy", 17, "<"),
    ("answers", "ans", 4, ">"),
    ("ttfa", "TTFA", 7, ">"),
    ("ttla", "TTLA", 7, ">"),
    ("head_of_line", "head-of-line", 13, ">"),
    ("wasted", "wasted tokens", 14, ">"),
    ("staleness", "stale", 6, ">"),
    ("misattribution", "misattrib", 10, ">"),
    ("sequencer_p99_us", "seq p99", 9, ">"),
    ("violations", "broken", 7, ">"),
]


def table(rows: list[Metrics]) -> str:
    """Render the policy comparison table."""
    head = "  ".join(f"{label:{align}{width}}" for _, label, width, align in HEADERS)
    out = [head, "─" * len(head)]
    for m in rows:
        cells = [
            f"{m.policy:<17}",
            f"{m.answers:>4}",
            f"{m.ttfa:>6.1f}s",
            f"{m.ttla:>6.1f}s",
            f"{m.head_of_line:>12.1f}s",
            f"{m.wasted_tokens:>7} ({m.wasted_pct:>3.0f}%)",
            f"{m.staleness:>6}",
            f"{m.misattribution:>10}",
            f"{m.sequencer_p99_us:>7.0f}µs",
            f"{m.violations:>7}",
        ]
        out.append("  ".join(cells))
    return "\n".join(out)
