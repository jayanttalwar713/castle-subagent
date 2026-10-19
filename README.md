# Answering a busy group chat without a backlog

Castle take-home, Option 3. Full reasoning in **[DESIGN.md](DESIGN.md)** (also
typeset as [DESIGN.pdf](DESIGN.pdf)); this file is what it does and how to run it.

---

## The problem, in six messages

```
 t=0   Ana:   "What's my exposure to a Taiwan escalation?"      slow: needs tools
 t=2   Ben:   "Is NVDA still my biggest position?"              fast: one lookup
 t=4   Ana:   "Actually make that Taiwan OR South China Sea."   changes a running request
 t=5   Cara:  "What's my exposure to Taiwan?"                   duplicate of Ana's
 t=9   Ben:   "And the second biggest?"                         depends on an earlier answer
 t=11  Cara:  "never mind the Taiwan one"                       cancels
```

Six messages, **three** pieces of work, two of which change shape after they
start. Answering one at a time makes Ben wait 14 seconds for a 1.5-second
question. Running them in parallel without care answers Taiwan twice, answers
a question Ana already replaced, answers "the second biggest?" before anyone
knows what the first was, and keeps paying for work Cara cancelled.

```console
$ python -m chatsched run --scenario transcript --policy DEPENDENCY_ORDER

[   0.0s] → Ana    ACK         re: "What's my exposure to a Taiwan escalation?" — on it
[   2.0s] → Ben    ACK         re: "Is NVDA still my biggest position?" — on it
[   3.5s] → Ben    ANSWER      re: "Is NVDA still my biggest position?" — …
[   4.0s] → Ana    AMEND_ACK   re: "What's my exposure to a Taiwan escalation?" — updated, still working
[   5.0s] → Cara   ACK         re: "What's my exposure to a Taiwan escalation?" — on it
[   9.0s] → Ben    ACK         re: "And the second biggest?" — on it
[  10.5s] → Ben    ANSWER      re: "And the second biggest?" — …
[  11.0s] → Cara   CANCEL_ACK  re: "What's my exposure to a Taiwan escalation?" — dropped
[  14.8s] → Ana    ANSWER      re: "What's my exposure to a Taiwan escalation?" — …

✓ all invariants hold
```

Ben is answered at 3.5s, not 14.8s. Cara's duplicate costs nothing. Her cancel
removes *her* from the question without cancelling Ana's. The reference waits
for the answer it depends on, and only for that.

## The thesis

> **Serialize on dependency, not on arrival.**

"A clear, ordered stream of answers" is four separate reader-facing properties
— attribution, completeness, non-contradiction, causal consistency — and only
the last one needs ordering, only between dependent pairs. FIFO buys all four
with one blunt instrument and charges head-of-line blocking for it.

The architecture follows: **serialize the cheap thing, parallelize the
expensive thing.** The one component with shared state (the Sequencer) is a
pure function that does no inference; the expensive component (workers, Opus)
is isolated and replicated.

## What the numbers say

```console
$ python -m chatsched compare --all
```

| scenario | policy | first answer | head-of-line delay | misattributed |
|---|---|---|---|---|
| slow Q1, fast Q2 | `SERIAL` | 22.5s | 0.0s | 1 |
| | `STRICT_FIFO` | 22.5s | **21.3s** | 1 |
| | `DEPENDENCY_ORDER` | **1.2s** | 0.0s | 0 |
| burst of six | `SERIAL` | 7.7s | 0.0s | 5 |
| | `STRICT_FIFO` | 7.7s | **11.2s** | 5 |
| | `DEPENDENCY_ORDER` | **1.8s** | 0.0s | 0 |

`STRICT_FIFO` is the interesting row. It parallelises the *work* and still
makes Ben wait, because his answer was ready at 1.2s and buffered behind a
slower question. That wait is the cost of ordering, isolated from the cost of
the work. It also shows that FIFO ordering does not give the reader
attribution — with several questions outstanding, answers arriving in order
still leave you counting positions.

`SERIAL` is in the table deliberately: it is the behaviour we claim to be
improving on, so it is measured rather than assumed.

Two further columns are correctness checks that must read zero — `stale` and
`broken`. And `seq p99` (7–34µs simulated, ~105µs live) is what makes
"serialize the cheap thing" a measurement rather than an assertion.

## Correctness

```console
$ python -m pytest                 # 185 tests, 90% coverage
$ HYPOTHESIS_PROFILE=soak pytest tests/test_fuzz.py   # 5,000 random chats
```

