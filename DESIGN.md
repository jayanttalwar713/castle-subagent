# Answering a busy group chat without a backlog

Castle take-home, Option 3. Written before the code, so the reasoning can be
judged separately from the implementation.

---

## Before you start

**This document assumes no background.** Every technical term is defined where
it first appears, and there is a glossary at the end. If you hit a word you
don't recognise, it is defined somewhere above — or it shouldn't be there.

Here is the whole world the design lives in:

- An **agent** is a program that answers questions using a language model. To
  answer "am I overweight energy?", it has to *look things up* — fetch your
  holdings, fetch prices. Those lookups are called **tool calls**, and they are
  the slow, expensive part. A single question might take twenty seconds and
  cost a few cents.
- Cost is measured in **tokens** — roughly, pieces of words. Everything the
  agent reads and writes is billed by the token. When this document says work
  was "wasted", it means tokens we paid for and threw away.
- The agent sits in a **group chat** with several people in it. They talk at
  the same time, interrupt, change their minds, and ask follow-ups.

That is it. Everything else is built up from here.

---

## 1. The problem, concretely

Castle's agent sits in a group chat. When the chat gets busy, several people
ask it several things at once. Today it answers them one at a time, so a queue
forms. The brief asks for a system that:

1. works on many requests **in parallel** — several at the same time, rather
   than finishing one before starting the next;
2. still gives each chat a **clear, ordered stream of answers**; and
3. handles people **changing or adding to a request while it is running**.

Here is the situation the system has to survive. Times are seconds from the
start of the burst:

```
 t=0   Ana:   "What's my exposure to a Taiwan escalation?"      ← slow: needs tool calls
 t=2   Ben:   "Is NVDA still my biggest position?"             ← fast: one lookup
 t=4   Ana:   "Actually make that Taiwan OR South China Sea."  ← changes a running request
 t=5   Cara:  "What's my exposure to Taiwan?"                  ← same question Ana asked
 t=9   Ben:   "And the second biggest?"                        ← depends on an earlier answer
 t=11  Cara:  "never mind the Taiwan one"                      ← cancels
```

Six messages, but only **three** pieces of work — and two of those change
shape after they have already started.

Throughout this document, a **request** means one piece of work the agent owes
somebody an answer for. A **message** is one line somebody typed. The two are
not the same thing, and §4 argues that confusing them is the central mistake
available here.

A system that treats each message as an independent job gets this transcript
wrong in at least four ways. It answers the Taiwan question twice (Ana and
Cara asked the same thing). It answers Ana's original question, which she
already replaced. It answers "the second biggest?" before anyone knows what
the first one was. And it keeps paying for work Cara cancelled.

That transcript is the specification. It is also scenario 1 of the test suite.

---

## 2. Why the three requirements fight each other

The three requirements are not independently satisfiable. Every *pair* of them
is in tension, which is why this is a design problem rather than a
to-do list.

### Parallel × Ordered

Work finishes out of order — a question that takes one second finishes before
one that takes thirty, whoever asked first. You could restore arrival order by
holding finished answers back until their predecessors are done. But then a
fast question asked third waits on a slow question asked first, and you have
moved the backlog from *computing* answers to *delivering* them. The user sees
no improvement.

> **Head-of-line blocking** — when the first item in a queue is slow,
> everything behind it waits, however fast those items are individually. It is
> the specific failure the brief describes as "answering them one at a time
> creates a backlog". Named after a queue of people at a door: one person
> fumbling for their ticket blocks everyone behind them.

### Parallel × Mutable

A change — *"actually make that…"* — targets **one specific computation that
is currently running**. The obvious way to get parallelism is to give each
request its own isolated worker that shares nothing with the others. But then
the change has nowhere to go: whoever receives the new message has no handle on
the worker that needs to hear it, and no shared context in which to work out
what "that" refers to.

### Ordered × Mutable

"Ordered" presumes a stable sequence of things to order. If request #2 becomes
a materially different request at t=30s — or is cancelled, or turns out to be
the same question as request #4 — the sequence is not stable. Ordering by the
arrival time of the *original* message is ordering by a fact that is no longer
true.

Any design that treats these as three features to implement will fight itself.
The way through is to notice that one of the three is not what it appears to
be.

---

## 3. First reframe: "ordered" is about legibility, not sequence

Nobody in a group chat actually wants strict arrival order.

> **FIFO** — "first in, first out", the rule that things come out in the order
> they went in. A supermarket queue is FIFO. It is the obvious reading of
> "ordered", and it is the one that costs head-of-line blocking.

Pull on "a clear, ordered stream of answers" and ask what a reader actually
needs, and it comes apart into four separate properties:

| Property | What it means for the reader | Needs ordering? |
|---|---|---|
| **Attribution** | For every answer, I can tell which question it answers. | No |
| **Completeness** | I can tell what is still outstanding. Nothing silently vanished. | No |
| **Non-contradiction** | I never see two live answers that disagree. A replaced answer is visibly replaced. | No |
| **Causal consistency** | If answer B depends on answer A, A arrives first. | **Yes** — and only between dependent pairs |

**Three of the four need no ordering at all.** FIFO delivery buys all four
with one blunt instrument and charges head-of-line blocking for it.

Implemented directly, each property is cheap, and none of them blocks:

- **Attribution** → quote the question back: `re: "exposure to Taiwan" — …`
- **Completeness** → acknowledge each request when it arrives, and keep the outstanding set visible
- **Non-contradiction** → say "this replaces my earlier answer" when it does, and never ship an answer that belongs to an out-of-date version of a request (§7)
- **Causal consistency** → record which request depends on which, and serialize *only* across that link

### The design thesis

> **Serialize on dependency, not on arrival.**

*Serialize* here means "force to happen one after another". The claim is that
the only thing that genuinely has to wait is an answer that needs another
answer first — and nothing else.

Everything downstream follows from that sentence. It turns ordering from a
structural property of the system into a **policy** that can be swapped per
chat — which in turn makes it something we can *measure* rather than assert
(§9, §10).

---

## 4. Second reframe: messages are not requests

The brief's phrasing — "several questions at once" — invites you to treat each
message as a job. In a real group chat that is wrong. Look at the transcript
again: six messages, three requests.

So the right question on the way in is not *"what does this message ask
for?"* but **"how does this message relate to the work already in flight?"**

That distinction is load-bearing. It is what lets "And the second biggest?"
create the dependency link §3 needs, and what lets Cara's repeat of Ana's
question avoid paying twice for one answer.

### The six relationships we handle

