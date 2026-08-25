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

import base64
import json

import httpx
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
# placeholder args to satisfy required parameters. `inspect` is real now
# (see test_inspect_* below and tests/test_inspector.py) — not listed here.
LEAF_INVOCATIONS = [
    ["new", "myapp"],
    ["promote", "myapp"],
    ["login"],
]

HELP_TARGETS = [
    [],
    ["setup", "--help"],
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
    assert "does not exist" in _flat(result.output)


def test_serve_nonexistent_target_fails_fast_even_with_type_override() -> None:
    """Regression check: `--type` used to skip the existence check
    entirely in `sidepage.core.target.detect_target_kind`, so a valid-
    looking but nonexistent target with an explicit `--type` would sail
    past validation into the real blocking loop instead of failing."""
    result = runner.invoke(app, ["serve", "definitely-does-not-exist.py", "--type", "code"])
    assert result.exit_code != 0
    assert "does not exist" in _flat(result.output)


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


def test_serve_domain_without_config_rejected() -> None:
    """--domain (BYO Cloudflare) is real, but requires `account domain set`
    first — rejected before target detection, same as --guardrail, but as
    a real ValueError (exit 1), not a not-yet-implemented notice (exit 0):
    the feature is built, this invocation is just missing a prerequisite."""
    result = runner.invoke(
        app, ["serve", "definitely-does-not-exist.py", "--domain", "example.com"]
    )
    assert result.exit_code == 1, result.output
    assert "isn't configured" in _flat(result.output)
    assert "account domain set" in result.output


def test_serve_domain_and_anon_mutually_exclusive() -> None:
    result = runner.invoke(
        app,
        ["serve", "definitely-does-not-exist.py", "--domain", "example.com", "--anon"],
    )
    assert result.exit_code == 1, result.output
    assert "mutually exclusive" in result.output


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


def test_inspect_no_apps_running_exits_cleanly() -> None:
    """With no target and nothing registered, `_pick_target` errors and
    returns before ever calling `input()` — safe to run in-process."""
    result = runner.invoke(app, ["inspect"])
    assert result.exit_code == 0, result.output
    assert "no apps running" in result.output


def test_inspect_unknown_target_fails() -> None:
    result = runner.invoke(app, ["inspect", "not-a-running-app"])
    assert result.exit_code != 0
    assert "not-a-running-app" in result.output


def test_account_domain_set_requires_api_token_name() -> None:
    """v4 delta: a single --api-token-name is required — the old
    --zone-token-name/--tunnel-token-name pair is gone."""
    result = runner.invoke(app, ["account", "domain", "set", "example.com"])
    assert result.exit_code != 0
    assert "Missing option" in result.output


def test_account_domain_set_rejects_unknown_secret_name() -> None:
    """Real validation, not just flag presence: the name must already
    resolve in the vault before any Cloudflare API call is attempted."""
    result = runner.invoke(
        app,
        ["account", "domain", "set", "example.com", "--api-token-name", "no-such-secret"],
    )
    assert result.exit_code != 0
    assert "no-such-secret" in result.output


class _FakeCloudflareProvisioningTransport(httpx.BaseTransport):
    """Minimal fake for the two Cloudflare API calls `account domain set`
    now makes for real (zone lookup, then create-tunnel) — see
    `tests/test_tunnel_byo.py` for the fuller stateful fake used to test
    `tunnel_manager` directly; this one only needs to cover a single
    happy-path `domain set` call."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/client/v4/zones":
            return httpx.Response(
                200,
                json={"success": True, "result": [{"id": "zone-1", "account": {"id": "acct-1"}}]},
            )
        if path == "/client/v4/accounts/acct-1/cfd_tunnel" and request.method == "POST":
            token = base64.b64encode(
                json.dumps({"a": "acct-1", "t": "tun-1", "s": "sec"}).encode()
            ).decode()
            result = {"id": "tun-1", "token": token}
            return httpx.Response(200, json={"success": True, "result": result})
        return httpx.Response(404, json={"success": False, "errors": [f"unhandled: {path}"]})


def test_serve_domain_with_configured_domain_passes_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once `account domain set` succeeds, `serve --domain` should clear
    domain validation entirely — proven by it reaching target detection
    (a later, different failure) instead of the "isn't configured" error.
    `account domain set` itself now makes real Cloudflare API calls
    (zone lookup + tunnel creation), so those are mocked here; `serve`
    still never reaches the tunnel-opening step, since a nonexistent
    target fails before that."""
    real_client = httpx.Client

    def fake_client(*a, **k):
        return real_client(*a, **{**k, "transport": _FakeCloudflareProvisioningTransport()})

    monkeypatch.setattr(httpx, "Client", fake_client)

    runner.invoke(app, ["secrets", "set", "cf-api-tok"], input="cftok\ncftok\n")
    set_result = runner.invoke(
        app, ["account", "domain", "set", "example.com", "--api-token-name", "cf-api-tok"]
    )
    assert set_result.exit_code == 0, set_result.output
    assert "cf-tunnel-token::example.com" in set_result.output

    result = runner.invoke(
        app, ["serve", "definitely-does-not-exist.py", "--domain", "example.com"]
    )
    assert result.exit_code != 0
    assert "does not exist" in _flat(result.output)
    assert "isn't configured" not in result.output


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
