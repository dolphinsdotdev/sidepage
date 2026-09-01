"""Tests for `sidepage serve --autoregister` and the PWA half of the app
registry it depends on.

Split the same way `tests/test_app_registry.py` and
`tests/test_serve_registry.py` are: everything that can be proven without
starting a real server is proven here in-process (the pre-flight's three
outcomes, the unregisterable-flag reporting, PWA storage and merge), and
the one claim that genuinely needs a running app — that the entry is
written *after* the app is serving, and not at all if it never gets
there — is a real subprocess test against `tests/fixtures/static-site`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sidepage.cli import app
from sidepage.core import app_registry
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.process import (
    ServeConfig,
    _autoregister_preflight,
    _unregisterable_flags_in_use,
)
from sidepage.core.pwa import PwaDisplay, PwaOptions
from sidepage.core.target import TargetKind

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures"
SIDEPAGE_BIN = str(Path(sys.executable).parent / "sidepage")


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


def _messages(capsys: pytest.CaptureFixture[str]) -> str:
    """All captured CLI output as one whitespace-normalized string.

    `sidepage.output.info`/`warn` write to stderr through Rich, which hard
    wraps at the console width — so a message that reads as one line in a
    terminal arrives here split across several, and a naive substring
    assertion would fail on the wrap point rather than on the content.
    """
    captured = capsys.readouterr()
    return " ".join(f"{captured.out}\n{captured.err}".split())


def _config(target: Path, **overrides) -> ServeConfig:
    base = {
        "target": target,
        "target_kind": None,
        "name": None,
        "domain": None,
        "auth": AuthTier.OPEN,
        "scope": Scope.LOCAL,
        "autoregister": True,
    }
    return ServeConfig(**{**base, **overrides})


def _register_static(app_name: str, target: Path, **overrides) -> app_registry.AppRegistration:
    base = {
        "target": target,
        "target_kind": TargetKind.STATIC,
        "name": None,
        "domain": None,
        "auth": AuthTier.OPEN,
        "scope": Scope.LOCAL,
        "anon": False,
        "env_secrets": (),
        "guardrail": None,
    }
    return app_registry.register(app_name, **{**base, **overrides})


# --- pre-flight: the three outcomes ---


def test_preflight_writes_when_nothing_is_registered(sidepage_home: Path) -> None:
    target = FIXTURES / "static-site"
    assert (
        _autoregister_preflight(
            _config(target), app_name="fresh", target=target, target_kind=TargetKind.STATIC
        )
        is True
    )


def test_preflight_is_a_noop_for_an_identical_existing_config(sidepage_home: Path) -> None:
    target = (FIXTURES / "static-site").resolve()
    _register_static("same", target)
    assert (
        _autoregister_preflight(
            _config(target), app_name="same", target=target, target_kind=TargetKind.STATIC
        )
        is False
    )


def test_preflight_noop_message_points_at_the_shorter_command(
    sidepage_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The identical-config path isn't silent — it tells the user the app
    is already registered and that `serve <app-name>` is all they need
    next time."""
    target = (FIXTURES / "static-site").resolve()
    _register_static("same-msg", target)
    _autoregister_preflight(
        _config(target), app_name="same-msg", target=target, target_kind=TargetKind.STATIC
    )
    out = _messages(capsys)
    assert "reusing existing app" in out
    assert "sidepage serve same-msg" in out


def test_preflight_rejects_a_different_existing_config(sidepage_home: Path) -> None:
    """A name registered with a *different* config is a real conflict:
    overwriting could clobber a hand-tuned `app register`, and keeping the
    old one silently would leave the user believing this invocation was
    saved."""
    target = (FIXTURES / "static-site").resolve()
    _register_static("conflict", target, auth=AuthTier.TOKEN)
    with pytest.raises(ValueError, match="already registered with a different config"):
        _autoregister_preflight(
            _config(target), app_name="conflict", target=target, target_kind=TargetKind.STATIC
        )


def test_preflight_tolerates_a_registration_saved_without_an_explicit_name(
    sidepage_home: Path,
) -> None:
    """`serve <app-name> --autoregister` for an app registered with no
    `--name` must not read as a conflict: the stored `name` is None while
    the live resolved one is the registry key itself. See
    `app_registry.same_config`'s `default_name`."""
    target = (FIXTURES / "static-site").resolve()
    _register_static("keyed", target, name=None)
    config = _config(target, name="keyed")
    assert (
        _autoregister_preflight(
            config, app_name="keyed", target=target, target_kind=TargetKind.STATIC
        )
        is False
    )