| Relationship | Meaning | Where to see it |
|---|---|---|
| `NEW` | An independent request. | transcript, Ana t=0 and Ben t=2 |
| `AMEND` | Changes an existing request. Carries one flag, `preserves_premise`. | transcript, Ana t=4 |
| `CANCEL` | Stop work; no answer is owed. | transcript, Cara t=11 |
| `DUPLICATE` | Someone already asked this; one answer serves both. | transcript, Cara t=5 |
| `REFERENCE` | A new request that needs a previous request's **answer** first. | transcript, Ben t=9 |
| `IGNORE` | Not a request at all. | `busy_chat`: "morning all", "thanks!!" |

`IGNORE` is the odd one out, and it is listed because leaving it out would be
dishonest about the classifier's job. The other five describe a relationship
*between* this message and some existing request. `IGNORE` says there is no
request here to relate to anything — it is a greeting, a thank-you, a joke, or
one member answering another. The Sequencer's response is to return no
commands at all, so `IGNORE` is the only relationship that never appears in
the scheduling diagrams.

It still matters, for two reasons. **It is most of a real group chat.** A
scheduler that treats every message as a request answers "morning all" with a
portfolio report. And **it is where the dangerous mistakes live**: "wait" or
"no" or "ok" are almost always social, but read as `CANCEL` they kill live
work. Five of the labelled set's fourteen hard cases are exactly this —
"interesting", "ok", "wait", "no", "is that still running?" — which is why
§ 10 reports the hard cases separately from the clear-cut ones.

The `preserves_premise` flag is doing real work. "Also include South China
Sea" *widens* the question — the research already done on Taiwan is still
valid, so we keep it. "Actually, not Taiwan, I meant inflation" *replaces* the
premise — that research is now worthless and must be thrown away. Same
relationship, one boolean, very different cost. Folding *widen*, *narrow* and
*replace* into one class with one flag removes three categories the classifier
would otherwise have to tell apart, and loses no behaviour.

### Who writes the amended question

Classifying a message as `AMEND` is only half the job. Something still has to
produce the question the worker is now meant to answer, and the obvious
approach does not survive contact with real messages:

> original: *"How did my book do in Q2?"*
> change:   *"sorry I meant Q3 not Q2"*
> glued together: *"How did my book do in Q2? sorry I meant Q3 not Q2"*

That hands the worker a self-contradiction and hopes it picks the right half.

So triage returns a third thing alongside the label and the flag: the merged
question, already resolved — *"How did my book do in Q3?"*. The model has both
texts in front of it and is the only component in a position to combine them,
so it is asked to state the result rather than leaving the Sequencer to
concatenate strings it cannot read. The field is `rewritten_question` in the
model's response and `rewritten_spec` on the event.

Concatenation survives as the fallback for when the model omits the field —
glue on a widening change, replace outright on a premise-replacing one, picked
by the same `preserves_premise` flag. It is a deliberately poor fallback, and
the reason the Sequencer prefers the rewrite whenever one is offered.

This is also the clearest case of the division of labour in § 3: judgement
about language lives in the model, and decisions about work live in the
Sequencer. "What is this question now?" is a language question. "Whose slot
does it get, and may the answer ship?" is not.

*(Two further relationships — `MERGE` and `SPLIT` — are designed in §12 and
deliberately not built. The reasoning is there.)*

### What the classifier is shown

Triage sees two things: the **live request set** — every request still in
flight, with its stage, asker, member and question — and the **recent chat**,
bounded to the last six messages.

The second was missing for most of this project's life, and the gap was not
obvious. The classifier's hardest job is telling a message aimed at the agent
from one aimed at another person in the room, and the request set cannot
settle that. Consider Ana typing:

```
ben: ana are you joining the 3pm call?
ana: no
```

`no` is an answer to Ben. Without the preceding line it is a bare word next to
a live Taiwan request, and the honest reading is a coin flip — in five samples
the classifier returned `IGNORE`, `NEW` *and* `CANCEL`, and `CANCEL` would
have killed Ana's question. With the line, it answers `IGNORE` five times out
of five.

The eval set's own note on that case read *"almost certainly answering someone
else in the chat"* — a label whose justification rested on evidence the
classifier was never given. That is the kind of mistake that survives a long
time, because every individual component looks reasonable.

Supplying it is the single largest improvement measured in this project:

| | without chat | with chat |
|---|---|---|
| Agreement with the labels | 143/152 (94%) | **145/152 (95%)** |
| On the genuinely ambiguous cases | 49/56 | **52/56** |
| Spread across identical passes | 92% – 97% | **95% – 97%** |
| `IGNORE` precision / recall | 0.97 / 0.95 | **1.00 / 1.00** |
| `CANCEL` precision / recall | 1.00 / 0.95 | **1.00 / 1.00** |
| `REFERENCE` precision / recall | 0.89 / 1.00 | **1.00 / 1.00** |

The headline number barely moves, and it is the least interesting column. What
changed is the **shape**: the entire class of "two people talking to each
other" errors is gone, `IGNORE` and `CANCEL` are exact, and the errors that
remain are `NEW`→`AMEND` and `DUPLICATE`→`NEW` — under-merging, the
recoverable direction.

The result that matters most is the third row. §10 found the run-to-run
variance large enough to swamp the confidence mechanism meant to control it.
Supplying the missing evidence narrowed it, and two independent four-pass runs
afterwards produced *identical* confusion matrices. The instability was never
really in the model; it was in asking a question that the available evidence
could not answer.

The window is capped at six messages for the reason §8 gives: the live request
set is bounded, so this must be too, or a chat that runs all day grows its own
classification cost without limit.

### Classifier errors must be asymmetric by design

> **Classifier** — a model whose job is to put a thing into one of a fixed set
> of categories. Here: given a message and the live request set, pick one of
> the six relationships above. **Confidence** is the classifier's own estimate
> of how likely it is to be right, from 0 to 1.

The classifier is a small language model and it will sometimes be wrong. What
matters is making sure it is wrong in the **recoverable** direction:

- A `NEW` misclassified as `AMEND` **destroys a request.** The original
  question is overwritten, never answered, and nobody finds out.
- An `AMEND` misclassified as `NEW` **produces a redundant answer.** Noisy, but
  every question still gets answered and the reader can see what happened.

The second failure is recoverable and the first is not. So the classifier is
**biased toward `NEW`**: each relationship has a confidence threshold it must
clear, the destructive ones have a higher bar, and anything below its
threshold falls back to `NEW`.

### Measuring that, and finding it does not work

The argument above is sound. The thresholds implementing it — `0.75` and
`0.50` — were invented, so §10 measures them against a labelled set. The
result is a negative one about this design:

> **Self-reported confidence barely separates right answers from wrong ones.**
> Across 152 observations the mean confidence is 0.93 when correct and 0.84
> when wrong — a gap of about +0.09, with mistakes arriving as low as 0.72 and
> as high as 0.95. Raising the threshold does reduce destroyed questions, but
> at a punitive exchange rate: moving from 0 to 0.90 saved between one and
> three of them while adding ten to twelve redundant answers, and 0.95 buys
> the rest by rejecting roughly a quarter of all predictions.

