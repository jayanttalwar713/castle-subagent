"""Triage: the confidence asymmetry, and parsing a real classifier response.

DESIGN.md §4 argues that a fallible classifier must be made to fail in the
*recoverable* direction. That argument is implemented by `apply_confidence_bias`
and is as load-bearing as version gating — so it gets the same treatment:
tested directly, and demonstrated end-to-end rather than asserted.

No network calls. The model client is stubbed, so these tests check our
handling of a classifier response, not the classifier itself.
"""

from __future__ import annotations

import json

import pytest

from chatsched.llm import TRIAGE_SCHEMA, LLMTriage
from chatsched.model import DeliveryKind, Relationship, Stage
from chatsched.triage import (
    ADDITIVE_THRESHOLD,
    DESTRUCTIVE_THRESHOLD,
    InboundMessage,
    apply_confidence_bias,
)
from conftest import Harness, msg


# ------------------------------------------------------ the asymmetry rule --


def test_new_is_never_second_guessed():
    """NEW is the safe answer, so it is never downgraded, at any confidence."""
    for confidence in (0.0, 0.3, 1.0):
        assert apply_confidence_bias(Relationship.NEW, confidence) == (
            Relationship.NEW,
            False,
        )


@pytest.mark.parametrize(
    "relationship",
    [Relationship.AMEND, Relationship.DUPLICATE, Relationship.REFERENCE],
)
def test_additive_relationships_survive_moderate_confidence(relationship):
    used, fell_back = apply_confidence_bias(relationship, ADDITIVE_THRESHOLD + 0.01)
    assert used is relationship and not fell_back


def test_cancel_needs_more_confidence_than_an_amendment():
    """The whole point of §4: destroying work requires a higher bar than
    adding to it."""
    confidence = (ADDITIVE_THRESHOLD + DESTRUCTIVE_THRESHOLD) / 2

    amend, amend_fell_back = apply_confidence_bias(Relationship.AMEND, confidence)
    cancel, cancel_fell_back = apply_confidence_bias(Relationship.CANCEL, confidence)

    assert amend is Relationship.AMEND and not amend_fell_back
    assert cancel is Relationship.NEW and cancel_fell_back
    assert DESTRUCTIVE_THRESHOLD > ADDITIVE_THRESHOLD


@pytest.mark.parametrize(
    "relationship",
    [Relationship.AMEND, Relationship.CANCEL, Relationship.DUPLICATE, Relationship.REFERENCE],
)
def test_low_confidence_always_falls_back_to_new(relationship):
    used, fell_back = apply_confidence_bias(relationship, 0.1)
    assert used is Relationship.NEW
    assert fell_back, "a fallback must be reported, not silent"


# --------------------------------------- the asymmetry, demonstrated ---------


def test_a_wrong_fallback_is_noisy_but_a_wrong_amend_is_destructive():
    """The claim of §4, shown rather than asserted.

    Both branches model the *same* classifier error — an amendment it is not
    sure about. Taking it produces one answer to a question nobody can see any
    more; falling back to NEW produces two answers, one of them redundant.
    Only the second failure leaves every question answered.
    """
    confident = Harness()
    original = confident.ask("what is my exposure to taiwan?", author="ana")
    confident.send(
        msg("make that inflation", at=1, rel=Relationship.AMEND,
            target=original, preserves=False)
    )
    confident.abort_landed(original)
    confident.finish(original, at=5)

    # One request, one answer — and "what is my exposure to taiwan?" is gone.
    assert len(confident.seq.requests) == 1
    assert len(confident.delivered(DeliveryKind.ANSWER)) == 1
    assert "taiwan" not in confident.seq.requests[original].spec.lower()

    unsure = Harness()
    first = unsure.ask("what is my exposure to taiwan?", author="ana")
    # Below threshold, so triage would have degraded this to NEW.
    degraded, fell_back = apply_confidence_bias(Relationship.AMEND, 0.2)
    assert degraded is Relationship.NEW and fell_back
    second = unsure.ask("make that inflation", author="ana", at=1)
    unsure.finish(first, at=5)
    unsure.finish(second, at=6)

    # Two requests, two answers. Noisy — and nothing was destroyed.
    assert len(unsure.seq.requests) == 2
    assert len(unsure.delivered(DeliveryKind.ANSWER)) == 2
    assert unsure.seq.requests[first].stage is Stage.DELIVERED
    assert "taiwan" in unsure.seq.requests[first].spec.lower()