The Sequencer is a **pure synchronous state machine** — `handle(event) →
[commands]`, no I/O, no clock, no model calls. That buys three things: unit
tests drive it directly, scenario replay is byte-reproducible, and a fuzzer
can run randomised message streams through it and assert every system
invariant on each one.

Coverage is 96%; the untested remainder is argparse wiring and
unreachable guards. The invariants live in [`chatsched/invariants.py`](chatsched/invariants.py)
and are executable, not prose: *no deadlock, completeness, no stale answers,
non-contradiction, causal consistency, attribution, pool bound, tokens
accounted, snapshot coherence.*

**The fuzzer found four real bugs that the hand-written tests did not:**

1. A `REFERENCE` whose parent had *already* died hung forever.
2. A parent that delivered, was amended, then cancelled stranded its waiting
   child — admission was asking "is the parent `DELIVERED`?" when the real
   question is whether the answer the child needs has shipped.
3. An amendment arriving in the ready-but-undelivered window dispatched a
   restart while the old worker's slot was already free, **over-running the
   pool limit**.
4. The same window let an answer computed against the *old* spec survive into
   the new version and ship.

It also falsified two invariants as too crude, which was worth as much. Each
has a named regression test.

### Version gating, demonstrated rather than asserted

A correctness property you can break on command is worth more than one you
claim. The abort of a superseded worker genuinely races its completion, so:

```console
$ python -m chatsched run --scenario late_amend                     # stale: 0 ✓
$ python -m chatsched run --scenario late_amend --no-version-gate   # stale: 1 ✗
```

With the gate off, the superseded worker's answer reaches the chat 5 seconds
early, and two invariants fail — including `tokens_accounted`, which notices
we shipped an answer that no worker ever computed.

## Measuring the classifier

Everything above tests the scheduler. The classifier needs a different
instrument, and `python -m chatsched eval` provides one: 38 labelled messages
against realistic live request sets, 14 of them deliberately ambiguous, scored
for precision, recall, and — the thing §4 actually claims — **which direction
the errors go**.

It produced a negative result about my own design. Across 152 observations,
self-reported confidence barely separates right from wrong (0.93 correct vs
0.85 wrong, with mistakes as high as 0.95). Sweeping the threshold from 0 to
0.90 does not reduce destroyed questions *at all* — it only adds redundant
ones. **The mechanism §4 leaned on does not work.**

Two things replaced it, both measured:

- A `CANCEL` whose message does not **name what it refers to** now asks
  instead of acting. "Never mind the Taiwan one" names its target; a bare
  "no" does not. `CANCEL` precision is 1.00 with this in place, and it needs
  no parked state — if they meant it, the reply names its target by
  construction.
- The error the data actually found was **over-merging**, not
  over-cancelling. Naming that bias in the prompt cut destroyed questions
  from 9 to 6 and `DUPLICATE`→`AMEND` from 4 to 1.

The eval also repeats, because the classifier is not deterministic: identical
runs score between 89% and 97%, so a single pass is an anecdote.

## Live mode

The same Sequencer, driven by asyncio against real models: `claude-haiku-4-5`
classifying every inbound message, `claude-opus-5` workers with a portfolio
tool. Nothing in the Sequencer changes between simulation and production —
only what drives it.

```console
$ cp .env.example .env && $EDITOR .env      # ANTHROPIC_API_KEY
$ python -m chatsched live --scenario transcript --explain
```

`--explain` prints every triage decision — the proposed relationship, the
confidence, the target, and whether the confidence threshold forced a fallback
to `NEW`. The classifier is the highest-risk component in the system, so its
decisions are inspectable rather than hidden:

```
Ana    Actually make that Taiwan or South China Sea   AMEND      → r1  conf=0.92
Cara   What's my exposure to Taiwan?                  DUPLICATE  → r1  conf=0.92
Ben    And the second biggest?                        REFERENCE  → r2  conf=0.95
Cara   never mind the Taiwan one                      CANCEL     → r1  conf=0.92
```

Classifier errors are made **asymmetric by design**: a `NEW` misclassified as
`AMEND` silently destroys somebody's question, while an `AMEND` misclassified
as `NEW` merely produces a redundant answer. Only one of those is recoverable,
so the threshold is biased toward `NEW` and destructive relationships need
more confidence than additive ones.

## Whose portfolio is it?

The brief says "a group chat" without saying whose portfolio is under
discussion, and the answer changes the design. If everyone shares one — a
household, an investment club, a member and their advisor — then two people
asking the same thing is one piece of work. If each has their own, identical
words are *different* questions, and answering both with one answer would
disclose one member's positions to another.