Worse than the exchange rate was the stability. Two identical 152-case runs
gave 7 destroyed questions and 4 — **the run-to-run variance on the same
input was as large as the effect the threshold was supposed to produce.** A
knob you cannot distinguish from noise is not a safety mechanism, whatever its
average behaviour.

That finding is what led to the chat context above, and it is worth being
precise about what changed. Supplying the missing evidence narrowed the spread
to 4 destroyed on both runs. The threshold did not become useful — the
confidence bands still overlap — but the *reason* it looked useless turned out
to be partly fixable, and fixing it was worth more than any tuning of the knob
would have been. **The instability was in the question, not the model.**

So a threshold cannot buy this asymmetry, because legitimate amendments and
mistaken ones arrive at indistinguishable confidence. Two things replace it:

**A signal that is not confidence.** Whether the message *names what it refers
to* separates them cleanly. "Never mind the Taiwan one" names its target; a
bare "no" — quite possibly aimed at another person in the chat — does not. So
a `CANCEL` whose message names nothing **asks instead of acting**, and that
costs no parked state: if they meant it, they say so again, and the reply
names its target by construction. `CANCEL` precision is 1.00 with this in
place.

**Fixing the error the measurement actually found.** The dominant failure was
not over-cancelling but **over-merging** — reaching for `AMEND` and
`DUPLICATE` where the honest answer was `NEW`. Telling the classifier about
that specific bias, and that topical similarity is not evidence of
relatedness, cut destroyed questions from 9 to 6 and `DUPLICATE`→`AMEND`
errors from 4 to 1.

**A signal that works, and why it is no longer needed.** Showing that
confidence fails raises an obvious question: is there *any* runtime signal
that separates right answers from wrong ones? There is, and it is not the
model's opinion of itself — it is whether the model agrees with itself.
Classifying each message five times, unanimous answers were 97% correct and
split ones 33–60%.

Two things made that more than a curiosity. **Majority voting did not help** —
3 errors against 3.4 for a single sample, and worse on a second run. The
mistakes were systematic rather than random, so five samples reproduced the
same bias five times. It was the *disagreement* that carried information, not
the vote. And the messages it flagged were exactly the ones a person would
hesitate over: "wait", "no", "is that still running?".

Then the chat context above was added, and the measurement was repeated:

| | before chat context | after |
|---|---|---|
| Cases where five samples disagree | 5–6 of 38 | **2 of 38** |
| Errors on a single sample | 3.4–4.2 | **1.8** |
| Destroyed questions | 1.6–2.6 | **1.0** |
| Correct when unanimous | 97% | 97% |

**The signal survived; the thing it was detecting mostly did not.** Giving the
classifier the evidence it was missing removed two thirds of the disagreements
outright, because they were never really the model being unsure — they were
the model being asked an unanswerable question and improvising a different
answer each time.

So it is not built, and the reason is better than the cost. Five triage calls
per message would now buy a flag on two cases out of thirty-eight, one of
which is genuinely ambiguous. **Detecting uncertainty is a worse fix than
removing its cause**, and the measurement is what made the difference visible.
If the remaining errors ever justify it, the mechanism is characterised and
ready; it is simply not earning its keep.

The thresholds stay, but it is worth being exact about what they now do.
They run on every classification, and across 114 measured classifications they
fired **zero times** — mean confidence is 0.939 and the lowest seen is 0.72,
while the destructive gate sits at 0.75 and only applies to `CANCEL` and
`IGNORE`, which never arrived that unsure. So they are not "catching the
low-confidence tail"; on this data there is no tail to catch. They are kept as
a cheap backstop against inputs unlike the ones measured, and nothing more.
The work is being done by `names_target` and by the prompt.

Because the classifier is the highest-risk component in the system, every
decision it makes — proposed relationship, confidence, target, and whether a
fallback was applied — is logged and printable (`--explain`). It is the one
part a reviewer should be able to audit directly.

---

## 5. Architecture

Four components. Only one of them holds state shared between requests, and it
is deliberately the cheap one.

```
            chat messages
                    │
                    ▼
        ┌───────────────────────┐
        │        TRIAGE         │   Classifies each message against the live
        │   claude-haiku-4-5    │   request set (§4) and emits one command.
        │                       │   Runs on everything, so it must be cheap.
        └───────────┬───────────┘
                    │  NEW / AMEND / CANCEL / DUPLICATE / REFERENCE / IGNORE
                    ▼
    ╔═══════════════════════════════════════════════╗
    ║         SEQUENCER   (one per chat)            ║  ← the only shared state.
    ║                                               ║    Handles one event at a
    ║   · the register of live requests             ║    time, so there is
    ║   · which request depends on which            ║    nothing to collide.
    ║   · admission  — what is allowed to start     ║    No model calls here.
    ║   · delivery   — what is allowed to ship      ║
    ╚═══╤═══════════════════════════════════════╤═══╝
        │                                       │
        │   every event above is appended to an append-only log on disk
        │   *before* it is handled. The Sequencer is a pure fold over that
        │   log, so replaying it rebuilds the chat exactly — which is all
        │   crash recovery is here (§14).
        │                                       │
        │ start (request, version)              │ results, version-checked
        ▼                                       │
    ┌───────────────────────────────┐           │
    │       WORKER POOL (bounded)   │───────────┘
    │                               │
    │  one isolated worker per      │
    │  request-version · opus-5     │
    │  each has an inbox of changes │
    │  it reads at safe moments     │
    └───────────────┬───────────────┘
                    │
                    ▼
        ┌───────────────────────┐
        │       DELIVERY        │   Owns every reader-facing property from §3:
        │                       │   attribution · acks · replacement notices
        └───────────────────────┘
```

> **Worker** — one running job answering one request. **Pool** — the fixed set
> of workers that may run at once (say, three). A request waits for a free
> **slot** before it starts. Bounding the pool is what stops a busy chat from
> launching forty expensive jobs at once.

### Why not either sketch in the brief

**Sketch 1 — a router plus parallel runtimes.** A small model reads each
message and sends it to its own isolated runtime. Good isolation, genuinely
parallel. But the router is *stateless with respect to the live request set* —
it does not know what is currently running. So it structurally cannot classify
relationships: `AMEND`, `DUPLICATE` and `REFERENCE` are unrepresentable,
because each of them is a statement *about an existing request*. A change
arrives with no addressable target. And no component owns ordering, so
requirement #2 has nowhere to live.

**Sketch 2 — one runtime with subagents.** The main agent answers easy things
itself and spawns helpers for hard ones. Changes are natural here, because
everything shares context. But the main runtime becomes a **serialization
point**: every incoming change, every helper's result, and every delivery
decision passes through one loop that is *also doing model inference*. Under a
burst the backlog comes back — now bottlenecked on the one component you
cannot replicate.

