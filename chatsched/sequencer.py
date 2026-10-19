"""The Sequencer: the only component with shared state, and a pure function.

`handle(event) -> [commands]`. No I/O, no clock reads, no model calls. Time
arrives on the event. That is DESIGN.md §5's "serialize the cheap thing" made
structural rather than rhetorical — this component *cannot* do inference, and
because it is pure, the whole system is deterministically testable and the
races in §8 do not exist rather than being handled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .depgraph import DependencyGraph
from .model import (
    Abort,
    Classified,
    Command,
    Completed,
    Deliver,
    DeliveryKind,
    Dispatch,
    Event,
    Failed,
    Inject,
    Relationship,
    Request,
    Stage,
    StageChanged,
)
from .policies import DependencyOrder, Policy


@dataclass
class DeliveryRecord:
    """One thing the chat saw, kept for the metrics table (§9)."""

    at: float
    kind: DeliveryKind
    request_id: str
    version: int
    attributed: bool
    outstanding: int
    stale: bool


@dataclass
class Sequencer:
    pool_size: int = 3
    policy: Policy = field(default_factory=DependencyOrder)
    #: Turn off to demonstrate the staleness invariant failing (§9). With
    #: gating on, a result whose version is not current is ignored; with it
    #: off, stale answers reach the chat and the invariant checker catches it.
    version_gate: bool = True
    #: Requests admitted within this many seconds of each other answer
    #: against the same data version, so concurrent answers agree with each
    #: other rather than straddling a price move.
    snapshot_window: float = 60.0

    requests: dict[str, Request] = field(default_factory=dict)
    graph: DependencyGraph = field(default_factory=DependencyGraph)
    journal: list[DeliveryRecord] = field(default_factory=list)

    #: request_id -> the version currently occupying a worker slot. Keyed by
    #: version so that a *stale* worker finishing cannot release the slot its
    #: replacement is holding.
    _in_flight: dict[str, int] = field(default_factory=dict)
    _ready: dict[str, float] = field(default_factory=dict)
    _seq: int = 0
    _ids: int = 0
    _snapshot: tuple[str, float] | None = None
    _snapshots: int = 0
    #: High-water mark of pool occupancy, asserted against the policy's
    #: concurrency limit by the invariant checker.
    max_in_flight: int = 0

    # -- public ------------------------------------------------------------

    def handle(self, ev: Event) -> list[Command]:
        out: list[Command] = []
        if isinstance(ev, Classified):
            out += self._on_classified(ev)
        elif isinstance(ev, StageChanged):
            out += self._on_stage(ev)
        elif isinstance(ev, Completed):
            out += self._on_completed(ev)
        elif isinstance(ev, Failed):
            out += self._on_failed(ev)
        else:  # pragma: no cover - exhaustive over Event
            raise TypeError(f"unhandled event {ev!r}")
        out += self._settle(ev.at)
        return out

    @property
    def outstanding(self) -> list[Request]:
        """Requests the reader is still owed an answer for (§3 completeness)."""
        return [r for r in self.requests.values() if not r.is_terminal]

    def live_view(self) -> list[dict]:
        """Bounded snapshot of the live request set, for triage (§8).

        All non-terminal requests plus the most recent few delivered ones. The
        live set is bounded; the chat transcript is not, and is not included.
        """
        live = [r for r in self.requests.values() if not r.is_terminal]
        done = sorted(
            (r for r in self.requests.values() if r.stage is Stage.DELIVERED),
            key=lambda r: r.delivered_at or 0.0,
        )[-3:]
        return [
            {
                "id": r.id,
                "spec": r.spec,
                "askers": list(r.askers),
                "member": r.member,
                "stage": r.stage.value,
                "answer": (r.answer or "")[:160] if r.stage is Stage.DELIVERED else None,
            }
            for r in [*live, *done]
        ]

    # -- message handling --------------------------------------------------

    def _on_classified(self, ev: Classified) -> list[Command]:
        if ev.relationship is Relationship.IGNORE:
            # Not a request. Creating one would acknowledge a thank-you and
            # spend a worker answering it.
            return []

        target = self.requests.get(ev.target_id) if ev.target_id else None

        # A relationship that needs a target but lost it (classifier error, or
        # the target was garbage collected) degrades to NEW rather than being
        # dropped. Biased toward answering the question (§4).
        if ev.relationship is not Relationship.NEW and target is None:
            return self._new_request(ev)

        match ev.relationship:
            case Relationship.NEW:
                return self._new_request(ev)
            case Relationship.AMEND:
                return self._amend(ev, target)
            case Relationship.CANCEL:
                return self._cancel(ev, target)
            case Relationship.DUPLICATE:
                return self._duplicate(ev, target)
            case Relationship.REFERENCE:
                return self._reference(ev, target)
        raise AssertionError(f"unhandled relationship {ev.relationship}")

    def _new_request(self, ev: Classified, depends_on: str | None = None) -> list[Command]:
        req = self._register(ev, depends_on=depends_on)
        return [self._deliver(req, DeliveryKind.ACK, to=(ev.author,))]

    def _amend(self, ev: Classified, target: Request) -> list[Command]:
        # A change that arrives after the answer shipped is a real state with
        # real required behaviour, not an error (§7, DELIVERED column).
        if target.stage is Stage.DELIVERED:
            return self._reopen(ev, target)
        if target.stage is Stage.DROPPED:
            return self._new_request(ev)

        stage = target.stage
        running_version = self._in_flight.get(target.id)
        target.spec = self._merge_spec(
            target.spec, ev.text, ev.preserves_premise, ev.rewritten_spec
        )
        target.version += 1
        if not ev.preserves_premise:
            # The premise changed, so the research is worthless (§7c).
            target.tool_results.clear()

        cmds: list[Command] = [self._deliver(target, DeliveryKind.AMEND_ACK, to=(ev.author,))]

        if stage is Stage.QUEUED:
            # Nothing has been paid for yet; the new spec is picked up at
            # admission. No worker to disturb.
            return cmds

        if stage is Stage.TOOL_CALL and ev.preserves_premise:
            # The tool call is already paid for. Let it land and apply the
            # change at the worker's next checkpoint (§7a). The worker adopts
            # the new version, so version gating still holds — and the slot
            # stays with the same worker.
            self._in_flight[target.id] = target.version
            return cmds + [Inject(target.id, target.version, ev.text)]

        # PLANNING, or any premise-replacing change: restart.
        #
        # Re-queue rather than dispatching inline. The amendment may have
        # arrived in the ready-but-undelivered window (§13) — the worker has
        # already finished and freed its slot, so an inline dispatch would
        # push the pool over its limit. Going through admission keeps the
        # bound, and costs nothing when the slot is still held.
        if running_version is not None:
            cmds.append(Abort(target.id, running_version, "superseded by amendment"))
        target.stage = Stage.QUEUED
        self._invalidate_pending(target)
        return cmds

    def _reopen(self, ev: Classified, target: Request) -> list[Command]:
        """Amend a request that already delivered: re-run it and ship either an
        addendum or a visible replacement notice."""
        target.spec = self._merge_spec(
            target.spec, ev.text, ev.preserves_premise, ev.rewritten_spec
        )
        target.version += 1
        target.stage = Stage.QUEUED
        self._invalidate_pending(target)
        if not ev.preserves_premise:
            target.tool_results.clear()
        target.redeliver_as = (
            DeliveryKind.ADDENDUM if ev.preserves_premise else DeliveryKind.SUPERSEDE
        )
        return [self._deliver(target, DeliveryKind.AMEND_ACK, to=(ev.author,))]

    def _cancel(self, ev: Classified, target: Request) -> list[Command]:
        if not ev.names_target and not target.is_terminal:
            # Measured: confidence does not separate a real "never mind the
            # Taiwan one" from a bare "no" that was aimed at another person in
            # the chat — both arrive around 0.92-0.95, so no threshold can
            # tell them apart (§4). What *does* separate them is whether the
            # message names what it refers to. When it does not, ask.
            #
            # Asking needs no parked state: if they meant it, they say so
            # again, and that reply names its target by construction.
            return [
                self._deliver(
                    target,
                    DeliveryKind.CONFIRM_CANCEL,
                    to=(ev.author,),
                    body="stop working on this?",
                )
            ]
        if target.stage is Stage.DELIVERED:
            return [
                self._deliver(target, DeliveryKind.ALREADY_ANSWERED, to=(ev.author,))
            ]
        if target.stage is Stage.DROPPED:
            return [self._deliver(target, DeliveryKind.CANCEL_ACK, to=(ev.author,))]

        # One asker cancelling does not cancel everyone else's question. In the
        # §1 transcript Cara cancels the Taiwan question she duplicated — Ana,
        # who asked it first, is still owed an answer.
        if ev.author in target.askers and len(target.askers) > 1:
            target.askers.remove(ev.author)
            return [self._deliver(target, DeliveryKind.CANCEL_ACK, to=(ev.author,))]

        cmds: list[Command] = [
            self._deliver(target, DeliveryKind.CANCEL_ACK, to=(ev.author,))
        ]
        if (running := self._in_flight.get(target.id)) is not None:
            cmds.append(Abort(target.id, running, "cancelled"))
        cmds += self._terminate(target, Stage.DROPPED, ev.at)
        return cmds

    def _duplicate(self, ev: Classified, target: Request) -> list[Command]:
        if target.stage is Stage.DROPPED:
            return self._new_request(ev)
        if ev.member != target.member:
            # Same words, different portfolio — so not the same question.
            # Fanning the existing answer out here would both answer the wrong
            # question and show one member another member's positions (§11).
            return self._new_request(ev)
        if ev.author not in target.askers:
            target.askers.append(ev.author)
        if target.stage is Stage.DELIVERED:
            # Fan the existing answer out to the new asker; pay nothing twice.
            return [
                self._deliver(
                    target,
                    DeliveryKind.ANSWER,
                    to=(ev.author,),
                    body=target.answer or "",
                )
            ]
        return [self._deliver(target, DeliveryKind.ACK, to=(ev.author,))]

    def _reference(self, ev: Classified, target: Request) -> list[Command]:
        # Referencing a request that already died would wait on an answer that
        # is never coming. The graph was released when the parent terminated,
        # so no later release would free this child — it would hang forever.
        # Treat it as an independent question instead (§8).
        if target.stage is Stage.DROPPED:
            return self._new_request(ev)

        req = self._register(ev, depends_on=None)
        # A cycle can only come from classifier error. Drop the newer edge and
        # treat the request as independent: a correct answer with weaker
        # ordering is the right direction to fail (§8).
        if self.graph.add_edge(req.id, target.id):
            req.depends_on = target.id
            req.origin_parent = target.id
        return [self._deliver(req, DeliveryKind.ACK, to=(ev.author,))]

    # -- worker events -----------------------------------------------------

    def _on_stage(self, ev: StageChanged) -> list[Command]:
        req = self.requests.get(ev.request_id)
        if req is None or not self._current(req, ev.version) or req.is_terminal:
            return []
        req.stage = ev.stage
        if ev.tool_results:
            req.tool_results = list(ev.tool_results)
        return []

    def _on_completed(self, ev: Completed) -> list[Command]:
        req = self.requests.get(ev.request_id)
        if req is None:
            return []
        req.tokens_by_version[ev.version] = (
            req.tokens_by_version.get(ev.version, 0) + ev.tokens
        )
        self._release_slot(req.id, ev.version)

        if not self._current(req, ev.version):
            # Stale result. We never needed to successfully kill the worker —
            # only to ignore it (§7b).
            return []
        if req.is_terminal:
            return []

        req.answer = ev.answer
        req.answer_version = ev.version
        req.answered_as_of = ev.as_of
        req.ready_at = ev.at
        self._ready[req.id] = ev.at
        return []

    def _on_failed(self, ev: Failed) -> list[Command]:
        req = self.requests.get(ev.request_id)
        if req is None:
            return []
        req.tokens_by_version[ev.version] = (
            req.tokens_by_version.get(ev.version, 0) + ev.tokens
        )
        self._release_slot(req.id, ev.version)
        if not self._current(req, ev.version) or req.is_terminal:
            return []
        cmds: list[Command] = [
            self._deliver(req, DeliveryKind.DROPPED_NOTICE, body=ev.error)
        ]
        return cmds + self._terminate(req, Stage.DROPPED, ev.at)

    # -- the settle loop ---------------------------------------------------

    def _settle(self, now: float) -> list[Command]:
        """Deliver what may ship, then admit what may run, to a fixpoint.

        Delivering a parent can unblock a child's admission, and admitting is
        bounded by the pool, so this terminates.
        """
        cmds: list[Command] = []
        while True:
            progressed = False
            batch = self._deliver_pass(now)
            if batch:
                cmds += batch
                progressed = True
            batch = self._admit_pass(now)
            if batch:
                cmds += batch
                progressed = True
            if not progressed:
                return cmds

    def _deliver_pass(self, now: float) -> list[Command]:
        cmds: list[Command] = []
        for rid in sorted(self._ready, key=lambda r: (self._ready[r], self.requests[r].arrival_seq)):
            req = self.requests[rid]
            if req.stage is Stage.DROPPED:
                del self._ready[rid]
                continue
            if not self.policy.may_deliver(req, self.requests):
                continue
            del self._ready[rid]
            kind = req.redeliver_as or DeliveryKind.ANSWER
            req.redeliver_as = None
            cmds.append(self._deliver(req, kind, body=req.answer or "", at=now))
            req.delivered_version = req.version
            cmds += self._terminate(req, Stage.DELIVERED, now)
            break  # state changed; re-enter the loop with a fresh view
        return cmds

    def _admit_pass(self, now: float) -> list[Command]:
        limit = self.policy.concurrency(self.pool_size)
        cmds: list[Command] = []
        while len(self._in_flight) < limit:
            req = self._next_admissible()
            if req is None:
                break
            cmds.append(self._dispatch(req, now))
        return cmds

    def _next_admissible(self) -> Request | None:
        best: Request | None = None
        for req in self.requests.values():
            if req.stage is not Stage.QUEUED or req.id in self._in_flight:
                continue
            if req.id in self._ready:
                continue
            # Dependency gating happens *here*, at admission — never while
            # holding a worker slot. A pool full of waiting dependents would
            # otherwise deadlock (§8).
            #
            # The gate is "does the parent's answer exist", not "is the parent
            # currently DELIVERED". Those differ: a parent that delivered and
            # was then amended is momentarily un-delivered, and one that
            # delivered and was then cancelled is DROPPED — but in both cases
            # the answer this child needs already exists. Gating on stage
            # stranded such children forever.
            if req.depends_on is not None:
                parent = self.requests.get(req.depends_on)
                if parent is None or parent.first_delivered_at is None:
                    continue
            if best is None or req.arrival_seq < best.arrival_seq:
                best = req
        return best

    def _dispatch(self, req: Request, now: float = 0.0) -> Dispatch:
        if req.depends_on is not None:
            parent = self.requests.get(req.depends_on)
            if parent is not None and parent.answer is not None:
                # A REFERENCE request cannot even be written down until its
                # parent has answered. Its spec is materialised here, at
                # admission — not at arrival (§8).
                req.spec = f"{req.spec}\n\n(in reference to: {parent.answer})"

                # It also inherits the parent's world. A follow-up is an
                # answer *about* an answer: "and the second biggest?" has to
                # be computed against the holdings its parent saw, or the two
                # can disagree while one is explicitly built on the other.
                # Freshness is the wrong instinct here — if you want current
                # data you ask a new question, not a follow-up.
                #
                # Inheriting the tool results too is the same decision read
                # as cost rather than correctness: the lookup has been paid
                # for, at exactly the version this request needs.
                if req.as_of is None:
                    req.as_of = parent.as_of
                if not req.tool_results:
                    req.tool_results = list(parent.tool_results)
                req.depends_on = None
        if req.as_of is None:
            # Pinned once. Later versions of this question re-run against the
            # same data, so an addendum is coherent with the answer it extends.
            req.as_of = self._snapshot_for(now)
        req.stage = Stage.PLANNING
        self._in_flight[req.id] = req.version
        self.max_in_flight = max(self.max_in_flight, len(self._in_flight))
        return Dispatch(
            req.id, req.version, req.spec, tuple(req.tool_results),
            req.as_of, req.member,
        )

    def _snapshot_for(self, now: float) -> str:
        """The data version to answer against.

        Reused across requests admitted close together, so two people asking
        at the same moment get figures that agree. Deterministic — the id is a
        counter, not a clock reading, because the Sequencer is a pure function.
        """
        if self._snapshot is not None and now - self._snapshot[1] <= self.snapshot_window:
            return self._snapshot[0]
        self._snapshots += 1
        self._snapshot = (f"as-of-{self._snapshots}", now)
        return self._snapshot[0]

    def _invalidate_pending(self, req: Request) -> None:
        """Throw away an answer that is ready but not yet delivered.

        An amendment can land in the window between "worker finished" and
        "policy released the answer" (§13). That pending answer was computed
        against the old spec, so it must not survive into the new version —
        otherwise a buffering policy ships it and staleness stops being zero.
        """
        self._ready.pop(req.id, None)
        req.ready_at = None
        req.answer_version = None

    def _release_slot(self, request_id: str, version: int) -> None:
        """Free the pool slot only if *this* worker is the one holding it.

        A superseded worker finishing late must not release the slot its
        replacement is occupying.
        """
        if self._in_flight.get(request_id) == version:
            del self._in_flight[request_id]

    # -- helpers -----------------------------------------------------------

    def _register(self, ev: Classified, depends_on: str | None) -> Request:
        self._ids += 1
        self._seq += 1
        req = Request(
            id=f"r{self._ids}",
            spec=ev.text,
            asked_text=ev.text,
            askers=[ev.author],
            member=ev.member,
            arrival_seq=self._seq,
            created_at=ev.at,
            depends_on=depends_on,
        )
        self.requests[req.id] = req
        return req

    def _terminate(self, req: Request, stage: Stage, now: float) -> list[Command]:
        """Move a request to a terminal state and deal with anyone waiting."""
        req.stage = stage
        self._ready.pop(req.id, None)
        self._in_flight.pop(req.id, None)
        if stage is Stage.DELIVERED:
            req.delivered_at = now
            if req.first_delivered_at is None:
                req.first_delivered_at = now
            # Children become admissible; the admit pass will pick them up.
            self.graph.release(req.id)
            return []

        # The parent died. Its dependents can only be specified if it managed
        # to answer at least once before dying; otherwise they are dropped with
        # an explicit notice. Leaving them waiting forever is the one
        # unacceptable outcome (§8).
        cmds: list[Command] = []
        for child_id in self.graph.release(req.id):
            child = self.requests.get(child_id)
            if child is None or child.is_terminal:
                continue
            if req.first_delivered_at is not None:
                continue  # the answer it needs already shipped; stays admissible
            cmds.append(
                self._deliver(
                    child,
                    DeliveryKind.DROPPED_NOTICE,
                    body=f"depended on {req.requote()!r}, which was not answered",
                )
            )
            cmds += self._terminate(child, Stage.DROPPED, now)
        return cmds

    def _current(self, req: Request, version: int) -> bool:
        """Version gating (§7b). With the gate off, stale work is accepted and
        the staleness metric stops being zero — that is the point of the flag."""
        if not self.version_gate:
            return True
        return req.version == version

    @staticmethod
    def _merge_spec(
        spec: str,
        change: str,
        preserves_premise: bool,
        rewritten: str | None = None,
    ) -> str:
        """The question as it now stands.

        Prefer triage's rewrite: it read the original and the change together
        and can state the result. Concatenation is the fallback, and it is a
        poor one — "How did my book do in Q2? sorry I meant Q3 not Q2" hands
        the worker a self-contradiction and hopes it picks the right half.
        """
        if rewritten:
            return rewritten.strip()
        return f"{spec} {change}".strip() if preserves_premise else change

    def _deliver(
        self,
        req: Request,
        kind: DeliveryKind,
        to: tuple[str, ...] | None = None,
        body: str = "",
        at: float | None = None,
    ) -> Deliver:
        attributed = self.policy.attributes
        if at is not None and kind in _COUNTED:
            self.journal.append(
                DeliveryRecord(
                    at=at,
                    kind=kind,
                    request_id=req.id,
                    version=req.version,
                    attributed=attributed,
                    outstanding=len(self.outstanding),
                    # Stale = we shipped an answer computed against a spec
                    # that had already changed underneath it.
                    stale=req.answer_version is not None
                    and req.answer_version != req.version,
                )
            )
        return Deliver(
            kind=kind,
            request_id=req.id,
            version=req.version,
            to=tuple(to or req.askers),
            re=req.requote() if attributed else "",
            body=body,
        )


#: Delivery kinds that count as "an answer reached the chat" for the metrics.
_COUNTED = frozenset(
    {
        DeliveryKind.ANSWER,
        DeliveryKind.ADDENDUM,
        DeliveryKind.SUPERSEDE,
    }
)
