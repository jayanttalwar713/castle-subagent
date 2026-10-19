"""Triage: relate an inbound message to the live request set.

Triage runs *outside* the Sequencer and hands it a decision, which is what
keeps the only shared-state component free of inference (DESIGN.md §5).

Two implementations:

  ScriptedTriage  deterministic, reads labels from a scenario fixture. Used by
                  the test suite and by scenario replay so correctness tests
                  never depend on a model call.
  LLMTriage       claude-haiku-4-5 over the live request set. Used in live mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .model import DESTRUCTIVE, Classified, Relationship


@dataclass(frozen=True)
class InboundMessage:
    msg_id: str
    author: str
    text: str
    at: float
    #: Whose portfolio this message is about. None = the chat's shared one.
    member: str | None = None
    #: Fixture-only: names the request this message creates, so later fixture
    #: lines can refer to it. Ignored by the LLM classifier.
    label: str | None = None
    target_label: str | None = None
    relationship: str | None = None
    preserves_premise: bool = True
    rewritten_spec: str | None = None
    names_target: bool = True


class Triage(Protocol):
    def classify(
        self, msg: InboundMessage, live: list[dict], labels: dict[str, str]
    ) -> Classified: ...


#: The async form `LLMTriage` implements, kept separate because the scripted
#: triage is synchronous and needs no chat history — it reads its answers from
#: a fixture. Only a model needs to be told what was said before.
class AsyncTriage(Protocol):
    async def aclassify(
        self,
        msg: InboundMessage,
        live: list[dict],
        recent: "list[InboundMessage]" = ...,
    ) -> Classified: ...


#: Below this, a destructive relationship degrades to NEW. A NEW misclassified
#: as AMEND destroys a request silently; the reverse merely produces a
#: redundant answer. Only one of those is recoverable (DESIGN.md §4).
DESTRUCTIVE_THRESHOLD = 0.75
ADDITIVE_THRESHOLD = 0.50


def apply_confidence_bias(
    relationship: Relationship, confidence: float
) -> tuple[Relationship, bool]:
    """Fall back to NEW when the classifier is not confident enough.

    Returns the relationship to actually use and whether a fallback happened.
    The fallback is reported, not hidden — see `chatsched.cli --explain`.
    """
    if relationship is Relationship.NEW:
        return relationship, False
    threshold = (
        DESTRUCTIVE_THRESHOLD if relationship in DESTRUCTIVE else ADDITIVE_THRESHOLD
    )
    if confidence < threshold:
        return Relationship.NEW, True
    return relationship, False


class ScriptedTriage:
    """Reads the relationship straight off the fixture.

    This exists so that the invariant tests and the policy comparison measure
    the *scheduler*, not the classifier. Mixing the two would mean a flaky
    classifier could mask a real scheduling bug.
    """

    name = "scripted"

    def classify(
        self, msg: InboundMessage, live: list[dict], labels: dict[str, str]
    ) -> Classified:
        rel = Relationship(msg.relationship.upper()) if msg.relationship else Relationship.NEW
        target = labels.get(msg.target_label) if msg.target_label else None
        return Classified(
            msg_id=msg.msg_id,
            author=msg.author,
            text=msg.text,
            at=msg.at,
            relationship=rel,
            member=msg.member,
            target_id=target,
            preserves_premise=msg.preserves_premise,
            rewritten_spec=msg.rewritten_spec,
            names_target=msg.names_target,
            confidence=1.0,
            reasoning="scripted fixture",
        )