**This design.** The component with shared state (the Sequencer) is cheap —
pure bookkeeping, no model calls at all. The expensive component (workers,
Opus) is isolated and replicated. In one line:

> **Serialize the cheap thing. Parallelize the expensive thing.**

The brief's second sketch does the exact opposite: it serializes inference and
parallelizes only the leaves.

This is the central claim of the submission, so §9 **measures** it rather than
asserting it — there is a metric whose whole job is to falsify it.


### Where this sits relative to the swarm post

The brief recommends Prime Intellect's *On the Nature of the Swarm* as
background. It argues that hierarchical agent trees bottleneck at the root —
the root's context fills no matter how large the swarm gets, recreating the
original problem one level up — and that the way out is peer-to-peer meshes
coordinating through shared state rather than through a coordinator.

**It reaches the same verdict on the brief's second sketch, by a different
route.** The post's objection is that the root runs out of *context*; the
objection above is that the root runs out of *throughput*, because it is doing
inference on the critical path. Two different resources, the same structural
fault: a component that cannot be replicated sits in the middle of everything.

It also names the escape route this design took. The post recommends
coordinating through shared state — git, issue trackers, things agents read and
write without anyone holding the whole picture. The Sequencer is exactly that:
shared state that is deliberately *stupid*. It has no context to fill because it
never reads an answer, and no throughput ceiling because it never calls a model.
So a star topology with a dumb hub avoids the bottleneck the post identifies,
without needing a mesh to do it.

**Where it criticises this design, it was right — and it found a bug.** The post
argues for persistent agents over disposable ones, because a sub-agent that
forgets everything on completion forces redundant work on the follow-up. Workers
here are strictly disposable, one per request-version, no memory. Taking that
seriously turned up something worse than waste.

A `REFERENCE` is an answer *about* an answer. But a follow-up admitted much
later than its parent was being pinned to a **different data version** — so
"and the second biggest?" could be computed against holdings the answer it
explicitly builds on never saw, and the two could disagree. That is the same
incoherence §8 already fixes *within* a request, missed *across* a dependency
edge. A follow-up now inherits its parent's data version and its tool results,
which fixes the coherence hole and removes the redundant lookup in the same
move. Freshness is the wrong instinct here: if you want current data you ask a
new question, not a follow-up.

What remains unaddressed is the broader version of the same idea. The post
observes that agents specialise naturally — whoever looked at the frontend first
becomes the frontend expert — and that routing should exploit it. Inheritance
only helps along a dependency edge; two *unrelated* questions about the same
member at the same data version still each pay for the lookup. The general fix
is a tool-result cache keyed by member, data version and call, which is a real
design problem rather than a small one, and is not built.

> One honest caveat on scale. The post's mesh argument is about swarms of ten
> thousand agents; this system runs a pool of three against one chat. At that
> size a dumb hub is not a compromise, it is the right answer — coordination
> costs nothing and a mesh would be ceremony. The argument starts to bite at a
> scale this design would have to grow into, and §12 is honest that cross-chat
> scheduling is not built.

### The Sequencer is a pure function

> **Pure function** — code that takes input and returns output, touching
> nothing else: no files, no network, no clock. Given the same input it always
> returns the same thing.

The Sequencer is written as `handle(event) → [commands]`. It never calls a
model, never reads the time (time arrives *on* each event), and never performs
I/O. It returns a list of instructions — *start this worker*, *stop that one*,
*ship this answer* — and something else carries them out.

This is not stylistic. It buys four concrete things:

1. **It makes §5's claim structural.** "The Sequencer does no inference" stops
   being a promise and becomes something the type of the function enforces.
2. **The races in §8 cannot happen.** One event is handled at a time, start to
   finish, so there is no moment where two things are half-applied.
3. **It makes the testing in §10 possible at all.** A pure function can be
   driven by a fuzzer thousands of times per second with no network calls.
4. **Crash recovery comes free.** If the whole state is a fold over events,

   ```
   state = fold(handle, events)
   ```

   then persisting the events *is* persisting the state. Recovery is replaying
   the log — no checkpoint format, no snapshot to keep in sync with the thing
   it describes, no reconciliation. §14 was going to list this as future work;
   it turned out to be about eighty lines, which is the point.

The same Sequencer runs unchanged in the simulator and in live mode against
real models. Only the thing driving it differs.

---

## 6. How a request moves through the system

Every request carries a **version number** that goes up by one every time its
specification changes. Every job started and every result returned is stamped
with `(request, version)`. This one mechanism does most of the work in §7.

```
   RECEIVED ──► QUEUED ──► PLANNING ──► TOOL_CALL ──► DRAFTING ──► DELIVERED
                  │           │             │             │
                  └───────────┴──── on a change: version+1, start again
                                               │
                  on CANCEL: ──────────────────┴──► DROPPED (acknowledged)
```

`QUEUED` means "waiting for a free worker slot". `PLANNING` is the model
working out what it needs, `TOOL_CALL` is the expensive lookup, `DRAFTING` is
writing the answer.

A queued request starts when **two** conditions hold: a worker slot is free,
**and** everything it depends on has already been answered. The second
condition is checked *before* a slot is taken, never while holding one — §8
explains why that ordering is load-bearing.

---

## 7. Handling changes mid-flight

Three mechanisms, and then the matrix that falls out of them.

### (a) Changes apply at checkpoints, not instantly

> **Checkpoint** — a defined safe moment in a worker's execution. Here: just
> before each model call, and just after a tool call returns.

A worker has an inbox of pending changes and reads it only at checkpoints. So
nothing is torn apart in the middle of an API call, and the system stays
predictable enough to test.

*Cost:* a change may wait up to one tool call before it takes effect. That is
stated as a tradeoff, not hidden. The alternative — interrupting a worker at an
arbitrary instruction — buys a few seconds and costs the ability to reason
about, or test, what state the worker was in when you stopped it.

### (b) Version checking makes reliable cancellation unnecessary

The Sequencer ignores any result whose version is not the current one.

That sounds small and is the most important mechanism here. Reliably
**stopping** work that is already running, across a network, is a genuinely
hard problem — your stop signal races the work finishing, and sometimes loses.
**Ignoring** work that came back out of date is trivial and cannot fail.

So we still try to stop superseded workers, to save money. But correctness
never depends on that succeeding.

This is not hypothetical: a stop signal sent while a worker is already writing
its final answer *does* lose the race, the worker finishes, and it reports a
result for a version that no longer exists. Version checking is what makes that
harmless. §10 shows it failing on command when the check is turned off.

### (c) Preserve work in proportion to what it cost

Tool calls are expensive in both time and tokens, and are frequently still
valid after a change. A draft is cheap to rewrite. So:

- a change that **preserves the premise** rewrites the answer over the tool
  results it already has
- a change that **replaces the premise** throws them away and starts over

### The stage × relationship matrix

