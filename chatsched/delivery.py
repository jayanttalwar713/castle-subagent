"""Rendering the chat stream.

DESIGN.md §3 argues that "a clear, ordered stream of answers" decomposes into
four reader-facing properties. This module is where three of them become
visible text: attribution (the `re:` requote), completeness (acknowledgements
and the outstanding set), and non-contradiction (replacement notices).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Deliver, DeliveryKind

#: How each delivery kind reads in the chat.
_PHRASING: dict[DeliveryKind, str] = {
    DeliveryKind.ACK: "on it",
    DeliveryKind.AMEND_ACK: "updated, still working",
    DeliveryKind.CANCEL_ACK: "dropped",
    DeliveryKind.ALREADY_ANSWERED: "already answered that one",
    DeliveryKind.DROPPED_NOTICE: "couldn't answer",
    DeliveryKind.ANSWER: "",
    DeliveryKind.ADDENDUM: "adding to my earlier answer",
    DeliveryKind.SUPERSEDE: "this replaces my earlier answer",
}


@dataclass
class Line:
    at: float
    to: tuple[str, ...]
    kind: DeliveryKind
    re: str
    body: str
    request_id: str = ""

    def render(self) -> str:
        who = ", ".join(self.to)
        prefix = _PHRASING[self.kind]
        parts = [p for p in (prefix, self.body) if p]
        text = " — ".join(parts) if len(parts) > 1 else (parts[0] if parts else "")
        # Attribution: the reader can tell which question this answers without
        # counting positions in a queue.
        if self.re:
            text = f're: "{self.re}" — {text}' if text else f're: "{self.re}"'
        return f"[{self.at:6.1f}s] → {who:<14} {self.kind.value:<17} {text}"


@dataclass
class Transcript:
    lines: list[Line] = field(default_factory=list)

    def record(self, cmd: Deliver, at: float) -> None:
        self.lines.append(
            Line(
                at=at,
                to=cmd.to,
                kind=cmd.kind,
                re=cmd.re,
                body=cmd.body,
                request_id=cmd.request_id,
            )
        )

    def render(self) -> str:
        return "\n".join(line.render() for line in self.lines)

    def answers(self) -> list[Line]:
        return [
            line
            for line in self.lines
            if line.kind
            in (DeliveryKind.ANSWER, DeliveryKind.ADDENDUM, DeliveryKind.SUPERSEDE)
        ]
