"""Integration tests for `sidepage serve` and `sidepage secrets` against the
two prioritized real fixtures: a static HTML site and a Streamlit app.

These launch the actual CLI as a subprocess and talk to it over real HTTP/
WebSocket — slower than the unit-style tests in `test_cli_smoke.py`, but
they're the only thing that actually proves the reverse proxy, process
wrapping, secrets vault, and registry work together end to end rather than
just parsing arguments correctly.

Each test gets an isolated `SIDEPAGE_HOME` (via the `sidepage_home` fixture)
so nothing touches the real user's `~/.config`/`~/.local/state`.
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


def _poll_until_ready(url: str, *, timeout: float) -> httpx.Response:
    """GET `url` until the wrapped app is actually up — the proxy serves a
    holding page (`sidepage.core.reverse_proxy`) while its readiness check
    is still in flight, so a single request right after the registry entry
    appears can legitimately see that instead of the real response."""
    deadline = time.monotonic() + timeout
    last: httpx.Response | None = None
    while time.monotonic() < deadline:
        try:
            last = httpx.get(url, timeout=5)
            if "starting…" not in last.text:
                return last
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    assert last is not None, f"never got any response from {url}"
    return last


def test_serve_static_site(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [str(FIXTURES / "static-site"), "--name", "it-static"], env=env
    )
    try:
        app = _wait_for_registry_entry(sidepage_home, "it-static", timeout=15)
        resp = httpx.get(app["url"], timeout=5)
        assert resp.status_code == 200
        assert "PrintStudio" in resp.text
    finally:
        _stop("it-static", env)
        proc.wait(timeout=15)

    registry_file = sidepage_home / "state" / "running_apps.json"
    registered = json.loads(registry_file.read_text()) if registry_file.exists() else {}
    assert "it-static" not in registered


def test_serve_static_site_auth_token_gate(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [
            str(FIXTURES / "static-site"),
            "--name",
            "it-static-auth",
            "--auth",
            "token",
            "--token",
            "test-token-xyz",
        ],
        env=env,
    )
    try:
        app = _wait_for_registry_entry(sidepage_home, "it-static-auth", timeout=15)
        no_cred = httpx.get(app["url"], timeout=5)
        assert no_cred.status_code == 401

        wrong_cred = httpx.get(app["url"], params={"token": "wrong"}, timeout=5)
        assert wrong_cred.status_code == 401

        good_header = httpx.get(
            app["url"], headers={"Authorization": "Bearer test-token-xyz"}, timeout=5
        )
        assert good_header.status_code == 200

        good_query = httpx.get(app["url"], params={"token": "test-token-xyz"}, timeout=5)
        assert good_query.status_code == 200
    finally:
        _stop("it-static-auth", env)
        proc.wait(timeout=15)


def test_serve_env_secret_injection(sidepage_home: Path, tmp_path: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    subprocess.run(
        [SIDEPAGE_BIN, "secrets", "set", "IT_TEST_SECRET"],
        input="sk-injected-value\nsk-injected-value\n",
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    echo_app = tmp_path / "echo_secret.py"
    echo_app.write_text(
        "import os\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "port = int(os.environ['PORT'])\n"
        "value = os.environ.get('IT_TEST_SECRET', 'MISSING')\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(value.encode())\n"
        "    def log_message(self, *a): pass\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )

    proc = _run_serve(
        [str(echo_app), "--name", "it-env", "--env", "IT_TEST_SECRET"], env=env
    )
    try:
        app = _wait_for_registry_entry(sidepage_home, "it-env", timeout=15)
        resp = _poll_until_ready(app["url"], timeout=15)
        assert resp.text == "sk-injected-value"
    finally:
        _stop("it-env", env)
        proc.wait(timeout=15)


def test_serve_missing_secret_fails_loud(sidepage_home: Path, tmp_path: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    trivial_app = tmp_path / "trivial.py"
    trivial_app.write_text(
        "import os\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "port = int(os.environ['PORT'])\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self): self.send_response(200); self.end_headers()\n"
        "    def log_message(self, *a): pass\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )
    result = subprocess.run(
        [SIDEPAGE_BIN, "serve", str(trivial_app), "--name", "it-missing-secret", "--env", "NOPE"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "NOPE" in result.stdout + result.stderr


def test_serve_streamlit_app(sidepage_home: Path) -> None:
    """The slower of the two fixtures — first run resolves streamlit/
    pandas/numpy via `uv run --with-requirements`, which can take a while
    even with a warm uv cache."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [str(FIXTURES / "streamlit-app" / "app.py"), "--name", "it-streamlit"], env=env
    )
    try:
        app = _wait_for_registry_entry(sidepage_home, "it-streamlit", timeout=60)
        resp = _poll_until_ready(app["url"], timeout=30)
        assert resp.status_code == 200
        assert "Streamlit" in resp.text

        usage = subprocess.run(
            [SIDEPAGE_BIN, "usage", "it-streamlit"], env=env, capture_output=True, text=True
        )
        assert "http requests" in usage.stdout
    finally:
        _stop("it-streamlit", env)
        proc.wait(timeout=20)