A change arrives for a request whose worker is at stage *S*:

| | **QUEUED** | **PLANNING** | **TOOL_CALL** | **DRAFTING** | **DELIVERED** |
|---|---|---|---|---|---|
| `AMEND` *(premise kept)* | rewrite in place; nothing lost | new version, start again | let the tool call finish — it is already paid for — then apply the change at the next checkpoint | rewrite the answer over the tool results we already have | send an addendum |
| `AMEND` *(premise replaced)* | replace in place | start again | start again, **throwing away the tool results** | start again | send a new answer with a visible "this replaces my earlier answer" |
| `CANCEL` | drop, acknowledge | stop, acknowledge | let the tool call settle, then drop | stop, acknowledge | acknowledge *"already answered"* — no retraction |
| `DUPLICATE` | attach the second asker; one answer, sent to both | same | same | same | send the existing answer to the new asker |

**The `DELIVERED` column is the one most designs omit.** A change can always
arrive after the answer has already shipped. "Too late" is a real state with
real required behaviour, not an error.

---

## 8. Edge cases handled on purpose

These are the places a plausible-looking implementation quietly breaks. Each is
handled explicitly and each has a test. Four of the six were found by the
fuzzer described in §10, not by being clever in advance.

**A cancelled parent with a waiting child.** Ben asks "and the second
biggest?", then cancels the first question. The follow-up is now waiting on an
answer that will never come. *Resolution:* when a request ends without ever
answering, everything waiting on it is dropped with an explicit notice naming
the question that died. Silence is the one unacceptable outcome.

**Waiting on the answer, not on the request's status.** A subtler version of
the same thing. If a parent answers, is then amended (which makes it live
again), and is then cancelled, its status is `DROPPED` — but the answer the
child needs *already shipped*. Asking "is the parent currently delivered?"
strands that child forever; asking "has the answer it needs been delivered?"
does not. The gate is written the second way.

**Dependency deadlock in the worker pool.** If a waiting request could take a
worker slot and *then* wait for its parent, a pool full of waiters would
deadlock — every slot held by something that cannot proceed, and the thing it
is waiting for unable to get a slot. *Resolution:* the dependency check happens
at **admission**. A request never occupies a slot while waiting on another
request.

> **Deadlock** — a standstill where everyone is waiting for someone else and
> nothing can move. Classically: two people each holding one chopstick, each
> waiting for the other's.

**The ready-but-undelivered window.** Between "the worker finished" and "the
ordering policy released the answer", a request has no worker but is not
finished either. A change arriving in that window used to do two bad things at
once: it started a replacement worker while the old slot was already free
(breaking the pool limit), and it left the now-obsolete answer sitting in the
delivery buffer where a later release could still ship it. *Resolution:* a
change always discards any undelivered answer, and always goes back through
admission rather than starting a worker directly.

**Answers that disagree with each other.** Each worker fetches holdings and
prices independently, so two questions asked a minute apart can straddle a
price move and report figures that contradict each other — and an addendum
can be computed against different data than the answer it is adding to,
which is incoherent however correct each half is alone. *Resolution:* a
**data version is pinned at first admission** and never changed, so every
version of a request answers against the same holdings; requests admitted
close together share a pin, so concurrent answers agree. The worker reports
back which version it actually used, and an invariant checks it — because a
worker silently dropping a field the Sequencer sends it is a mistake this
codebase has already made once, with retained tool results.

**A follow-up answering against a different world than its parent.** "And the
second biggest?" builds explicitly on an earlier answer, but if it is admitted
after the snapshot window has rolled it would be pinned to newer data — so the
two answers could disagree while one is derived from the other. *Resolution:* a
dependent request inherits its parent's data version and tool results, and an
invariant checks it. Found by taking the swarm post's critique of disposable
agents seriously (§5).

**Two things happening at the same instant.** A result arriving and a change
arriving can land at the same moment. *Resolution:* the Sequencer handles one
event at a time, to completion. There is no interleaving to get wrong — the
concrete payoff of keeping the shared-state component free of model calls (§5).

**Dependency cycles from classifier error.** A misclassification could make A
wait for B while B waits for A — a loop nobody can ever get out of.
*Resolution:* detected when the link is created, and broken by dropping the
newer one. The result is a correct answer with weaker ordering, which is the
right direction to fail.

**Unbounded classifier input.** Triage needs to see the live request set, and
that set grows. *Resolution:* it sees every unfinished request plus the last
few answered ones. The live set is bounded; the chat history is not, and is not
included.

---

## 9. What we measure, and why

Everything below is recorded per scenario, per policy, and printed as a
comparison table.

| Metric | What it tells you |
|---|---|
| **Time to first / last answer** | How long the burst feels. The user-perceived win. |
| **Head-of-line delay** | Total time that *finished* answers spent waiting on unfinished ones. This isolates the cost of ordering from the cost of the work. |
| **Wasted tokens** | Money spent on versions of requests that were never delivered. **The price of mutability, in dollars.** |
| **Staleness** | Answers shipped whose question had already changed. **Must be zero** — a correctness check, not a performance number. |
| **Misattribution exposure** | Answers shipped with no "re: …" while more than one question was outstanding — i.e. the reader had to count positions to know what was being answered. |
| **Sequencer p99** | How long the Sequencer spends per event. **Directly tests "serialize the cheap thing."** If this is not tiny, the central claim of §5 is false. |

> **p99** — the 99th percentile: the value that 99 out of 100 measurements come
> in under. Used instead of an average because averages hide the occasional
> very slow case, which is usually the one that matters.

The last three are what make this an engineering argument rather than a
rhetorical one. Two of them are designed to be falsifiable: `misattribution`
and `sequencer p99` would both go non-zero if the design were wrong about
itself.

### Ordering policies, compared head to head

Because §3 made ordering a policy rather than a structure, these are
interchangeable and comparable on identical inputs:

| Policy | Behaviour |
|---|---|
| `SERIAL` | One request at a time. **The honest baseline** — the behaviour the brief describes as creating a backlog. |
| `STRICT_FIFO` | Work in parallel, but hold each answer until everything that arrived before it is finished. Isolates the cost of literal ordering. |
| `DEPENDENCY_ORDER` | Ship as soon as ready, with attribution and acknowledgements; a request waits only for something it genuinely depends on. **Proposed default.** |

`SERIAL` is in the list deliberately. Option 2 of the brief asks for a
comparison against "a simple baseline such as SPY alone"; the equivalent
discipline here is to measure against the behaviour we claim to be improving
on, not only against other clever designs.

---

## 10. How we know it is correct

Measuring speed is the easy half. The harder question is whether the thing is
*right*, and that needs a different kind of evidence.

### Executable invariants

> **Invariant** — something that must be true of the system at all times, no
> matter what happens. "The pool never runs more workers than it has slots" is
> an invariant.

