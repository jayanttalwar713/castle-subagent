"""Every shipped scenario, under every policy, with all invariants asserted.

Scenario replay uses the scripted classifier, never a live model call, so
these tests measure the scheduler rather than the classifier. Mixing the two
would mean a flaky classifier could mask a real scheduling bug.
"""

from __future__ import annotations

import pytest

from chatsched import invariants
from chatsched.cli import ALL_POLICIES
from chatsched.policies import get_policy
from chatsched.sim import SCENARIO_DIR, Simulation, load_scenario

SCENARIOS = sorted(p.stem for p in SCENARIO_DIR.glob("*.json"))


def run(name: str, policy_name: str, pool_size: int = 3, version_gate: bool = True):
    messages, work, _title = load_scenario(name)
    policy = get_policy(policy_name)
    sim = Simulation(
        messages=messages,
        work=work,
        policy=policy,
        pool_size=pool_size,
        version_gate=version_gate,
    )
    result = sim.run()
    return result, policy


def test_scenarios_are_discovered():
    assert len(SCENARIOS) >= 5, SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("policy_name", ALL_POLICIES)
def test_invariants_hold(scenario, policy_name):
    result, policy = run(scenario, policy_name)
    violations = invariants.check(result.sequencer, result.transcript, policy, 3)
    assert not violations, "\n".join(str(v) for v in violations)


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("policy_name", ALL_POLICIES)
def test_every_question_gets_an_outcome(scenario, policy_name):
    result, _policy = run(scenario, policy_name)
    assert result.sequencer.requests, "scenario produced no requests"
    assert all(r.is_terminal for r in result.sequencer.requests.values())


def test_dependency_order_beats_fifo_on_head_of_line_delay():
    """The headline claim of §9, asserted rather than eyeballed.

    FIFO parallelises the *work* and still makes a finished answer wait for
    everything that arrived before it. That wait is the cost of ordering.
    """
    fifo, _ = run("head_of_line", "STRICT_FIFO")
    dep, _ = run("head_of_line", "DEPENDENCY_ORDER")

    def held(result):
        return sum(
            (r.delivered_at or 0) - (r.ready_at or 0)
            for r in result.sequencer.requests.values()
            if r.ready_at is not None and r.delivered_at is not None
        )

    assert held(fifo) > 15.0
    assert held(dep) == pytest.approx(0.0, abs=1e-6)

    # And the fast question is answered an order of magnitude sooner.
    first_fifo = min(line.at for line in fifo.transcript.answers())
    first_dep = min(line.at for line in dep.transcript.answers())
    assert first_dep < first_fifo / 10


def test_serial_is_the_honest_baseline():
    """SERIAL has zero head-of-line delay by construction — with one worker,
    an answer is delivered the moment it is ready. Its cost shows up as
    latency instead, which is why both are in the table."""
    serial, _ = run("burst", "SERIAL")
    dep, _ = run("burst", "DEPENDENCY_ORDER")
    assert serial.finished_at > dep.finished_at


def test_cancelling_mid_draft_charges_the_work_already_done():
    result, _ = run("cancel_after_draft", "DEPENDENCY_ORDER")
    wasted = sum(r.wasted_tokens for r in result.sequencer.requests.values())
    assert wasted > 0, "cancelled work should be billed as waste, not hidden"


def test_premise_preserving_amendment_does_not_pay_for_tools_twice():
    """§7c. The injected change rides the same worker, so the expensive tool
    call is charged once."""
    result, _ = run("amend_mid_tool", "DEPENDENCY_ORDER")
    req = next(iter(result.sequencer.requests.values()))
    assert req.version == 2          # the amendment did land
    assert req.wasted_tokens == 0    # and nothing was thrown away


def test_version_gating_is_what_keeps_staleness_at_zero():
    """The --no-version-gate demonstration, as a test.

    Same scenario, same policy; the only difference is the gate. With it on,
    staleness is zero and every invariant holds. With it off, a superseded
    worker's answer reaches the chat.
    """
    gated, policy = run("late_amend", "DEPENDENCY_ORDER")
    assert not invariants.check(gated.sequencer, gated.transcript, policy, 3)
    assert not any(rec.stale for rec in gated.sequencer.journal)

    ungated, policy = run("late_amend", "DEPENDENCY_ORDER", version_gate=False)
    violations = invariants.check(ungated.sequencer, ungated.transcript, policy, 3)
    assert any(v.invariant == "no_stale" for v in violations), violations
    assert any(rec.stale for rec in ungated.sequencer.journal)
