"""Ordering policies.

DESIGN.md §3 turns ordering from a structural constraint into a per-chat
policy. These are the three that got built; they are swappable and comparable
on identical scenarios (§9).

A policy answers three questions:

  concurrency   how many requests may occupy the worker pool at once
  may_deliver   may this ready answer ship right now
  attributes    does delivery carry a "re: <question>" requote
"""

from __future__ import annotations

from typing import Mapping, Protocol

from .model import Request


class Policy(Protocol):
    name: str
    attributes: bool

    def concurrency(self, pool_size: int) -> int: ...

    def may_deliver(self, req: Request, requests: Mapping[str, Request]) -> bool: ...


class Serial:
    """One request at a time. The honest baseline — the behaviour the brief
    describes as creating a backlog.

    Needs no attribution because only one request is ever outstanding, so
    there is never any ambiguity about what an answer answers.
    """

    name = "SERIAL"
    attributes = False

    def concurrency(self, pool_size: int) -> int:
        return 1

    def may_deliver(self, req: Request, requests: Mapping[str, Request]) -> bool:
        return True


class StrictFifo:
    """Parallel execution, delivery buffered back into arrival order.

    Isolates the cost of literal ordering: the work happens concurrently, but
    a finished answer waits for every earlier one. The head-of-line delay
    metric is exactly the time lost here.

    Note `attributes = False`. FIFO ordering is often *assumed* to give the
    reader attribution for free. It does not — with several requests
    outstanding, "here is the answer" in arrival order still leaves the reader
    counting positions. The misattribution-exposure metric makes that visible.
    """

    name = "STRICT_FIFO"
    attributes = False

    def concurrency(self, pool_size: int) -> int:
        return pool_size

    def may_deliver(self, req: Request, requests: Mapping[str, Request]) -> bool:
        return all(
            other.is_terminal
            for other in requests.values()
            if other.arrival_seq < req.arrival_seq
        )


class DependencyOrder:
    """Deliver as soon as ready, with attribution and acknowledgements.

    Dependent requests hold only for their parent — and that hold is applied
    at *admission*, so by the time an answer is ready its parent has already
    delivered and `may_deliver` is unconditionally true. The ordering
    guarantee lives in the admission rule, not in a delivery queue.
    """

    name = "DEPENDENCY_ORDER"
    attributes = True

    def concurrency(self, pool_size: int) -> int:
        return pool_size

    def may_deliver(self, req: Request, requests: Mapping[str, Request]) -> bool:
        return True


POLICIES: dict[str, type] = {
    Serial.name: Serial,
    StrictFifo.name: StrictFifo,
    DependencyOrder.name: DependencyOrder,
}


def get_policy(name: str) -> Policy:
    try:
        return POLICIES[name.upper()]()
    except KeyError:
        raise SystemExit(
            f"unknown policy {name!r}; choose one of {', '.join(POLICIES)}"
        ) from None
