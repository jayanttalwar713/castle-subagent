"""Deterministic discrete-event simulator.

The Sequencer is a pure function, so "concurrency" here is a priority queue of
scheduled events rather than threads or asyncio. Two consequences:

  * Scenario replay is byte-for-byte reproducible, so the policy comparison in
    §9 measures the scheduler rather than machine noise.
  * The fuzzer in the test suite can run thousands of randomised message
    streams in seconds and assert invariants on every one.

Tools are mocked with controllable latency. That is a deliberate upgrade over
real tools, not a shortcut: reproducing "the amendment arrives *during* the
tool call" requires controlling exactly when that call returns (§10).
"""

from __future__ import annotations

import heapq
import json
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from .delivery import Transcript
from .journal import EventLog
from .model import (
    Abort,
    Completed,
    Deliver,
    Dispatch,
    Failed,
    Inject,
    Stage,
    StageChanged,
)
from .policies import DependencyOrder, Policy
from .sequencer import Sequencer
from .triage import InboundMessage, ScriptedTriage, Triage

#: Token cost per stage. Tool calls dominate, which is what makes "preserve
#: work in proportion to what it cost" (§7c) worth doing.
TOKENS = {Stage.PLANNING: 200, Stage.TOOL_CALL: 600, Stage.DRAFTING: 400}


@dataclass
class Run:
    """One worker working on one request-version."""

    request_id: str
    version: int
    spec: str
    stages: list[tuple[Stage, float, int]]
    as_of: str | None = None
    #: Index of the stage currently executing.
    index: int = 0
    tokens_spent: int = 0
    aborted: bool = False
    #: A change waiting to be applied at this worker's next checkpoint (§7a).
    pending_injection: tuple[int, str] | None = None


@dataclass
class Work:
    """How long each stage takes for a given request."""

    plan: float = 0.4
    tool: float = 3.0
    draft: float = 0.8


@dataclass
class Result:
    transcript: Transcript
    sequencer: Sequencer
    sequencer_times: list[float]
    finished_at: float


