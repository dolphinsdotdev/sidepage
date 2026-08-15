"""Tests for the three v5 `serve` additions (`docs/SPEC_V5_DRAFT.md`):

- §20 timeout / idle-timeout auto-teardown (`sidepage.core.process`,
  `sidepage.core.reverse_proxy.ActivityTracker`)
- §21 Tier 1 lazy start — CODE/NOTEBOOK's `subprocess.Popen` deferred to
  first inbound request (`sidepage.core.reverse_proxy._build_proxy_app`)
- `--peer <role>=<app-name>` boot-time env injection plus the live
  `GET /.sidepage/peers.json` endpoint (`sidepage.core.registry.resolve_peer_url`)

The fast, in-process cases (bad flag combos rejected before the blocking
loop) use `CliRunner`, same posture as `tests/test_cli_smoke.py`. Anything
that needs a real running app launches the CLI as a real subprocess, same
posture as `tests/test_serve_integration.py`.
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
from typer.testing import CliRunner

from sidepage.cli import app as cli_app

FIXTURES = Path(__file__).parent / "fixtures"
SIDEPAGE_BIN = str(Path(sys.executable).parent / "sidepage")

runner = CliRunner()


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


def _registry_file(sidepage_home: Path) -> Path:
    return sidepage_home / "state" / "running_apps.json"


def _wait_for_registry_entry(sidepage_home: Path, name: str, *, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    path = _registry_file(sidepage_home)
    while time.monotonic() < deadline:
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError:
                data = {}
            if name in data:
                return data[name]
        time.sleep(0.2)
    raise TimeoutError(f"{name!r} never appeared in the registry within {timeout}s")


def _wait_for_registry_entry_gone(sidepage_home: Path, name: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    path = _registry_file(sidepage_home)
    while time.monotonic() < deadline:
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError:
                data = {}
            if name not in data:
                return
        time.sleep(0.2)
    raise TimeoutError(f"{name!r} was still in the registry after {timeout}s")


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
    deadline = time.monotonic() + timeout
    last: httpx.Response | None = None
    while time.monotonic() < deadline:
        try:
            last = httpx.get(url, timeout=5)
            if "starting…" not in last.text:
                return last
        except httpx.TransportError:
            pass
        time.sleep(0.3)
    assert last is not None, f"never got any response from {url}"
    return last


# --- fast, in-process validation (fails before the blocking loop) ---


def test_negative_timeout_rejected() -> None:
    result = runner.invoke(
        cli_app, ["serve", "definitely-does-not-exist.py", "--timeout", "-5"]
    )
    assert result.exit_code == 1, result.output
    assert "--timeout must be a positive" in result.output


def test_zero_idle_timeout_rejected() -> None:
    result = runner.invoke(
        cli_app, ["serve", "definitely-does-not-exist.py", "--idle-timeout", "0"]
    )
    assert result.exit_code == 1, result.output
    assert "--idle-timeout must be a positive" in result.output


def test_peer_bad_format_rejected() -> None:
    result = runner.invoke(
        cli_app, ["serve", "definitely-does-not-exist.py", "--peer", "no-equals-sign"]
    )
    assert result.exit_code == 1, result.output
    assert "ROLE=APP-NAME" in result.output


def test_peer_rejected_for_static_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--peer boot-injects into a subprocess env and the live endpoint only
    exists on the code/notebook proxy route table — a static target has
    neither, so this must fail loud rather than silently ignore --peer."""
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path / "home"))
    result = runner.invoke(
        cli_app,
        ["serve", str(FIXTURES / "static-site"), "--peer", "x=some-app", "--name", "unused"],
    )
    assert result.exit_code == 1, result.output
    assert "--peer isn't supported for static targets" in result.output


# --- §20 timeout / idle-timeout: real subprocess, real teardown ---


def test_absolute_timeout_auto_stops(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [str(FIXTURES / "static-site"), "--name", "v5-timeout", "--timeout", "2"], env=env
    )
    try:
        _wait_for_registry_entry(sidepage_home, "v5-timeout", timeout=15)
        # No explicit stop: the app must tear itself down once --timeout
        # elapses, exiting the CLI subprocess on its own.
        proc.wait(timeout=15)
        assert proc.returncode == 0
        assert "v5-timeout" not in json.loads(_registry_file(sidepage_home).read_text())
    finally:
        if proc.poll() is None:
            _stop("v5-timeout", env)
            proc.wait(timeout=15)


def test_idle_timeout_auto_stops_with_no_traffic(sidepage_home: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [str(FIXTURES / "static-site"), "--name", "v5-idle", "--idle-timeout", "2"], env=env
    )
    try:
        _wait_for_registry_entry(sidepage_home, "v5-idle", timeout=15)
        proc.wait(timeout=15)
        assert proc.returncode == 0
        assert "v5-idle" not in json.loads(_registry_file(sidepage_home).read_text())
    finally:
        if proc.poll() is None:
            _stop("v5-idle", env)
            proc.wait(timeout=15)


