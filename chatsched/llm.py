"""The two places a model actually runs.

  LLMTriage   claude-haiku-4-5 on ingress — classifies every inbound message
              against the live request set. Runs on everything that arrives,
              so it has to be effectively free.
  LLMWorker   claude-opus-5 in the worker pool — answers one request-version,
              with a portfolio tool, draining its mutation inbox at the
              checkpoints DESIGN.md §7a defines.

The tiered split is a cost argument, not a stylistic one.
"""

from __future__ import annotations

import json
from pathlib import Path

from anthropic import AsyncAnthropic

from .model import Classified, Relationship
from .triage import InboundMessage, apply_confidence_bias

TRIAGE_MODEL = "claude-haiku-4-5"
WORKER_MODEL = "claude-opus-5"

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# --------------------------------------------------------------------- triage


TRIAGE_SYSTEM = """\
You sit on the ingress path of a group-chat investing agent. For each new \
message, decide how it relates to the work already in flight. You are not \
answering the question — you are classifying the message against a live set \
of requests.

Relationships:
  NEW        An independent request. The default.
  AMEND      Changes an existing request's specification.
  CANCEL     Stop work on an existing request; no answer is owed.
  DUPLICATE  Someone already asked this; one answer should serve both. Only
             when it is the same question about the SAME member's portfolio —
             two people asking identical words about different portfolios are
             asking different questions.
  REFERENCE  A new request whose specification depends on a prior request's
             ANSWER, e.g. "and the second biggest?" after "what is my biggest
             position?". Set target_id to the request it depends on.
  IGNORE     Not a request at all. Thanks, acknowledgements, reactions, jokes,
             two people talking to each other rather than to you. Most traffic
             in a busy group chat is this.

For AMEND, set rewritten_question to the question as it NOW stands, resolved. You have read both the original and the change, so state the result rather than leaving them to be concatenated: "How did my book do in Q2?" plus "sorry I meant Q3" becomes "How did my book do in Q3?". Leave it empty for every other relationship.

Set names_target to whether the message ITSELF identifies what it refers to. \
"never mind the Taiwan one" names it; a bare "no", "stop" or "wait" does not \
and might not even be addressed to you. This matters more than your \
confidence: we would rather ask than destroy somebody's question.

For AMEND, also decide preserves_premise:
  true   the change widens or narrows the same question, so research already
         done is still valid ("also include South China Sea")
  false  the change replaces the premise, so prior research is worthless
         ("actually I meant inflation, not Taiwan")

You will be tempted to relate a message to work already in flight. Measured \
against a labelled set, that temptation is this classifier's dominant error: \
it reaches for AMEND and DUPLICATE when the honest answer is NEW. Relating a \
message to an existing request is a claim that needs evidence in the message \
itself — a demonstrative ("that", "it", "the Taiwan one"), a correction \
("sorry, I meant"), or wording that only makes sense as a follow-up. Topical \
similarity is not evidence: two questions about the same holding are usually \
two questions. When nothing in the message points at existing work, answer \
NEW.

Confidence matters, and the errors are not symmetric. A NEW question \
misclassified as AMEND silently destroys somebody's question; a real question \
misclassified as IGNORE is never answered and nobody finds out. An AMEND or \
IGNORE misclassified as NEW merely produces a redundant answer. When you are \
unsure, say so in `confidence` and prefer NEW. Claim high confidence on CANCEL \
only when the message plainly asks to stop, and on IGNORE only when the \
message plainly asks for nothing.
"""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "relationship": {
            "type": "string",
            "enum": ["NEW", "AMEND", "CANCEL", "DUPLICATE", "REFERENCE", "IGNORE"],
        },
        # Empty string rather than null: simpler to validate, and the caller
        # treats "" as "no target".
        "target_id": {"type": "string"},
        "preserves_premise": {"type": "boolean"},
        "rewritten_question": {"type": "string"},
        "names_target": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": [
        "relationship",
        "target_id",
        "preserves_premise",
        "rewritten_question",
        "confidence",
        "reasoning",
        "names_target",
    ],
    "additionalProperties": False,
}


