"""Unit tests for the Sequencer.

Organised to mirror DESIGN.md: the stage × relationship matrix of §7, then the
edge cases of §8 that a plausible-looking implementation gets wrong.
"""

from __future__ import annotations

import pytest

from chatsched.model import (
    Abort,
    Completed,
    Deliver,
    DeliveryKind,
    Dispatch,
    Inject,
    Relationship,
    Stage,
)
from chatsched.policies import Serial, StrictFifo
from conftest import Harness, msg


# ---------------------------------------------------------------- basics --


def test_new_request_is_acked_and_dispatched():
    h = Harness()
    rid = h.ask("what is my exposure to rates?")
    assert h.delivered(DeliveryKind.ACK)
    assert [d.request_id for d in h.dispatches()] == [rid]
    assert h.seq.requests[rid].stage is Stage.PLANNING


def test_completion_delivers_an_answer():
    h = Harness()
    rid = h.ask("q")
    h.finish(rid, at=4.0)
    answers = h.delivered(DeliveryKind.ANSWER)
    assert len(answers) == 1
    assert answers[0].request_id == rid
    assert h.seq.requests[rid].stage is Stage.DELIVERED
    assert h.seq.requests[rid].delivered_at == 4.0


def test_pool_bound_is_respected():
    h = Harness(pool_size=2)
    ids = [h.ask(f"q{i}", at=i) for i in range(5)]
    assert h.seq.max_in_flight == 2
    assert len(h.dispatches()) == 2
    # Draining one slot admits exactly one more.
    h.finish(ids[0], at=10.0)
    assert len(h.dispatches()) == 3


def test_serial_policy_runs_one_at_a_time():
    h = Harness(pool_size=4, policy=Serial())
    [h.ask(f"q{i}", at=i) for i in range(3)]
    assert h.seq.max_in_flight == 1


# ------------------------------------------------- §7 the amend matrix ----


def test_amend_while_queued_rewrites_in_place_without_dispatching_twice():
    h = Harness(pool_size=1)
    first = h.ask("slow one", at=0)
    queued = h.ask("second one", at=1)  # no free slot, stays QUEUED
    assert h.seq.requests[queued].stage is Stage.QUEUED

    h.send(msg("also include bonds", at=2, rel=Relationship.AMEND, target=queued))

    assert h.seq.requests[queued].version == 2
    assert "bonds" in h.seq.requests[queued].spec
    # Nothing was paid for, so nothing is aborted and nothing restarts.
    assert h.of(Abort) == []
    assert h.dispatches(queued) == []

    h.finish(first, at=5)
    # Admitted now, carrying the amended spec.
    assert "bonds" in h.dispatches(queued)[0].spec


def test_amend_while_planning_restarts_the_worker():
    h = Harness()
    rid = h.ask("q")
    h.send(msg("make it Q3", at=1, rel=Relationship.AMEND, target=rid))

    aborts = h.of(Abort)
    assert len(aborts) == 1 and aborts[0].version == 1
    # Re-queued, not re-dispatched: the old worker still holds the slot.
    assert h.seq.requests[rid].stage is Stage.QUEUED
    assert [d.version for d in h.dispatches(rid)] == [1]

    h.abort_landed(rid)
    assert [d.version for d in h.dispatches(rid)] == [1, 2]


def test_amend_mid_tool_call_injects_rather_than_restarting():
    """§7a: the tool call is already paid for. Let it land, apply the change
    at the worker's next checkpoint."""
    h = Harness()
    rid = h.ask("q")
    h.advance_to(rid, Stage.TOOL_CALL, at=1)
    h.send(msg("also South China Sea", at=2, rel=Relationship.AMEND, target=rid))

    assert h.of(Abort) == []
    injects = h.of(Inject)
    assert len(injects) == 1
    # The worker adopts the new version, so version gating still holds.
    assert injects[0].version == 2 == h.seq.requests[rid].version
    assert len(h.dispatches(rid)) == 1


