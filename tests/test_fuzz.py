"""Randomised message streams, with every system invariant asserted.

This is the strongest correctness argument in the project. Hand-written cases
only cover the failure modes we thought of; the fuzzer covers the ones we
didn't. It is only possible because the Sequencer is a pure function and the
simulator is deterministic — thousands of runs take seconds and never touch
the network.

What it found, none of which was in the hand-written tests:

  * A REFERENCE whose parent had *already* died hung forever — the dependency
    edge had been released before the child existed, so nothing would free it.
  * A parent that delivered, was amended, then cancelled stranded its waiting
    child: admission asked "is the parent DELIVERED?" when the real question
    is whether the answer the child needs has shipped.
  * An amendment arriving in the ready-but-undelivered window dispatched a
    restart inline while the old worker's slot was already free, pushing the
    pool over its concurrency limit.
  * The same window let a buffered answer computed against the *old* spec
    survive into the new version and ship — staleness, from the one path
    version gating did not cover.

It also falsified two invariants that were themselves too crude, which is
worth as much: non-contradiction was counting distinct answer bodies rather
than checking that each change was announced, and causal consistency compared
against a parent's *latest* delivery rather than the one its child consumed.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from chatsched import invariants
from chatsched.policies import DependencyOrder, Serial, StrictFifo
from chatsched.sim import Simulation, Work
from chatsched.triage import InboundMessage

AUTHORS = ["ana", "ben", "cara"]
RELATIONSHIPS = ["AMEND", "CANCEL", "DUPLICATE", "REFERENCE"]


@st.composite
def message_stream(draw):
    """Generate a plausible busy chat: new questions, amendments, cancels,
    duplicates and references to things already asked."""
    count = draw(st.integers(min_value=1, max_value=14))
    messages: list[dict] = []
    work: dict[str, Work] = {}
    labels: list[str] = []
    at = 0.0

    for _ in range(count):
        at += draw(st.floats(min_value=0.0, max_value=4.0, allow_nan=False, allow_infinity=False))
        author = draw(st.sampled_from(AUTHORS))

        # Bias toward NEW so streams stay varied rather than collapsing into
        # one request that everyone amends.
        if not labels or draw(st.integers(min_value=0, max_value=2)) == 0:
            label = f"q{len(labels)}"
            labels.append(label)
            work[label] = Work(
                plan=draw(st.floats(0.1, 1.0)),
                tool=draw(st.floats(0.1, 6.0)),
                draft=draw(st.floats(0.1, 1.5)),
            )
            messages.append(
                {"at": at, "author": author, "text": f"question {label}",
                 "rel": "NEW", "label": label}
            )
            continue

        target = draw(st.sampled_from(labels))
        rel = draw(st.sampled_from(RELATIONSHIPS))
        entry = {
            "at": at,
            "author": author,
            "text": f"{rel.lower()} against {target}",
            "rel": rel,
            "target": target,
        }
        if rel == "AMEND":
            entry["preserves_premise"] = draw(st.booleans())
        if rel == "REFERENCE":
            label = f"q{len(labels)}"
            labels.append(label)
            work[label] = Work(
                plan=draw(st.floats(0.1, 1.0)),
                tool=draw(st.floats(0.1, 4.0)),
                draft=draw(st.floats(0.1, 1.0)),
            )
            entry["label"] = label
        messages.append(entry)

    return messages, work


def _build(raw: list[dict]) -> list[InboundMessage]:
    return [
        InboundMessage(
            msg_id=f"m{i}",
            author=m["author"],
            text=m["text"],
            at=m["at"],
            label=m.get("label"),
            target_label=m.get("target"),
            relationship=m.get("rel", "NEW"),
            preserves_premise=bool(m.get("preserves_premise", True)),
        )
        for i, m in enumerate(raw)
    ]


POLICIES = [Serial, StrictFifo, DependencyOrder]


@given(stream=message_stream(), policy_index=st.integers(0, len(POLICIES) - 1),
       pool_size=st.integers(1, 4))
def test_invariants_hold_on_random_streams(stream, policy_index, pool_size):
    raw, work = stream
    policy = POLICIES[policy_index]()
    sim = Simulation(
        messages=_build(raw), work=work, policy=policy, pool_size=pool_size
    )
    result = sim.run()

    violations = invariants.check(
        result.sequencer, result.transcript, policy, pool_size
    )
    assert not violations, (
        f"{policy.name} pool={pool_size}\n"
        + "\n".join(f"  {v}" for v in violations)
        + "\nstream:\n"
        + "\n".join(f"  {m}" for m in raw)
    )


@given(stream=message_stream())
def test_every_request_terminates(stream):
    """The no-silent-loss property, isolated: whatever the stream, nothing is
    still waiting when the simulation drains."""
    raw, work = stream
    sim = Simulation(messages=_build(raw), work=work, policy=DependencyOrder(), pool_size=2)
    result = sim.run()
    assert all(r.is_terminal for r in result.sequencer.requests.values())
