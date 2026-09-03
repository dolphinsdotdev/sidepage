"""Integration tests for `serve`/`proxy --detach` and `--json`.

Replaces `tests/test_skill_scripts.py`, which tested a pair of shell
wrappers that existed only because `serve`/`proxy` blocked forever and
printed for humans. Both properties now live in the CLI, so the thing
worth proving moved with them.

Same real-subprocess posture as `tests/test_serve_integration.py`: these
run the actual binary against real fixtures. Backgrounding, readiness
detection, and failure reporting are all things that only work or don't
against a real process — a mocked `Popen` would prove nothing about
whether a detached child actually survives its parent.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
STATIC_SITE = FIXTURES / "static-site"
SIDEPAGE_BIN = str(Path(sys.executable).parent / "sidepage")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {**os.environ, "SIDEPAGE_HOME": str(home)}


def _cli(args: list[str], env: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [SIDEPAGE_BIN, *args], env=env, capture_output=True, text=True, timeout=timeout
    )


def _stop(name: str, env: dict[str, str]) -> None:
    _cli(["stop", name], env, timeout=30)


def _get_status(url: str) -> int:
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 — loopback, our own server
        return resp.status


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


def test_detach_returns_running_payload_and_app_is_reachable(env: dict[str, str]) -> None:
    """The core contract: the parent exits, and by the time it does the app
    is genuinely serving — not merely spawned."""
    result = _cli(["serve", str(STATIC_SITE), "--name", "d-static", "--detach", "--json"], env)
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "running", payload
        assert payload["app"] == "d-static"
        assert payload["url"].startswith("http://127.0.0.1:")
        assert payload["tunnel_url"] is None  # no --anon/--domain
        # The claim under test — "running" has to mean serving.
        assert _get_status(payload["url"]) == 200
    finally:
        _stop("d-static", env)


def test_detached_child_outlives_parent(env: dict[str, str]) -> None:
    """`--detach` is worthless if the child dies with the shell that
    launched it. Proven by the parent having already exited above; here we
    check the app is still up a beat later and still in the registry."""
    result = _cli(["serve", str(STATIC_SITE), "--name", "d-survive", "--detach", "--json"], env)
    payload = json.loads(result.stdout)
    try:
        time.sleep(2)
        assert _get_status(payload["url"]) == 200
        listing = _cli(["ls"], env)
        assert "d-survive" in listing.stdout
    finally:
        _stop("d-survive", env)


def test_detach_failure_reports_child_error_and_exits_nonzero(env: dict[str, str]) -> None:
    """The failure the shell wrapper could never get right: the reported
    error is the child's actual message, not a regex guess at one."""
    result = _cli(
        ["serve", str(FIXTURES / "does-not-exist"), "--name", "d-bad", "--detach", "--json"], env
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed", payload
    assert payload["exit_code"] != 0
    assert "does not exist" in payload["error"]
    assert Path(payload["log"]).exists()


def test_json_stdout_carries_only_json(env: dict[str, str]) -> None:
    """`--json` has to be pipeable into a parser without pre-filtering, so
    every human-readable line must be on stderr instead."""
    result = _cli(["serve", str(STATIC_SITE), "--name", "d-pure", "--detach", "--json"], env)
    try:
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1, result.stdout
        json.loads(lines[0])  # parses whole-stdout, not just a substring
    finally:
        _stop("d-pure", env)


def test_detach_without_json_is_human_readable(env: dict[str, str]) -> None:
    result = _cli(["serve", str(STATIC_SITE), "--name", "d-human", "--detach"], env)
    try:
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "d-human" in combined
        assert "sidepage stop d-human" in combined
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)
    finally:
        _stop("d-human", env)


