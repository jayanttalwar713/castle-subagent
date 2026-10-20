"""The chat history triage is shown, and the bound on it (DESIGN.md §4, §8).

The classifier is asked to tell a message aimed at the agent from one aimed
at another member. For a long time it was given the live *request* set and
never the recent *messages* — which is the one piece of evidence that
distinguishes them. A bare "no" is almost always answering whoever spoke
last, and there is no way to know that from a list of requests.

Measured, supplying it took IGNORE from 0.97/0.95 precision/recall to
1.00/1.00 and narrowed the run-to-run spread from 92–97% to 95–97%.
"""

from __future__ import annotations

import json

import pytest

from chatsched.evaluate import FIXTURES, load_cases
from chatsched.llm import LLMTriage
from chatsched.triage import InboundMessage


class CapturingClient:
    """Stands in for the API and keeps whatever prompt it was handed."""

    def __init__(self) -> None:
        self.prompt: str | None = None
        outer = self

        class _Messages:
            @staticmethod
            async def create(**kw):
                outer.prompt = kw["messages"][0]["content"]

                class R:
                    content = [
                        type("B", (), {
                            "type": "text",
                            "text": json.dumps({
                                "relationship": "IGNORE", "target_id": "",
                                "preserves_premise": True, "rewritten_question": "",
                                "names_target": False, "confidence": 0.9,
                                "reasoning": "stub",
                            }),
                        })()
                    ]

                return R()

        self.messages = _Messages()


LIVE = [{"id": "r1", "spec": "Taiwan exposure", "askers": ["ana"],
         "member": "ana", "stage": "TOOL_CALL", "answer": None}]


def _msg(text: str, author: str = "ana", at: float = 10.0) -> InboundMessage:
    return InboundMessage(msg_id="m", author=author, text=text, at=at, member="ana")


async def _classify(recent):
    client = CapturingClient()
    await LLMTriage(client=client).aclassify(_msg("no"), LIVE, recent)
    return client.prompt


@pytest.mark.asyncio
async def test_the_recent_chat_reaches_the_prompt():
    prompt = await _classify([_msg("ana are you joining the 3pm call?", "ben", 8.0)])
    assert "Recent chat" in prompt
    assert "ben: ana are you joining the 3pm call?" in prompt
    assert "ana: no" in prompt, "the message being classified must close the chat"


@pytest.mark.asyncio
async def test_no_chat_means_no_chat_block():
    """An empty window must not leave a dangling header — the first message
    in a chat has nothing before it, and that is the common case."""
    prompt = await _classify([])
    assert "Recent chat" not in prompt
    assert prompt.startswith("Live requests")


@pytest.mark.asyncio
async def test_the_window_is_bounded():
    """§8 claims the classifier's input is bounded by construction. A chat
    that runs all day must not grow its own triage cost without limit."""
    many = [_msg(f"line {i}", "ben", float(i)) for i in range(40)]
    prompt = await _classify(many)

    # Exact-match on the whole line: "line 3" is a substring of "line 34".
    lines = set(prompt.splitlines())
    kept = [i for i in range(40) if f"  ben: line {i}" in lines]
    assert len(kept) == LLMTriage.RECENT_WINDOW
    assert kept == list(range(40 - LLMTriage.RECENT_WINDOW, 40)), "keep the newest"


@pytest.mark.asyncio
async def test_the_window_keeps_chat_order():
    prompt = await _classify([_msg("first", "ben", 1.0), _msg("second", "cara", 2.0)])
    assert prompt.index("first") < prompt.index("second") < prompt.index("ana: no")


@pytest.mark.asyncio
async def test_the_prompt_still_carries_the_live_requests():
    """Chat history is additional evidence, not a replacement for the request
    set — the relationship is still to the work in flight."""
    prompt = await _classify([_msg("hello", "ben", 1.0)])
    assert "Live requests" in prompt and "Taiwan exposure" in prompt


# ------------------------------------------------------------- the fixture --


def test_every_eval_context_has_chat():
    raw = json.loads((FIXTURES / "triage_eval.json").read_text())
    assert set(raw["chat"]) == set(raw["contexts"]), (
        "a context without chat silently reverts to the old behaviour for "
        "every case that uses it"
    )


def test_cases_carry_their_contexts_chat():
    cases = load_cases()
    by_ctx = {}
    for c in cases:
        by_ctx.setdefault(tuple(c.chat), set()).add(c.text)
    assert any(chat for chat in by_ctx), "no case got any chat history"


def test_chat_is_shared_per_context_not_written_per_case():
    """The guard against overfitting: chat lives on the context, so it cannot
    be tuned to make one label come out right without moving every other case
    that shares it."""
    raw = json.loads((FIXTURES / "triage_eval.json").read_text())
    cases = load_cases()
    for name, chat in raw["chat"].items():
        sharing = [c for c, raw_c in zip(cases, raw["cases"])
                   if raw_c["context"] == name]
        assert all(c.chat == chat for c in sharing), name
        if len(sharing) > 1 and chat:
            assert len({tuple(c.chat) for c in sharing}) == 1


# ------------------------------------- a bad response must not stop the chat --


class BrokenClient:
    """Returns a response that cannot be parsed — the real failure mode is a
    verbose `reasoning` field running past max_tokens and truncating the JSON
    mid-string, which is exactly how this bug was found."""

    def __init__(self, body: str, stop_reason: str = "max_tokens") -> None:
        outer = self

        class _Messages:
            @staticmethod
            async def create(**kw):
                class R:
                    content = [type("B", (), {"type": "text", "text": body})()]
                    stop_reason = outer.stop
                return R()

        self.stop = stop_reason
        self.messages = _Messages()


@pytest.mark.parametrize("body", [
    '{"relationship": "AMEND", "target_id": "r1", "reasoning": "the member is cle',
    "not json at all",
    '{"relationship": "NONSENSE", "target_id": "", "confidence": 0.9}',
    "{}",
])
@pytest.mark.asyncio
async def test_an_unusable_response_degrades_to_new(body):
    """One bad response must not raise. Raising ends the ingest loop, which
    silently stops the entire chat being answered — far worse than a
    redundant answer."""
    from chatsched.model import Relationship

    triage = LLMTriage(client=BrokenClient(body))
    decision = await triage.aclassify(_msg("what's my Taiwan exposure?"), LIVE, [])

    assert decision.relationship is Relationship.NEW, "NEW is the recoverable reading"
    assert decision.fallback_applied
    assert decision.target_id is None
    assert decision.confidence == 0.0


@pytest.mark.asyncio
async def test_the_fallback_is_recorded_not_hidden():
    """`--explain` has to show it, or a silently-degraded classifier looks
    like a confident one."""
    triage = LLMTriage(client=BrokenClient("truncated{"))
    await triage.aclassify(_msg("hello"), LIVE, [])

    entry = triage.decisions[-1]
    assert entry["fallback"] is True
    assert "unusable" in entry["reasoning"]


def test_reasoning_is_length_capped_in_the_schema():
    """The root cause: `reasoning` was the one unbounded field, so a verbose
    answer could run the response past max_tokens and truncate the JSON."""
    from chatsched.llm import TRIAGE_SCHEMA

    assert TRIAGE_SCHEMA["properties"]["reasoning"]["maxLength"] <= 500