class LLMTriage:
    """Relationship classifier over the live request set."""

    name = TRIAGE_MODEL

    def __init__(self, client: AsyncAnthropic | None = None) -> None:
        self.client = client or AsyncAnthropic()
        #: Every decision, so the classifier is inspectable rather than a
        #: black box. `--explain` prints this.
        self.decisions: list[dict] = []

    async def aclassify(self, msg: InboundMessage, live: list[dict]) -> Classified:
        prompt = (
            f"Live requests (id · stage · asker(s) · question):\n"
            + (
                "\n".join(
                    f"  {r['id']} · {r['stage']} · member={r.get('member')} · "
                    f"{', '.join(r['askers'])} · {r['spec']}"
                    + (f"\n      answered: {r['answer']}" if r.get("answer") else "")
                    for r in live
                )
                or "  (nothing in flight)"
            )
            + f"\n\nNew message from {msg.author} (member={msg.member}):\n  {msg.text}\n"
        )

        response = await self.client.messages.create(
            model=TRIAGE_MODEL,
            max_tokens=1024,
            system=TRIAGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}},
        )
        raw = next(b.text for b in response.content if b.type == "text")
        parsed = json.loads(raw)

        relationship = Relationship(parsed["relationship"])
        confidence = float(parsed["confidence"])
        # A fallible classifier with asymmetric consequences gets an
        # asymmetric threshold (DESIGN.md §4).
        effective, fell_back = apply_confidence_bias(relationship, confidence)

        target = parsed["target_id"] or None
        if effective is Relationship.NEW:
            target = None

        self.decisions.append(
            {
                "message": msg.text,
                "author": msg.author,
                "proposed": relationship.value,
                "used": effective.value,
                "confidence": confidence,
                "fallback": fell_back,
                "target": target,
                "reasoning": parsed["reasoning"],
            }
        )

        return Classified(
            msg_id=msg.msg_id,
            author=msg.author,
            text=msg.text,
            at=msg.at,
            relationship=effective,
            member=msg.member,
            target_id=target,
            preserves_premise=bool(parsed["preserves_premise"]),
            rewritten_spec=parsed.get("rewritten_question") or None,
            names_target=bool(parsed.get("names_target", True)),
            confidence=confidence,
            fallback_applied=fell_back,
            reasoning=parsed["reasoning"],
        )


# --------------------------------------------------------------------- worker


WORKER_SYSTEM = """\
You answer portfolio questions for one member of an investing service, in a \
group chat. Be direct and specific; lead with the answer, then the reasoning. \
Two or three sentences unless the question genuinely needs more.

Call lookup_holdings whenever the answer depends on what the member actually \
owns — which is most of the time. Account for look-through exposure: someone \
who holds QQQ also holds NVIDIA.

If the question changes while you are working, you will be told. Fold the \
change in and answer the question as it now stands.
"""

HOLDINGS_TOOL = {
    "name": "lookup_holdings",
    "description": (
        "Look up this member's current portfolio: cash, positions, prices and "
        "sectors. Call this whenever the answer depends on what they actually "
        "own, including questions about concentration, sizing, or exposure."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why the holdings are needed for this question.",
            }
        },
        "required": ["reason"],
    },
}


#: Stands in for the tool call whose result we carried over from a previous
#: version of the same request.
_RETAINED_TOOL_USE_ID = "toolu_retained"


def load_portfolio() -> dict:
    return json.loads((FIXTURES / "portfolio.json").read_text())


class LLMWorker:
    """Answers one request-version with claude-opus-5 and a portfolio tool."""

    name = WORKER_MODEL

    def __init__(self, client: AsyncAnthropic | None = None) -> None:
        self.client = client or AsyncAnthropic()
        self.portfolio = load_portfolio()

    async def answer(
        self,
        spec: str,
        checkpoint,
        retained: tuple[str, ...] = (),
        as_of: str | None = None,
    ) -> tuple[str, int, list[str]]:
        """Run the agent loop.

        `checkpoint(phase, results=())` is an async callable invoked at each
        boundary — before a model call, when a tool call starts, and after its
        result returns. It reports progress and drains the mutation inbox,
        returning any change to fold in. This is what makes changes apply at
        checkpoints rather than preemptively (§7a): nothing is torn apart
        mid-API-call.
        """
        tokens = 0
        tool_results: list[str] = list(retained)

        if retained:
            # The Sequencer kept the tool results from the previous version of
            # this question because the change preserved its premise (§7c).
            # Seed them as an already-completed tool call so the model drafts
            # against them instead of paying for the lookup twice. Without
            # this the retention is bookkeeping that saves nothing.
            messages: list[dict] = [
                {"role": "user", "content": spec},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": _RETAINED_TOOL_USE_ID,
                            "name": HOLDINGS_TOOL["name"],
                            "input": {"reason": "carried over from the earlier "
                                                "version of this question"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": _RETAINED_TOOL_USE_ID,
                            "content": retained[0],
                        }
                    ],
                },
            ]
        else:
            messages = [{"role": "user", "content": spec}]

        for _ in range(4):  # bounded: plan -> tool -> draft, with slack
            if (change := await checkpoint("before_model")) is not None:
                messages.append(
                    {"role": "user", "content": f"The question has changed: {change}"}
                )

            response = await self.client.messages.create(
                model=WORKER_MODEL,
                max_tokens=2048,
                system=WORKER_SYSTEM,
                tools=[HOLDINGS_TOOL],
                messages=messages,
            )
            tokens += response.usage.input_tokens + response.usage.output_tokens

            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")
                return text.strip(), tokens, tool_results

            await checkpoint("tool_call")
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                payload = json.dumps(
                    {**self.portfolio, "data_version": as_of}
                    if as_of
                    else self.portfolio
                )
                tool_results.append(payload)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": payload,
                    }
                )
            messages.append({"role": "user", "content": results})

            if (change := await checkpoint("after_tool", tuple(tool_results))) is not None:
                messages.append(
                    {"role": "user", "content": f"The question has changed: {change}"}
                )

        return "(could not finish within the step budget)", tokens, tool_results
