"""Dependency edges between requests.

Only REFERENCE produces edges ("and the second biggest?" depends on the answer
to "is NVDA my biggest position?"). This is the *only* place the system
serializes — see DESIGN.md §3: serialize on dependency, not on arrival.
"""

from __future__ import annotations


class DependencyGraph:
    """A tiny DAG keyed by request id.

    Deliberately not a general graph library: the only operations this system
    needs are "add an edge unless it would create a cycle", "who is waiting on
    this?", and "release everyone waiting on a request that died".
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._children: dict[str, set[str]] = {}

    def parent_of(self, child: str) -> str | None:
        return self._parent.get(child)

    def children_of(self, parent: str) -> set[str]:
        return set(self._children.get(parent, ()))

    def would_cycle(self, child: str, parent: str) -> bool:
        """True if making `child` depend on `parent` closes a loop.

        A cycle can only come from classifier error — a correct REFERENCE
        always points backwards in time. We detect it rather than trust it.
        """
        if child == parent:
            return True
        seen: set[str] = set()
        node: str | None = parent
        while node is not None and node not in seen:
            if node == child:
                return True
            seen.add(node)
            node = self._parent.get(node)
        return False

    def add_edge(self, child: str, parent: str) -> bool:
        """Record that `child` waits on `parent`.

        Returns False and records nothing if the edge would create a cycle;
        the caller then treats the request as independent. The result is a
        correct answer with weaker ordering — the right direction to fail
        (DESIGN.md §8).
        """
        if self.would_cycle(child, parent):
            return False
        # A child has at most one parent; a re-pointed edge replaces the old.
        if (old := self._parent.get(child)) is not None:
            self._children.get(old, set()).discard(child)
        self._parent[child] = parent
        self._children.setdefault(parent, set()).add(child)
        return True

    def release(self, parent: str) -> set[str]:
        """Detach and return everyone directly waiting on `parent`.

        Called when a parent reaches a terminal state. If it delivered, the
        children are now admissible. If it died without delivering, they are
        orphans and the Sequencer decides what to tell the asker — the one
        thing it must not do is leave them waiting forever (DESIGN.md §8).
        """
        children = self._children.pop(parent, set())
        for child in children:
            self._parent.pop(child, None)
        return children

    def forget(self, request_id: str) -> None:
        """Drop a request from the graph entirely."""
        if (parent := self._parent.pop(request_id, None)) is not None:
            self._children.get(parent, set()).discard(request_id)
        self._children.pop(request_id, None)