# ------------------------------------------------- parsing a response --------


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, payload: dict) -> None:
        self.content = [_Block(json.dumps(payload))]


class _Messages:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Response(self.payload)


class FakeClient:
    """Stands in for AsyncAnthropic. Returns a canned classifier response."""

    def __init__(self, payload: dict) -> None:
        self.messages = _Messages(payload)


def _payload(**overrides) -> dict:
    base = {
        "relationship": "AMEND",
        "target_id": "r1",
        "preserves_premise": True,
        "confidence": 0.9,
        "reasoning": "widens the same question",
    }
    base.update(overrides)
    return base


LIVE = [{"id": "r1", "spec": "taiwan exposure", "askers": ["ana"],
         "stage": "TOOL_CALL", "answer": None}]


async def _classify(payload: dict, text: str = "also south china sea"):
    triage = LLMTriage(client=FakeClient(payload))
    message = InboundMessage(msg_id="m1", author="ana", text=text, at=1.0)
    return triage, await triage.aclassify(message, LIVE)


@pytest.mark.asyncio
async def test_a_confident_response_is_passed_through():
    triage, decision = await _classify(_payload())
    assert decision.relationship is Relationship.AMEND
    assert decision.target_id == "r1"
    assert decision.preserves_premise is True
    assert not decision.fallback_applied


@pytest.mark.asyncio
async def test_an_unconfident_cancel_is_degraded_and_its_target_dropped():
    """A fallback to NEW must also clear the target — otherwise the Sequencer
    would create a new request pointed at somebody else's question."""
    _triage, decision = await _classify(_payload(relationship="CANCEL", confidence=0.4))
    assert decision.relationship is Relationship.NEW
    assert decision.fallback_applied
    assert decision.target_id is None


@pytest.mark.asyncio
async def test_an_empty_target_becomes_none_not_an_empty_string():
    """The schema uses "" for absent rather than null; "" must not reach the
    Sequencer, which would look up a request whose id is the empty string."""
    _triage, decision = await _classify(_payload(relationship="NEW", target_id=""))
    assert decision.target_id is None


@pytest.mark.asyncio
async def test_every_decision_is_recorded_for_auditing():
    """The classifier is the highest-risk component (§13), so `--explain` must
    have something to print for every message — including the fallback flag."""
    triage, _decision = await _classify(_payload(relationship="CANCEL", confidence=0.4))
    assert len(triage.decisions) == 1
    record = triage.decisions[0]
    assert record["proposed"] == "CANCEL"
    assert record["used"] == "NEW"
    assert record["fallback"] is True
    assert record["reasoning"]


@pytest.mark.asyncio
async def test_the_live_request_set_is_sent_to_the_classifier():
    """Triage classifies against what is in flight — if the live set never
    reached the prompt, every message would look like a NEW question."""
    triage, _decision = await _classify(_payload())
    prompt = triage.client.messages.calls[0]["messages"][0]["content"]
    assert "r1" in prompt and "taiwan exposure" in prompt
    assert "also south china sea" in prompt


def test_the_schema_is_strict_enough_to_parse_blind():
    """Structured output is what lets us json.loads the response without
    defensive parsing, so the schema has to actually constrain it."""
    assert TRIAGE_SCHEMA["additionalProperties"] is False
    assert set(TRIAGE_SCHEMA["required"]) == set(TRIAGE_SCHEMA["properties"])
    enum = TRIAGE_SCHEMA["properties"]["relationship"]["enum"]
    assert set(enum) == {r.value for r in Relationship}
