"""Integration tests for `skills/sidepage-serve/scripts/start_site.sh` and
`stop_site.sh` — the two scripts a dispatched agent actually calls, since
`sidepage serve`/`sidepage proxy` block forever and have no daemon mode.

Mirrors the real-subprocess posture of `tests/test_serve_integration.py` and
`tests/test_serve_registry.py`: these run the actual scripts, which run the
actual `sidepage` binary, against real fixtures — the thing worth proving is
that the scripts' mode dispatch, backgrounding, and JSON shaping actually
work, not just that they parse.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
SCRIPTS_DIR = REPO_ROOT / "skills" / "sidepage-serve" / "scripts"
START_SITE = SCRIPTS_DIR / "start_site.sh"
STOP_SITE = SCRIPTS_DIR / "stop_site.sh"
SIDEPAGE_BIN_DIR = str(Path(sys.executable).parent)


@pytest.fixture
def sidepage_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def script_env(sidepage_home: Path, tmp_path: Path) -> dict[str, str]:
    # start_site.sh/stop_site.sh call bare `sidepage`, so it must resolve via
    # PATH — same binary tests/test_serve_integration.py addresses directly.
    env = {
        **os.environ,
        "SIDEPAGE_HOME": str(sidepage_home),
        "PATH": f"{SIDEPAGE_BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}",
        "SIDEPAGE_SKILL_LOG_DIR": str(tmp_path / "skill-logs"),
    }
    return env


def _run_script(script: Path, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=40
    )


def _run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    sidepage_bin = str(Path(SIDEPAGE_BIN_DIR) / "sidepage")
    return subprocess.run(
        [sidepage_bin, *args], env=env, capture_output=True, text=True, timeout=15
    )


class _EchoServer:
    """A real HTTP server on an OS-assigned port, standing in for a service
    the user already has running — exactly what `sidepage proxy` wraps."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:  # quiet test output
            pass

    def __init__(self) -> None:
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), self._Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.httpd.shutdown()


def test_start_new_target_reports_running_with_url(script_env: dict[str, str]) -> None:
    result = _run_script(
        START_SITE, ["new", "skill-test-static", str(FIXTURES / "static-site")], script_env
    )
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "running", payload
        assert payload["app"] == "skill-test-static"
        assert payload["url"].startswith("http://127.0.0.1:")
    finally:
        _run_script(STOP_SITE, ["skill-test-static"], script_env)


def test_start_new_target_reports_failure_with_log_tail(script_env: dict[str, str]) -> None:
    result = _run_script(
        START_SITE, ["new", "skill-test-bad", str(FIXTURES / "does-not-exist")], script_env
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed", payload
    assert payload["error"], "expected a non-empty log tail on failure"


def test_start_registered_app_uses_stored_config(script_env: dict[str, str]) -> None:
    reg = _run_cli(
        ["app", "register", str(FIXTURES / "static-site"), "skill-test-registered"], script_env
    )
    assert reg.returncode == 0, reg.stdout + reg.stderr

    result = _run_script(START_SITE, ["registered", "skill-test-registered"], script_env)
    try:
        payload = json.loads(result.stdout)
        assert payload["status"] == "running", payload
        assert payload["app"] == "skill-test-registered"
    finally:
        _run_script(STOP_SITE, ["skill-test-registered"], script_env)


def test_start_proxy_wraps_already_running_service(script_env: dict[str, str]) -> None:
    upstream = _EchoServer()
    try:
        result = _run_script(
            START_SITE, ["proxy", "skill-test-proxy", "--port", str(upstream.port)], script_env
        )
        payload = json.loads(result.stdout)
        assert payload["status"] == "running", payload
        assert payload["url"].startswith("http://127.0.0.1:")
    finally:
        _run_script(STOP_SITE, ["skill-test-proxy"], script_env)
        upstream.shutdown()


def test_stop_site_tears_down_and_confirms(script_env: dict[str, str]) -> None:
    start = _run_script(
        START_SITE, ["new", "skill-test-stop", str(FIXTURES / "static-site")], script_env
    )
    assert json.loads(start.stdout)["status"] == "running"

    stop = _run_script(STOP_SITE, ["skill-test-stop"], script_env)
    payload = json.loads(stop.stdout)
    assert payload["status"] == "stopped", payload

    # give teardown a moment to land in the registry, mirroring the polling
    # style _wait_for_registry_entry uses elsewhere in this suite
    deadline = time.monotonic() + 10
    status = None
    while time.monotonic() < deadline:
        status = _run_cli(["status", "skill-test-stop"], script_env)
        if status.returncode != 0:
            break
        time.sleep(0.5)
    assert status is not None and status.returncode != 0, "app still running after stop"
