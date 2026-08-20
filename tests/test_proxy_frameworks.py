"""`sidepage proxy` against two real frameworks, each chosen to exercise
one documented caveat from `sidepage.commands.proxy --help` for real
rather than by assertion alone:

- **Flask** (`tests/fixtures/flask-app`) — the Origin/Host/CSRF caveat
  ("forwarding helps only an app configured to trust it") and the
  localhost-trust security warning, both demonstrated directly against a
  real Werkzeug dev server.
- **Vite** (`tests/fixtures/vite-app`) — the `server.allowedHosts`
  rejection and its wildcard fix, plus a genuinely new finding made while
  building this fixture: Vite's dev server binds IPv6 loopback (`::1`)
  only when started without `--host`, and `sidepage proxy` only ever
  dials `127.0.0.1` (IPv4) — so a *bare* `npm run dev` is unreachable
  through `proxy` at all, independent of the `allowedHosts` question.
  Locked in here as a real, reproduced regression case, not just prose.

Both fixtures are launched directly (`uv run .../app.py`, `npm run dev`),
never through `sidepage serve` — the point of `proxy` is wrapping
something sidepage didn't start, so the tests reproduce that setup
faithfully rather than going through sidepage's own launch path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from sidepage.core.ecosystem import resolve_python_runner

FIXTURES = Path(__file__).parent / "fixtures"
FLASK_APP = FIXTURES / "flask-app"
VITE_APP = FIXTURES / "vite-app"
SIDEPAGE_BIN = str(Path(sys.executable).parent / "sidepage")


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


def _run_proxy(args: list[str], env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [SIDEPAGE_BIN, "proxy", *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop(name: str, env: dict[str, str]) -> None:
    subprocess.run([SIDEPAGE_BIN, "stop", name], env=env, capture_output=True, timeout=15)


def _poll_until_ready(url: str, *, timeout: float, headers: dict[str, str] | None = None) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last: httpx.Response | None = None
    while time.monotonic() < deadline:
        try:
            last = httpx.get(url, headers=headers, timeout=5)
            if "starting…" not in last.text:
                return last
        except httpx.TransportError:
            pass
        time.sleep(0.3)
    assert last is not None, f"never got any response from {url}"
    return last


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- Flask fixture: launched directly, never through `sidepage serve` ---


class _FlaskFixture:
    def __init__(self, *, trust_proxy: bool) -> None:
        self.port = _free_port()
        runner = resolve_python_runner(FLASK_APP)
        env = {**os.environ, "PORT": str(self.port)}
        if trust_proxy:
            env["TRUST_PROXY"] = "1"
        self.proc = subprocess.Popen(
            [*runner, "python", str(FLASK_APP / "app.py")],
            cwd=FLASK_APP,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{self.port}/", timeout=2)
                return
            except httpx.TransportError:
                time.sleep(0.3)
        raise TimeoutError("flask fixture never came up")

    def is_alive(self) -> bool:
        try:
            httpx.get(f"http://127.0.0.1:{self.port}/", timeout=2)
            return True
        except httpx.TransportError:
            return False

    def shutdown(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def flask_naive():
    app = _FlaskFixture(trust_proxy=False)
    yield app
    app.shutdown()


@pytest.fixture
def flask_trusted():
    app = _FlaskFixture(trust_proxy=True)
    yield app
    app.shutdown()


def test_flask_naive_gets_correct_host_but_ignores_forwarded_proto(
    sidepage_home: Path, flask_naive: _FlaskFixture
) -> None:
    """Corrects an assumption made while writing this test, worth stating
    plainly: `request.host` in Flask (and most WSGI/ASGI frameworks) reads
    straight from the *literal* `Host` header of the request it received
    — since sidepage forwards the real `Host` directly (Caddy-style, not
    nginx's clobber-then-rely-on-X-Forwarded-Host), Flask gets the right
    `request.host` with **zero app-side config**, ProxyFix or not.

    `request.scheme` is the real differentiator: it's derived from
    `X-Forwarded-Proto` only when the app explicitly opts in (ProxyFix)
    — without it, a naive app reports "http" even when the header says
    "https", because Werkzeug doesn't trust that header by default (an
    untrusted client could otherwise spoof it)."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_proxy(["--port", str(flask_naive.port), "--name", "it-flask-naive"], env=env)
    try:
        app = _wait_for_registry_entry(sidepage_home, "it-flask-naive", timeout=15)
        resp = _poll_until_ready(
            f"http://127.0.0.1:{app['listen_port']}/whoami",
            timeout=15,
            headers={"Host": "myapp.example.com", "X-Forwarded-Proto": "https"},
        )
        data = resp.json()
        assert data["host"] == "myapp.example.com"  # correct with no app config at all
        assert data["scheme"] == "http"  # NOT https — ProxyFix wasn't asked to trust it
    finally:
        _stop("it-flask-naive", env)
        proc.wait(timeout=15)