def test_premise_replacing_amend_discards_tool_results():
    """§7c: preserve work in proportion to what it cost — unless the premise
    changed, in which case the research is worthless."""
    h = Harness()
    rid = h.ask("taiwan exposure")
    h.advance_to(rid, Stage.TOOL_CALL, at=1)
    h.advance_to(rid, Stage.DRAFTING, at=2)  # carries tool results back
    assert h.seq.requests[rid].tool_results == ["tool-result"]

    h.send(
        msg("actually, inflation", at=3, rel=Relationship.AMEND, target=rid, preserves=False)
    )
    h.abort_landed(rid)

    assert h.seq.requests[rid].tool_results == []
    assert h.dispatches(rid)[-1].retained_tool_results == ()
    assert h.seq.requests[rid].spec == "actually, inflation"


def test_premise_preserving_amend_while_drafting_reuses_tool_results():
    h = Harness()
    rid = h.ask("taiwan exposure")
    h.advance_to(rid, Stage.TOOL_CALL, at=1)
    h.advance_to(rid, Stage.DRAFTING, at=2)

    h.send(msg("and south china sea", at=3, rel=Relationship.AMEND, target=rid))
    h.abort_landed(rid)

    # Re-drafts, but does not pay for the tool call again.
    assert h.dispatches(rid)[-1].retained_tool_results == ("tool-result",)


@pytest.mark.parametrize(
    "preserves,expected",
    [(True, DeliveryKind.ADDENDUM), (False, DeliveryKind.SUPERSEDE)],
)
def test_amend_after_delivery_reopens_with_a_visible_notice(preserves, expected):
    """§7, the DELIVERED column — the one most designs omit."""
    h = Harness()
    rid = h.ask("q")
    h.finish(rid, at=3)
    assert h.seq.requests[rid].stage is Stage.DELIVERED

    h.send(msg("one more thing", at=4, rel=Relationship.AMEND, target=rid, preserves=preserves))
    assert h.seq.requests[rid].stage is Stage.PLANNING  # re-admitted

    h.finish(rid, at=8)
    assert h.delivered(expected)


# ------------------------------------------------------------- §7b gating --


def test_stale_result_is_ignored():
    h = Harness()
    rid = h.ask("q")
    h.send(msg("changed", at=1, rel=Relationship.AMEND, target=rid))  # -> v2

    h.send(Completed(rid, 1, "stale answer", 100, 5.0))  # the v1 worker lands

    assert h.delivered(DeliveryKind.ANSWER) == []
    assert h.seq.requests[rid].stage is not Stage.DELIVERED


def test_stale_worker_does_not_free_the_replacement_slot():
    """A superseded worker finishing late must not release the pool slot its
    replacement is holding — that would let the pool over-admit."""
    h = Harness(pool_size=1)
    rid = h.ask("q")
    waiting = h.ask("other", at=1)
    h.send(msg("changed", at=2, rel=Relationship.AMEND, target=rid))  # v2 dispatched

    h.send(Completed(rid, 1, "stale", 100, 3.0))  # the abandoned v1 worker

    # The waiting request must still be queued; the slot belongs to v2.
    assert h.seq.requests[waiting].stage is Stage.QUEUED
    assert h.seq.max_in_flight == 1


def test_stale_tokens_are_still_charged():
    """Wasted tokens are the price of mutability, and we bill ourselves."""
    h = Harness()
    rid = h.ask("q")
    h.send(msg("changed", at=1, rel=Relationship.AMEND, target=rid))
    h.send(Completed(rid, 1, "stale", 700, 3.0))
    h.finish(rid, at=5, tokens=400)

    req = h.seq.requests[rid]
    assert req.total_tokens == 1100
    assert req.wasted_tokens == 700


def test_disabling_the_gate_lets_stale_answers_through():
    """The --no-version-gate demo: the invariant fails on command."""
    h = Harness(version_gate=False)
    rid = h.ask("q")
    h.send(msg("changed", at=1, rel=Relationship.AMEND, target=rid))

    h.send(Completed(rid, 1, "stale answer", 100, 5.0))

    assert h.delivered(DeliveryKind.ANSWER)
    assert any(rec.stale for rec in h.seq.journal)


