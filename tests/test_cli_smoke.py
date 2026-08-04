"""Smoke tests for the CLI shell: argument parsing, help text, and command
wiring — not full behavior.

`serve`/`stop` are deliberately **not** exercised here beyond `--help` and
fast-failing error paths (bad target, unsupported flag combos). A valid
`serve` call now really blocks the process waiting for Ctrl+C/SIGTERM
(`sidepage.core.process.serve`) — running that in-process via `CliRunner`
would hang the test suite forever, since there's no way to deliver Ctrl+C
to code executing inside the test process itself. Real behavioral coverage
for `serve` (and `secrets`, which it depends on) lives in
`test_serve_integration.py`, which launches the CLI as a real subprocess
and can actually stop it afterward.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sidepage.cli import app

runner = CliRunner()


def _flat(text: str) -> str:
    """Collapse Rich's word-wrapped console output back to single spaces
    so substring assertions aren't at the mercy of where a line happened
    to break."""
    return " ".join(text.split())


@pytest.fixture(autouse=True)
def _isolated_sidepage_home(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Several commands here are real (`secrets`, `stop`, registry lookups)
    — redirect them at a throwaway directory so nothing touches the actual
    user's `~/.config`/`~/.local/state/sidepage`."""
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))

# Every leaf command that's still a pure placeholder, with enough
# placeholder args to satisfy required parameters.
LEAF_INVOCATIONS = [
    ["new", "myapp"],
    ["promote", "myapp"],
    ["inspect"],
    ["inspect", "myapp"],
    ["login"],
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
    ["secrets", "--help"],
    ["secrets", "set", "--help"],
    ["secrets", "list", "--help"],
    ["secrets", "remove", "--help"],
]

# Commands that existed in v1/v3 and were removed in a later migration —
# these should fail to parse (Click's "no such command"), not succeed.
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
    """Every still-placeholder leaf command should parse successfully and
    reach the `not_implemented` placeholder rather than failing argument
    parsing."""
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert "not yet implemented" in result.output


@pytest.mark.parametrize("argv", REMOVED_COMMANDS, ids=lambda argv: " ".join(argv))
def test_removed_command_no_longer_exists(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code != 0
    assert "No such command" in result.output


def test_serve_nonexistent_target_fails_fast() -> None:
    """No target on disk means `sidepage.core.target.detect_target_kind`
    raises before `serve` ever reaches its blocking loop — safe to run
    in-process."""
    result = runner.invoke(app, ["serve", "definitely-does-not-exist.py"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_serve_nonexistent_target_fails_fast_even_with_type_override() -> None:
    """Regression check: `--type` used to skip the existence check
    entirely in `sidepage.core.target.detect_target_kind`, so a valid-
    looking but nonexistent target with an explicit `--type` would sail
    past validation into the real blocking loop instead of failing."""
    result = runner.invoke(app, ["serve", "definitely-does-not-exist.py", "--type", "code"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_serve_guardrail_flag_rejected_before_touching_target() -> None:
    """--guardrail is rejected in `sidepage.core.process._validate_supported`
    before target detection even runs, so this is safe against a
    nonexistent target too — and proves the guardrail check really does
    run first."""
    result = runner.invoke(
        app, ["serve", "definitely-does-not-exist.py", "--guardrail", "config.yaml"]
    )
    assert result.exit_code == 0, result.output
    assert "isn't implemented" in _flat(result.output)
    assert "guardrail" in result.output.lower()


def test_serve_domain_rejected() -> None:
    """--domain (BYO Cloudflare) isn't implemented — rejected before
    target detection, same as --guardrail."""
    result = runner.invoke(
        app, ["serve", "definitely-does-not-exist.py", "--domain", "example.com"]
    )
    assert result.exit_code == 0, result.output
    assert "isn't implemented" in _flat(result.output)


def test_serve_non_local_scope_rejected() -> None:
    result = runner.invoke(
        app, ["serve", "definitely-does-not-exist.py", "--scope", "web"]
    )
    assert result.exit_code == 0, result.output
    assert "isn't implemented" in _flat(result.output)


def test_serve_network_and_oauth_auth_rejected() -> None:
    for tier in ("network", "oauth"):
        result = runner.invoke(
            app, ["serve", "definitely-does-not-exist.py", "--auth", tier]
        )
        assert result.exit_code == 0, result.output
        assert "isn't implemented" in _flat(result.output)


def test_stop_unknown_app() -> None:
    result = runner.invoke(app, ["stop", "no-such-app-registered"])
    assert result.exit_code != 0
    assert "no running app" in result.output


def test_account_domain_set_requires_both_token_names() -> None:
    """v4: --zone-token-name and --tunnel-token-name are both required —
    account domain set no longer accepts raw credential values at all."""
    result = runner.invoke(app, ["account", "domain", "set", "example.com"])
    assert result.exit_code != 0
    assert "Missing option" in result.output


def test_secrets_set_prompts_for_hidden_value_and_persists() -> None:
    """`secrets set` never takes the value as a CLI argument — it prompts,
    with confirmation, for hidden input. Real: this writes to the vault, so
    clean up afterward."""
    result = runner.invoke(app, ["secrets", "set", "SMOKE_TEST_KEY"], input="sk-test\nsk-test\n")
    assert result.exit_code == 0, result.output
    assert "sk-test" not in result.output
    assert "stored secret" in result.output

    listed = runner.invoke(app, ["secrets", "list"])
    assert "SMOKE_TEST_KEY" in listed.output

    removed = runner.invoke(app, ["secrets", "remove", "SMOKE_TEST_KEY"])
    assert removed.exit_code == 0
    assert "SMOKE_TEST_KEY" not in runner.invoke(app, ["secrets", "list"]).output


def test_secrets_set_confirmation_mismatch_fails() -> None:
    result = runner.invoke(app, ["secrets", "set", "API_KEY"], input="sk-test\nsk-other\n")
    assert result.exit_code != 0
