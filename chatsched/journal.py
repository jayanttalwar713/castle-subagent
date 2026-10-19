"""Durable event log, and recovery by replaying it (DESIGN.md §5, §14).

The Sequencer is a pure fold over events:

    state = fold(handle, events)

Crash recovery is therefore not a feature that has to be designed — it is a
consequence of that shape. Persist the events, replay them into a fresh
Sequencer, and you are exactly where you were. No checkpoints, no snapshot
format, no reconciliation logic, and nothing to keep in sync with the state
it describes.

**This module lives outside the Sequencer on purpose.** Writing a log is I/O,
and I/O inside the Sequencer would destroy the very property that makes
recovery cheap. The driver owns the log; the Sequencer never learns it exists.

One thing replay does *not* do is re-execute commands. Folding the events
regenerates every command the Sequencer originally emitted — including the
deliveries that already reached the chat before the crash. Replaying those
would post every answer a second time, so `replay()` discards them and
returns state only. Suppressing a *partially* executed command — a worker
that was mid-flight when the process died — is a harder problem and §14 says
so rather than pretending otherwise.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import IO, Iterable, Iterator

from .model import Classified, Completed, Event, Failed, Relationship, StageChanged, Stage
from .policies import Policy, get_policy
from .sequencer import Sequencer

#: Wire tag -> event class. The tag is written to disk, so these strings are
#: a format: renaming one invalidates every log already on disk.
_KINDS: dict[str, type] = {
    "classified": Classified,
    "stage": StageChanged,
    "completed": Completed,
    "failed": Failed,
}
_TAGS: dict[type, str] = {v: k for k, v in _KINDS.items()}


def encode(ev: Event) -> dict:
    """One event as a JSON-safe dict."""
    try:
        tag = _TAGS[type(ev)]
    except KeyError:  # pragma: no cover - guards a new event type
        raise TypeError(f"not a loggable event: {type(ev).__name__}") from None
    body = asdict(ev)
    for key, value in body.items():
        if isinstance(value, (Relationship, Stage)):
            body[key] = value.value
        elif isinstance(value, tuple):
            body[key] = list(value)
    return {"kind": tag, **body}


def decode(record: dict) -> Event:
    """Inverse of `encode`. Raises on anything it does not recognise, rather
    than silently dropping an event and reconstructing the wrong state."""
    body = dict(record)
    try:
        cls = _KINDS[body.pop("kind")]
    except KeyError:
        raise ValueError(f"unknown event kind in log: {record.get('kind')!r}") from None
    if cls is Classified:
        body["relationship"] = Relationship(body["relationship"])
    elif cls is StageChanged:
        body["stage"] = Stage(body["stage"])
        body["tool_results"] = tuple(body.get("tool_results", ()))
    return cls(**body)


class EventLog:
    """Append-only event log on disk, one JSON object per line.

    Append-only because the Sequencer never revises history: an amendment is
    a new event, not an edit to the one it changes. That is what lets the log
    be opened in append mode and never rewritten.
    """

    def __init__(self, path: str | Path, *, append: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: IO[str] | None = self.path.open("a" if append else "w")

    def append(self, ev: Event) -> None:
        """Record one event. Flushed immediately: an event buffered in
        userspace when the process dies is an event that did not happen, and
        the whole point of the log is to survive that moment."""
        assert self._fh is not None, "log is closed"
        self._fh.write(json.dumps(encode(ev)) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read(path: str | Path) -> Iterator[Event]:
    """Stream the events back.

    A truncated final line is tolerated and dropped: a crash mid-write is the
    expected way for this file to end, and refusing to recover because the
    last event is half-written would defeat the purpose.
    """
    with Path(path).open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                return  # torn write at the tail; everything before it stands
            yield decode(record)


def replay(
    events: Iterable[Event],
    *,
    pool_size: int = 3,
    policy: Policy | str | None = None,
    version_gate: bool = True,
    snapshot_window: float = 60.0,
) -> Sequencer:
    """Rebuild a Sequencer by folding the events back through `handle`.

    The configuration has to match the run that produced the log — pool size
    and policy change the decisions the fold makes, so recovering under a
    different policy reconstructs a state the system was never in. The caller
    is responsible for passing the same settings; nothing here can check it,
    because the log records what the chat did, not how the process was
    configured.
    """
    if isinstance(policy, str):
        policy = get_policy(policy)
    seq = Sequencer(
        pool_size=pool_size,
        policy=policy or get_policy("DEPENDENCY_ORDER"),
        version_gate=version_gate,
        snapshot_window=snapshot_window,
    )
    for ev in events:
        seq.handle(ev)  # commands discarded — see the module docstring
    return seq


def state_digest(seq: Sequencer) -> dict:
    """A comparable summary of everything a recovered Sequencer must match.

    Used by the tests to assert that replay lands on the *same* state rather
    than merely a plausible one. Deliberately explicit: comparing the objects
    directly would also pass if both sides were empty.
    """
    return {
        "requests": {
            rid: {
                "spec": r.spec,
                "askers": sorted(r.askers),
                "member": r.member,
                "stage": r.stage.value,
                "version": r.version,
                "answer": r.answer,
                "as_of": r.as_of,
                "delivered_version": r.delivered_version,
                "delivered_at": r.delivered_at,
                "first_delivered_at": r.first_delivered_at,
                "origin_parent": r.origin_parent,
                "depends_on": r.depends_on,
                "tool_results": list(r.tool_results),
                "tokens_by_version": dict(r.tokens_by_version),
            }
            for rid, r in sorted(seq.requests.items())
        },
        "deliveries": [
            (d.at, d.kind.value, d.request_id, d.version, d.stale)
            for d in seq.journal
        ],
        "max_in_flight": seq.max_in_flight,
    }
