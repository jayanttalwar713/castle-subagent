"""System invariants, checked after a run.

These are the properties DESIGN.md claims. Writing them as executable checks
rather than prose is what lets the fuzzer assert them over thousands of
randomised message streams, and what makes `--no-version-gate` a
*demonstration* rather than an assertion: turn the gate off and `no_stale`
starts failing.

Each check returns violations rather than raising, so a single run can report
everything that is wrong at once.
"""

from __future__ import annotations

from dataclasses import dataclass

from .delivery import Transcript
from .model import DeliveryKind, Stage
from .policies import Policy
from .sequencer import Sequencer

#: Delivery kinds that settle a request from the reader's point of view.
RESOLVING = frozenset(
    {
        DeliveryKind.ANSWER,
        DeliveryKind.ADDENDUM,
        DeliveryKind.SUPERSEDE,
        DeliveryKind.CANCEL_ACK,
        DeliveryKind.ALREADY_ANSWERED,
        DeliveryKind.DROPPED_NOTICE,
    }
)

ANSWERING = frozenset(
    {DeliveryKind.ANSWER, DeliveryKind.ADDENDUM, DeliveryKind.SUPERSEDE}
)


@dataclass(frozen=True)
class Violation:
    invariant: str
    detail: str

    def __str__(self) -> str:
        return f"{self.invariant}: {self.detail}"


def check(
    seq: Sequencer, transcript: Transcript, policy: Policy, pool_size: int
) -> list[Violation]:
    out: list[Violation] = []
    out += _no_deadlock(seq)
    out += _completeness(seq, transcript)
    out += _no_stale(seq)
    out += _non_contradiction(transcript)
    out += _causal_consistency(seq)
    out += _attribution(transcript, policy)
    out += _pool_bound(seq, policy, pool_size)
    out += _tokens_accounted(seq)
    out += _snapshot_coherence(seq)
    return out


def _no_deadlock(seq: Sequencer) -> list[Violation]:
    """Nothing is left waiting forever.

    Catches the dependency deadlock of §8 — a pool full of dependents waiting
    on parents that can never be admitted — and any request stranded by an
    orphaned edge.
    """
    stuck = [r.id for r in seq.requests.values() if not r.is_terminal]
    if stuck:
        detail = ", ".join(
            f"{seq.requests[rid].id}={seq.requests[rid].stage.value}" for rid in stuck
        )
        return [Violation("no_deadlock", f"non-terminal at end of run: {detail}")]
    return []


def _completeness(seq: Sequencer, transcript: Transcript) -> list[Violation]:
    """Nothing silently vanishes — §3's second reader-facing property.

    Every request the reader was acknowledged for must end with something
    visible: an answer, a cancellation, or an explicit "couldn't answer".
    """
    resolved = {
        line.request_id for line in transcript.lines if line.kind in RESOLVING
    }
    missing = [r.id for r in seq.requests.values() if r.id not in resolved]
    if missing:
        return [
            Violation(
                "completeness",
                f"requests with no reader-visible outcome: {', '.join(sorted(missing))}",
            )
        ]
    return []


def _no_stale(seq: Sequencer) -> list[Violation]:
    """Version gating (§7b): an answer computed against a spec that has since
    changed must never reach the chat."""
    return [
        Violation(
            "no_stale",
            f"{rec.request_id} delivered at v{rec.version} after its spec changed",
        )
        for rec in seq.journal
        if rec.stale
    ]


