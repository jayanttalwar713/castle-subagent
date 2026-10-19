"""Core domain types.

Nothing in this module does I/O, reads a clock, or calls a model. Time always
arrives on an event's `at` field. That is what lets the Sequencer be a pure
function and the test suite be deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Relationship(str, Enum):
    """How an inbound message relates to the work already in flight.

    This is a relationship over the live request set, not an intent over a
    message — see DESIGN.md §4. MERGE and SPLIT are designed but not built;
    see DESIGN.md §12 for why.
    """

    NEW = "NEW"
    AMEND = "AMEND"
    CANCEL = "CANCEL"
    DUPLICATE = "DUPLICATE"
    REFERENCE = "REFERENCE"
    #: Not a request at all — "thanks!", "haha", a side conversation between
    #: two people. Most traffic in a busy group chat is this, and without the
    #: class every one of them dispatches an expensive worker.
    IGNORE = "IGNORE"


#: Relationships that can destroy work if the classifier is wrong. These
#: require higher confidence than the additive ones (DESIGN.md §4).
#:
#: IGNORE belongs here for the same reason CANCEL does: a real question
#: misclassified as chit-chat is silently never answered, and nobody finds
#: out. The reverse — answering a "thanks!" — is merely embarrassing.
DESTRUCTIVE = frozenset({Relationship.CANCEL, Relationship.IGNORE})


class Stage(str, Enum):
    """Where a request is in its lifecycle (DESIGN.md §6)."""

    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    TOOL_CALL = "TOOL_CALL"
    DRAFTING = "DRAFTING"
    DELIVERED = "DELIVERED"
    DROPPED = "DROPPED"


TERMINAL = frozenset({Stage.DELIVERED, Stage.DROPPED})
RUNNING = frozenset({Stage.PLANNING, Stage.TOOL_CALL, Stage.DRAFTING})


@dataclass
class Request:
    """One unit of work. Several chat messages may map onto a single Request."""

    id: str
    spec: str
    askers: list[str]
    #: Whose portfolio this question is about. Two people asking the same
    #: words about *different* portfolios are asking different questions —
    #: see DESIGN.md §11. None means the chat has one shared context.
    member: str | None
    arrival_seq: int
    created_at: float

    #: The question as it was originally typed. `spec` accumulates amendments
    #: and materialised parent answers; attribution should quote back what the
    #: person actually asked, not the machine-readable spec.
    asked_text: str = ""

    version: int = 1
    stage: Stage = Stage.QUEUED

    #: Set for REFERENCE requests. Gates admission, and the spec is
    #: materialised from this parent's answer at admission time (§8). Cleared
    #: once satisfied, so the invariant checker uses `origin_parent` instead.
    depends_on: str | None = None
    origin_parent: str | None = None

    #: Tool results survive a premise-preserving amendment and are thrown away
    #: by one that replaces the premise (§7c).
    tool_results: list[str] = field(default_factory=list)

    answer: str | None = None
    #: The version the pending answer was actually computed at. With version
    #: gating on this always equals `version`; with the gate off it may lag,
    #: which is exactly what the staleness metric detects (§9).
    answer_version: int | None = None
    delivered_version: int | None = None

    #: Set when a delivered request is amended, so the re-run ships as a
    #: visible addendum or replacement rather than a bare answer (§7).
    redeliver_as: "DeliveryKind | None" = None

    #: Which version of the underlying data this question is answered
    #: against. Pinned once, at first admission, and never changed — so every
    #: version of the request sees the same holdings and prices. Without it an
    #: addendum can be computed against different data than the answer it is
    #: adding to, and two concurrent questions can report figures that
    #: disagree (DESIGN.md §8).
    as_of: str | None = None
    #: The data version the pending answer was actually computed against.
    answered_as_of: str | None = None

    admitted_at: float | None = None
    ready_at: float | None = None
    delivered_at: float | None = None
    #: When this request *first* answered. A request amended after delivery is
    #: re-run and delivers again later, so `delivered_at` moves — but a child
    #: that referenced it consumed the first answer, and that is the timestamp
    #: causal consistency is about.
    first_delivered_at: float | None = None

    #: Tokens charged per request-version. Versions that never deliver are
    #: the "wasted tokens" metric — the price of mutability (§9).
    tokens_by_version: dict[int, int] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.stage in TERMINAL

    @property
    def is_running(self) -> bool:
        return self.stage in RUNNING

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens_by_version.values())

    @property
    def wasted_tokens(self) -> int:
        """Tokens spent on versions that were never delivered."""
        if self.delivered_version is None:
            return self.total_tokens
        return self.total_tokens - self.tokens_by_version.get(self.delivered_version, 0)

    def requote(self, width: int = 44) -> str:
        """Short quote of the question, used for attribution on delivery (§3)."""
        text = " ".join((self.asked_text or self.spec).split())
        return text if len(text) <= width else text[: width - 1] + "…"


# --------------------------------------------------------------------------
# Events in (things that happen to the Sequencer)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Classified:
    """A chat message that triage has already related to the live request set.

    Triage runs *outside* the Sequencer and hands it the decision. That keeps
    the only shared-state component free of inference (DESIGN.md §5).
    """

    msg_id: str
    author: str
    text: str
    at: float
    relationship: Relationship
    member: str | None = None
    target_id: str | None = None
    #: For AMEND: the question as it now stands, resolved by triage. Triage
    #: has already read both the original and the change, so it can state the
    #: result — far better than concatenating them and hoping the worker
    #: untangles "How did my book do in Q2? sorry I meant Q3 not Q2".
    rewritten_spec: str | None = None
    #: Does the message itself identify what it refers to ("never mind the
    #: Taiwan one") or does it rely entirely on context ("no", "stop")?
    #: Measured to be a far better guard than confidence for destructive
    #: relationships — see DESIGN.md §4.
    names_target: bool = True
    preserves_premise: bool = True
    confidence: float = 1.0
    #: True when triage wanted something else but fell below the confidence
    #: threshold and fell back to NEW. Logged so the decision is inspectable.
    fallback_applied: bool = False
    reasoning: str = ""


@dataclass(frozen=True)
class StageChanged:
    request_id: str
    version: int
    stage: Stage
    at: float
    #: Carried when leaving TOOL_CALL. The Sequencer retains these so a later
    #: premise-preserving amendment can re-draft without paying again (§7c).
    tool_results: tuple[str, ...] = ()


@dataclass(frozen=True)
class Completed:
    request_id: str
    version: int
    answer: str
    tokens: int
    at: float
    #: Which data version the worker actually used. Reported back so the
    #: invariant checker can catch a worker that ignored the pin.
    as_of: str | None = None


@dataclass(frozen=True)
class Failed:
    request_id: str
    version: int
    error: str
    tokens: int
    at: float


Event = Classified | StageChanged | Completed | Failed


# --------------------------------------------------------------------------
# Commands out (things the Sequencer asks the world to do)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Dispatch:
    """Start a worker for exactly this (request_id, version)."""

    request_id: str
    version: int
    spec: str
    retained_tool_results: tuple[str, ...] = ()
    as_of: str | None = None


@dataclass(frozen=True)
class Abort:
    """Best-effort cancellation. Correctness never depends on this landing —
    version gating already makes the stale result unusable (DESIGN.md §7b)."""

    request_id: str
    version: int
    reason: str


@dataclass(frozen=True)
class Inject:
    """Hand a change to a running worker, to be applied at its next
    checkpoint rather than preemptively (DESIGN.md §7a)."""

    request_id: str
    version: int
    change: str


class DeliveryKind(str, Enum):
    ACK = "ACK"
    ANSWER = "ANSWER"
    ADDENDUM = "ADDENDUM"
    SUPERSEDE = "SUPERSEDE"
    CANCEL_ACK = "CANCEL_ACK"
    #: Ask before destroying work the message did not clearly point at.
    CONFIRM_CANCEL = "CONFIRM_CANCEL"
    ALREADY_ANSWERED = "ALREADY_ANSWERED"
    DROPPED_NOTICE = "DROPPED_NOTICE"
    AMEND_ACK = "AMEND_ACK"


@dataclass(frozen=True)
class Deliver:
    """Something the chat should see. Delivery formats it; the Sequencer only
    decides that it may ship, and to whom."""

    kind: DeliveryKind
    request_id: str
    version: int
    to: tuple[str, ...]
    re: str
    body: str = ""


Command = Dispatch | Abort | Inject | Deliver