def test_flask_proxyfix_trusts_forwarded_proto(
    sidepage_home: Path, flask_trusted: _FlaskFixture
) -> None:
    """The one-time fix `proxy --help` recommends for Flask actually
    works: with ProxyFix wired in, `request.scheme` correctly reflects
    the forwarded `X-Forwarded-Proto` (relayed by sidepage's proxy
    verbatim when the inbound request already carries one — see
    `sidepage.core.reverse_proxy._forwarded_headers`) instead of the
    plain-HTTP scheme sidepage's own listen socket always uses."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_proxy(["--port", str(flask_trusted.port), "--name", "it-flask-trusted"], env=env)
    try:
        app = _wait_for_registry_entry(sidepage_home, "it-flask-trusted", timeout=15)
        resp = _poll_until_ready(
            f"http://127.0.0.1:{app['listen_port']}/whoami",
            timeout=15,
            headers={"Host": "myapp.example.com", "X-Forwarded-Proto": "https"},
        )
        data = resp.json()
        assert data["host"] == "myapp.example.com"
        assert data["scheme"] == "https"
        assert data["url_root"] == "https://myapp.example.com/"
    finally:
        _stop("it-flask-trusted", env)
        proc.wait(timeout=15)


def test_flask_localhost_trust_defeated_through_proxy(
    sidepage_home: Path, flask_naive: _FlaskFixture
) -> None:
    """The security warning, demonstrated rather than asserted: a
    real "only localhost may reach this" check
    (`request.remote_addr != "127.0.0.1"`) — the same mechanism Werkzeug's
    own interactive debugger and countless admin/debug endpoints use —
    passes through the proxy regardless of who the real caller was,
    because every proxied request genuinely does arrive at Flask from
    127.0.0.1 (sidepage's own proxy process), not from wherever the actual
    caller connected from."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_proxy(["--port", str(flask_naive.port), "--name", "it-flask-admin"], env=env)
    try:
        app = _wait_for_registry_entry(sidepage_home, "it-flask-admin", timeout=15)
        resp = _poll_until_ready(f"http://127.0.0.1:{app['listen_port']}/debug/admin", timeout=15)
        assert resp.status_code == 200
        assert "only reachable from localhost" in resp.text
    finally:
        _stop("it-flask-admin", env)
        proc.wait(timeout=15)


def test_proxy_stop_leaves_flask_running(sidepage_home: Path, flask_naive: _FlaskFixture) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_proxy(["--port", str(flask_naive.port), "--name", "it-flask-teardown"], env=env)
    app = _wait_for_registry_entry(sidepage_home, "it-flask-teardown", timeout=15)
    _poll_until_ready(f"http://127.0.0.1:{app['listen_port']}/", timeout=15)

    _stop("it-flask-teardown", env)
    proc.wait(timeout=15)

    assert flask_naive.is_alive()


# --- Vite fixture: launched directly via npm, never through `sidepage serve` ---


def _npm_bin() -> str:
    found = shutil.which("npm")
    if found is None:
        pytest.skip("npm not available")
    return found


@pytest.fixture(scope="module", autouse=True)
def _ensure_vite_installed() -> None:
    if not (VITE_APP / "node_modules" / ".bin" / "vite").exists():
        subprocess.run(
            [_npm_bin(), "install", "--no-audit", "--no-fund"],
            cwd=VITE_APP,
            check=True,
            timeout=180,
        )


class _ViteFixture:
    def __init__(self, *, host: str | None = "127.0.0.1", config_dir: Path | None = None) -> None:
        self.port = _free_port()
        argv = [_npm_bin(), "exec", "vite", "--", "--port", str(self.port), "--strictPort"]
        if host is not None:
            argv += ["--host", host]
        self.proc = subprocess.Popen(
            argv,
            cwd=config_dir or VITE_APP,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def wait_ready(self, *, host: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"vite exited early ({self.proc.returncode}):\n{out}")
            try:
                httpx.get(f"http://{host}:{self.port}/", timeout=2)
                return
            except httpx.TransportError:
                time.sleep(0.3)
        raise TimeoutError("vite fixture never came up")

    def shutdown(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def vite_app():
    app = _ViteFixture(host="127.0.0.1")
    app.wait_ready(host="127.0.0.1")
    yield app
    app.shutdown()


def test_vite_bare_default_reachable_via_proxy_ipv6_fallback(sidepage_home: Path) -> None:
    """Verifies the fix for a real finding made while building this
    fixture: a *bare* `npm run dev` (no `--host`) binds Vite's dev server
    to IPv6 loopback (`::1`) only — `serve`'s own launchers never hit this
    since sidepage always passes an explicit `--host`/`--server.address
    127.0.0.1` at spawn time, but `proxy` wraps a process it never
    launched and can't control the bind address of. `sidepage proxy` now
    falls back to `[::1]` when a plain `127.0.0.1` dial doesn't answer
    (`sidepage.core.reverse_proxy.check_upstream_ready`/`UpstreamAddress`)
    instead of leaving such a server permanently unreachable."""
    bare = _ViteFixture(host=None)
    try:
        bare.wait_ready(host="[::1]")
        # The exact thing that used to fail before the fix — confirms the
        # gap this test locks in is real, not just theoretical.
        with pytest.raises(httpx.TransportError):
            httpx.get(f"http://127.0.0.1:{bare.port}/", timeout=2)

        env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
        proc = _run_proxy(["--port", str(bare.port), "--name", "it-vite-ipv6"], env=env)
        try:
            app = _wait_for_registry_entry(sidepage_home, "it-vite-ipv6", timeout=15)
            resp = _poll_until_ready(f"http://127.0.0.1:{app['listen_port']}/", timeout=15)
            assert resp.status_code == 200
            assert "vite fixture ok" in resp.text
        finally:
            _stop("it-vite-ipv6", env)
            proc.wait(timeout=15)
    finally:
        bare.shutdown()


def test_vite_default_allowedHosts_accepts_local_ip_host(
    sidepage_home: Path, vite_app: _ViteFixture
) -> None:
    """A numeric IP `Host` (what `sidepage proxy` sends for plain local
    access, no --domain/--anon) is accepted by Vite's default
    `allowedHosts` — the rejection only triggers for a real hostname,
    confirmed below."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_proxy(["--port", str(vite_app.port), "--name", "it-vite-local"], env=env)
    try:
        app = _wait_for_registry_entry(sidepage_home, "it-vite-local", timeout=15)
        resp = _poll_until_ready(f"http://127.0.0.1:{app['listen_port']}/", timeout=15)
        assert resp.status_code == 200
        assert "vite fixture ok" in resp.text
    finally:
        _stop("it-vite-local", env)
        proc.wait(timeout=15)


def test_vite_rejects_unrecognized_hostname_through_proxy(
    sidepage_home: Path, vite_app: _ViteFixture
) -> None:
    """The documented caveat, reproduced for real: a hostname (standing in
    for a --domain/--anon tunnel hostname) that isn't on Vite's
    `allowedHosts` gets rejected with exactly the message `proxy --help`
    describes — forwarding the real Host correctly is what makes Vite see
    it at all, and seeing it is what gets it rejected."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_proxy(["--port", str(vite_app.port), "--name", "it-vite-blocked"], env=env)
    try:
        app = _wait_for_registry_entry(sidepage_home, "it-vite-blocked", timeout=15)
        _poll_until_ready(f"http://127.0.0.1:{app['listen_port']}/", timeout=15)
        resp = httpx.get(
            f"http://127.0.0.1:{app['listen_port']}/",
            headers={"Host": "myapp-test.example.com"},
            timeout=5,
        )
        assert resp.status_code == 403
        assert "not allowed" in resp.text
        assert "server.allowedHosts" in resp.text
    finally:
        _stop("it-vite-blocked", env)
        proc.wait(timeout=15)


def test_vite_wildcard_allowedHosts_fixes_it(
    sidepage_home: Path, tmp_path: Path
) -> None:
    """The one-time fix `proxy --help` recommends for `--anon` (a
    `.`-prefixed wildcard, since the hostname changes every run) actually
    works against a real Vite dev server, not just Django's documented
    equivalent."""
    fixed_dir = tmp_path / "vite-fixed"
    shutil.copytree(VITE_APP, fixed_dir, ignore=shutil.ignore_patterns("node_modules"))
    (fixed_dir / "node_modules").symlink_to(VITE_APP / "node_modules")
    (fixed_dir / "vite.config.js").write_text(
        'import { defineConfig } from "vite";\n'
        "export default defineConfig({ server: { allowedHosts: ['.example.com'] } });\n"
    )

    app = _ViteFixture(host="127.0.0.1", config_dir=fixed_dir)
    app.wait_ready(host="127.0.0.1")
    env = {**os.environ, "SIDEPAGE_HOME": str(tmp_path / "home")}
    proc = _run_proxy(["--port", str(app.port), "--name", "it-vite-fixed"], env=env)
    try:
        entry = _wait_for_registry_entry(tmp_path / "home", "it-vite-fixed", timeout=15)
        _poll_until_ready(f"http://127.0.0.1:{entry['listen_port']}/", timeout=15)
        resp = httpx.get(
            f"http://127.0.0.1:{entry['listen_port']}/",
            headers={"Host": "myapp-test.example.com"},
            timeout=5,
        )
        assert resp.status_code == 200
        assert "vite fixture ok" in resp.text
    finally:
        _stop("it-vite-fixed", env)
        proc.wait(timeout=15)
        app.shutdown()
