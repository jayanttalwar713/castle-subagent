"""Live mode: the same Sequencer, driven by asyncio against real models.

The Sequencer is a pure synchronous function, so it does not change at all
between simulation and production. Only the thing that *drives* it changes —
here, a real clock, real Haiku triage, and real Opus workers with a portfolio
tool. That substitutability is the point of keeping it pure.

Workers never call the Sequencer directly. They push events onto a queue that
the main loop drains, so `handle()` is still only ever entered from one place,
one event at a time. The "single-threaded per chat, so no locks" property of
DESIGN.md §5 survives contact with concurrency.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import invariants, metrics
from .delivery import Transcript
from .model import (
    Abort,
    Classified,
    Completed,
    Deliver,
    Dispatch,
    Failed,
    Inject,
    Relationship,
    Stage,
    StageChanged,
)
from .policies import get_policy
from .sequencer import Sequencer
from .triage import InboundMessage

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


@dataclass
class LiveRun:
    """One worker, one request-version, plus its mutation inbox."""

    request_id: str
    version: int
    task: asyncio.Task | None = None
    inbox: list[tuple[int, str]] = field(default_factory=list)


async def _main(
    messages: list[InboundMessage],
    title: str,
    policy_name: str,
    pool_size: int,
    explain: bool,
) -> int:
    from .llm import LLMTriage, LLMWorker  # needs ANTHROPIC_API_KEY

    policy = get_policy(policy_name)
    seq = Sequencer(pool_size=pool_size, policy=policy)
    transcript = Transcript()
    triage = LLMTriage()
    worker = LLMWorker()

    events: asyncio.Queue = asyncio.Queue()
    runs: dict[str, LiveRun] = {}
    sequencer_times: list[float] = []
    started = time.monotonic()
    log: list[dict] = []

    def now() -> float:
        return time.monotonic() - started

    def feed(event) -> None:
        start = time.perf_counter()
        commands = seq.handle(event)
        sequencer_times.append(time.perf_counter() - start)
        for cmd in commands:
            apply(cmd)

    def apply(cmd) -> None:
        if isinstance(cmd, Dispatch):
            run = LiveRun(cmd.request_id, cmd.version)
            runs[cmd.request_id] = run
            run.task = asyncio.create_task(_work(run, cmd))
        elif isinstance(cmd, Abort):
            run = runs.get(cmd.request_id)
            # Best effort only. Version gating already makes the stale result
            # unusable, so nothing here is load-bearing (§7b).
            if run is not None and run.version == cmd.version and run.task:
                run.task.cancel()
        elif isinstance(cmd, Inject):
            if (run := runs.get(cmd.request_id)) is not None:
                run.inbox.append((cmd.version, cmd.change))
        elif isinstance(cmd, Deliver):
            transcript.record(cmd, now())
            print(transcript.lines[-1].render(), flush=True)
            log.append(
                {
                    "at": now(),
                    "kind": cmd.kind.value,
                    "request_id": cmd.request_id,
                    "version": cmd.version,
                    "to": list(cmd.to),
                    "re": cmd.re,
                    "body": cmd.body,
                }
            )

    async def _work(run: LiveRun, cmd: Dispatch) -> None:
        async def checkpoint(phase: str, results: tuple[str, ...] = ()):
            """A worker boundary: report progress, then drain the inbox."""
            if phase == "tool_call":
                await events.put(
                    StageChanged(run.request_id, run.version, Stage.TOOL_CALL, now())
                )
            elif phase == "after_tool":
                await events.put(
                    StageChanged(
                        run.request_id, run.version, Stage.DRAFTING, now(), results
                    )
                )
            if not run.inbox:
                return None
            pending, run.inbox = run.inbox, []
            run.version = pending[-1][0]  # adopt the newest version
            return " ".join(change for _v, change in pending)

        try:
            text, tokens, _results = await worker.answer(
                cmd.spec, checkpoint, cmd.retained_tool_results,
                cmd.as_of, cmd.member,
            )
        except asyncio.CancelledError:
            await events.put(
                Failed(run.request_id, run.version, "aborted", 0, now())
            )
            raise
        except Exception as exc:  # a real failure still owes the reader a word
            await events.put(
                Failed(run.request_id, run.version, str(exc)[:160], 0, now())
            )
            return
        await events.put(
            Completed(run.request_id, run.version, text, tokens, now(), cmd.as_of)
        )

    triage_failures: list[Exception] = []

    async def _ingest() -> None:
        """Replay the chat against a real clock, classifying as we go."""
        # What has already been said, in order. Triage needs it to tell a
        # reply to another member from an instruction to the agent — a bare
        # "no" is almost always the former (§4). Bounded inside aclassify.
        said: list[InboundMessage] = []
        for msg in messages:
            delay = msg.at - now()
            if delay > 0:
                await asyncio.sleep(delay)
            live = seq.live_view()
            try:
                decision = await triage.aclassify(msg, live, said)
            except Exception as exc:
                # One bad message must not abandon the rest of the chat. A
                # timeout on message three used to end the ingest loop, so
                # messages four onward were never read at all — silently.
                # Workers already degrade this way; triage should too.
                triage_failures.append(exc)
                print(f"[{now():6.1f}s] ! triage failed on {msg.author}'s "
                      f"message ({type(exc).__name__}); treating it as a new "
                      f"question", flush=True)
                decision = Classified(
                    msg_id=msg.msg_id, author=msg.author, text=msg.text,
                    at=now(), relationship=Relationship.NEW, member=msg.member,
                    confidence=0.0, fallback_applied=True,
                    reasoning=f"triage unavailable: {type(exc).__name__}",
                )
            said.append(msg)
            print(
                f"[{now():6.1f}s] ← {msg.author:<14} {'':17} {msg.text}",
                flush=True,
            )
            await events.put(decision)

    print(f"\n{title}")
    print(f"policy: {policy.name}   pool: {pool_size}   "
          f"triage: {triage.name}   workers: {worker.name}\n")

    ingest = asyncio.create_task(_ingest())
    idle_since: float | None = None
    while True:
        try:
            event = await asyncio.wait_for(events.get(), timeout=0.25)
        except asyncio.TimeoutError:
            # Surface an ingestion failure loudly. Without this the loop would
            # see "ingestion finished, nothing outstanding", declare success,
            # and print a clean invariant report over a run that did nothing.
            if ingest.done() and not ingest.cancelled() and ingest.exception():
                print(f"\ntriage failed: {ingest.exception()}", flush=True)
                for task in (r.task for r in runs.values() if r.task):
                    task.cancel()
                return 2
            # Done when ingestion has finished and nothing is outstanding.
            if ingest.done() and not seq.outstanding:
                if idle_since is None:
                    idle_since = now()
                elif now() - idle_since > 0.5:
                    break
            continue
        idle_since = None
        feed(event)

    for task in (r.task for r in runs.values() if r.task):
        task.cancel()

    if not seq.requests:
        print("\nno requests were created — nothing ran.", flush=True)
        return 2

    if messages and len(triage_failures) == len(messages):
        # Every single message failed. That is not a flaky call, it is a
        # broken configuration — a bad key, a wrong model name — and the run
        # must not report success just because the fallback kept it moving.
        print(f"\ntriage failed on every message ({triage_failures[0]!r}); "
              "the run is not meaningful.", flush=True)
        return 2

    violations = invariants.check(seq, transcript, policy, pool_size)
    stats = metrics.compute(
        seq, transcript, policy, sequencer_times, violations=len(violations)
    )

    print()
    print(metrics.table([stats]))
    if explain:
        print("\ntriage decisions")
        for d in triage.decisions:
            flag = "  ← fell back to NEW" if d["fallback"] else ""
            target = f" → {d['target']}" if d["target"] else ""
            print(
                f"  {d['author']:<6} {d['message'][:44]:<46} "
                f"{d['proposed']:<10}{target:<6} conf={d['confidence']:.2f}{flag}"
            )
            print(f"         {d['reasoning'][:100]}")
    if violations:
        print("\ninvariant violations:")
        for v in violations:
            print(f"  ✗ {v}")
    else:
        print("\n✓ all invariants hold")

    RUNS_DIR.mkdir(exist_ok=True)
    out = RUNS_DIR / f"live-{int(time.time())}.jsonl"
    with out.open("w") as fh:
        for entry in log:
            fh.write(json.dumps(entry) + "\n")
        for d in triage.decisions:
            fh.write(json.dumps({"triage": d}) + "\n")
    print(f"\nevent log: {out.relative_to(RUNS_DIR.parent)}")

    return 1 if violations else 0


def run_live(
    messages: list[InboundMessage],
    title: str,
    policy_name: str,
    pool_size: int,
    explain: bool,
) -> int:
    return asyncio.run(_main(messages, title, policy_name, pool_size, explain))
