"""Fast, in-process tests for the local app registry (registry spec v2):
`sidepage.core.app_registry` directly, plus the `sidepage app
register|list|show|unregister` CLI and the parser-reuse mechanism behind
`register`/`show --with`.

Real subprocess proof that `serve <app-name>` actually applies the merged
config end to end lives in `tests/test_serve_registry.py` — everything
here is either pure data-layer testing or `CliRunner`-based, no real
`serve` process ever starts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sidepage.cli import app
from sidepage.core import app_registry
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.exceptions import AppNotRegisteredError, AppRegistrationError
from sidepage.core.target import TargetKind

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


# --- core/app_registry.py: pure data-layer round trip ---


def test_register_then_get_round_trips(sidepage_home: Path) -> None:
    target = (FIXTURES / "static-site").resolve()
    registered = app_registry.register(
        "abc-app",
        target=target,
        target_kind=TargetKind.STATIC,
        name=None,
        domain=None,
        auth=AuthTier.TOKEN,
        scope=Scope.LOCAL,
        anon=False,
        env_secrets=(),
        guardrail=None,
    )
    assert registered.target == target
    fetched = app_registry.get("abc-app")
    assert fetched == registered


def test_get_unknown_returns_none_not_error(sidepage_home: Path) -> None:
    assert app_registry.get("never-registered") is None


def test_list_registered_sorted(sidepage_home: Path) -> None:
    for name in ("zebra", "apple", "mango"):
        app_registry.register(
            name,
            target=(FIXTURES / "static-site").resolve(),
            target_kind=TargetKind.STATIC,
            name=None,
            domain=None,
            auth=AuthTier.OPEN,
            scope=Scope.LOCAL,
            anon=False,
            env_secrets=(),
            guardrail=None,
        )
    assert app_registry.list_registered() == ["apple", "mango", "zebra"]


def test_register_duplicate_name_raises(sidepage_home: Path) -> None:
    kwargs = dict(
        target=(FIXTURES / "static-site").resolve(),
        target_kind=TargetKind.STATIC,
        name=None,
        domain=None,
        auth=AuthTier.OPEN,
        scope=Scope.LOCAL,
        anon=False,
        env_secrets=(),
        guardrail=None,
    )
    app_registry.register("dup", **kwargs)
    with pytest.raises(AppRegistrationError):
        app_registry.register("dup", **kwargs)


def test_unregister_removes_entry(sidepage_home: Path) -> None:
    app_registry.register(
        "gone-soon",
        target=(FIXTURES / "static-site").resolve(),
        target_kind=TargetKind.STATIC,
        name=None,
        domain=None,
        auth=AuthTier.OPEN,
        scope=Scope.LOCAL,
        anon=False,
        env_secrets=(),
        guardrail=None,
    )
    app_registry.unregister("gone-soon")
    assert app_registry.get("gone-soon") is None


def test_unregister_unknown_raises_not_silent(sidepage_home: Path) -> None:
    with pytest.raises(AppNotRegisteredError):
        app_registry.unregister("never-existed")


def test_env_secrets_stored_as_names_not_values(sidepage_home: Path) -> None:
    """The registry stores --env references (vault secret *names*), never
    values — there's no value to leak in the first place at this layer,
    since ServeConfig.env_secrets is always just names."""
    registered = app_registry.register(
        "with-env",
        target=(FIXTURES / "static-site").resolve(),
        target_kind=TargetKind.STATIC,
        name=None,
        domain=None,
        auth=AuthTier.OPEN,
        scope=Scope.LOCAL,
        anon=False,
        env_secrets=("MY_API_KEY", "OTHER_SECRET"),
        guardrail=None,
    )
    assert registered.env_secrets == ("MY_API_KEY", "OTHER_SECRET")


def test_stored_json_shape_matches_spec_field_names(sidepage_home: Path) -> None:
    """The registry spec's example stored entry uses these exact field
    names (target/type/auth/scope/domain/env/registered_at) — a schema
    drift here would silently break `app show`'s usefulness."""
    from sidepage.config.settings import app_registry_file

    app_registry.register(
        "shape-check",
        target=(FIXTURES / "static-site").resolve(),
        target_kind=TargetKind.STATIC,
        name=None,
        domain=None,
        auth=AuthTier.TOKEN,
        scope=Scope.LOCAL,
        anon=False,
        env_secrets=(),
        guardrail=None,
    )
    import json

    stored = json.loads(app_registry_file().read_text())["shape-check"]
    for key in ("target", "type", "auth", "scope", "domain", "env", "registered_at"):
        assert key in stored
    assert stored["type"] == "static"
    assert stored["auth"] == "token"