def test_attached_json_emits_payload_then_keeps_serving(env: dict[str, str]) -> None:
    """`--json` without `--detach` still blocks — but it must emit the same
    readiness line first, so a caller that wants to own the process itself
    gets the URL without parsing prose."""
    proc = subprocess.Popen(
        [SIDEPAGE_BIN, "serve", str(STATIC_SITE), "--name", "d-attached", "--json"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        payload = json.loads(line)
        assert payload["status"] == "running"
        assert payload["app"] == "d-attached"
        assert _get_status(payload["url"]) == 200
        assert proc.poll() is None, "attached --json must not exit after printing"
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def test_json_includes_token_when_auth_is_token(env: dict[str, str]) -> None:
    """A caller that gates an app needs the token programmatically — having
    to scrape it out of `info access token: …` was the old alternative."""
    result = _cli(
        ["serve", str(STATIC_SITE), "--name", "d-token", "--auth", "token", "--detach", "--json"],
        env,
    )
    try:
        payload = json.loads(result.stdout)
        assert payload["status"] == "running"
        # The detach parent reports from the registry, which holds no
        # secret; the token is in the child's own first log line.
        log = Path(payload["log"]).read_text()
        child_payload = json.loads(log.splitlines()[0])
        assert child_payload["auth"] == "token"
        assert child_payload["token"]
    finally:
        _stop("d-token", env)


def test_proxy_detach_wraps_running_service(env: dict[str, str]) -> None:
    upstream = _EchoServer()
    try:
        result = _cli(
            ["proxy", "--port", str(upstream.port), "--name", "d-proxy", "--detach", "--json"], env
        )
        payload = json.loads(result.stdout)
        assert payload["status"] == "running", payload
        assert payload["url"].startswith("http://127.0.0.1:")
        assert _get_status(payload["url"]) == 200
    finally:
        _stop("d-proxy", env)
        upstream.shutdown()


def test_detach_replays_a_registered_app(env: dict[str, str]) -> None:
    reg = _cli(["app", "register", str(STATIC_SITE), "d-registered"], env)
    assert reg.returncode == 0, reg.stdout + reg.stderr

    result = _cli(["serve", "d-registered", "--detach", "--json"], env)
    try:
        payload = json.loads(result.stdout)
        assert payload["status"] == "running", payload
        assert payload["app"] == "d-registered"
    finally:
        _stop("d-registered", env)


def test_detach_and_json_are_not_persisted_by_autoregister(env: dict[str, str]) -> None:
    """Both describe this call site, not the app. Registering them would
    make `sidepage serve <name>` silently detach later."""
    result = _cli(
        [
            "serve",
            str(STATIC_SITE),
            "--name",
            "d-autoreg",
            "--detach",
            "--json",
            "--autoregister",
        ],
        env,
    )
    try:
        assert json.loads(result.stdout)["status"] == "running"
        shown = _cli(["app", "show", "d-autoreg"], env)
        assert "--detach" not in shown.stdout
        assert "--json" not in shown.stdout
    finally:
        _stop("d-autoreg", env)


# `qrcode.print_tty` draws with ANSI background-colour runs, not block
# characters — this is the escape it emits for a light module.
_QR_MARKER = "\x1b[1;47m"


def _run_on_pty(args: list[str], env: dict[str, str]) -> str:
    """Run the CLI with stdout/stderr attached to a real pty and return
    everything it wrote. `--qr` refuses to render off a terminal, so a
    plain pipe would prove nothing about whether it works."""
    import pty

    pid, fd = pty.fork()
    if pid == 0:  # child
        os.environ.update(env)
        os.execv(SIDEPAGE_BIN, [SIDEPAGE_BIN, *args])
    chunks = []
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # pty closes with EIO when the child exits
            break
        if not chunk:
            break
        chunks.append(chunk)
    os.waitpid(pid, 0)
    return b"".join(chunks).decode(errors="replace")


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")
def test_detach_renders_the_qr_from_the_parent(env: dict[str, str]) -> None:
    """The child's stdout is a log file, where a QR can only fail. The
    parent is the process actually attached to a terminal, so it renders
    the code itself once the URL is known."""
    try:
        out = _run_on_pty(
            ["serve", str(STATIC_SITE), "--name", "d-qr", "--detach", "--qr"], env
        )
        assert _QR_MARKER in out, "no QR rendered on the parent's terminal"
        assert "can't render a QR code" not in out
    finally:
        _stop("d-qr", env)


def test_qr_never_reaches_stdout_under_json(env: dict[str, str]) -> None:
    """A QR code on stdout would corrupt the one line `--json` promises,
    so it goes to stderr with the rest of the human-facing output."""
    result = _cli(
        ["serve", str(STATIC_SITE), "--name", "d-qrjson", "--detach", "--qr", "--json"], env
    )
    try:
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1, result.stdout
        json.loads(lines[0])
        assert _QR_MARKER not in result.stdout
    finally:
        _stop("d-qrjson", env)


def test_qr_is_not_passed_to_the_detached_child(env: dict[str, str]) -> None:
    """If `--qr` reached the child it would try to draw into a log file,
    fail, and leave a misleading warning there."""
    result = _cli(["serve", str(STATIC_SITE), "--name", "d-qrlog", "--detach", "--qr"], env)
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        log = Path(env["SIDEPAGE_HOME"]) / "state" / "logs" / "d-qrlog.log"
        assert "can't render a QR code" not in log.read_text()
    finally:
        _stop("d-qrlog", env)


def test_stop_is_reported_by_status_after_detached_start(env: dict[str, str]) -> None:
    start = _cli(["serve", str(STATIC_SITE), "--name", "d-stop", "--detach", "--json"], env)
    payload = json.loads(start.stdout)
    assert payload["status"] == "running"

    stopped = _cli(["stop", "d-stop"], env)
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    time.sleep(1)
    with pytest.raises((urllib.error.URLError, OSError)):
        _get_status(payload["url"])
