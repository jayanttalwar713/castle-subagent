"""Shared test helpers.

The Sequencer is a pure function, so unit tests drive it directly: feed an
event, assert on the commands. No clock, no asyncio, no API calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from hypothesis import HealthCheck, settings

# The fuzzer is the project's main correctness argument, so how hard it runs is
# a knob rather than a constant:  HYPOTHESIS_PROFILE=soak pytest tests/test_fuzz.py
_COMMON = dict(deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("default", max_examples=300, **_COMMON)
settings.register_profile("soak", max_examples=5000, **_COMMON)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))

from chatsched.model import (
    Abort,
    Classified,
    Completed,
    Deliver,
    DeliveryKind,
    Dispatch,
    Failed,
    Relationship,
    Stage,
    StageChanged,
)
from chatsched.policies import DependencyOrder, Policy
from chatsched.sequencer import Sequencer


def msg(
    text: str,
    author: str = "ana",
    at: float = 0.0,
    rel: Relationship = Relationship.NEW,
    target: str | None = None,
    preserves: bool = True,
) -> Classified:
    return Classified(
        msg_id=f"m-{text[:8]}",
        author=author,
        text=text,
        at=at,
        relationship=rel,
        target_id=target,
        preserves_premise=preserves,
    )


@dataclass
class Harness:
    """Drives a Sequencer and remembers everything it emitted."""

    pool_size: int = 3
    policy: Policy = field(default_factory=DependencyOrder)
    version_gate: bool = True

    def __post_init__(self) -> None:
        self.seq = Sequencer(
            pool_size=self.pool_size,
            policy=self.policy,
            version_gate=self.version_gate,
        )
        self.all: list = []

    def send(self, event) -> list:
        out = self.seq.handle(event)
        self.all.extend(out)
        return out

    # -- convenience ------------------------------------------------------

    def ask(self, text: str, author: str = "ana", at: float = 0.0) -> str:
        """Ask a new question; returns the request id."""
        before = set(self.seq.requests)
        self.send(msg(text, author=author, at=at))
        return next(iter(set(self.seq.requests) - before))

    def advance_to(self, rid: str, stage: Stage, at: float = 1.0) -> list:
        req = self.seq.requests[rid]
        results = ("tool-result",) if stage is Stage.DRAFTING else ()
        return self.send(StageChanged(rid, req.version, stage, at, results))

    def finish(
        self, rid: str, at: float = 5.0, version: int | None = None, tokens: int = 100
    ) -> list:
        req = self.seq.requests[rid]
        return self.send(
            Completed(rid, version or req.version, f"answer-{rid}", tokens, at)
        )

    def abort_landed(self, rid: str, at: float = 9.0, tokens: int = 50) -> list:
        """Model the aborted worker actually stopping.

        An abort is best-effort and asynchronous: the Sequencer re-queues the
        request and only re-admits it once the old worker releases its pool
        slot. Tests must play that half of the handshake.
        """
        aborts = [c for c in self.all if isinstance(c, Abort) and c.request_id == rid]
        assert aborts, f"no abort was issued for {rid}"
        return self.send(Failed(rid, aborts[-1].version, "aborted", tokens, at))

    def of(self, kind: type) -> list:
        return [c for c in self.all if isinstance(c, kind)]

    def delivered(self, kind: DeliveryKind) -> list[Deliver]:
        return [c for c in self.all if isinstance(c, Deliver) and c.kind is kind]

    def dispatches(self, rid: str | None = None) -> list[Dispatch]:
        out = self.of(Dispatch)
        return [d for d in out if rid is None or d.request_id == rid]
