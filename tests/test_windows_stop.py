"""Tests for the `POST /.sidepage/stop` control route
(`sidepage.core.reverse_proxy._stop_route`) and its consumer,
`sidepage.core.process.stop()`'s Windows branch
(`sidepage.core.process._request_stop_windows`) — see that function's
docstring for why POSIX doesn't need this (a cross-process `SIGTERM` is
delivered as a real signal there, caught by the target's own handler) but
Windows does (the same call there maps straight to `TerminateProcess`,
never reaching that handler at all).

The route itself has no OS-specific code — it's tested for real,
unconditionally, on whichever platform runs this file. `process.stop()`'s
branch selection (`sys.platform == "win32"`) is forced via monkeypatching
`sys.platform` so the Windows code path gets real exercise even without a
Windows runner (this repo has none yet — see `.github/workflows/tests.yml`
and `docs/CHECKLIST.md`); the hard-kill fallback inside it still runs
`sidepage.core._platform.terminate_process`'s *POSIX* branch when this
runs on a POSIX machine (`_platform`'s own Windows/POSIX split is frozen
at import time from the real `sys.platform`, unaffected by this
monkeypatch) — still a real kill, just not literally `TerminateProcess`.
None of this is a substitute for the real-Windows smoke test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from sidepage.core import process, registry
from sidepage.core.token_runtime import read_control_token_file

FIXTURES = Path(__file__).parent / "fixtures"
SIDEPAGE_BIN = str(Path(sys.executable).parent / "sidepage")


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


def _run_serve(name: str, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [SIDEPAGE_BIN, "serve", str(FIXTURES / "static-site"), "--name", name],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _cli_stop(name: str, env: dict[str, str]) -> None:
    subprocess.run([SIDEPAGE_BIN, "stop", name], env=env, capture_output=True, timeout=15)


def _wait_for_app(name: str, *, timeout: float) -> registry.RunningApp:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app = registry.get(name)
        if app is not None:
            return app
        time.sleep(0.2)
    raise TimeoutError(f"{name!r} never appeared in the registry within {timeout}s")


def _wait_gone(name: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if registry.get(name) is None:
            return
        time.sleep(0.2)
    raise TimeoutError(f"{name!r} was still in the registry after {timeout}s")


# --- POST /.sidepage/stop itself — fully cross-platform, no skip needed ---


def test_stop_route_with_correct_token_tears_down_app(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve("stop-route-ok", env)
    try:
        app = _wait_for_app("stop-route-ok", timeout=15)
        control = read_control_token_file("stop-route-ok", app.pid)

        resp = httpx.post(
            f"{app.url}/.sidepage/stop",
            headers={"X-Sidepage-Control-Token": control.value},
            timeout=5,
        )
        assert resp.status_code == 200

        proc.wait(timeout=15)
        assert proc.returncode == 0
        _wait_gone("stop-route-ok", timeout=5)
    finally:
        if proc.poll() is None:
            _cli_stop("stop-route-ok", env)
            proc.wait(timeout=15)


def test_stop_route_with_wrong_token_is_rejected(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve("stop-route-bad", env)
    try:
        app = _wait_for_app("stop-route-bad", timeout=15)

        resp = httpx.post(
            f"{app.url}/.sidepage/stop",
            headers={"X-Sidepage-Control-Token": "not-the-right-token"},
            timeout=5,
        )
        assert resp.status_code == 403

        time.sleep(0.5)
        assert proc.poll() is None, "a wrong control token must not stop the app"
        assert registry.get("stop-route-bad") is not None
    finally:
        _cli_stop("stop-route-bad", env)
        proc.wait(timeout=15)


def test_stop_route_with_missing_token_is_rejected(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve("stop-route-missing", env)
    try:
        _wait_for_app("stop-route-missing", timeout=15)
        app = registry.get("stop-route-missing")
        assert app is not None

        resp = httpx.post(f"{app.url}/.sidepage/stop", timeout=5)
        assert resp.status_code == 403

        time.sleep(0.5)
        assert proc.poll() is None, "a missing control token must not stop the app"
    finally:
        _cli_stop("stop-route-missing", env)
        proc.wait(timeout=15)


# --- process.stop()'s Windows branch, forced via monkeypatching sys.platform ---


def test_windows_stop_uses_control_route_gracefully(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve("win-stop-ok", env)
    try:
        _wait_for_app("win-stop-ok", timeout=15)
        monkeypatch.setattr(sys, "platform", "win32")

        process.stop("win-stop-ok")  # must not raise

        proc.wait(timeout=15)
        assert proc.returncode == 0, "graceful stop must exit the process cleanly, not kill it"
        assert registry.get("win-stop-ok") is None
    finally:
        if proc.poll() is None:
            _cli_stop("win-stop-ok", env)
            proc.wait(timeout=15)


def test_windows_stop_hard_kills_when_control_channel_unreachable(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the graceful control channel being unusable (e.g. a
    missing control-token file) — process.stop() must still fall back to
    a hard kill rather than hanging or leaving the app running forever."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve("win-stop-fallback", env)
    try:
        app = _wait_for_app("win-stop-fallback", timeout=15)
        control_file = (
            sidepage_home / "state" / "runtime" / f"win-stop-fallback-{app.pid}-control.json"
        )
        assert control_file.exists()
        control_file.unlink()

        monkeypatch.setattr(sys, "platform", "win32")
        process.stop("win-stop-fallback")  # must not raise, must not hang forever

        proc.wait(timeout=15)
        assert registry.get("win-stop-fallback") is None
    finally:
        if proc.poll() is None:
            _cli_stop("win-stop-fallback", env)
            proc.wait(timeout=15)