# ------------------------------------------------------------ cancellation --


def test_cancel_while_running_aborts_and_drops():
    h = Harness()
    rid = h.ask("q")
    h.send(msg("never mind", at=1, rel=Relationship.CANCEL, target=rid))

    assert h.of(Abort)
    assert h.seq.requests[rid].stage is Stage.DROPPED
    assert h.delivered(DeliveryKind.CANCEL_ACK)


def test_one_asker_cancelling_does_not_cancel_everyone_elses_question():
    """From the §1 transcript: Cara cancels the question she duplicated, but
    Ana asked it first and is still owed an answer."""
    h = Harness()
    rid = h.ask("taiwan exposure", author="ana")
    h.send(msg("same question", author="cara", at=1, rel=Relationship.DUPLICATE, target=rid))
    assert h.seq.requests[rid].askers == ["ana", "cara"]

    h.send(msg("never mind", author="cara", at=2, rel=Relationship.CANCEL, target=rid))

    assert h.seq.requests[rid].askers == ["ana"]
    assert h.seq.requests[rid].stage is not Stage.DROPPED
    h.finish(rid, at=5)
    assert h.delivered(DeliveryKind.ANSWER)[0].to == ("ana",)


def test_cancel_after_delivery_acknowledges_rather_than_retracting():
    h = Harness()
    rid = h.ask("q")
    h.finish(rid, at=3)
    h.send(msg("never mind", at=4, rel=Relationship.CANCEL, target=rid))
    assert h.delivered(DeliveryKind.ALREADY_ANSWERED)


# -------------------------------------------------------------- duplicates --


def test_duplicate_attaches_the_asker_and_pays_once():
    h = Harness()
    rid = h.ask("taiwan exposure", author="ana")
    h.send(msg("exposure to taiwan?", author="cara", at=1, rel=Relationship.DUPLICATE, target=rid))

    assert len(h.seq.requests) == 1
    assert len(h.dispatches()) == 1
    h.finish(rid, at=5)
    assert set(h.delivered(DeliveryKind.ANSWER)[0].to) == {"ana", "cara"}


def test_duplicate_after_delivery_fans_the_existing_answer_out():
    h = Harness()
    rid = h.ask("q", author="ana")
    h.finish(rid, at=3)
    h.send(msg("same q", author="cara", at=4, rel=Relationship.DUPLICATE, target=rid))

    fanout = h.delivered(DeliveryKind.ANSWER)[-1]
    assert fanout.to == ("cara",)
    assert len(h.dispatches()) == 1  # nothing recomputed


# --------------------------------------------------------------- reference --


def test_reference_waits_for_its_parent_then_materialises_its_spec():
    h = Harness()
    parent = h.ask("is NVDA my biggest position?", author="ben")
    child_before = set(h.seq.requests)
    h.send(msg("and the second biggest?", author="ben", at=1, rel=Relationship.REFERENCE, target=parent))
    child = next(iter(set(h.seq.requests) - child_before))

    # Gated at admission — never holding a slot while waiting (§8).
    assert h.seq.requests[child].stage is Stage.QUEUED
    assert h.dispatches(child) == []

    h.finish(parent, at=4)

    assert h.dispatches(child)
    assert "answer-" in h.dispatches(child)[0].spec


def test_reference_is_dropped_with_a_notice_when_its_parent_dies():
    """§8: silence is the one unacceptable outcome."""
    h = Harness()
    parent = h.ask("q", author="ben")
    before = set(h.seq.requests)
    h.send(msg("and the second?", author="ben", at=1, rel=Relationship.REFERENCE, target=parent))
    child = next(iter(set(h.seq.requests) - before))

    h.send(msg("never mind", author="ben", at=2, rel=Relationship.CANCEL, target=parent))

    assert h.seq.requests[child].stage is Stage.DROPPED
    notices = h.delivered(DeliveryKind.DROPPED_NOTICE)
    assert [n.request_id for n in notices] == [child]