So the assumption is made explicit rather than guessed: every request carries
the member it is about, `DUPLICATE` requires a match, and a chat that sets no
member is treated as having one shared context.

```console
$ python -m chatsched run --scenario two_members

[   0.0s] → Ana          ACK      re: "What's my exposure to a Taiwan escalation?" — on it
[   1.0s] → Dev          ACK      re: "What's my exposure to a Taiwan escalation?" — on it
[   2.0s] → Cara         ACK      re: "What's my exposure to a Taiwan escalation?" — on it
[   4.2s] → Ana, Cara    ANSWER   …     ← one answer, two askers: same portfolio
[   5.2s] → Dev          ANSWER   …     ← his own answer: different portfolio
```

## Layout

```
chatsched/
  model.py        domain types — requests, events, commands
  sequencer.py    the pure state machine (the core)
  depgraph.py     dependency edges, cycle detection
  policies.py     SERIAL · STRICT_FIFO · DEPENDENCY_ORDER
  delivery.py     the chat stream: acks, attribution, replacement notices
  invariants.py   executable correctness properties
  metrics.py      the comparison table
  sim.py          deterministic discrete-event simulator
  live.py         asyncio runner for real models
  llm.py          Haiku triage · Opus workers
scenarios/        eight replayable fixtures
tests/            unit · scenario · property-based fuzz
```

## Setup

```console
$ python3 -m venv .venv && . .venv/bin/activate
$ pip install -r requirements.txt
$ python -m chatsched compare --all      # no API key needed
```

Simulation and tests never make a network call; only `live` needs a key.

## How close to production?

Not close, and [DESIGN.md §14](DESIGN.md) says exactly where. The scheduling
core is solid — nine invariants, ~80,000 randomised runs. What is missing is
infrastructure that is well understood and would have crowded out the part
being judged: **crash recovery** (nearly free given the pure Sequencer, just
not written), **backpressure** (a busy day currently becomes an unbounded
queue), **fair sharing across chats**, and **operability**. §15 answers the
questions a reviewer is most likely to ask.

## Reading the code

Suggested order, shortest path to understanding the whole thing. The first two
files are the system; everything else serves them.

| # | File | Why it matters |
|---|---|---|
| 1 | [`model.py`](chatsched/model.py) | The vocabulary. A `Request`, the five relationships, the events in and the commands out. Nothing happens here, but nothing makes sense without it. |
| 2 | [`sequencer.py`](chatsched/sequencer.py) | **The whole system.** `handle(event) → [commands]` — 593 lines, of which ~380 are code and the rest explain *why*. No imports that touch I/O. If you only read one file, read this one — every design claim in DESIGN.md is implemented here. |
| 3 | [`policies.py`](chatsched/policies.py) | Three tiny classes. Shows how §3 turned ordering from structure into a swappable choice. |
| 4 | [`invariants.py`](chatsched/invariants.py) | The nine properties, as code. The best summary of what "correct" means here. |
| 5 | [`tests/test_fuzz.py`](tests/test_fuzz.py) | How the randomised testing works, and a docstring listing every bug it found. |
| 6 | [`sim.py`](chatsched/sim.py) | The deterministic clock and the mock workers. Read it to see why replay is reproducible. |
| 7 | [`llm.py`](chatsched/llm.py) / [`live.py`](chatsched/live.py) | The real models, and the asyncio runner. Same Sequencer, different driver. |
| 8 | [`evaluate.py`](chatsched/evaluate.py) | The classifier measurement. Read `destructive_error` — it is where the §4 argument becomes arithmetic. |

Skimmable: `delivery.py` (string formatting), `metrics.py` (arithmetic),
`cli.py` (argparse), `depgraph.py` (~60 lines, but it only does three things).

Three places where the reasoning is subtler than the code looks:

- **`_settle()`** in `sequencer.py` — deliver, then admit, to a fixpoint. The
  loop exists because delivering a parent can unblock a child.
- **`_release_slot()`** — frees the pool slot only if *this* worker still owns
  it. A superseded worker finishing late must not free its replacement's slot.
- **`_snapshot_for()`** — the data version is a counter, not a clock reading,
  because the Sequencer has to stay a pure function.

## Scope

Five hours. What is deliberately designed-but-not-built — `MERGE`/`SPLIT`
relationships, `BOUNDED_REORDER(k)`, cross-chat fair-share scheduling — and
the reasoning for each cut is in [DESIGN.md §12](DESIGN.md). Known failure
modes are §13; the two that bit during implementation are now handled and
tested.