# --- CLI: sidepage app register|list|show|unregister ---


def test_cli_register_list_show_unregister_round_trip(sidepage_home: Path) -> None:
    reg = runner.invoke(
        app, ["app", "register", f"{FIXTURES / 'static-site'} --auth token", "cli-app"]
    )
    assert reg.exit_code == 0, reg.output
    assert "registered" in reg.output

    listed = runner.invoke(app, ["app", "list"])
    assert "cli-app" in listed.output

    shown = runner.invoke(app, ["app", "show", "cli-app"])
    assert shown.exit_code == 0, shown.output
    assert "token" in shown.output
    assert "static" in shown.output

    unreg = runner.invoke(app, ["app", "unregister", "cli-app"])
    assert unreg.exit_code == 0, unreg.output

    listed_after = runner.invoke(app, ["app", "list"])
    assert "cli-app" not in listed_after.output


def test_cli_register_rejects_literal_token(sidepage_home: Path) -> None:
    result = runner.invoke(
        app,
        ["app", "register", f"{FIXTURES / 'static-site'} --token sk-literal-secret", "bad-app"],
    )
    assert result.exit_code != 0
    assert "cannot register" in result.output
    assert "sk-literal-secret" not in result.output  # never echoed back
    assert app_registry.get("bad-app") is None


def test_cli_register_rejects_nonexistent_target(sidepage_home: Path) -> None:
    result = runner.invoke(app, ["app", "register", "does-not-exist.py", "ghost-app"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_cli_show_unknown_app_fails(sidepage_home: Path) -> None:
    result = runner.invoke(app, ["app", "show", "no-such-app"])
    assert result.exit_code != 0
    assert "no-such-app" in result.output


def test_cli_show_with_preview_does_not_mutate_registration(sidepage_home: Path) -> None:
    invocation = f"{FIXTURES / 'static-site'} --scope local"
    runner.invoke(app, ["app", "register", invocation, "preview-app"])

    preview = runner.invoke(app, ["app", "show", "preview-app", "--with", "--scope web"])
    assert preview.exit_code == 0, preview.output
    assert "web" in preview.output

    # The stored base config is untouched by the preview.
    unchanged = app_registry.get("preview-app")
    assert unchanged is not None
    assert unchanged.scope is Scope.LOCAL


# --- Parser reuse: the spec's core design claim ---


def test_register_auto_detects_type_same_as_serve_would(sidepage_home: Path) -> None:
    """No --type given at registration: the stored type is resolved via
    the same detection serve uses (static, from a directory target), not
    left as "auto" — see AppRegistration.target_kind's type (TargetKind,
    never an "auto" sentinel)."""
    runner.invoke(app, ["app", "register", str(FIXTURES / "static-site"), "auto-detect-app"])
    registered = app_registry.get("auto-detect-app")
    assert registered is not None
    assert registered.target_kind is TargetKind.STATIC


def test_register_respects_explicit_type_override(sidepage_home: Path) -> None:
    result = runner.invoke(
        app, ["app", "register", f"{FIXTURES / 'static-site'} --type static", "explicit-type-app"]
    )
    assert result.exit_code == 0, result.output
    registered = app_registry.get("explicit-type-app")
    assert registered is not None
    assert registered.target_kind is TargetKind.STATIC