def test_reference_cycle_degrades_to_an_independent_request():
    """A cycle can only come from classifier error: answer it anyway, with
    weaker ordering (§8)."""
    h = Harness()
    a = h.ask("first")
    before = set(h.seq.requests)
    h.send(msg("second", at=1, rel=Relationship.REFERENCE, target=a))
    b = next(iter(set(h.seq.requests) - before))
    h.seq.graph.add_edge(a, b)  # would close the loop; must be refused

    assert h.seq.graph.parent_of(a) is None
    assert h.seq.requests[b].origin_parent == a


# ------------------------------------------------------- classifier error --


def test_relationship_with_a_missing_target_degrades_to_new():
    """Bias toward answering the question rather than dropping it (§4)."""
    h = Harness()
    h.send(msg("make that Q3", at=1, rel=Relationship.AMEND, target="r-nonexistent"))
    assert len(h.seq.requests) == 1
    assert h.delivered(DeliveryKind.ACK)


def test_reference_survives_a_parent_that_answered_then_died():
    """Regression, found by the fuzzer.

    A parent that delivers releases its dependency edges. If it is then
    amended (un-delivering it) and cancelled, nothing would ever notify the
    waiting child, and admission — which asked "is the parent DELIVERED?" —
    would never admit it again. The child hung forever.

    The fix is that admission asks whether the parent's *answer exists*, which
    it does: the child can still be specified from the answer that shipped.
    """
    h = Harness(pool_size=1)
    parent = h.ask("parent question", author="ben")
    blocker = h.ask("occupies the only slot", author="ana", at=0.5)  # noqa: F841
    before = set(h.seq.requests)
    h.send(msg("and the follow-up?", author="ben", at=1, rel=Relationship.REFERENCE, target=parent))
    child = next(iter(set(h.seq.requests) - before))

    h.finish(parent, at=3)                                   # releases the edge
    h.send(msg("rethink it", at=4, rel=Relationship.AMEND, target=parent))   # un-delivers
    h.send(msg("never mind", at=5, rel=Relationship.CANCEL, target=parent))  # kills it

    assert h.seq.requests[parent].stage is Stage.DROPPED
    # The child must still be answerable, not stranded.
    assert h.seq.requests[child].stage is not Stage.DROPPED
    h.finish(child, at=9)
    assert any(d.request_id == child for d in h.delivered(DeliveryKind.ANSWER))


def test_reference_to_an_already_dead_request_becomes_independent():
    """Regression, found by the fuzzer: the parent's edges were released when
    it died, so a child registered *afterwards* would never be released."""
    h = Harness()
    parent = h.ask("doomed", author="ben")
    h.send(msg("never mind", author="ben", at=1, rel=Relationship.CANCEL, target=parent))
    assert h.seq.requests[parent].stage is Stage.DROPPED

    before = set(h.seq.requests)
    h.send(msg("and the follow-up?", author="ben", at=2, rel=Relationship.REFERENCE, target=parent))
    child = next(iter(set(h.seq.requests) - before))

    assert h.seq.requests[child].depends_on is None
    assert h.dispatches(child)


def test_amend_in_the_ready_window_does_not_overrun_the_pool():
    """Regression, found by the fuzzer.

    Between "worker finished" and "policy released the answer" (§13) the
    request's stage is still DRAFTING but its pool slot is already free.
    Dispatching the restart inline pushed the pool over its limit.
    """
    h = Harness(pool_size=2, policy=StrictFifo())
    slow = h.ask("slow one holding up the FIFO queue", at=0)
    pending = h.ask("finishes early but is buffered", at=1)
    h.advance_to(pending, Stage.DRAFTING, at=2)
    h.finish(pending, at=3)  # ready, but FIFO holds it behind `slow`
    assert h.seq.requests[pending].stage is Stage.DRAFTING

    h.ask("third question queued behind the pool", at=4)
    h.send(msg("rethink that", at=5, rel=Relationship.AMEND, target=pending, preserves=False))

    assert h.seq.max_in_flight <= 2
    h.finish(slow, at=20)