The properties this document claims are written as code that checks them, not
as prose that asserts them:

| Invariant | What it forbids |
|---|---|
| `no_deadlock` | Any request still unfinished when everything has settled |
| `completeness` | A request the reader was acknowledged for that never produced any visible outcome |
| `no_stale` | Shipping an answer whose question had already changed |
| `non_contradiction` | The answer text changing without an addendum or replacement notice |
| `causal_consistency` | A dependent answer arriving before the answer it depends on |
| `attribution` | A policy that promises a requote shipping without one |
| `pool_bound` | More workers in flight than the pool allows |
| `tokens_accounted` | Shipping an answer that no worker ever actually computed |
| `snapshot_coherence` | Answering against a different data version than the one the request was pinned to |

### Measuring the classifier

Everything above tests the *scheduler*. The classifier needs a different
instrument, because its failures are judgements rather than bugs.

`python -m chatsched eval` runs 38 labelled messages — each against a
realistic live request set, 14 of them marked genuinely ambiguous — through
real Haiku and reports per-relationship precision and recall, a confusion
matrix, and the thing §4 actually claims: **which direction the errors go.**

Two measurement details matter more than the headline number:

- **It repeats.** The classifier is not deterministic; identical passes over
  the same 38 cases score between 95% and 97%. A single pass is an anecdote,
  so `--repeats` runs the whole set several times and reports the spread. The
  variance is not a footnote: it was 92–97% before §4's chat context, which is
  how the missing input was found in the first place.
- **It separates the hard cases.** A good score on "never mind the Taiwan one"
  hides a bad one on "no". Clear-cut and ambiguous cases are reported apart.

The labels are my judgement, which is the honest limitation: this measures
agreement with me, not ground truth.

What it found is in §4, and it changed the design — which is the point of
building it rather than asserting the thresholds were fine.

### Randomised testing

> **Property-based testing** (or *fuzzing*) — instead of writing example cases
> by hand, you generate thousands of random inputs and assert that your
> invariants hold on every one. Hand-written tests cover the failures you
> thought of; this covers the ones you didn't.

Because the Sequencer is a pure function (§5) and the simulator is
deterministic, a randomly generated chat can be replayed in well under a
millisecond. So the test suite generates random message streams — arbitrary
mixes of new questions, amendments, cancels, duplicates and references, at
arbitrary times, under all three policies and every pool size — and asserts all
nine invariants on each. Fifteen thousand such chats replay in about forty
seconds, without a single network call:

```
$ HYPOTHESIS_PROFILE=soak pytest tests/test_fuzz.py
```

**This found four real bugs that the hand-written tests did not**, all of them
listed in §8: the orphaned follow-up, the delivered-then-cancelled parent, the
pool overrun in the ready window, and the stale answer surviving that same
window. It also falsified two of the invariants as too crude — which was worth
as much, because it forced them to state the property precisely rather than
approximately.

### A correctness property you can break on command

Claiming "staleness is always zero" is weak. Being able to make it non-zero on
demand is strong.

The stop-signal race in §7b is modelled faithfully: a worker already writing
its answer finishes regardless. So running the same scenario with version
checking disabled ships the obsolete answer — and the invariant checker
catches it, along with a second violation noticing that the delivered answer
was never computed by any worker. Turn the check back on and both return to
zero.

That is a flag, not a thought experiment:

```
$ python -m chatsched run --scenario late_amend                      # stale: 0 ✓
$ python -m chatsched run --scenario late_amend --no-version-gate    # stale: 1 ✗
```

The second command prints the violations and the stale answer that caused
them. Both runs are also asserted as tests, so the demonstration cannot
quietly stop working: one test requires the gate to hold, and the other
requires it to fail when switched off. A safety mechanism nobody has ever
watched fail is indistinguishable from one that does nothing.

### Crash recovery, demonstrated the same way

The same standard applies to §14's claim that recovery falls out of the
Sequencer being a pure fold. Asserting it is cheap; showing it is not:

```
$ python -m chatsched run --scenario transcript --journal runs/t.jsonl
$ python -m chatsched recover runs/t.jsonl --crash-after 6

  15 events in runs/t.jsonl
  recovered 3 requests under DEPENDENCY_ORDER, pool 3

    r1  v2  DELIVERED  Ana   What's my exposure to a Taiwan escalation?
    r2  v1  DELIVERED  Ben   Is NVDA still my biggest position?
    r3  v1  DELIVERED  Ben   And the second biggest?

  crash after event 6 of 15, recover, carry on:
    ✓ identical to the run that never crashed
```

The test suite is stricter than the demo. For every scenario it kills the run
after **every** event in turn — not one chosen point — recovers from the log,
feeds it the events it had not yet seen, and asserts the final state is
identical to the run that never died. The fuzzer then does the same to
randomly generated chats at a random crash point.

That last part matters more than it looks. Recovery that produces a *plausible*
state is worse than no recovery, because nobody notices it is wrong.

### Scenario suite

Nine replayable fixtures, each targeting one failure mode. Every scenario runs
under all three policies, with all invariants asserted.

| Scenario | What it targets |
|---|---|
| The §1 transcript | Everything at once; the headline demo |
| Burst of six simultaneous questions | Parallelism and pool saturation |
| Slow Q1, fast Q2 | Head-of-line blocking, in isolation |
| Amendment lands mid-tool-call | A change at the worst possible moment |
| Cancel after the draft is written | Wasted-spend accounting |
| Premise replaced mid-flight, then again after delivery | Throwing work away, and the replacement notice |
| Two members, same words | Deduplication must not cross a portfolio boundary |
| A genuinely busy chat | Every relationship at once, including the ones that cost nothing |
| Amendment during the final draft | The stop-signal race; the version-check demo |

Tools are mocked with **controllable latency** — the simulator decides exactly
when a tool call returns. This is a deliberate upgrade over real tools rather
than a shortcut: reproducing "the amendment arrives *during* the tool call"
requires controlling precisely that, and a real API makes the race
unreproducible.

Workers also have a **live mode** that makes real `claude-opus-5` calls against
a portfolio fixture, with real `claude-haiku-4-5` triage — so the demo shows
the architecture doing the actual job, not just moving tokens around. Scenario
replay uses mocks for determinism; live mode is for the demonstration.

---

## 11. Assumptions

Stated as assumptions because several are load-bearing, and a reader should be
able to reject one and immediately know what breaks.