def test_idle_timeout_resets_on_traffic(sidepage_home: Path) -> None:
    """Repeated requests inside the idle window must keep the app alive —
    the timer resets on each proxied request, it isn't a flat lifetime."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_serve(
        [str(FIXTURES / "static-site"), "--name", "v5-idle-reset", "--idle-timeout", "2"],
        env=env,
    )
    try:
        entry = _wait_for_registry_entry(sidepage_home, "v5-idle-reset", timeout=15)
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            resp = httpx.get(entry["url"], timeout=5)
            assert resp.status_code == 200
            time.sleep(0.5)
        # Still alive after 3.5s of continuous traffic against a 2s idle window.
        assert proc.poll() is None

        # Now stop sending traffic — it should die within roughly one more
        # idle window past the last request.
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            _stop("v5-idle-reset", env)
            proc.wait(timeout=15)


# --- §21 Tier 1 lazy start: subprocess deferred to first request ---


def test_code_subprocess_not_launched_until_first_request(
    sidepage_home: Path, tmp_path: Path
) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    marker = tmp_path / "started.marker"
    app_script = tmp_path / "lazy_app.py"
    app_script.write_text(
        "import os\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        f"open({str(marker)!r}, 'w').close()\n"
        "port = int(os.environ['PORT'])\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'up')\n"
        "    def log_message(self, *a): pass\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )

    proc = _run_serve([str(app_script), "--name", "v5-lazy"], env=env)
    try:
        entry = _wait_for_registry_entry(sidepage_home, "v5-lazy", timeout=15)
        # The proxy is up (registry entry exists) but nobody has requested
        # anything yet — the wrapped subprocess must not have started.
        assert not marker.exists(), (
            "subprocess.Popen ran before any request reached the proxy — lazy start not deferred"
        )

        resp = _poll_until_ready(entry["url"], timeout=15)
        assert resp.text == "up"
        assert marker.exists()
    finally:
        _stop("v5-lazy", env)
        proc.wait(timeout=15)


# --- --peer: boot-time env injection + live GET /.sidepage/peers.json ---


def _write_peer_echo_app(path: Path) -> None:
    path.write_text(
        "import os\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "port = int(os.environ['PORT'])\n"
        "value = os.environ.get('SIDEPAGE_PEER_UPSTREAM_URL', 'MISSING')\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(value.encode())\n"
        "    def log_message(self, *a): pass\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )


def test_peer_url_injected_into_subprocess_env(sidepage_home: Path, tmp_path: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    upstream_proc = _run_serve(
        [str(FIXTURES / "static-site"), "--name", "v5-peer-upstream"], env=env
    )
    try:
        upstream = _wait_for_registry_entry(sidepage_home, "v5-peer-upstream", timeout=15)

        consumer_script = tmp_path / "peer_consumer.py"
        _write_peer_echo_app(consumer_script)
        consumer_proc = _run_serve(
            [
                str(consumer_script),
                "--name",
                "v5-peer-consumer",
                "--peer",
                "upstream=v5-peer-upstream",
            ],
            env=env,
        )
        try:
            consumer = _wait_for_registry_entry(sidepage_home, "v5-peer-consumer", timeout=15)
            resp = _poll_until_ready(consumer["url"], timeout=15)
            assert resp.text == upstream["url"]
        finally:
            _stop("v5-peer-consumer", env)
            consumer_proc.wait(timeout=15)
    finally:
        _stop("v5-peer-upstream", env)
        upstream_proc.wait(timeout=15)


def test_peer_live_json_endpoint_re_resolves(sidepage_home: Path, tmp_path: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    upstream_proc = _run_serve(
        [str(FIXTURES / "static-site"), "--name", "v5-peer-json-upstream"], env=env
    )
    try:
        upstream = _wait_for_registry_entry(sidepage_home, "v5-peer-json-upstream", timeout=15)

        consumer_script = tmp_path / "peer_consumer2.py"
        _write_peer_echo_app(consumer_script)
        consumer_proc = _run_serve(
            [
                str(consumer_script),
                "--name",
                "v5-peer-json-consumer",
                "--peer",
                "upstream=v5-peer-json-upstream",
            ],
            env=env,
        )
        try:
            consumer = _wait_for_registry_entry(sidepage_home, "v5-peer-json-consumer", timeout=15)
            _poll_until_ready(consumer["url"], timeout=15)  # boot past the holding page first

            peers = httpx.get(f"{consumer['url']}/.sidepage/peers.json", timeout=5)
            assert peers.status_code == 200
            assert peers.json() == {"upstream": upstream["url"]}
        finally:
            _stop("v5-peer-json-consumer", env)
            consumer_proc.wait(timeout=15)
    finally:
        _stop("v5-peer-json-upstream", env)
        upstream_proc.wait(timeout=15)


def test_peer_not_running_fails_loud(sidepage_home: Path, tmp_path: Path) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    consumer_script = tmp_path / "peer_consumer3.py"
    _write_peer_echo_app(consumer_script)
    result = subprocess.run(
        [
            SIDEPAGE_BIN,
            "serve",
            str(consumer_script),
            "--name",
            "v5-peer-missing",
            "--peer",
            "upstream=does-not-exist",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "does-not-exist" in result.stdout + result.stderr
    assert "isn't currently running" in result.stdout + result.stderr