def test_amend_in_the_ready_window_discards_the_pending_answer():
    """Same window, staleness side: the buffered answer was computed against
    the old spec and must not survive into the new version."""
    h = Harness(pool_size=2, policy=StrictFifo())
    slow = h.ask("slow one", at=0)
    pending = h.ask("fast one", at=1)
    h.advance_to(pending, Stage.DRAFTING, at=2)
    h.finish(pending, at=3)

    # Nothing to abort — the worker already finished — so the restart is
    # admitted straight away against the new spec.
    h.send(msg("rethink that", at=4, rel=Relationship.AMEND, target=pending, preserves=False))
    assert h.seq.requests[pending].stage is Stage.PLANNING
    h.finish(slow, at=6)  # releases the FIFO gate

    # The stale answer must not have shipped.
    assert not [d for d in h.delivered(DeliveryKind.ANSWER) if d.request_id == pending]
    assert not any(rec.stale for rec in h.seq.journal)


def test_dependent_waits_for_delivery_not_merely_for_a_computed_answer():
    """Causal consistency is about what the *reader* saw. A parent whose
    answer is computed but still buffered has not delivered, so its dependent
    must not be admitted yet."""
    h = Harness(pool_size=3, policy=StrictFifo())
    blocker = h.ask("blocks the FIFO queue", at=0)  # noqa: F841
    parent = h.ask("parent", at=1)
    before = set(h.seq.requests)
    h.send(msg("follow-up", at=2, rel=Relationship.REFERENCE, target=parent))
    child = next(iter(set(h.seq.requests) - before))

    h.advance_to(parent, Stage.DRAFTING, at=3)
    h.finish(parent, at=4)  # computed, but buffered behind `blocker`

    assert h.seq.requests[parent].first_delivered_at is None
    assert h.dispatches(child) == []


# ------------------------------------------------------- member identity --


def test_duplicate_across_members_is_not_a_duplicate():
    """Two people asking identical words about *different* portfolios are
    asking different questions. Fanning one answer out would answer the wrong
    question and disclose one member's positions to another (§11)."""
    h = Harness()
    before = set(h.seq.requests)
    h.send(msg("taiwan exposure?", author="ana", at=0,
               rel=Relationship.NEW, member="ana"))
    ana = next(iter(set(h.seq.requests) - before))

    before = set(h.seq.requests)
    h.send(msg("taiwan exposure?", author="dev", at=1,
               rel=Relationship.DUPLICATE, target=ana, member="dev"))

    created = set(h.seq.requests) - before
    assert created, "a different member's question must get its own request"
    assert h.seq.requests[ana].askers == ["ana"], "dev must not be attached to ana's"
    assert len(h.dispatches()) == 2, "both questions must actually be worked on"


def test_duplicate_within_one_member_still_pays_once():
    """The optimisation survives the guard: two people sharing a portfolio
    (a household, an investment club) still get one answer, fanned out."""
    h = Harness()
    before = set(h.seq.requests)
    h.send(msg("taiwan exposure?", author="ana", at=0,
               rel=Relationship.NEW, member="household-7"))
    rid = next(iter(set(h.seq.requests) - before))

    h.send(msg("exposure to taiwan?", author="cara", at=1,
               rel=Relationship.DUPLICATE, target=rid, member="household-7"))

    assert len(h.seq.requests) == 1
    assert len(h.dispatches()) == 1
    h.finish(rid, at=5)
    assert set(h.delivered(DeliveryKind.ANSWER)[0].to) == {"ana", "cara"}


