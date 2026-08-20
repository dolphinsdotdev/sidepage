"""Integration tests for `sidepage proxy` (`sidepage.commands.proxy`,
`sidepage.core.process.proxy`) — wrapping an *already-running* local
service instead of a target `serve` launches itself.

Real subprocess/real HTTP tests (mirroring `tests/test_serve_integration.py`
and `tests/test_serve_v5.py`'s posture) cover: the round trip through the
proxy, the forwarded-header fix in `sidepage.core.reverse_proxy` (Host/
X-Forwarded-Host/-For/-Proto actually landing at the upstream), the
`--auth token` gate, and the one genuinely new behavior versus `serve` —
`sidepage stop` tearing down the proxy/tunnel/registry entry while
leaving the wrapped service running untouched. The "already running
service" in every case is a small in-process `http.server`, standing in
for something the user started themselves outside sidepage.

Fast, in-process `CliRunner` tests (mirroring `test_serve_v5.py`'s "fast,
in-process validation" section and `test_cli_smoke.py`'s style) cover the
`--type`/`--env`/`--guardrail`/`--peer` rejections — these must fail
before ever touching the network, since they're declared-but-rejected,
not silently accepted.

The BYO-domain test reuses the same fake-Cloudflare-API-plus-fake-
`cloudflared`-process approach `tests/test_tunnel_byo.py` uses for
`tunnel_manager` directly, self-contained here per that file's own
convention of not cross-importing test harnesses. `core.process.proxy()`
is called in-process (not via subprocess) with a short `--timeout` so it
self-terminates through its own normal teardown path — the only clean way
to end a function that otherwise blocks forever waiting for Ctrl+C.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from sidepage.cli import app as cli_app
from sidepage.core import account, secrets_vault, tunnel_manager

SIDEPAGE_BIN = str(Path(sys.executable).parent / "sidepage")

runner = CliRunner()


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


def _flat(text: str) -> str:
    return " ".join(text.split())


# --- in-process "already running" upstream: echoes received headers back
# as JSON, standing in for a service the user started outside sidepage ---


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps(dict(self.headers.items())).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # quiet test output
        pass


class _EchoServer:
    """A real HTTP server bound to an OS-assigned port, run on a daemon
    thread — this is deliberately not anything sidepage launched or knows
    about, matching what `proxy` is meant to wrap."""

    def __init__(self) -> None:
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _EchoHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def is_alive(self) -> bool:
        try:
            httpx.get(f"http://127.0.0.1:{self.port}/", timeout=2)
            return True
        except httpx.TransportError:
            return False

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)


@pytest.fixture
def echo_server() -> _EchoServer:
    server = _EchoServer()
    yield server
    server.shutdown()


# --- real-subprocess helpers, mirroring test_serve_integration.py / test_serve_v5.py ---


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


def test_proxy_omitted_name_defaults_to_proxy_port(
    sidepage_home: Path, echo_server: _EchoServer
) -> None:
    """No --name, no --domain/--anon: must default to proxy-<port> and
    actually come up under that name, not just pass CLI validation."""
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    expected_name = f"proxy-{echo_server.port}"
    proc = _run_proxy(["--port", str(echo_server.port)], env=env)
    try:
        app = _wait_for_registry_entry(sidepage_home, expected_name, timeout=15)
        assert app["name"] == expected_name
    finally:
        _stop(expected_name, env)
        proc.wait(timeout=15)


# --- round trip + forwarded headers ---


def test_proxy_round_trip_and_forwarded_headers(
    sidepage_home: Path, echo_server: _EchoServer
) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_proxy(
        ["--port", str(echo_server.port), "--name", "it-proxy-headers"], env=env
    )
    try:
        app = _wait_for_registry_entry(sidepage_home, "it-proxy-headers", timeout=15)
        resp = _poll_until_ready(
            f"http://127.0.0.1:{app['listen_port']}/",
            timeout=10,
        )
        # Confirm the round trip actually went through the wrapped service,
        # not just the proxy's own holding page.
        received = json.loads(resp.text)
        assert received["host"] == f"127.0.0.1:{app['listen_port']}"
        assert received["x-forwarded-host"] == f"127.0.0.1:{app['listen_port']}"
        assert received["x-forwarded-proto"] == "http"
        assert "x-forwarded-for" in received

        custom_host = httpx.get(
            f"http://127.0.0.1:{app['listen_port']}/",
            headers={"Host": "myapp-test.example.com"},
            timeout=5,
        )
        custom_received = json.loads(custom_host.text)
        assert custom_received["host"] == "myapp-test.example.com"
        assert custom_received["x-forwarded-host"] == "myapp-test.example.com"
    finally:
        _stop("it-proxy-headers", env)
        proc.wait(timeout=15)

    assert echo_server.is_alive(), "proxy's own upstream must not be affected by its own teardown"


def test_proxy_auth_token_gate(sidepage_home: Path, echo_server: _EchoServer) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_proxy(
        [
            "--port",
            str(echo_server.port),
            "--name",
            "it-proxy-auth",
            "--auth",
            "token",
            "--token",
            "test-token-xyz",
        ],
        env=env,
    )
    try:
        app = _wait_for_registry_entry(sidepage_home, "it-proxy-auth", timeout=15)
        no_cred = httpx.get(f"http://127.0.0.1:{app['listen_port']}/", timeout=5)
        assert no_cred.status_code == 401

        with_cred = _poll_until_ready(
            f"http://127.0.0.1:{app['listen_port']}/?token=test-token-xyz", timeout=10
        )
        assert with_cred.status_code == 200
    finally:
        _stop("it-proxy-auth", env)
        proc.wait(timeout=15)


# --- the one genuinely new behavior: stop tears down the proxy, not the service ---


def test_proxy_stop_leaves_service_running(
    sidepage_home: Path, echo_server: _EchoServer
) -> None:
    env = {**os.environ, "SIDEPAGE_HOME": str(sidepage_home)}
    proc = _run_proxy(
        ["--port", str(echo_server.port), "--name", "it-proxy-teardown"], env=env
    )
    app = _wait_for_registry_entry(sidepage_home, "it-proxy-teardown", timeout=15)
    _poll_until_ready(f"http://127.0.0.1:{app['listen_port']}/", timeout=10)

    _stop("it-proxy-teardown", env)
    proc.wait(timeout=15)

    _wait_for_registry_entry_gone(sidepage_home, "it-proxy-teardown", timeout=10)
    # The proxy's own listen port must be gone (teardown really happened)...
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{app['listen_port']}/", timeout=2)
    # ...but the service `proxy` was pointed at was never sidepage's to
    # stop, and must still answer directly.
    assert echo_server.is_alive()
    stdout = proc.stdout.read() if proc.stdout else ""
    assert "was not touched and may still be running" in _flat(stdout)


# --- fast, in-process: declared-but-rejected flags fail before touching the network ---


def test_proxy_rejects_type_flag() -> None:
    result = runner.invoke(
        cli_app, ["proxy", "--port", "1", "--name", "x", "--type", "static"]
    )
    assert result.exit_code == 1, result.output
    assert "--type doesn't apply to" in _flat(result.output)


def test_proxy_rejects_env_flag() -> None:
    result = runner.invoke(cli_app, ["proxy", "--port", "1", "--name", "x", "--env", "FOO"])
    assert result.exit_code == 1, result.output
    assert "--env doesn't apply to" in _flat(result.output)


def test_proxy_rejects_guardrail_flag() -> None:
    result = runner.invoke(
        cli_app, ["proxy", "--port", "1", "--name", "x", "--guardrail", "config.yaml"]
    )
    assert result.exit_code == 1, result.output
    assert "--guardrail doesn't apply to" in _flat(result.output)


def test_proxy_rejects_peer_flag() -> None:
    result = runner.invoke(
        cli_app, ["proxy", "--port", "1", "--name", "x", "--peer", "api=some-app"]
    )
    assert result.exit_code == 1, result.output
    assert "--peer doesn't apply to" in _flat(result.output)


def test_proxy_name_required_with_anon() -> None:
    result = runner.invoke(cli_app, ["proxy", "--port", "1", "--anon"])
    assert result.exit_code == 1, result.output
    assert "--name is required" in _flat(result.output)


def test_proxy_name_required_with_domain() -> None:
    """The --name check must fire before domain validation (this domain
    isn't even configured), since a missing --name is checked in the CLI
    layer before core_proxy is ever called — proven by getting the --name
    error, not the "isn't configured" one, from an unconfigured domain."""
    result = runner.invoke(cli_app, ["proxy", "--port", "1", "--domain", "example.com"])
    assert result.exit_code == 1, result.output
    assert "--name is required" in _flat(result.output)
    assert "isn't configured" not in result.output


def test_proxy_domain_and_anon_mutually_exclusive() -> None:
    result = runner.invoke(
        cli_app,
        ["proxy", "--port", "1", "--name", "x", "--domain", "example.com", "--anon"],
    )
    assert result.exit_code == 1, result.output
    assert "mutually exclusive" in result.output


def test_proxy_domain_without_config_rejected() -> None:
    result = runner.invoke(
        cli_app, ["proxy", "--port", "1", "--name", "x", "--domain", "example.com"]
    )
    assert result.exit_code == 1, result.output
    assert "isn't configured" in _flat(result.output)


# --- BYO-domain: same fake-Cloudflare/fake-cloudflared approach as test_tunnel_byo.py ---


class _StatefulCloudflareTransport(httpx.BaseTransport):
    """Minimal, self-contained fake of the Cloudflare API surface
    `tunnel_manager` touches for BYO-domain — duplicated from
    `tests/test_tunnel_byo.py` rather than imported, matching this
    project's existing convention of self-contained test files (see e.g.
    `test_cli_smoke.py`'s own smaller single-purpose fake)."""

    def __init__(self, *, zone_id: str = "zone-1", account_id: str = "acct-1") -> None:
        self.zone_id = zone_id
        self.account_id = account_id
        self.dns_records: dict[str, dict] = {}
        self.tunnels: dict[str, dict] = {}
        self._next_tunnel_id = 1

    def _ok(self, result) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": result})

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/client/v4/zones":
            return self._ok([{"id": self.zone_id, "account": {"id": self.account_id}}])

        if path == f"/client/v4/accounts/{self.account_id}/cfd_tunnel" and method == "POST":
            tunnel_id = f"tun-{self._next_tunnel_id}"
            self._next_tunnel_id += 1
            self.tunnels[tunnel_id] = {"ingress": []}
            token = base64.b64encode(
                json.dumps({"a": self.account_id, "t": tunnel_id, "s": "sec"}).encode()
            ).decode()
            return self._ok({"id": tunnel_id, "token": token})

        if path == f"/client/v4/zones/{self.zone_id}/dns_records":
            if method == "GET":
                name = request.url.params.get("name")
                record = self.dns_records.get(name)
                return self._ok([record] if record else [])
            if method == "POST":
                body = json.loads(request.content)
                self.dns_records[body["name"]] = {"id": "rec-1", **body}
                return self._ok({"id": "rec-1"})

        for tunnel_id in self.tunnels:
            config_path = (
                f"/client/v4/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/configurations"
            )
            if path == config_path:
                if method == "GET":
                    return self._ok({"config": {"ingress": self.tunnels[tunnel_id]["ingress"]}})
                if method == "PUT":
                    body = json.loads(request.content)
                    self.tunnels[tunnel_id]["ingress"] = body["config"]["ingress"]
                    return self._ok({"config": body["config"]})

        return httpx.Response(404, json={"success": False, "errors": [f"unhandled: {path}"]})


class _FakeProcess:
    _next_pid = 90001

    def __init__(self) -> None:
        self.pid = _FakeProcess._next_pid
        _FakeProcess._next_pid += 1
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15


def test_proxy_byo_domain_opens_tunnel_and_registers(
    sidepage_home: Path, echo_server: _EchoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`proxy --domain` must reach `tunnel_manager.open_byo_tunnel` with
    this app's own listen port (not `--port`, the upstream) and create a
    matching DNS record — the same wiring `serve --domain` uses, exercised
    end to end via a short `--timeout` so `core.process.proxy()` (which
    otherwise blocks until Ctrl+C) self-terminates through its own normal
    teardown path instead of needing a signal sent across threads.

    `open_byo_tunnel` is spied on (call-through, not replaced) rather than
    inspected via post-teardown Cloudflare state, because `close_tunnel`'s
    BYO-domain branch removes the ingress rule as part of normal teardown
    — by the time `core_proxy(config)` returns, the ingress rule this test
    would otherwise want to inspect is already gone. The DNS record isn't
    touched by teardown, so it's checked as a secondary, teardown-durable
    signal that the right hostname was provisioned.
    """
    real_client = httpx.Client
    transport = _StatefulCloudflareTransport()

    def fake_client(*a, **k):
        return real_client(*a, **{**k, "transport": transport})

    def fake_popen(*a, **k):
        return _FakeProcess()

    monkeypatch.setattr(httpx, "Client", fake_client)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel_manager, "resolve_cloudflared_binary", lambda **k: Path("/bin/true"))

    real_open_byo_tunnel = tunnel_manager.open_byo_tunnel
    captured: dict = {}

    def spy_open_byo_tunnel(app_name, domain, listen_port, **kwargs):
        captured["app_name"] = app_name
        captured["listen_port"] = listen_port
        return real_open_byo_tunnel(app_name, domain, listen_port, **kwargs)

    monkeypatch.setattr(tunnel_manager, "open_byo_tunnel", spy_open_byo_tunnel)

    secrets_vault.set_secret("cf-api-tok", "fake-api-token")
    domain_config = account.configure_domain("example.com", api_token_name="cf-api-tok")

    from sidepage.core.auth import AuthTier
    from sidepage.core.directory_client import Scope
    from sidepage.core.process import ProxyConfig, proxy as core_proxy

    config = ProxyConfig(
        port=echo_server.port,
        name="it-byo-proxy",
        domain="example.com",
        auth=AuthTier.OPEN,
        scope=Scope.LOCAL,
        timeout=0.2,
    )
    core_proxy(config)  # blocks until --timeout fires, then tears itself down

    assert domain_config.domain == "example.com"
    assert captured["app_name"] == "it-byo-proxy"
    # The tunnel must route to *this app's own* sidepage-allocated proxy
    # port, not to `--port` (the upstream echo server) — proxy tunnels to
    # its own proxy layer, same as serve does.
    assert captured["listen_port"] != echo_server.port

    hostname = next(iter(transport.dns_records))
    assert hostname.startswith("it-byo-proxy") and hostname.endswith(".example.com")

    assert echo_server.is_alive()
