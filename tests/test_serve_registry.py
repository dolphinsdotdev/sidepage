"""Real subprocess integration test for the local app registry (registry
spec v2): `sidepage app register` + `sidepage serve <app-name>` against
the real `tests/fixtures/static-site` fixture.

`tests/test_app_registry.py` covers the registry and its CLI in isolation
(fast, in-process). This file exists to prove the actual claim end to
end: a registered app's stored `--auth token` really gates the served app
when `serve <app-name>` is run with no overrides, and an explicit
`--auth open` on that same invocation really overrides it for that one
run — the merge semantics aren't just unit-tested against
`merge_with_registered` in isolation, they're proven against a real
running proxy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SIDEPAGE_BIN = str(Path(sys.executable).parent / "sidepage")


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


def _run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [SIDEPAGE_BIN, *args], env=env, capture_output=True, text=True, timeout=15
    )


def _run_serve(args: list[str], env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [SIDEPAGE_BIN, "serve", *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop(name: str, env: dict[str, str]) -> None:
    subprocess.run([SIDEPAGE_BIN, "stop", name], env=env, capture_output=True, timeout=15)


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


def test_serve_registered_app_uses_stored_auth_and_registry_key_as_name(
    sidepage_home: Path,
) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    reg = _run_cli(
        ["app", "register", f"{FIXTURES / 'static-site'} --auth token", "registry-site"], env=env
    )
    assert reg.returncode == 0, reg.stdout + reg.stderr

    proc = _run_serve(["registry-site"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "registry-site", timeout=20)
        # No explicit --name at registration or at serve time: the
        # registry key itself is the runtime app name, not the target
        # directory's own basename.
        assert entry["name"] == "registry-site"
        assert entry["target"] == str((FIXTURES / "static-site").resolve())

        blocked = httpx.get(entry["url"], timeout=5)
        assert blocked.status_code == 401  # registered --auth token, no override
    finally:
        _stop("registry-site", env)
        proc.wait(timeout=15)


def test_serve_registered_app_cli_override_wins_for_one_invocation(sidepage_home: Path) -> None:
    """--auth open passed at serve time overrides the registered --auth
    token for this one run — and the registry entry itself stays token
    afterward (override is non-destructive)."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    reg = _run_cli(
        ["app", "register", f"{FIXTURES / 'static-site'} --auth token", "override-site"], env=env
    )
    assert reg.returncode == 0, reg.stdout + reg.stderr

    proc = _run_serve(["override-site", "--auth", "open"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "override-site", timeout=20)
        allowed = httpx.get(entry["url"], timeout=5)
        assert allowed.status_code == 200  # --auth open overrode the stored token tier
    finally:
        _stop("override-site", env)
        proc.wait(timeout=15)

    show = _run_cli(["app", "show", "override-site"], env=env)
    assert "token" in show.stdout  # base registration untouched by the one-off override


def test_serve_unregistered_name_falls_back_to_literal_path_error(sidepage_home: Path) -> None:
    """An argument that isn't a registered app name behaves exactly like
    today, pre-registry: treated as a literal target path, which then
    fails target detection the same way it always has."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    result = _run_cli(["serve", "definitely-not-registered-or-a-real-file.py"], env=env)
    assert result.returncode != 0
    assert "does not exist" in result.stdout + result.stderr