def test_a_chat_with_no_member_context_behaves_as_before():
    """None means "this chat has one shared portfolio", which is the default
    and what every other scenario assumes."""
    h = Harness()
    rid = h.ask("taiwan exposure?", author="ana")
    h.send(msg("same question", author="cara", at=1, rel=Relationship.DUPLICATE, target=rid))
    assert len(h.seq.requests) == 1
    assert h.seq.requests[rid].askers == ["ana", "cara"]


# ------------------------------------------------- not a request at all --


def test_chitchat_creates_no_request_and_spends_nothing():
    """Most traffic in a busy group chat is not a request. Without the IGNORE
    class, "thanks!!" acknowledges itself and dispatches a worker (§4)."""
    h = Harness()
    out = h.send(msg("thanks!! 🙏", author="ben", at=1, rel=Relationship.IGNORE))

    assert out == []
    assert h.seq.requests == {}
    assert h.dispatches() == []


def test_ignoring_does_not_disturb_work_in_flight():
    h = Harness()
    rid = h.ask("taiwan exposure?", author="ana")
    h.send(msg("haha nice", author="ben", at=1, rel=Relationship.IGNORE))

    assert h.seq.requests[rid].stage is Stage.PLANNING
    assert h.seq.requests[rid].version == 1
    h.finish(rid, at=5)
    assert h.delivered(DeliveryKind.ANSWER)


def test_an_unsure_ignore_answers_the_question_instead_of_dropping_it():
    """IGNORE is destructive — a real question silently never answered — so
    it carries the high threshold and degrades to NEW (§4)."""
    from chatsched.triage import apply_confidence_bias

    used, fell_back = apply_confidence_bias(Relationship.IGNORE, 0.6)
    assert used is Relationship.NEW and fell_back


# ------------------------------------------- the amended question itself --


def test_concatenating_an_amendment_produces_a_contradiction():
    """The fallback path, shown so the fix has something to be better than."""
    h = Harness()
    rid = h.ask("How did my book do in Q2?", author="ana")
    h.send(msg("sorry I meant Q3 not Q2", at=1, rel=Relationship.AMEND, target=rid))
    h.abort_landed(rid)

    spec = h.seq.requests[rid].spec
    assert "Q2" in spec and "Q3" in spec, "both questions are in the spec at once"


def test_triage_rewriting_the_question_removes_the_contradiction():
    """Triage read the original and the change together, so it can state the
    result. The worker should receive one question, not two."""
    h = Harness()
    rid = h.ask("How did my book do in Q2?", author="ana")
    h.send(msg("sorry I meant Q3 not Q2", at=1, rel=Relationship.AMEND, target=rid,
               rewritten="How did my book do in Q3?"))
    h.abort_landed(rid)

    spec = h.seq.requests[rid].spec
    assert spec == "How did my book do in Q3?"
    assert "Q2" not in spec
    assert h.dispatches(rid)[-1].spec == "How did my book do in Q3?"


def test_a_rewrite_is_used_for_a_premise_replacement_too():
    h = Harness()
    rid = h.ask("taiwan exposure?", author="ana")
    h.send(msg("actually inflation", at=1, rel=Relationship.AMEND, target=rid,
               preserves=False, rewritten="What is my exposure to inflation?"))
    h.abort_landed(rid)
    assert h.seq.requests[rid].spec == "What is my exposure to inflation?"


# ------------------------------------- asking rather than guessing -------


def test_a_cancel_that_names_nothing_asks_instead_of_destroying():
    """Measured, not assumed: confidence does not separate "never mind the
    Taiwan one" from a bare "no" aimed at another person — both land around
    0.92-0.95, so no threshold can tell them apart. Referential explicitness
    does (§4)."""
    h = Harness()
    rid = h.ask("taiwan exposure?", author="ana")

    h.send(msg("no", author="ana", at=1, rel=Relationship.CANCEL,
               target=rid, names_target=False))

    assert h.delivered(DeliveryKind.CONFIRM_CANCEL)
    assert h.seq.requests[rid].stage is not Stage.DROPPED, "work was destroyed on a guess"
    assert h.of(Abort) == []


