"""Command line entry point.

    python -m chatsched run      --scenario transcript --policy DEPENDENCY_ORDER
    python -m chatsched compare  --scenario transcript
    python -m chatsched compare  --all
    python -m chatsched compare  --scenario supersede --no-version-gate
    python -m chatsched live     --scenario transcript        (real model calls)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import invariants, metrics
from .policies import POLICIES, get_policy
from .sim import SCENARIO_DIR, Simulation, load_scenario

ALL_POLICIES = ["SERIAL", "STRICT_FIFO", "DEPENDENCY_ORDER"]


def _run(name: str, policy_name: str, pool_size: int, version_gate: bool):
    messages, work, title = load_scenario(name)
    policy = get_policy(policy_name)
    sim = Simulation(
        messages=messages,
        work=work,
        policy=policy,
        pool_size=pool_size,
        version_gate=version_gate,
    )
    result = sim.run()
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
        args.scenario, args.policy, args.pool_size, not args.no_version_gate
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
    return 1 if violations and not args.no_version_gate else 0


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


def _load_dotenv() -> None:
    """Read .env into the environment, without taking a dependency for it.

    The README tells you to put your key in .env; without this, nothing would
    actually read it. Existing environment variables always win.
    """
    path = Path(__file__).resolve().parent.parent / ".env"
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
    p_run.set_defaults(func=cmd_run)

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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