1. **Chats are independent.** No ordering requirement across chats. *(If false: the Sequencer stops being per-chat, and the no-collisions property goes with it.)*
2. **Who the chat is for is explicit, not inferred.** The brief says "a group chat" without saying whose portfolio is being discussed, and the answer changes the design. If everyone in the chat shares one portfolio — a household, an investment club, a member and their advisor — then two people asking the same question really is one piece of work. If each person has their own portfolio, it is not: identical words are different questions, and answering both with one answer would disclose one member's positions to another. Rather than guess, each request carries the member it is about; `DUPLICATE` requires a match, and a chat that sets no member is treated as having one shared context. *(If this were left implicit, the deduplication in §4 would be a data-disclosure bug rather than an optimisation.)*
3. **Within a chat, the only real ordering requirement is dependency.** The thesis of §3, and the most important assumption here. *(If false — say a compliance rule demands literal arrival order — `STRICT_FIFO` already exists as a policy and we pay the blocking knowingly.)*
4. **Tool calls cannot be interrupted once started.** Conservative. *(If false, cancellation gets cheaper and the `TOOL_CALL` column of §7 simplifies.)*
5. **Model calls can be interrupted.** True over HTTP — though §7b exists precisely because interruption can still lose the race.
6. **The classifier is fallible,** and its errors are made asymmetric by design (§4).
7. **Answer volume per chat is low enough that acknowledgements are not spam.** *(If false, acknowledgements collapse into a single edited status message.)*
8. **Single process.** The event log is the boundary where splitting across machines would go.

---

## 12. What is built, and what is only designed

The cut line is itself a design decision, so it is stated rather than hidden.

Not all of it was planned. The fuzzer and the classifier eval each found things
that *changed* the design rather than confirming it — four scheduling bugs, and
the discovery that §4's confidence mechanism does not work — and the list of
what is built grew as a result. The cut line itself still holds:
`MERGE`/`SPLIT`, `BOUNDED_REORDER` and cross-chat scheduling are not built, for
the reasons given below.

### Built

- Request register, lifecycle, dependency links, and the Sequencer as a pure function
- Worker pool, change inboxes, checkpoint handling, version checking
- Triage over the six relationships of §4, with the confidence asymmetry, and every decision auditable
- Delivery: attribution, acknowledgements, addenda, replacement notices
- Three policies, nine scenarios, the full metrics table
- Nine executable invariants, a randomised fuzzer, and the version-check demonstration
- A labelled eval for the classifier (§4), which changed the design rather than confirming it
- Live mode: real Haiku triage and real Opus workers against a portfolio fixture

### Designed, not built — and why

| Omitted | Reason |
|---|---|
| `MERGE` / `SPLIT` relationships | The highest implementation cost in the §7 matrix — folding two running workers together, or spawning a second out of one — for the rarest real cases, and the two categories a small classifier is least reliable at telling apart. The matrix slot is designed; the code is not written. Adding them is additive, not structural. |
| `BOUNDED_REORDER(k)` policy | Ship out of order, but never more than *k* places ahead of the oldest outstanding question. A real idea; three policies already prove the argument. |
| Fair sharing across chats | One very busy chat can currently monopolise the worker pool. A per-chat cap is the obvious knob; real fair-share scheduling is not built. |
| Real Slack or Discord integration | Authentication and webhooks are not what the brief grades. The replay harness demonstrates the same system better. |
| A database behind the event log | The append-only log is enough to rebuild any chat (§14). Indexing, retention and compaction are real work, and none of it changes a scheduling decision. |
| Retries, backoff, rate limiting | Real, orthogonal, and well understood. |

---

## 13. Known failure modes

Listed because a design document that claims no weaknesses is not describing a
real system.

- **Genuinely ambiguous messages.** *"Actually, Q3"* may be an amendment or a new question; a human would ask which. The §4 bias mitigates this; it does not solve it. Across repeated eval runs the residual errors are not scattered — they land on the same three or four messages every time:

  | Message | Labelled | Predicted |
  |---|---|---|
  | *"what about bonds?"* | `NEW` | `AMEND` |
  | *"What's my exposure to Taiwan?"* (Cara, after Ana) | `DUPLICATE` | `NEW` |

  Two others — *"is that still running?"* and *"drop it"* — were on this list
  until the classifier was given the recent chat (§4), which fixed both. What
  remains is one genuinely ambiguous message and one case of under-merging,
  which is the recoverable direction.

- **Questions about the system are not a category.** *"Is that still running?"*, *"did you get that?"*, *"how long will this take?"* are not portfolio questions and not chit-chat, and the taxonomy has no slot for them. With the chat context of §4 they now land on `IGNORE` reliably, which is the safe reading — no worker is dispatched to answer a question about the scheduler — but the member still gets nothing back. The Sequencer holds the answer and needs no model to produce it. A `STATUS` relationship would close this; it is not built, and it is the change I would make next to §4.
- **The ready-but-undelivered window is handled, not eliminated.** A change landing between "answer ready" and "answer shipped" now correctly discards the pending answer (§8), but the reader may still see an acknowledgement followed by a different answer than they were expecting. Correct, mildly disorienting.
- **Cross-chat pool starvation.** One very busy chat can monopolise the workers. Per-chat caps exist; fair sharing does not (§12).
- **Dependency cycles from classifier error.** Detected and broken (§8), at the cost of weaker ordering for that pair.
- **Unbounded chat history.** The live request set is bounded and the classifier's input is bounded by construction (§8), but a very long-lived chat will eventually need its transcript summarised.
- **Member identity is trusted, not verified.** The system keeps one member's answers away from another (§11), but it takes the member on the inbound message at face value. Real access control belongs at the chat boundary, not here.
- **The classifier is the weakest link.** Everything downstream depends on it relating messages to requests correctly, and that is a genuinely hard judgement. The asymmetric thresholds make its errors recoverable rather than destructive, and `--explain` makes every decision auditable — but neither makes it right.

---

---

## 14. How far from production is this?

Honestly: it is a working prototype and a design artifact, not a system you
could put in front of members on Monday. The scheduling core is in good shape;
almost everything around it is deliberately absent. Being specific about which
is which seems more useful than a verdict.

| Concern | State | What is missing |
|---|---|---|
| **Scheduling correctness** | ready | Nine invariants, 15,000 randomised chats per soak run, every relationship and policy exercised. |
| **Behaviour under change** | ready | The §7 matrix is implemented and tested, including the cases that arrive too late. |
| **Classification quality** | measured | 95% agreement across repeated runs — roughly 1 message in 20 is misread. Tolerable because the errors are *shaped* to be recoverable, not because they are rare: `IGNORE` and `CANCEL` are exact, and what remains is under-merging. The residual misses are listed in §13. |
| **Surviving a restart** | ready | Every event is written to an append-only log before it is handled, and replaying the log rebuilds the chat. Tested by killing the run after *every* event in every scenario, recovering, and asserting the final state is identical. |
| **Sustained load** | missing | No backpressure. If messages arrive faster than the pool drains, the queue grows without limit and nobody is told. |
| **Many chats at once** | missing | One Sequencer per chat, but nothing supervises them. A single busy chat can take the whole worker pool. |
| **Who may see what** | partial | Members' questions and answers are kept apart, but the member on an inbound message is taken at face value. Real access control belongs at the chat boundary. |
| **Cost control** | partial | Waste is measured but not acted on. No budget ceiling, no debouncing of someone typing three corrections in four seconds. |
| **Operability** | missing | No tracing, no metrics export, no alerting. The event log is the only forensic surface. |
| **Real chat integration** | missing | No Slack or Discord transport. Deliberate — see §12. |

