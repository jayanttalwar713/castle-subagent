"""Command line entry point.

    python -m chatsched run      --scenario transcript --policy DEPENDENCY_ORDER
    python -m chatsched compare  --scenario transcript
    python -m chatsched compare  --all
    python -m chatsched compare  --scenario supersede --no-version-gate
    python -m chatsched live     --scenario transcript        (real model calls)
    python -m chatsched run      --scenario burst --journal runs/burst.jsonl
    python -m chatsched recover  runs/burst.jsonl --crash-after 6
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import invariants, metrics
from .journal import EventLog, read as read_log, replay, state_digest
from .policies import POLICIES, get_policy
from .sim import SCENARIO_DIR, Simulation, load_scenario

ALL_POLICIES = ["SERIAL", "STRICT_FIFO", "DEPENDENCY_ORDER"]


def _run(name: str, policy_name: str, pool_size: int, version_gate: bool,
         journal_path: str | None = None):
    messages, work, title = load_scenario(name)
    policy = get_policy(policy_name)
    log = EventLog(journal_path) if journal_path else None
    sim = Simulation(
        messages=messages,
        work=work,
        policy=policy,
        pool_size=pool_size,
        version_gate=version_gate,
        journal=log,
    )
    result = sim.run()
    if log is not None:
        log.close()
    violations = invariants.check(
        result.sequencer, result.transcript, policy, pool_size
    )
    stats = metrics.compute(
        result.sequencer,
        result.transcript,
        policy,
        result.sequencer_times,
        violations=len(violations),
    )
    return title, result, violations, stats


def cmd_run(args) -> int:
    title, result, violations, stats = _run(
        args.scenario, args.policy, args.pool_size, not args.no_version_gate,
        journal_path=args.journal,
    )
    print(f"\n{title}")
    print(f"policy: {stats.policy}   pool: {args.pool_size}"
          f"{'   version gating: OFF' if args.no_version_gate else ''}\n")
    print(result.transcript.render())
    print()
    print(metrics.table([stats]))
    if violations:
        print("\ninvariant violations:")
        for v in violations:
            print(f"  ✗ {v}")
    else:
        print("\n✓ all invariants hold")
    if args.journal:
        print(f"\nevent log: {args.journal}   "
              f"(replay it with: python -m chatsched recover {args.journal})")
    return 1 if violations and not args.no_version_gate else 0


def cmd_recover(args) -> int:
    """Rebuild a chat's state from its event log, and show that dying partway
    through changes nothing (DESIGN.md §14).

    The Sequencer is a pure fold over events, so this is the whole of crash
    recovery: read the log, fold it, carry on.
    """
    path = Path(args.log)
    if not path.exists():
        print(f"no such event log: {path}", file=sys.stderr)
        return 2

    events = list(read_log(path))
    if not events:
        print(f"{path} contains no events", file=sys.stderr)
        return 2

    policy = get_policy(args.policy)
    whole = replay(events, pool_size=args.pool_size, policy=policy)
    print(f"\n{len(events)} events in {path}")
    print(f"recovered {len(whole.requests)} requests under "
          f"{policy.name}, pool {args.pool_size}\n")

    for rid, req in sorted(whole.requests.items()):
        who = ", ".join(req.askers)
        print(f"  {rid}  v{req.version}  {req.stage.value:<10} {who:<14} {req.requote()}")

    cut = args.crash_after
    if cut is None:
        cut = len(events) // 2
    cut = max(0, min(cut, len(events)))

    survivor = replay(events[:cut], pool_size=args.pool_size, policy=get_policy(args.policy))
    for ev in events[cut:]:
        survivor.handle(ev)

    same = state_digest(survivor) == state_digest(whole)
    print(f"\ncrash after event {cut} of {len(events)}, recover, carry on:")
    print(f"  {'✓ identical to the run that never crashed' if same else '✗ STATE DIVERGED'}")
    return 0 if same else 1


def cmd_compare(args) -> int:
    scenarios = (
        sorted(p.stem for p in SCENARIO_DIR.glob("*.json"))
        if args.all
        else [args.scenario]
    )
    broken = 0
    for name in scenarios:
        rows = []
        title = name
        for policy_name in ALL_POLICIES:
            title, _result, violations, stats = _run(
                name, policy_name, args.pool_size, not args.no_version_gate
            )
            rows.append(stats)
            broken += len(violations)
        print(f"\n{title}")
        print(metrics.table(rows))
    if args.no_version_gate:
        print(
            f"\nversion gating OFF — {broken} invariant violation(s). "
            "Turn it back on and this returns to zero."
        )
    else:
        print(f"\n{'✓ all invariants hold' if not broken else f'✗ {broken} violation(s)'}")
    return 0


def _load_dotenv(path: Path | None = None) -> None:
    """Read .env into the environment, without taking a dependency for it.

    The README tells you to put your key in .env; without this, nothing would
    actually read it. Existing environment variables always win.
    """
    path = path or Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def cmd_live(args) -> int:
    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set.\n"
            "  cp .env.example .env   and put your key in it, or export it directly.\n"
            "Everything except `live` runs without a key.",
            file=sys.stderr,
        )
        return 2

    from .live import run_live  # imported lazily: needs the key above

    messages, work, title = load_scenario(args.scenario)
    return run_live(messages, title, args.policy, args.pool_size, args.explain)


def cmd_eval(args) -> int:
    """Measure the classifier rather than asserting things about it (§10)."""
    import asyncio

    from .evaluate import load_cases, render, run_eval

    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; the eval needs real model calls.",
              file=sys.stderr)
        return 2

    cases = load_cases()
    print(f"evaluating triage on {len(cases)} labelled cases "
          f"({sum(1 for c in cases if c.hard)} marked genuinely ambiguous)…")
    report = asyncio.run(run_eval(cases, repeats=args.repeats))
    print(render(report, show_errors=not args.quiet))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chatsched")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--scenario", default="transcript",
                       help="fixture name in scenarios/, or a path")
        p.add_argument("--pool-size", type=int, default=3)
        p.add_argument("--no-version-gate", action="store_true",
                       help="accept stale worker results, to show the "
                            "staleness invariant failing on command")

    p_run = sub.add_parser("run", help="replay one scenario under one policy")
    common(p_run)
    p_run.add_argument("--policy", default="DEPENDENCY_ORDER", choices=sorted(POLICIES))
    p_run.add_argument("--journal", metavar="PATH",
                       help="append every event to a durable log, so the run "
                            "can be recovered with `recover`")
    p_run.set_defaults(func=cmd_run)

    p_rec = sub.add_parser("recover",
                           help="rebuild a chat from its event log (§14)")
    p_rec.add_argument("log", help="path to a .jsonl event log")
    p_rec.add_argument("--policy", default="DEPENDENCY_ORDER", choices=sorted(POLICIES),
                       help="must match the run that produced the log")
    p_rec.add_argument("--pool-size", type=int, default=3)
    p_rec.add_argument("--crash-after", type=int, metavar="N",
                       help="pretend the process died after N events "
                            "(default: halfway)")
    p_rec.set_defaults(func=cmd_recover)

    p_cmp = sub.add_parser("compare", help="all three policies, side by side")
    common(p_cmp)
    p_cmp.add_argument("--all", action="store_true", help="every scenario")
    p_cmp.set_defaults(func=cmd_compare)

    p_live = sub.add_parser("live", help="real Haiku triage and Opus workers")
    common(p_live)
    p_live.add_argument("--policy", default="DEPENDENCY_ORDER", choices=sorted(POLICIES))
    p_live.add_argument("--explain", action="store_true",
                        help="print every triage decision and its confidence")
    p_live.set_defaults(func=cmd_live)

    p_eval = sub.add_parser("eval", help="measure the triage classifier")
    p_eval.add_argument("--repeats", type=int, default=1,
                        help="run the whole set N times; the classifier is "
                             "not deterministic and a single pass is noisy")
    p_eval.add_argument("--quiet", action="store_true",
                        help="summary only, without the per-case disagreements")
    p_eval.set_defaults(func=cmd_eval)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