def test_a_cancel_that_names_its_target_is_obeyed_immediately():
    """Asking every time would be its own failure. The common case — a cancel
    that says what it cancels — must not cost a round trip."""
    h = Harness()
    rid = h.ask("taiwan exposure?", author="ana")

    h.send(msg("never mind the Taiwan one", author="ana", at=1,
               rel=Relationship.CANCEL, target=rid, names_target=True))

    assert h.delivered(DeliveryKind.CANCEL_ACK)
    assert h.delivered(DeliveryKind.CONFIRM_CANCEL) == []
    assert h.seq.requests[rid].stage is Stage.DROPPED


def test_confirming_needs_no_parked_state():
    """The confirmation costs no new state machine: if they meant it, they
    say so again — and that reply names its target by construction."""
    h = Harness()
    rid = h.ask("taiwan exposure?", author="ana")
    h.send(msg("no", author="ana", at=1, rel=Relationship.CANCEL,
               target=rid, names_target=False))

    h.send(msg("yes, cancel the taiwan one", author="ana", at=2,
               rel=Relationship.CANCEL, target=rid, names_target=True))

    assert h.seq.requests[rid].stage is Stage.DROPPED


def test_an_ambiguous_cancel_of_an_answered_question_still_just_acknowledges():
    """Nothing is at risk once the answer has shipped, so do not ask."""
    h = Harness()
    rid = h.ask("q", author="ana")
    h.finish(rid, at=3)
    h.send(msg("no", author="ana", at=4, rel=Relationship.CANCEL,
               target=rid, names_target=False))
    assert h.delivered(DeliveryKind.ALREADY_ANSWERED)
    assert h.delivered(DeliveryKind.CONFIRM_CANCEL) == []


# ----------------------------------------------- pinned data snapshots --


def test_every_version_of_a_request_answers_against_the_same_data():
    """An addendum computed against different holdings than the answer it is
    adding to is incoherent, however correct each half is on its own (§8)."""
    h = Harness()
    rid = h.ask("taiwan exposure?", author="ana", at=0)
    pinned = h.seq.requests[rid].as_of
    assert pinned is not None

    h.send(msg("also south china sea", at=600, rel=Relationship.AMEND, target=rid))
    h.abort_landed(rid, at=601)

    # Re-run long after the snapshot window would have rolled over.
    assert h.seq.requests[rid].as_of == pinned
    assert h.dispatches(rid)[-1].as_of == pinned


def test_questions_asked_at_the_same_moment_agree_with_each_other():
    """Two people asking concurrently should not straddle a price move and
    report figures that contradict each other."""
    h = Harness()
    first = h.ask("how concentrated am I in semis?", author="ana", at=0)
    second = h.ask("what's my exposure to Taiwan?", author="ben", at=2)

    assert h.seq.requests[first].as_of == h.seq.requests[second].as_of


def test_questions_asked_far_apart_use_fresh_data():
    """The pin is for coherence, not for serving stale numbers forever."""
    h = Harness()
    early = h.ask("q", author="ana", at=0)
    late = h.ask("q2", author="ben", at=5_000)

    assert h.seq.requests[early].as_of != h.seq.requests[late].as_of


def test_a_worker_that_ignores_the_pin_is_caught():
    """The pin is worthless unless workers honour it — and a worker silently
    dropping a field the Sequencer sends it is a mistake already made once in
    this codebase, with retained tool results."""
    from chatsched import invariants
    from chatsched.delivery import Transcript
    from chatsched.model import Completed

    h = Harness()
    rid = h.ask("q", author="ana", at=0)
    h.send(Completed(rid, 1, "answer", 100, 5.0, as_of="some-other-version"))

    transcript = Transcript()
    for cmd in h.all:
        if isinstance(cmd, Deliver):
            transcript.record(cmd, 5.0)
    violations = invariants.check(h.seq, transcript, h.policy, h.pool_size)
    assert any(v.invariant == "snapshot_coherence" for v in violations), violations
