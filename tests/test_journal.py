"""Crash recovery by replaying the event log (DESIGN.md §5, §14).

§5 claims that making the Sequencer a pure fold over events buys cheap crash
recovery. These tests are what turn that from an assertion into a
demonstration — the same move as `--no-version-gate` in §10.

The headline property is not "replay produces a state". It is that a process
which dies mid-chat, recovers from the log, and carries on ends up in the
*identical* state as one that never died. Recovery that merely produces a
plausible state is worse than none, because nobody notices it is wrong.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from chatsched import journal
from chatsched.journal import EventLog, decode, encode, read, replay, state_digest
from chatsched.model import (
    Classified,
    Completed,
    Failed,
    Relationship,
    Stage,
    StageChanged,
)
from chatsched.policies import DependencyOrder, Serial, StrictFifo
from chatsched.sim import SCENARIO_DIR, Simulation, load_scenario

from test_fuzz import _build, message_stream

SCENARIOS = sorted(p.stem for p in SCENARIO_DIR.glob("*.json"))

# --------------------------------------------------------------- the format --


EVENTS = [
    Classified(
        msg_id="m1", author="ana", text="what's my Taiwan exposure?", at=0.0,
        relationship=Relationship.NEW, member="ana",
    ),
    Classified(
        msg_id="m2", author="ana", text="actually, South China Sea too", at=4.0,
        relationship=Relationship.AMEND, target_id="r1",
        rewritten_spec="Taiwan or South China Sea exposure",
        names_target=True, preserves_premise=True, confidence=0.82,
        fallback_applied=False, reasoning="names the target",
    ),
    StageChanged(request_id="r1", version=2, stage=Stage.DRAFTING, at=7.5,
                 tool_results=("holdings", "prices")),
    Completed(request_id="r1", version=2, answer="~19% via TSM", tokens=1200,
              at=9.0, as_of="2026-10-18"),
    Failed(request_id="r2", version=1, error="aborted", tokens=300, at=9.5),
]


@pytest.mark.parametrize("ev", EVENTS, ids=lambda e: type(e).__name__)
def test_every_event_type_round_trips(ev):
    """A log is only as good as its worst-serialised field. Enum values and
    the tool-results tuple are the two that do not survive a naive json dump."""
    assert decode(json.loads(json.dumps(encode(ev)))) == ev


def test_an_unknown_event_kind_is_refused_not_skipped():
    """Dropping an event silently reconstructs a state the system was never
    in, which is the one failure recovery must not have."""
    with pytest.raises(ValueError, match="unknown event kind"):
        decode({"kind": "something_new", "at": 0.0})


def test_a_torn_final_line_is_tolerated():
    """Dying mid-write is the expected way for this file to end."""
    path = journal.Path(_tmp()) / "torn.jsonl"
    with EventLog(path) as log:
        for ev in EVENTS[:3]:
            log.append(ev)
    with path.open("a") as fh:
        fh.write('{"kind": "completed", "request_i')  # power cut, mid-write

    recovered = list(read(path))
    assert recovered == EVENTS[:3]


def test_everything_before_the_torn_line_still_replays():
    path = journal.Path(_tmp()) / "torn2.jsonl"
    with EventLog(path) as log:
        log.append(EVENTS[0])
    with path.open("a") as fh:
        fh.write("{not json at all")
    assert len(replay(read(path)).requests) == 1


_counter = [0]


def _tmp() -> str:
    """A scratch directory per call. `tmp_path` would be cleaner, but these
    helpers are also used from the property test, which cannot take fixtures."""
    import tempfile

    _counter[0] += 1
    return tempfile.mkdtemp(prefix=f"chatsched-journal-{_counter[0]}-")


# ------------------------------------------------------- recovery, for real --


def _run_logged(name: str, policy, pool_size: int = 3):
    """Run a scenario with durability on, and hand back the log path."""
    messages, work, _title = load_scenario(name)
    path = journal.Path(_tmp()) / "events.jsonl"
    with EventLog(path) as log:
        sim = Simulation(
            messages=messages,
            policy=policy(),
            pool_size=pool_size,
            work=work,
            journal=log,
        )
        result = sim.run()
    return result, path, policy, pool_size


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("policy", [Serial, StrictFifo, DependencyOrder])
def test_replaying_the_log_reconstructs_the_run_exactly(name, policy):
    """Every scenario, every policy: fold the log and you are back where you
    were. No checkpoint format, no reconciliation — just the events."""
    result, path, policy, pool_size = _run_logged(name, policy)
    recovered = replay(read(path), pool_size=pool_size, policy=policy())
    assert state_digest(recovered) == state_digest(result.sequencer)


@pytest.mark.parametrize("name", SCENARIOS)
def test_a_crash_mid_chat_is_invisible_once_recovered(name):
    """The property that actually matters.

    Kill the process after every possible event, recover from the log, feed
    it the events it had not yet seen, and the chat ends in the same state as
    one that never crashed. If recovery were lossy this would drift, and the
    drift would be silent.
    """
    result, path, policy, pool_size = _run_logged(name, DependencyOrder)
    events = list(read(path))
    expected = state_digest(result.sequencer)

    for crash_after in range(len(events) + 1):
        survivor = replay(events[:crash_after], pool_size=pool_size, policy=policy())
        for ev in events[crash_after:]:
            survivor.handle(ev)  # the process came back and carried on
        assert state_digest(survivor) == expected, (
            f"{name}: crashing after event {crash_after} of {len(events)} "
            "produced a different final state"
        )


def test_the_log_records_every_event_the_sequencer_saw():
    """A log missing events is the failure mode this whole module exists to
    avoid, and it would still pass a round-trip test."""
    messages, work, _title = load_scenario("transcript")
    seen: list = []
    path = journal.Path(_tmp()) / "events.jsonl"

    with EventLog(path) as log:
        sim = Simulation(messages=messages, policy=DependencyOrder(),
                         work=work, journal=log)
        original = sim.seq.handle
        sim.seq.handle = lambda ev: (seen.append(ev), original(ev))[1]  # type: ignore[method-assign]
        sim.run()

    assert list(read(path)) == seen
    assert len(seen) > 10, "scenario too small to be a meaningful check"


def test_running_without_a_log_is_still_the_default():
    """Durability is opt-in: the fuzzer runs 15,000 chats and must not pay
    for a file write on each one."""
    messages, work, _title = load_scenario("transcript")
    sim = Simulation(messages=messages, policy=DependencyOrder(), work=work)
    assert sim.journal is None
    assert sim.run().sequencer.requests


# ------------------------------------------------------------ against fuzz --


@given(stream=message_stream(), crash_fraction=st.floats(0.0, 1.0))
def test_any_random_chat_survives_a_crash_at_any_point(stream, crash_fraction):
    """The scenario suite covers the chats we thought of. This covers the
    rest — including the four shapes the fuzzer originally found bugs in."""
    raw, work = stream
    path = journal.Path(_tmp()) / "fuzz.jsonl"

    with EventLog(path) as log:
        sim = Simulation(messages=_build(raw), policy=DependencyOrder(),
                         pool_size=2, work=work, journal=log)
        result = sim.run()

    events = list(read(path))
    crash_after = int(len(events) * crash_fraction)
    survivor = replay(events[:crash_after], pool_size=2, policy=DependencyOrder())
    for ev in events[crash_after:]:
        survivor.handle(ev)

    assert state_digest(survivor) == state_digest(result.sequencer)


def test_recovering_under_a_different_policy_is_the_callers_mistake():
    """Documented, not prevented. The log records what the chat did, not how
    the process was configured, so nothing here can catch this — and a test
    that pretends otherwise would be lying about the guarantee."""
    result, path, _, _ = _run_logged("burst", DependencyOrder)
    wrong = replay(read(path), pool_size=3, policy=Serial())
    assert state_digest(wrong) != state_digest(result.sequencer)