def _non_contradiction(transcript: Transcript) -> list[Violation]:
    """The reader never sees two live answers that disagree (§3).

    The property is about *announcement*, not about counting deliveries.
    Walking a request's answers in time order, the answer text may change —
    but only on a line that says so, an addendum or a replacement notice. Two
    deliveries of the same text are fine: that is duplicate fan-out, one
    answer sent to a second asker, and it contradicts nothing.
    """
    out: list[Violation] = []
    last: dict[str, str] = {}
    for line in sorted(transcript.lines, key=lambda line: line.at):
        if line.kind not in ANSWERING:
            continue
        previous = last.get(line.request_id)
        if (
            previous is not None
            and line.body != previous
            and line.kind is DeliveryKind.ANSWER
        ):
            out.append(
                Violation(
                    "non_contradiction",
                    f"{line.request_id} changed its answer at {line.at:.2f} "
                    "with no addendum or replacement notice",
                )
            )
        last[line.request_id] = line.body
    return out


def _causal_consistency(seq: Sequencer) -> list[Violation]:
    """If answer B depends on answer A, A arrives first (§3).

    This is the only ordering the system promises, and the only place it
    serializes.
    """
    out: list[Violation] = []
    for req in seq.requests.values():
        if req.origin_parent is None or req.delivered_at is None:
            continue
        parent = seq.requests.get(req.origin_parent)
        if parent is None or parent.first_delivered_at is None:
            out.append(
                Violation(
                    "causal_consistency",
                    f"{req.id} delivered but its parent {req.origin_parent} never did",
                )
            )
        elif parent.first_delivered_at > req.delivered_at:
            out.append(
                Violation(
                    "causal_consistency",
                    f"{req.id} delivered at {req.delivered_at:.2f} before its parent "
                    f"{parent.id} at {parent.first_delivered_at:.2f}",
                )
            )
    return out


def _attribution(transcript: Transcript, policy: Policy) -> list[Violation]:
    """A policy that promises attribution must actually carry it (§3)."""
    if not policy.attributes:
        return []
    return [
        Violation("attribution", f"{line.request_id} answered with no requote")
        for line in transcript.lines
        if line.kind in ANSWERING and not line.re
    ]


def _pool_bound(seq: Sequencer, policy: Policy, pool_size: int) -> list[Violation]:
    limit = policy.concurrency(pool_size)
    if seq.max_in_flight > limit:
        return [
            Violation(
                "pool_bound",
                f"{seq.max_in_flight} workers in flight, limit is {limit}",
            )
        ]
    return []


def _tokens_accounted(seq: Sequencer) -> list[Violation]:
    """Every delivered answer was actually computed by some worker.

    Guards against the Sequencer inventing a delivery — the failure mode where
    a bug in the settle loop ships an answer no worker produced.
    """
    out: list[Violation] = []
    for req in seq.requests.values():
        if req.stage is not Stage.DELIVERED:
            continue
        if req.delivered_version not in req.tokens_by_version:
            out.append(
                Violation(
                    "tokens_accounted",
                    f"{req.id} delivered v{req.delivered_version} with no worker spend",
                )
            )
    return out



def _snapshot_coherence(seq: Sequencer) -> list[Violation]:
    """A delivered answer used the data version its request was pinned to.

    The pin is what makes an addendum coherent with the answer it extends,
    and what stops two concurrent questions reporting figures that disagree.
    It is only worth anything if workers honour it — and a worker quietly
    ignoring a field the Sequencer sends it is a mistake this codebase has
    already made once, with retained tool results (§7c).
    """
    out: list[Violation] = []
    for req in seq.requests.values():
        if req.stage is not Stage.DELIVERED or req.as_of is None:
            continue
        parent = seq.requests.get(req.origin_parent) if req.origin_parent else None
        if parent is not None and parent.as_of and req.as_of != parent.as_of:
            out.append(
                Violation(
                    "snapshot_coherence",
                    f"{req.id} answers against {req.as_of} but builds on "
                    f"{parent.id}, which answered against {parent.as_of}",
                )
            )
        if req.answered_as_of is not None and req.answered_as_of != req.as_of:
            out.append(
                Violation(
                    "snapshot_coherence",
                    f"{req.id} was pinned to {req.as_of} but answered "
                    f"against {req.answered_as_of}",
                )
            )
    return out
