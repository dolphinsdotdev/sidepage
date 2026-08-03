"""Smoke tests for the CLI shell itself.

These only check that the command tree is wired correctly (every command in
the v3 spec exists, `--help` works, required arguments are enforced) — they
do not test behavior, since there isn't any yet. See `sidepage.core` for the
unimplemented SDK surface these commands will eventually call.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sidepage.cli import app

runner = CliRunner()

# Every leaf command from the v3 spec, with enough placeholder args to
# satisfy required parameters, given as the argv list `sidepage` would
# receive.
LEAF_INVOCATIONS = [
    ["new", "myapp"],
    ["serve", "app.py"],
    ["serve", "app.py", "--anon"],
    ["serve", "app.py", "--token", "secret"],
    ["serve", "app.py", "--type", "code"],
    ["stop", "myapp"],
    ["promote", "myapp"],
    ["usage", "myapp"],
    ["inspect"],
    ["inspect", "myapp"],
    ["ls"],
    ["status", "myapp"],
    ["login"],
    ["account", "status"],
    ["account", "domain", "set", "example.com"],
]

HELP_TARGETS = [
    [],
    ["new", "--help"],
    ["serve", "--help"],
    ["stop", "--help"],
    ["promote", "--help"],
    ["usage", "--help"],
    ["inspect", "--help"],
    ["ls", "--help"],
    ["status", "--help"],
    ["login", "--help"],
    ["account", "--help"],
    ["account", "status", "--help"],
    ["account", "domain", "--help"],
    ["account", "domain", "set", "--help"],
]

# Commands that existed in v1 and were removed in the v3 migration — these
# should fail to parse (Click's "no such command"), not succeed.
REMOVED_COMMANDS = [
    ["run", "app.py"],
    ["whoami"],
    ["name", "check", "myapp"],
    ["keys", "create", "myapp"],
    ["keys", "list"],
    ["tunnel", "login"],
    ["tunnel", "status"],
]


def test_root_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "sidepage" in result.stdout


def test_root_no_args_shows_help() -> None:
    # `no_args_is_help=True` makes Click print help and exit 2 (a usage
    # error, not a crash) when no subcommand is given.
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Usage" in result.output


@pytest.mark.parametrize("argv", HELP_TARGETS, ids=lambda argv: " ".join(argv) or "root")
def test_help_exits_cleanly(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    expected_exit_code = 2 if not argv else 0
    assert result.exit_code == expected_exit_code, result.output


@pytest.mark.parametrize("argv", LEAF_INVOCATIONS, ids=lambda argv: " ".join(argv))
def test_leaf_command_wired_but_unimplemented(argv: list[str]) -> None:
    """Every leaf command should parse successfully and reach the
    `not_implemented` placeholder rather than failing argument parsing."""
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert "not yet implemented" in result.output


@pytest.mark.parametrize("argv", REMOVED_COMMANDS, ids=lambda argv: " ".join(argv))
def test_v1_command_no_longer_exists(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code != 0
    assert "No such command" in result.output


def test_serve_guardrail_flag_reports_its_own_placeholder() -> None:
    result = runner.invoke(app, ["serve", "app.py", "--guardrail", "config.yaml"])
    assert result.exit_code == 0, result.output
    assert "sidepage serve --guardrail" in result.output


def test_serve_anon_and_auth_token_combine() -> None:
    """--anon and --auth are orthogonal — an anonymous Quick Tunnel can
    still require a token."""
    result = runner.invoke(app, ["serve", "app.py", "--anon", "--auth", "token"])
    assert result.exit_code == 0, result.output
    assert "not yet implemented" in result.output


def test_serve_auth_oauth_is_still_a_valid_choice() -> None:
    """oauth is deferred/unimplemented, not removed from the CLI surface."""
    result = runner.invoke(app, ["serve", "app.py", "--auth", "oauth"])
    assert result.exit_code == 0, result.output
    assert "not yet implemented" in result.output