@dataclass
class Simulation:
    messages: list[InboundMessage]
    policy: Policy = field(default_factory=DependencyOrder)
    pool_size: int = 3
    version_gate: bool = True
    triage: Triage = field(default_factory=ScriptedTriage)
    work: dict[str, Work] = field(default_factory=dict)
    #: Optional append-only event log. Every event entering the Sequencer is
    #: written here before it is handled, which is what makes the run
    #: recoverable (§14). None means run without durability, which is what
    #: the test suite wants almost everywhere.
    journal: "EventLog | None" = None

    def __post_init__(self) -> None:
        self.seq = Sequencer(
            pool_size=self.pool_size,
            policy=self.policy,
            version_gate=self.version_gate,
        )
        self.transcript = Transcript()
        self.sequencer_times: list[float] = []
        self._queue: list[tuple[float, int, str, object]] = []
        self._tick = 0
        self._now = 0.0
        self._runs: dict[str, Run] = {}
        self._labels: dict[str, str] = {}
        self._work_for: dict[str, Work] = {}
        self._pending_work: Work | None = None

    # -- public ------------------------------------------------------------

    def run(self, max_time: float = 10_000.0) -> Result:
        for msg in self.messages:
            self._schedule(msg.at, "message", msg)
        while self._queue:
            at, _, kind, payload = heapq.heappop(self._queue)
            if at > max_time:
                break
            self._now = at
            if kind == "message":
                self._on_message(payload)  # type: ignore[arg-type]
            elif kind == "boundary":
                self._on_boundary(payload)  # type: ignore[arg-type]
            elif kind == "abort":
                self._on_abort(payload)  # type: ignore[arg-type]
        return Result(
            transcript=self.transcript,
            sequencer=self.seq,
            sequencer_times=self.sequencer_times,
            finished_at=self._now,
        )

    # -- event handlers ----------------------------------------------------

    def _on_message(self, msg: InboundMessage) -> None:
        decision = self.triage.classify(msg, self.seq.live_view(), self._labels)
        before = set(self.seq.requests)
        # A brand-new request is dispatched *inside* this _feed, before we know
        # its id, so stage its work profile where _start_run can find it.
        self._pending_work = self.work.get(msg.label) if msg.label else None
        self._feed(decision)
        self._pending_work = None
        # A fixture line that created a request registers its label so later
        # lines can refer to it (AMEND that one, CANCEL that one, ...).
        new = set(self.seq.requests) - before
        if msg.label and new:
            rid = next(iter(new))
            self._labels[msg.label] = rid
            self._work_for[rid] = self.work.get(msg.label, Work())

    def _on_boundary(self, payload: tuple[Run, int]) -> None:
        run, index = payload
        if run.aborted or self._runs.get(run.request_id) is not run:
            return
        run.index = index
        stage, _, tokens = run.stages[index]
        run.tokens_spent += tokens

        # Checkpoint: drain the mutation inbox here, at a stage boundary,
        # rather than tearing the worker apart mid-call (§7a).
        if run.pending_injection is not None:
            new_version, _change = run.pending_injection
            run.pending_injection = None
            run.version = new_version
            work = self._work_for.get(run.request_id, Work())
            run.stages.append((Stage.DRAFTING, work.draft * 0.5, TOKENS[Stage.DRAFTING] // 2))

        nxt = index + 1
        if nxt < len(run.stages):
            next_stage, duration, _ = run.stages[nxt]
            # Leaving TOOL_CALL hands the expensive results back to the
            # Sequencer, so a later premise-preserving amendment can re-draft
            # without paying for them twice (§7c).
            results = (
                (f"tool-result:{run.request_id}",)
                if stage is Stage.TOOL_CALL
                else ()
            )
            self._feed(
                StageChanged(
                    run.request_id, run.version, next_stage, self._now, results
                )
            )
            run.index = nxt
            self._schedule(self._now + duration, "boundary", (run, nxt))
            return

        self._runs.pop(run.request_id, None)
        self._feed(
            Completed(
                request_id=run.request_id,
                version=run.version,
                answer=self._answer_for(run),
                tokens=run.tokens_spent,
                at=self._now,
                as_of=run.as_of,
            )
        )

    def _on_abort(self, payload: tuple[str, int]) -> None:
        request_id, version = payload
        run = self._runs.get(request_id)
        if run is None or run.version != version:
            return
        if run.index >= len(run.stages) - 1:
            # The abort lost the race: this worker is already in its last
            # stage and will finish and report regardless. That is not a
            # simulator shortcut — it is the situation version gating exists
            # for. Correctness must not depend on cancellation landing (§7b),
            # and `--no-version-gate` exists to show what happens when it does.
            return
        run.aborted = True
        self._runs.pop(request_id, None)
        # Charge what the abandoned work already cost. This is the "wasted
        # tokens" metric — the price of mutability, in dollars (§9).
        self._feed(
            Failed(request_id, version, "aborted", run.tokens_spent, self._now)
        )

    # -- command application ----------------------------------------------

    def _feed(self, event) -> None:
        # Logged *before* it is handled. An event that changed the state but
        # never reached the log is unrecoverable; one that reached the log but
        # was never handled simply replays. Only one of those two orderings
        # is safe.
        if self.journal is not None:
            self.journal.append(event)
        start = perf_counter()
        commands = self.seq.handle(event)
        self.sequencer_times.append(perf_counter() - start)
        for cmd in commands:
            self._apply(cmd)

    def _apply(self, cmd) -> None:
        if isinstance(cmd, Dispatch):
            self._start_run(cmd)
        elif isinstance(cmd, Abort):
            # Scheduled rather than applied inline, so the Sequencer is never
            # re-entered from inside its own command list.
            self._schedule(self._now, "abort", (cmd.request_id, cmd.version))
        elif isinstance(cmd, Inject):
            if (run := self._runs.get(cmd.request_id)) is not None:
                run.pending_injection = (cmd.version, cmd.change)
        elif isinstance(cmd, Deliver):
            self.transcript.record(cmd, self._now)

    def _start_run(self, cmd: Dispatch) -> None:
        work = self._work_for.get(cmd.request_id)
        if work is None:
            work = self._pending_work or Work()
            self._work_for[cmd.request_id] = work
        if cmd.retained_tool_results:
            # Tool results survived the amendment, so skip the expensive call
            # and go straight to re-drafting (§7c).
            stages = [
                (Stage.PLANNING, work.plan * 0.3, TOKENS[Stage.PLANNING] // 3),
                (Stage.DRAFTING, work.draft, TOKENS[Stage.DRAFTING]),
            ]
        else:
            stages = [
                (Stage.PLANNING, work.plan, TOKENS[Stage.PLANNING]),
                (Stage.TOOL_CALL, work.tool, TOKENS[Stage.TOOL_CALL]),
                (Stage.DRAFTING, work.draft, TOKENS[Stage.DRAFTING]),
            ]
        run = Run(cmd.request_id, cmd.version, cmd.spec, stages, as_of=cmd.as_of)
        self._runs[cmd.request_id] = run
        self._schedule(self._now + stages[0][1], "boundary", (run, 0))

    # -- helpers -----------------------------------------------------------

    def _schedule(self, at: float, kind: str, payload) -> None:
        self._tick += 1
        heapq.heappush(self._queue, (at, self._tick, kind, payload))

    def _answer_for(self, run: Run) -> str:
        req = self.seq.requests.get(run.request_id)
        spec = req.spec if req else run.spec
        return f"[answer to: {' '.join(spec.split())[:60]}]"


# --------------------------------------------------------------------------
# Scenario fixtures
# --------------------------------------------------------------------------

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"


def load_scenario(path: str | Path) -> tuple[list[InboundMessage], dict[str, Work], str]:
    """Load a replayable fixture: messages, per-request work profiles, title."""
    p = Path(path)
    if not p.exists():
        p = SCENARIO_DIR / f"{path}.json"
    raw = json.loads(p.read_text())
    messages = [
        InboundMessage(
            msg_id=f"m{i + 1}",
            author=m["author"],
            text=m["text"],
            at=float(m["at"]),
            member=m.get("member"),
            label=m.get("label"),
            target_label=m.get("target"),
            relationship=m.get("rel", "NEW"),
            preserves_premise=bool(m.get("preserves_premise", True)),
            rewritten_spec=m.get("rewritten"),
            names_target=bool(m.get("names_target", True)),
        )
        for i, m in enumerate(raw["messages"])
    ]
    work = {
        label: Work(**profile) for label, profile in raw.get("work", {}).items()
    }
    return messages, work, raw.get("title", p.stem)
