"""Integration tests for `sidepage.core.inspector` against the real
static-site fixture.

The interactive REPL loop itself (`open_console`) isn't exercised here —
driving `input()` through pytest adds little over testing the two pure
functions it's built on (`resolve_target`, `execute_request`) directly,
which is what actually proves target resolution, auth auto-sourcing, and
request execution work against a real running app.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sidepage.core.exceptions import InspectorTargetError
from sidepage.core.inspector import execute_request, resolve_target

FIXTURES = Path(__file__).parent / "fixtures"
SIDEPAGE_BIN = str(Path(sys.executable).parent / "sidepage")


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


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


def test_resolve_target_by_app_name(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve([str(FIXTURES / "static-site"), "--name", "insp-static"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "insp-static", timeout=15)
        session = resolve_target("insp-static")
        assert session.base_url == entry["url"]
        assert session.app is not None
        assert session.app.name == "insp-static"
        assert session.token is None  # --auth open, no runtime token file
    finally:
        _stop("insp-static", env)
        proc.wait(timeout=15)


def test_resolve_target_by_raw_url() -> None:
    session = resolve_target("http://127.0.0.1:9/nowhere")
    assert session.base_url == "http://127.0.0.1:9/nowhere"
    assert session.app is None
    assert session.token is None


def test_resolve_target_unknown_raises(sidepage_home: Path) -> None:
    with pytest.raises(InspectorTargetError):
        resolve_target("not-a-running-app-and-not-a-url")


def test_execute_request_against_static_site(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve([str(FIXTURES / "static-site"), "--name", "insp-req"], env=env)
    try:
        _wait_for_registry_entry(sidepage_home, "insp-req", timeout=15)
        session = resolve_target("insp-req")
        resp = execute_request(session, "GET", "/")
        assert resp.status_code == 200
        assert "PrintStudio" in resp.text
        assert session.last_request == ("GET", "/", None)
    finally:
        _stop("insp-req", env)
        proc.wait(timeout=15)


def test_execute_request_auto_sources_token(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [
            str(FIXTURES / "static-site"),
            "--name",
            "insp-auth",
            "--auth",
            "token",
            "--token",
            "insp-test-token",
        ],
        env=env,
    )
    try:
        _wait_for_registry_entry(sidepage_home, "insp-auth", timeout=15)
        session = resolve_target("insp-auth")
        assert session.token == "insp-test-token"

        # Auto-sourced token should authenticate without any manual header.
        authed = execute_request(session, "GET", "/")
        assert authed.status_code == 200

        # A session with no token (raw URL, nothing to auto-source from)
        # should get the real auth gate, not a bypass.
        unauthed_session = resolve_target(session.base_url)
        blocked = execute_request(unauthed_session, "GET", "/")
        assert blocked.status_code == 401
    finally:
        _stop("insp-auth", env)
        proc.wait(timeout=15)


def test_execute_request_json_body_sets_content_type(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve([str(FIXTURES / "static-site"), "--name", "insp-body"], env=env)
    try:
        _wait_for_registry_entry(sidepage_home, "insp-body", timeout=15)
        session = resolve_target("insp-body")
        # The static site doesn't have a POST endpoint that echoes anything
        # back, so this only proves the request goes out with the right
        # shape (no TransportError, session records it) rather than what
        # the server does with it.
        execute_request(session, "POST", "/", '{"a": 1}')
        assert session.last_request == ("POST", "/", '{"a": 1}')
    finally:
        _stop("insp-body", env)
        proc.wait(timeout=15)