The pattern is deliberate: everything marked ready is something the brief asked
about, and everything missing is infrastructure that is well understood and
would have crowded out the part being judged.

### What I would build next, in order

1. **Backpressure.** The one missing piece that turns a busy day into an outage
   rather than a slowdown.
2. **Debouncing changes.** The system already measures wasted spend; it should
   act on it instead of only reporting it.
3. **Fair sharing across chats.** Per-chat caps are the obvious knob; proper
   scheduling is not written.
4. **Exactly-once delivery across a restart.** Recovery rebuilds *state*
   correctly, but the commands the fold regenerates — including answers that
   already reached the chat — are discarded rather than reconciled. A worker
   that was mid-flight when the process died is simply lost work. Getting this
   right needs the delivery journal consulted on replay, which is a day's work
   and was not the part being judged.
5. **Streaming delivery** — and an answer to the question it raises: what does
   "this replaces my earlier answer" mean when half of that answer has already
   been read? I do not know, and that is the most interesting open question here.

---

## 15. Questions you might reasonably ask

Each of these is a decision that could have gone the other way, with the reason
it did not.

**Why is the Sequencer a pure function? That looks like dogma.**
It buys three specific things. The claim in §5 — that the shared-state component
does no inference — stops being a promise and becomes something the shape of the
code enforces. The races in §8 cannot occur, because one event is handled start
to finish. And it is the only reason the fuzzer exists: a function with no I/O
can be driven tens of thousands of times per second. The cost is that something
else must carry out the commands, and that seam is where two real bugs appeared
— live mode silently ignoring fields the Sequencer sent it. The invariants now
check for exactly that.

**Isn't "ordered" just FIFO? You redefined the requirement to make it easier.**
That is the fair challenge. FIFO is one *way* to buy four properties, not the
property itself. Ask what a reader needs — to know what an answer answers, what
is still outstanding, which answers are live, and that a follow-up arrives after
what it follows — and only the last involves order at all. It is also
falsifiable rather than rhetorical: `STRICT_FIFO` is implemented, so if literal
order is genuinely required you switch one policy and pay 21 seconds of
head-of-line delay knowingly. The argument is that you should not pay it by
accident.

**Why version numbers rather than cancelling properly?**
Because reliably stopping distributed work is hard and ignoring out-of-date work
is trivial. The stop signal races the work finishing and sometimes loses — not
hypothetically; it is modelled and demonstrable. We still try to stop superseded
workers to save money, but correctness never depends on it landing.

**Five relationships. Why not three, or nine?**
Each earns its place by producing behaviour the others cannot: `REFERENCE`
creates the only dependency edge, `DUPLICATE` is the only one that avoids paying
twice, `IGNORE` is the only one that costs nothing at all. Widening, narrowing
and replacing a question collapse into one class with one boolean, because the
only thing that differs is whether the research survives. `MERGE` and `SPLIT`
carry the matrix's highest implementation cost for its rarest cases, and are the
two a small classifier is least able to tell apart.

**You measured your own mitigation and it failed. Why keep it?**
Because the thresholds still catch a genuinely low-confidence tail and cost
almost nothing. What changed is what the document *claims*: they are no longer
described as the mechanism that makes errors asymmetric, because the data says
they are not. The replacement is a different signal — whether the message names
what it refers to. "Never mind the Taiwan one" does; a bare "no" does not, and
might not even be addressed to the agent.

**Why mock the tools? Real ones would be more convincing.**
The opposite, for the thing being tested. Reproducing "the amendment arrives
*during* the tool call" requires controlling exactly when that call returns; a
real API makes the race unreproducible and the scenario becomes a coin flip. So
replay is mocked and deterministic, and a live mode runs the same Sequencer
against real Haiku and Opus so the architecture can be seen doing the actual
job. Both exist because they answer different questions.

**What is the weakest part?**
The classifier, and it is not close. Everything downstream depends on it, and at
95% agreement roughly one message in twenty is misread. That is why the errors
are shaped rather than merely counted, why destructive readings ask instead of
acting, and why every decision is logged with its confidence and reasoning. The
second weakest is that it is one process with no backpressure.

**What surprised you?**
That the fuzzer found four real bugs the hand-written tests did not, and that
two of them lived in the window this document had already named as a known
weakness — identified in prose, unhandled in code. And that my own agent had
head-of-line blocking in its ingest loop: a stalled classifier call would block
every later message, in a system built to remove exactly that.

**What would you change about the approach, not the code?**
I would build the eval first. Measuring the classifier changed a design
decision; writing thresholds down from intuition and defending them in prose did
not. The same is probably true of the ordering policies — the comparison table
settled arguments the design document could only assert.


---
## Glossary

| Term | Meaning |
|---|---|
| **Agent** | A program that answers questions using a language model, calling tools to look things up along the way. |
| **Tool call** | The agent fetching external data — holdings, prices. The slow, expensive step. |
| **Token** | The unit language models are billed in, roughly a piece of a word. "Wasted tokens" are ones we paid for and threw away. |
| **Message** | One line somebody typed in the chat. |
| **Request** | One piece of work the agent owes an answer for. Several messages can relate to one request. |
| **Parallel** | Several requests being worked on at the same time. |
| **Worker** | One running job, answering one request. |
| **Pool / slot** | The bounded set of workers allowed to run at once; a request waits for a free slot. |
| **FIFO** | "First in, first out" — strict arrival order. |
| **Head-of-line blocking** | A slow item at the front of a queue delaying everything behind it. |
| **Deadlock** | A standstill where everything is waiting on something else and nothing can proceed. |
| **Race** | Two things happening at once where the outcome depends on which lands first. |
| **Version** | A counter on each request, incremented whenever its question changes. Used to recognise and ignore out-of-date results. |
| **Stale** | A result computed against a question that has since changed. |
| **Checkpoint** | A defined safe moment in a worker's run where it reads pending changes. |
| **Classifier** | A model that sorts each message into one of the six relationships of §4. |
| **Confidence** | The classifier's own estimate, 0 to 1, of whether it is right. |
| **Pure function** | Code that takes input and returns output without touching files, network, or the clock. Same input, same output, every time. |
| **Deterministic** | Produces exactly the same result every run — which is what makes replay and fuzzing possible. |
| **Invariant** | Something that must be true of the system at all times. |
| **Property-based testing / fuzzing** | Generating thousands of random inputs and asserting the invariants hold on all of them. |
| **p99** | The 99th percentile — the value 99 of 100 measurements fall under. |