# --- unregisterable flags are reported, never silently dropped ---


def test_unregisterable_flags_detects_every_per_invocation_flag() -> None:
    target = FIXTURES / "static-site"
    config = _config(
        target,
        token="secret",
        timeout=60.0,
        idle_timeout=30.0,
        peers=(("api", "other"),),
        qr=True,
    )
    assert _unregisterable_flags_in_use(config) == [
        "--token",
        "--timeout",
        "--idle-timeout",
        "--peer",
        "--qr",
    ]


def test_unregisterable_flags_empty_for_a_plain_invocation() -> None:
    assert _unregisterable_flags_in_use(_config(FIXTURES / "static-site")) == []


def test_pwa_is_not_reported_as_unregisterable() -> None:
    """PWA settings describe what the served app *is*, so they're stored,
    not dropped — the whole reason they're absent from this list."""
    config = _config(FIXTURES / "static-site", pwa=PwaOptions(name="Dash"))
    assert _unregisterable_flags_in_use(config) == []


def test_preflight_names_the_unsaved_flags(
    sidepage_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = FIXTURES / "static-site"
    _autoregister_preflight(
        _config(target, timeout=60.0, qr=True),
        app_name="noisy",
        target=target,
        target_kind=TargetKind.STATIC,
    )
    out = _messages(capsys)
    assert "--timeout" in out
    assert "--qr" in out


def test_preflight_explains_the_token_separately(
    sidepage_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--token` is unsaved for a stronger reason than the others — it's a
    process-scoped secret, not just per-invocation config — so it gets its
    own line rather than only appearing in the list."""
    target = FIXTURES / "static-site"
    _autoregister_preflight(
        _config(target, token="hunter2"),
        app_name="tokenful",
        target=target,
        target_kind=TargetKind.STATIC,
    )
    out = _messages(capsys)
    assert "--token" in out
    assert "process-scoped" in out
    # The value itself must never end up in the registry, and nothing here
    # should echo it back either.
    assert "hunter2" not in out


# --- PWA storage and merge ---


def test_pwa_options_round_trip_through_the_registry(sidepage_home: Path, tmp_path: Path) -> None:
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"not really a png")
    stored = _register_static(
        "pwa-app",
        (FIXTURES / "static-site").resolve(),
        pwa=PwaOptions(
            name="Dashboard",
            short_name="Dash",
            theme="#123456",
            bg="#abcdef",
            icon=icon,
            display=PwaDisplay.FULLSCREEN,
            force=True,
            no_sw=True,
        ),
    )
    assert stored.pwa is not None

    loaded = app_registry.get("pwa-app")
    assert loaded is not None and loaded.pwa is not None
    assert loaded.pwa.name == "Dashboard"
    assert loaded.pwa.short_name == "Dash"
    assert loaded.pwa.theme == "#123456"
    assert loaded.pwa.bg == "#abcdef"
    assert loaded.pwa.display is PwaDisplay.FULLSCREEN
    assert loaded.pwa.force is True
    assert loaded.pwa.no_sw is True
    # Paths are stored absolute so `serve <app-name>` works from any cwd.
    assert loaded.pwa.icon == icon.resolve()


def test_registration_without_pwa_round_trips_as_none(sidepage_home: Path) -> None:
    _register_static("plain", (FIXTURES / "static-site").resolve())
    loaded = app_registry.get("plain")
    assert loaded is not None
    assert loaded.pwa is None


def test_entry_written_before_pwa_was_stored_still_loads(sidepage_home: Path) -> None:
    """An entry from a version that predates the `pwa` key is a valid
    registration with PWA off, not a corrupt one."""
    _register_static("legacy", (FIXTURES / "static-site").resolve())
    path = sidepage_home / "state" / "registry.json"
    raw = json.loads(path.read_text())
    del raw["legacy"]["pwa"]
    path.write_text(json.dumps(raw))

    loaded = app_registry.get("legacy")
    assert loaded is not None
    assert loaded.pwa is None


def test_app_register_cli_stores_pwa_flags(sidepage_home: Path) -> None:
    target = FIXTURES / "static-site"
    result = runner.invoke(
        app,
        ["app", "register", f"{target} --pwa --pwa-name Dashboard --pwa-theme #222222", "dash"],
    )
    assert result.exit_code == 0, result.output

    stored = app_registry.get("dash")
    assert stored is not None and stored.pwa is not None
    assert stored.pwa.name == "Dashboard"
    assert stored.pwa.theme == "#222222"


def test_app_show_reports_stored_pwa(sidepage_home: Path) -> None:
    target = FIXTURES / "static-site"
    runner.invoke(app, ["app", "register", f"{target} --pwa --pwa-name Dashboard", "shown"])
    result = runner.invoke(app, ["app", "show", "shown"])
    assert result.exit_code == 0, result.output
    assert "Dashboard" in result.output


def test_show_with_previews_pwa_override_as_a_whole_unit(sidepage_home: Path) -> None:
    """PWA merges as one unit: an explicit `--pwa*` on this invocation
    replaces the registered config wholesale rather than merging field by
    field, so the registered name does not survive an override that
    doesn't restate it."""
    target = FIXTURES / "static-site"
    runner.invoke(
        app,
        ["app", "register", f"{target} --pwa --pwa-name Registered --pwa-theme #111111", "merged"],
    )
    result = runner.invoke(app, ["app", "show", "merged", "--with", "--pwa --pwa-theme #999999"])
    assert result.exit_code == 0, result.output
    assert "#999999" in result.output
    assert "Registered" not in result.output


def test_show_with_keeps_registered_pwa_when_no_pwa_flag_is_passed(sidepage_home: Path) -> None:
    target = FIXTURES / "static-site"
    runner.invoke(app, ["app", "register", f"{target} --pwa --pwa-name Registered", "kept"])
    result = runner.invoke(app, ["app", "show", "kept", "--with", "--auth token"])
    assert result.exit_code == 0, result.output
    assert "Registered" in result.output


# --- `proxy` rejects it outright ---


def test_proxy_rejects_autoregister(sidepage_home: Path) -> None:
    result = runner.invoke(app, ["proxy", "--port", "5173", "--name", "x", "--autoregister"])
    assert result.exit_code == 1
    assert "doesn't apply to `proxy`" in result.output


# --- real subprocess: written only once the app is actually serving ---


def _wait_for_registry_entry(sidepage_home: Path, name: str, *, timeout: float) -> dict:
    registry_file = sidepage_home / "state" / "running_apps.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if registry_file.exists():
            try:
                data = json.loads(registry_file.read_text())
            except json.JSONDecodeError:
                data = {}
            if name in data:
                return data[name]
        time.sleep(0.2)
    raise TimeoutError(f"{name!r} never appeared in the registry within {timeout}s")


def test_autoregister_writes_the_entry_once_the_app_is_serving(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = subprocess.Popen(
        [
            SIDEPAGE_BIN,
            "serve",
            str(FIXTURES / "static-site"),
            "--name",
            "auto-live",
            "--autoregister",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_registry_entry(sidepage_home, "auto-live", timeout=20)
        saved = json.loads((sidepage_home / "state" / "registry.json").read_text())
        assert "auto-live" in saved
        assert saved["auto-live"]["target"] == str((FIXTURES / "static-site").resolve())
        assert saved["auto-live"]["type"] == "static"
    finally:
        subprocess.run(
            [SIDEPAGE_BIN, "stop", "auto-live"], env=env, capture_output=True, timeout=15
        )
        proc.wait(timeout=15)

    # The saved config outlives the run it was saved from — that's the
    # whole point of registering it.
    assert app_registry.get("auto-live") is not None


def test_autoregister_saves_nothing_when_the_app_never_starts(sidepage_home: Path) -> None:
    """A config that failed validation is never persisted — the reason the
    write happens after startup rather than before it."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    result = subprocess.run(
        [
            SIDEPAGE_BIN,
            "serve",
            str(FIXTURES / "static-site"),
            "--name",
            "auto-dead",
            "--autoregister",
            "--domain",
            "never-configured.example",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert not (sidepage_home / "state" / "registry.json").exists()
