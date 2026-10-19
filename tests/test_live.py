"""The asyncio live runner, driven by fake models.

`live.py` re-implements things `sim.py` already does — adopting an injected
version, draining a worker's inbox at checkpoints, deciding when a run is
finished. Duplicated logic that nothing tests is where bugs live, and the
silent-success bug these tests now guard against was in exactly this file.

Everything here is stubbed: no network, no API key. The runner uses a real
clock, so the timings are deliberately coarse.
"""

from __future__ import annotations

import asyncio

import pytest

import chatsched.llm as llm_module
from chatsched import live
from chatsched.model import DeliveryKind, Relationship
from chatsched.triage import InboundMessage

pytestmark = pytest.mark.asyncio

STAGE = 0.12  # seconds per fake worker stage


class FakeTriage:
    """Reads the relationship off the fixture, like ScriptedTriage, but async."""

    name = "fake-triage"

    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.labels: dict[str, str] = {}
        self.seen: list[str] = []

    async def aclassify(self, msg: InboundMessage, live_view):
        self.seen.append(msg.text)
        rel = Relationship((msg.relationship or "NEW").upper())
        target = None
        if msg.target_label:
            # Fixtures name targets by label; map onto whatever the Sequencer
            # called the request, in creation order.
            ids = [r["id"] for r in live_view]
            target = self.labels.get(msg.target_label) or (ids[0] if ids else None)
        if msg.label and live_view is not None:
            self.labels.setdefault(msg.label, f"r{len(self.labels) + 1}")
        from chatsched.model import Classified

        self.decisions.append({"message": msg.text, "author": msg.author,
                               "proposed": rel.value, "used": rel.value,
                               "confidence": 1.0, "fallback": False,
                               "target": target, "reasoning": "fixture"})
        return Classified(
            msg_id=msg.msg_id, author=msg.author, text=msg.text, at=msg.at,
            relationship=rel, target_id=target,
            preserves_premise=msg.preserves_premise, confidence=1.0,
        )


class FakeWorker:
    """Walks the real checkpoint protocol so inbox draining is exercised."""

    name = "fake-worker"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.changes_seen: list[str] = []
        self.specs: list[str] = []
        self.retained_seen: list[tuple] = []
        self.as_of_seen: list[str | None] = []

    async def answer(self, spec, checkpoint, retained=(), as_of=None):
        self.specs.append(spec)
        self.retained_seen.append(tuple(retained))
        self.as_of_seen.append(as_of)
        if self.fail:
            raise RuntimeError("worker exploded")
        for phase, results in (
            ("before_model", ()),
            ("tool_call", ()),
            ("after_tool", ("tool-result",)),
        ):
            await asyncio.sleep(STAGE)
            change = (
                await checkpoint(phase, results)
                if phase == "after_tool"
                else await checkpoint(phase)
            )
            if change:
                self.changes_seen.append(change)
                spec = f"{spec} + {change}"
        await asyncio.sleep(STAGE)
        return f"[answer: {spec}]", 500, ["tool-result"]


@pytest.fixture
def fakes(monkeypatch):
    triage, worker = FakeTriage(), FakeWorker()
    monkeypatch.setattr(llm_module, "LLMTriage", lambda *a, **k: triage)
    monkeypatch.setattr(llm_module, "LLMWorker", lambda *a, **k: worker)
    monkeypatch.setattr(live, "RUNS_DIR", live.RUNS_DIR)  # keep logs where they are
    return triage, worker


def message(text, author="ana", at=0.0, rel="NEW", target=None, label=None, preserves=True):
    return InboundMessage(
        msg_id=f"m-{text[:6]}", author=author, text=text, at=at,
        relationship=rel, target_label=target, label=label,
        preserves_premise=preserves,
    )


async def run(messages, pool_size=3, policy="DEPENDENCY_ORDER", explain=False):
    return await live._main(messages, "test", policy, pool_size, explain)


# ------------------------------------------------------------- happy path --


async def test_two_questions_are_answered_and_invariants_hold(fakes):
    _triage, worker = fakes
    code = await run([
        message("first question", label="a"),
        message("second question", author="ben", at=0.05, label="b"),
    ])
    assert code == 0
    assert len(worker.specs) == 2


async def test_a_change_during_planning_restarts_the_worker(fakes):
    """Matrix row AMEND × PLANNING: nothing expensive has happened yet, so the
    cheapest correct thing is to abort and start again with the new spec."""
    _triage, worker = fakes
    code = await run([
        message("taiwan exposure", label="q"),
        # Before the first checkpoint, so the request is still PLANNING.
        message("also south china sea", at=STAGE * 0.4, rel="AMEND", target="q"),
    ])
    assert code == 0
    assert worker.changes_seen == [], "a restart should not also inject"
    assert len(worker.specs) == 2, "the worker should have been restarted"
    assert "also south china sea" in worker.specs[-1]


