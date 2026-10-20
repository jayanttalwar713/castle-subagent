"""The command line: exit codes, and the things a reviewer types first.

These are smoke tests with teeth. The exit code is the part a CI job or a
reviewer's shell actually reads, and it is the part most likely to silently
lie — `run` must fail when invariants fail, and `live` must refuse to start
rather than produce a confusing auth traceback.
"""

from __future__ import annotations

import os

import pytest

from chatsched import cli


def test_run_reports_success_and_prints_a_transcript(capsys):
    assert cli.main(["run", "--scenario", "transcript"]) == 0
    out = capsys.readouterr().out
    assert "all invariants hold" in out
    assert "ANSWER" in out
    assert "DEPENDENCY_ORDER" in out


@pytest.mark.parametrize("policy", ["SERIAL", "STRICT_FIFO", "DEPENDENCY_ORDER"])
def test_every_policy_runs_from_the_command_line(policy, capsys):
    assert cli.main(["run", "--scenario", "burst", "--policy", policy]) == 0
    assert policy in capsys.readouterr().out


def test_compare_prints_a_row_per_policy(capsys):
    assert cli.main(["compare", "--scenario", "head_of_line"]) == 0
    out = capsys.readouterr().out
    for policy in ("SERIAL", "STRICT_FIFO", "DEPENDENCY_ORDER"):
        assert policy in out


def test_compare_all_covers_every_scenario(capsys):
    assert cli.main(["compare", "--all"]) == 0
    out = capsys.readouterr().out
    assert "all invariants hold" in out
    assert out.count("SERIAL") >= 5


def test_disabling_version_gating_surfaces_the_violation(capsys):
    """The demo has to actually say something went wrong, or it proves
    nothing. §10."""
    cli.main(["run", "--scenario", "late_amend", "--no-version-gate"])
    out = capsys.readouterr().out
    assert "no_stale" in out
    assert "invariant violations" in out


def test_the_same_scenario_is_clean_with_gating_on(capsys):
    assert cli.main(["run", "--scenario", "late_amend"]) == 0
    assert "all invariants hold" in capsys.readouterr().out


def test_an_unknown_policy_is_rejected():
    with pytest.raises(SystemExit):
        cli.main(["run", "--policy", "NONSENSE"])


def test_an_unknown_scenario_fails_loudly():
    with pytest.raises(FileNotFoundError):
        cli.main(["run", "--scenario", "no-such-scenario"])


def test_live_refuses_to_start_without_a_key(monkeypatch, tmp_path, capsys):
    """Better than an auth traceback from three frames deep."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    assert cli.main(["live", "--scenario", "transcript"]) == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_dotenv_is_read_and_parsed(monkeypatch, tmp_path):
    """The README tells you to put the key in .env, so something has to read
    it. Comments, blank lines, quotes and junk all have to survive."""
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "ANTHROPIC_API_KEY=from-dotenv\n"
        'QUOTED="quoted-value"\n'
        "MALFORMED\n"
    )
    for name in ("ANTHROPIC_API_KEY", "QUOTED", "MALFORMED"):
        monkeypatch.delenv(name, raising=False)

    cli._load_dotenv(env)

    assert os.environ["ANTHROPIC_API_KEY"] == "from-dotenv"
    assert os.environ["QUOTED"] == "quoted-value"
    assert "MALFORMED" not in os.environ


def test_an_exported_key_beats_the_dotenv_file(monkeypatch, tmp_path):
    """Otherwise a stale .env would silently shadow the key you just
    exported, and you would debug the wrong account."""
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=from-dotenv\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-environment")

    cli._load_dotenv(env)

    assert os.environ["ANTHROPIC_API_KEY"] == "from-environment"


def test_a_missing_dotenv_is_not_an_error(tmp_path):
    cli._load_dotenv(tmp_path / "nope.env")


# ------------------------------------------------------- recovery (§14) --


def test_run_writes_an_event_log_when_asked(tmp_path, capsys):
    log = tmp_path / "events.jsonl"
    assert cli.main(["run", "--scenario", "transcript", "--journal", str(log)]) == 0
    assert "event log:" in capsys.readouterr().out
    assert log.exists() and log.read_text().count("\n") > 10


def test_recover_rebuilds_the_chat_and_says_so(tmp_path, capsys):
    log = tmp_path / "events.jsonl"
    cli.main(["run", "--scenario", "transcript", "--journal", str(log)])
    capsys.readouterr()

    assert cli.main(["recover", str(log)]) == 0
    out = capsys.readouterr().out
    assert "identical to the run that never crashed" in out
    assert "DELIVERED" in out


def test_recover_accepts_an_explicit_crash_point(tmp_path, capsys):
    log = tmp_path / "events.jsonl"
    cli.main(["run", "--scenario", "busy_chat", "--journal", str(log)])
    capsys.readouterr()

    assert cli.main(["recover", str(log), "--crash-after", "3"]) == 0
    assert "crash after event 3" in capsys.readouterr().out


def test_recover_refuses_a_log_that_is_not_there(tmp_path, capsys):
    """Exit 2, not a traceback — the exit code is what a shell reads."""
    assert cli.main(["recover", str(tmp_path / "nope.jsonl")]) == 2
    assert "no such event log" in capsys.readouterr().err


def test_recover_refuses_an_empty_log(tmp_path, capsys):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert cli.main(["recover", str(empty)]) == 2
    assert "no events" in capsys.readouterr().err


def test_quality_refuses_without_a_key(monkeypatch, capsys):
    """Same contract as `eval`: exit 2 rather than an auth traceback."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_load_dotenv", lambda *a, **k: None)
    assert cli.main(["quality"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
