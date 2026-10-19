"""The Opus worker's agent loop, with the model client stubbed.

What matters here is not the quality of the answer — that needs a real model
and a human reading it — but the mechanics around it: that the portfolio tool
is actually called and its result fed back, that the checkpoint protocol is
walked in the right order so changes can be drained, and that token spend is
accumulated across every turn rather than only the last one.
"""

from __future__ import annotations

import json

import pytest

from chatsched.llm import HOLDINGS_TOOL, LLMWorker, load_portfolio

pytestmark = pytest.mark.asyncio


class _Usage:
    def __init__(self, tokens: int) -> None:
        self.input_tokens = tokens
        self.output_tokens = tokens


class _Text:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ToolUse:
    type = "tool_use"

    def __init__(self, block_id: str = "toolu_1") -> None:
        self.id = block_id
        self.name = "lookup_holdings"
        self.input = {"reason": "need the positions"}


class _Reply:
    def __init__(self, content, stop_reason, tokens=100):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage(tokens)


class _Messages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


class FakeClient:
    def __init__(self, replies):
        self.messages = _Messages(replies)


def tool_then_answer(answer="NVDA is ~19% including look-through."):
    return [
        _Reply([_Text("Let me check."), _ToolUse()], "tool_use", tokens=150),
        _Reply([_Text(answer)], "end_turn", tokens=250),
    ]


async def collect(phases):
    """A checkpoint that records the protocol and injects nothing."""

    async def checkpoint(phase, results=()):
        phases.append((phase, results))
        return None

    return checkpoint


async def test_the_tool_is_called_and_its_result_fed_back():
    worker = LLMWorker(client=FakeClient(tool_then_answer()))
    phases: list[tuple] = []
    text, tokens, results = await worker.answer("is NVDA my biggest?", await collect(phases))

    assert "NVDA" in text
    # Two model calls: one that asked for the tool, one that answered.
    calls = worker.client.messages.calls
    assert len(calls) == 2
    assert calls[0]["tools"] == [HOLDINGS_TOOL]

    # The second call carries the tool result back to the model.
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1"
    assert json.loads(tool_result["content"])["holdings"], "portfolio never reached the model"
    assert results == [json.dumps(load_portfolio())]


async def test_tokens_are_summed_across_every_turn():
    """Charging only the final call would understate spend by the whole
    tool-use round trip, and the wasted-tokens metric would lie."""
    worker = LLMWorker(client=FakeClient(tool_then_answer()))
    _text, tokens, _results = await worker.answer("q", await collect([]))
    assert tokens == (150 + 150) + (250 + 250)


async def test_the_checkpoint_protocol_is_walked_in_order():
    """§7a. The order is what lets a change be drained at a safe moment: once
    before each model call, once when a tool call starts, once when it
    returns — and the tool results ride the last one so they can be retained."""
    worker = LLMWorker(client=FakeClient(tool_then_answer()))
    phases: list[tuple] = []
    await worker.answer("q", await collect(phases))

    names = [phase for phase, _ in phases]
    assert names == ["before_model", "tool_call", "after_tool", "before_model"]
    after_tool = next(results for phase, results in phases if phase == "after_tool")
    assert after_tool, "tool results must reach the Sequencer to be retained"


async def test_a_change_drained_at_a_checkpoint_reaches_the_model():
    """The injected amendment has to actually alter the conversation, not
    merely be acknowledged."""
    worker = LLMWorker(client=FakeClient(tool_then_answer()))
    delivered = {"done": False}

    async def checkpoint(phase, results=()):
        if phase == "after_tool" and not delivered["done"]:
            delivered["done"] = True
            return "also South China Sea"
        return None

    await worker.answer("taiwan exposure", checkpoint)

    final_call = worker.client.messages.calls[-1]
    folded = [
        m for m in final_call["messages"]
        if isinstance(m.get("content"), str) and "South China Sea" in m["content"]
    ]
    assert folded, "the change never reached the model"


async def test_an_answer_with_no_tool_call_still_returns():
    worker = LLMWorker(client=FakeClient([_Reply([_Text("$48,200 in cash.")], "end_turn")]))
    text, tokens, results = await worker.answer("cash balance?", await collect([]))
    assert "48,200" in text
    assert results == []


async def test_the_loop_is_bounded_rather_than_infinite():
    """A model that asks for the tool forever must not spin."""
    worker = LLMWorker(client=FakeClient([
        _Reply([_ToolUse(f"toolu_{i}")], "tool_use") for i in range(20)
    ]))
    text, _tokens, _results = await worker.answer("q", await collect([]))
    assert "step budget" in text
    assert len(worker.client.messages.calls) < 10


async def test_the_tool_description_says_when_to_call_it():
    """A description that only says what a tool does triggers less reliably
    than one that says when to reach for it."""
    description = HOLDINGS_TOOL["description"].lower()
    assert "call this" in description or "whenever" in description
    assert HOLDINGS_TOOL["input_schema"]["required"] == ["reason"]


async def test_retained_tool_results_are_reused_instead_of_refetched():
    """§7c, in live mode. The Sequencer keeps tool results across a
    premise-preserving amendment — but retention saves nothing unless the
    worker actually drafts against them rather than paying again.
    """
    worker = LLMWorker(client=FakeClient([_Reply([_Text("Still ~19%.")], "end_turn")]))
    carried = json.dumps(load_portfolio())

    text, _tokens, results = await worker.answer(
        "taiwan exposure, also south china sea", await collect([]), (carried,)
    )

    # One model call, and no tool round trip: the expensive step was skipped.
    calls = worker.client.messages.calls
    assert len(calls) == 1
    assert results == [carried]

    # The carried result is presented as an already-completed tool call.
    seeded = calls[0]["messages"]
    assert seeded[1]["role"] == "assistant"
    assert seeded[1]["content"][0]["type"] == "tool_use"
    assert seeded[2]["content"][0]["type"] == "tool_result"
    assert seeded[2]["content"][0]["content"] == carried


async def test_without_retained_results_the_tool_is_still_called():
    """The other half of the claim: the saving comes from retention, not from
    the worker having quietly stopped looking things up."""
    worker = LLMWorker(client=FakeClient(tool_then_answer()))
    await worker.answer("taiwan exposure", await collect([]))
    assert len(worker.client.messages.calls) == 2