async def test_a_change_mid_tool_call_is_injected_at_the_next_checkpoint(fakes):
    """Matrix row AMEND × TOOL_CALL, and the §7a mechanism end to end.

    The tool call is already paid for, so the change is not applied the
    instant it arrives — it waits in the worker's inbox and is drained at the
    next checkpoint. The worker adopts the new version, so version gating
    still accepts its result and nothing is recomputed.
    """
    _triage, worker = fakes
    code = await run([
        message("taiwan exposure", label="q"),
        # Inside the TOOL_CALL window: after the tool_call checkpoint at
        # 2*STAGE, before the after_tool checkpoint at 3*STAGE.
        message("also south china sea", at=STAGE * 2.5, rel="AMEND", target="q"),
    ])
    assert code == 0
    assert worker.changes_seen == ["also south china sea"], "change never reached the worker"
    assert len(worker.specs) == 1, "injection must not restart the worker"


async def test_a_dependent_question_waits_for_its_parent(fakes):
    _triage, worker = fakes
    code = await run([
        message("biggest position?", author="ben", label="p"),
        message("and the second?", author="ben", at=0.05, rel="REFERENCE", target="p"),
    ])
    assert code == 0
    # The follow-up's spec was materialised from the parent's answer.
    assert any("[answer:" in spec for spec in worker.specs[1:])


async def test_the_pool_bound_holds_under_real_concurrency(fakes):
    """Simulated concurrency is cooperative; this one is genuine asyncio."""
    code = await run([message(f"q{i}", at=i * 0.01, label=f"q{i}") for i in range(6)],
                     pool_size=2)
    assert code == 0


# --------------------------------------------------------- failure paths --


async def test_a_failing_worker_still_tells_the_reader(monkeypatch):
    """A worker that raises owes the asker a word. Completeness (§3) is not
    suspended because something broke."""
    triage, worker = FakeTriage(), FakeWorker(fail=True)
    monkeypatch.setattr(llm_module, "LLMTriage", lambda *a, **k: triage)
    monkeypatch.setattr(llm_module, "LLMWorker", lambda *a, **k: worker)
    code = await run([message("doomed question", label="q")])
    # Invariants still hold: the request reached a terminal state and the
    # reader was told why.
    assert code == 0


async def test_triage_failure_exits_nonzero_instead_of_claiming_success(monkeypatch):
    """Regression. When triage raised, the ingest task died silently, no
    requests were created, and the runner then reported a clean invariant
    check over a run that did nothing — the most misleading output this
    system could produce."""

    class ExplodingTriage(FakeTriage):
        async def aclassify(self, msg, live_view):
            raise RuntimeError("401 invalid x-api-key")

    monkeypatch.setattr(llm_module, "LLMTriage", lambda *a, **k: ExplodingTriage())
    monkeypatch.setattr(llm_module, "LLMWorker", lambda *a, **k: FakeWorker())

    code = await run([message("anything", label="q")])
    assert code == 2, "a failed run must not report success"


async def test_an_empty_run_refuses_to_claim_invariants_hold(fakes):
    """Zero requests is not zero violations."""
    code = await run([])
    assert code == 2


async def test_retained_tool_results_reach_the_worker_in_live_mode(fakes):
    """The Sequencer keeps tool results across a premise-preserving amendment
    (§7c). They have to actually arrive at the worker — the simulator honoured
    this and live mode silently did not, so the "0% wasted" headline was a
    property of the mock.
    """
    _triage, worker = fakes
    code = await run([
        message("taiwan exposure", label="q"),
        # After the tool result is back (3*STAGE) but before the run ends.
        message("also south china sea", at=STAGE * 3.4, rel="AMEND", target="q"),
    ])
    assert code == 0
    assert len(worker.specs) == 2, "a restart was expected at this stage"
    assert worker.retained_seen[-1], "the restart re-fetched instead of reusing"


async def test_the_pinned_data_version_reaches_the_worker(fakes):
    """The Sequencer pins a data version per request so concurrent answers
    agree and an addendum is coherent with the answer it extends (§8). Like
    retained tool results, it is worth nothing unless the worker receives it —
    and that is a field this codebase has already dropped once."""
    _triage, worker = fakes
    code = await run([
        message("first question", label="a"),
        message("second question", author="ben", at=0.05, label="b"),
    ])
    assert code == 0
    assert all(seen is not None for seen in worker.as_of_seen), "pin never arrived"
    # Asked at the same moment, so they must answer against the same data.
    assert len(set(worker.as_of_seen)) == 1
