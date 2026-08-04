"""Tests for BYO-domain tunneling (`sidepage.core.tunnel_manager`) — v4
delta: a single Cloudflare API token that provisions its own tunnel
(`provision_byo_domain`), one `cloudflared` process shared across every
app on a domain (`_ensure_shared_tunnel_running`/
`_stop_shared_tunnel_if_unused`, reference-counted against
`sidepage.core.registry`), and ingress routing mutated remotely via
GET-modify-PUT (`_upsert_ingress_rule`/`_remove_ingress_rule`) instead of
one `cloudflared --url` process per app.

No real Cloudflare account is used here — `decode_tunnel_token` is pure
(no I/O) and tested directly; every Cloudflare API call and `cloudflared`
subprocess is mocked via `_StatefulCloudflareTransport`, a fake that
actually keeps zone/tunnel/DNS/ingress state across requests within a
test (not just canned single responses) — that statefulness is what lets
the GET-modify-PUT idempotency tests mean anything. End-to-end
verification against a real Cloudflare zone was done manually, not in
this automated suite — see `docs/CHECKLIST.md` §6.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from sidepage.core import registry, secrets_vault, tunnel_manager
from sidepage.core.exceptions import TunnelError
from sidepage.core.tunnel_manager import decode_tunnel_token

# Captured before any test monkeypatches httpx.Client — every fake client
# factory below must build on the *real* Client, not the (by-then-patched)
# name `httpx.Client`, or it recurses into itself.
_RealHttpxClient = httpx.Client


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


def _make_token(
    *,
    account_id: str = "acct123",
    tunnel_id: str = "tun-abc-123",
    secret: str = "s3cr3t",
    urlsafe: bool = False,
) -> str:
    payload = json.dumps({"a": account_id, "t": tunnel_id, "s": secret}).encode()
    encoder = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return encoder(payload).decode().rstrip("=")


# --- decode_tunnel_token: pure, no I/O ---


def test_decode_tunnel_token_standard_base64() -> None:
    decoded = decode_tunnel_token(_make_token())
    assert decoded.account_id == "acct123"
    assert decoded.tunnel_id == "tun-abc-123"


def test_decode_tunnel_token_urlsafe_base64() -> None:
    decoded = decode_tunnel_token(_make_token(urlsafe=True))
    assert decoded.tunnel_id == "tun-abc-123"


def test_decode_tunnel_token_garbage_raises() -> None:
    with pytest.raises(TunnelError):
        decode_tunnel_token("not-valid-base64-json-!!!")


def test_decode_tunnel_token_missing_fields_raises() -> None:
    token = base64.b64encode(json.dumps({"x": "y"}).encode()).decode()
    with pytest.raises(TunnelError):
        decode_tunnel_token(token)


def test_decode_tunnel_token_trailing_newline_still_decodes() -> None:
    decoded = decode_tunnel_token(_make_token(tunnel_id="tun-newline") + "\n")
    assert decoded.tunnel_id == "tun-newline"


def test_decode_tunnel_token_stray_percent_still_decodes() -> None:
    decoded = decode_tunnel_token(_make_token(tunnel_id="tun-percent") + "%")
    assert decoded.tunnel_id == "tun-percent"


# --- shared fake Cloudflare backend: stateful across requests within a test ---


class _StatefulCloudflareTransport(httpx.BaseTransport):
    """Fakes just enough of the Cloudflare API surface `tunnel_manager`
    touches, with real state (not canned single responses) so multi-call
    sequences — GET-modify-PUT ingress updates in particular — actually
    exercise something meaningful. One zone, one tunnel, created on
    first use."""

    def __init__(self, *, zone_id: str = "zone-id-123", account_id: str = "acct-123") -> None:
        self.zone_id = zone_id
        self.account_id = account_id
        self.dns_records: dict[str, dict] = {}  # hostname -> record
        self._next_dns_id = 1
        self.tunnels: dict[str, dict] = {}  # tunnel_id -> {"ingress": [...]}
        self._next_tunnel_id = 1
        self.requests: list[httpx.Request] = []

    def _ok(self, result) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": result})

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method

        if path == "/client/v4/zones":
            return self._ok([{"id": self.zone_id, "account": {"id": self.account_id}}])

        if path == f"/client/v4/accounts/{self.account_id}/cfd_tunnel" and method == "POST":
            tunnel_id = f"tun-{self._next_tunnel_id}"
            self._next_tunnel_id += 1
            self.tunnels[tunnel_id] = {"ingress": []}
            token = _make_token(account_id=self.account_id, tunnel_id=tunnel_id)
            return self._ok({"id": tunnel_id, "token": token})

        if path == f"/client/v4/zones/{self.zone_id}/dns_records":
            if method == "GET":
                name = request.url.params.get("name")
                record = self.dns_records.get(name)
                return self._ok([record] if record else [])
            if method == "POST":
                body = json.loads(request.content)
                record_id = f"rec-{self._next_dns_id}"
                self._next_dns_id += 1
                self.dns_records[body["name"]] = {"id": record_id, **body}
                return self._ok({"id": record_id})

        if path.startswith(f"/client/v4/zones/{self.zone_id}/dns_records/") and method == "PUT":
            record_id = path.rsplit("/", 1)[-1]
            body = json.loads(request.content)
            self.dns_records[body["name"]] = {"id": record_id, **body}
            return self._ok({"id": record_id})

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

        errors = [f"unhandled: {method} {path}"]
        return httpx.Response(404, json={"success": False, "errors": errors})


class _FakeProcess:
    """Stands in for subprocess.Popen for the shared cloudflared process.
    `pid` is synthetic (not a real OS pid) — tests that need liveness
    checks to behave monkeypatch `tunnel_manager._is_pid_alive` /
    `tunnel_manager.os.kill` rather than relying on a real process, since
    signaling a *real* pid from a test would be unsafe."""

    _next_pid = 90001

    def __init__(self) -> None:
        self.pid = _FakeProcess._next_pid
        _FakeProcess._next_pid += 1
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def _patch_cloudflare(
    monkeypatch: pytest.MonkeyPatch, *, popen_exits_early: bool = False
) -> _StatefulCloudflareTransport:
    transport = _StatefulCloudflareTransport()

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return _RealHttpxClient(*args, **kwargs)

    def fake_popen(*a, **k):
        proc = _FakeProcess()
        if popen_exits_early:
            proc.returncode = 1
        return proc

    monkeypatch.setattr(httpx, "Client", fake_client)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        tunnel_manager, "resolve_cloudflared_binary", lambda **k: Path("/usr/bin/true")
    )
    monkeypatch.setattr(tunnel_manager.time, "sleep", lambda *_: None)
    return transport


class _PidTracker:
    """Backs a fake replacement for tunnel_manager's `_is_pid_alive` and
    `os.kill` so shared-process lifecycle tests can simulate a process
    dying on SIGTERM without ever touching a real OS process."""

    def __init__(self) -> None:
        self.alive: set[int] = set()
        self.signals: list[tuple[int, int]] = []

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    def kill(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        self.alive.discard(pid)


class _FakeOsModule:
    """Stands in for the `os` name inside `tunnel_manager`'s own module
    namespace — swapped in via `monkeypatch.setattr(tunnel_manager, "os",
    ...)`, which rebinds the name *tunnel_manager.py itself* looks up,
    not the real `os` module object. Mutating the real module's `kill`
    attribute instead (`tunnel_manager.os.kill = ...`) would be a global
    change: `sidepage.core.registry` does its own real `os.kill(pid, 0)`
    liveness checks, and those must keep working normally in these tests
    — this scoping is what keeps the fake from leaking into it."""

    def __init__(self, tracker: _PidTracker) -> None:
        self._tracker = tracker

    def kill(self, pid: int, sig: int) -> None:
        self._tracker.kill(pid, sig)


@pytest.fixture
def pid_tracker(monkeypatch: pytest.MonkeyPatch) -> _PidTracker:
    tracker = _PidTracker()
    monkeypatch.setattr(tunnel_manager, "_is_pid_alive", tracker.is_alive)
    monkeypatch.setattr(tunnel_manager, "os", _FakeOsModule(tracker))
    return tracker


def _provision(monkeypatch: pytest.MonkeyPatch, *, domain: str = "example.com") -> tuple:
    """Shared setup: provisions a domain against the fake transport and
    stores its two vault secrets, returning (transport, provisioned)."""
    transport = _patch_cloudflare(monkeypatch)
    api_token = "fake-api-token"
    secrets_vault.set_secret(f"api-tok-{domain}", api_token)
    provisioned = tunnel_manager.provision_byo_domain(domain, api_token)
    secrets_vault.set_secret(f"tunnel-tok-{domain}", provisioned.tunnel_token)
    return transport, provisioned


# --- provision_byo_domain ---


def test_provision_byo_domain_creates_tunnel(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, provisioned = _provision(monkeypatch)
    assert provisioned.zone_id == transport.zone_id
    assert provisioned.account_id == transport.account_id
    assert provisioned.tunnel_id in transport.tunnels
    decoded = decode_tunnel_token(provisioned.tunnel_token)
    assert decoded.tunnel_id == provisioned.tunnel_id


def test_provision_byo_domain_missing_zone_raises(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _NoZoneTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/client/v4/zones":
                return httpx.Response(200, json={"success": True, "result": []})
            return httpx.Response(404, json={"success": False, "errors": ["unreachable"]})

    def fake_client(*a, **k):
        return _RealHttpxClient(*a, **{**k, "transport": _NoZoneTransport()})

    monkeypatch.setattr(httpx, "Client", fake_client)
    with pytest.raises(TunnelError, match="no Cloudflare zone found"):
        tunnel_manager.provision_byo_domain("no-such-zone.example", "tok")


# --- open_byo_tunnel: CNAME + ingress wiring ---


def _open(
    transport: _StatefulCloudflareTransport, provisioned, app_name: str, port: int, *, domain: str
) -> tunnel_manager.TunnelHandle:
    return tunnel_manager.open_byo_tunnel(
        app_name,
        domain,
        port,
        zone_id=provisioned.zone_id,
        account_id=provisioned.account_id,
        tunnel_id=provisioned.tunnel_id,
        api_token_name=f"api-tok-{domain}",
        tunnel_token_name=f"tunnel-tok-{domain}",
    )


def test_open_byo_tunnel_creates_cname_and_ingress_rule(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, provisioned = _provision(monkeypatch)

    handle = _open(transport, provisioned, "myapp", 4321, domain="example.com")

    assert handle.url.startswith("https://myapp-")
    assert handle.url.endswith(".example.com")
    assert handle.mode is tunnel_manager.TunnelMode.BYO_DOMAIN
    assert handle.process is None  # shared process, not owned by this handle

    hostname = handle.hostname
    assert hostname in transport.dns_records
    record = transport.dns_records[hostname]
    assert record["type"] == "CNAME"
    assert record["content"] == f"{provisioned.tunnel_id}.cfargotunnel.com"

    ingress = transport.tunnels[provisioned.tunnel_id]["ingress"]
    assert {"hostname": hostname, "service": "http://127.0.0.1:4321"} in ingress
    assert ingress[-1] == {"service": "http_status:404"}  # catch-all always last


def test_open_byo_tunnel_hostname_stable_across_calls(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, provisioned = _provision(monkeypatch)
    first = _open(transport, provisioned, "stableapp", 2222, domain="example.com")
    second = _open(transport, provisioned, "stableapp", 3333, domain="example.com")
    assert first.url == second.url
    assert first.hostname == second.hostname


def test_open_byo_tunnel_ingress_upsert_is_idempotent_not_duplicated(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running for the same app/port shouldn't grow the ingress array —
    the old rule must be replaced, not appended alongside a new one, and
    there must be exactly one trailing catch-all no matter how many times
    this runs."""
    transport, provisioned = _provision(monkeypatch)
    _open(transport, provisioned, "sameapp", 1111, domain="example.com")
    _open(transport, provisioned, "sameapp", 1111, domain="example.com")
    _open(transport, provisioned, "sameapp", 1111, domain="example.com")

    ingress = transport.tunnels[provisioned.tunnel_id]["ingress"]
    hostnamed_rules = [r for r in ingress if "hostname" in r]
    catch_alls = [r for r in ingress if r == {"service": "http_status:404"}]
    assert len(hostnamed_rules) == 1
    assert len(catch_alls) == 1


def test_open_byo_tunnel_two_apps_share_one_ingress_config(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different apps on the same domain must both end up routed
    through the same tunnel's ingress config — neither upsert should wipe
    out the other's rule (the GET-modify-PUT contract)."""
    transport, provisioned = _provision(monkeypatch)
    h1 = _open(transport, provisioned, "app-one", 5001, domain="example.com")
    h2 = _open(transport, provisioned, "app-two", 5002, domain="example.com")

    ingress = transport.tunnels[provisioned.tunnel_id]["ingress"]
    hostnames = {r["hostname"] for r in ingress if "hostname" in r}
    assert hostnames == {h1.hostname, h2.hostname}
    assert ingress[-1] == {"service": "http_status:404"}


# --- shared cloudflared process: spawned once, killed only when unused ---


def test_shared_tunnel_process_spawned_once_for_two_apps(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch, pid_tracker: _PidTracker
) -> None:
    transport, provisioned = _provision(monkeypatch)
    spawn_calls = []
    real_popen = subprocess.Popen

    def counting_popen(*a, **k):
        proc = real_popen(*a, **k)
        spawn_calls.append(proc)
        pid_tracker.alive.add(proc.pid)
        return proc

    monkeypatch.setattr(subprocess, "Popen", counting_popen)

    _open(transport, provisioned, "app-one", 6001, domain="example.com")
    _open(transport, provisioned, "app-two", 6002, domain="example.com")

    assert len(spawn_calls) == 1  # second app reused the already-running process


def test_shared_tunnel_process_killed_only_when_last_app_stops(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch, pid_tracker: _PidTracker
) -> None:
    transport, provisioned = _provision(monkeypatch)
    real_popen = subprocess.Popen

    def tracking_popen(*a, **k):
        proc = real_popen(*a, **k)
        pid_tracker.alive.add(proc.pid)
        return proc

    monkeypatch.setattr(subprocess, "Popen", tracking_popen)

    h1 = _open(transport, provisioned, "app-one", 7001, domain="example.com")
    h2 = _open(transport, provisioned, "app-two", 7002, domain="example.com")

    # registry.list_running prunes entries whose pid isn't alive (see
    # sidepage.core.registry._is_alive) — that's a *real*, unpatched
    # os.kill(pid, 0) check, so these entries need a genuinely-alive pid,
    # not an arbitrary placeholder. The test process's own pid qualifies
    # and is guaranteed alive for the duration of the test.
    this_pid = os.getpid()
    registry.register(
        registry.RunningApp(
            name="app-one",
            pid=this_pid,
            target="t",
            target_kind="static",
            listen_port=7001,
            url="http://x",
            tunnel_url=h1.url,
            started_at=0.0,
            domain="example.com",
        )
    )
    registry.register(
        registry.RunningApp(
            name="app-two",
            pid=this_pid,
            target="t",
            target_kind="static",
            listen_port=7002,
            url="http://x",
            tunnel_url=h2.url,
            started_at=0.0,
            domain="example.com",
        )
    )

    # Contract: caller unregisters the app being stopped *before* calling
    # close_tunnel (see sidepage.core.process._teardown / close_tunnel's
    # docstring) — this is what lets the refcount below be accurate.
    registry.unregister("app-one")
    tunnel_manager.close_tunnel(h1)
    assert not pid_tracker.signals  # app-two still running — process must survive

    registry.unregister("app-two")
    tunnel_manager.close_tunnel(h2)
    assert pid_tracker.signals  # last app gone — process must have been signaled


def test_shared_tunnel_process_start_failure_raises(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, provisioned = _provision(monkeypatch)

    def dead_on_arrival(*a, **k):
        proc = _FakeProcess()
        proc.returncode = 1  # simulates cloudflared exiting immediately (bad token, etc.)
        return proc

    monkeypatch.setattr(subprocess, "Popen", dead_on_arrival)

    with pytest.raises(TunnelError, match="cloudflared exited early"):
        _open(transport, provisioned, "doomed-app", 8000, domain="example.com")


def test_stale_pidfile_from_dead_process_is_replaced(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch, pid_tracker: _PidTracker
) -> None:
    """A pid file left behind by a process that died without going
    through _stop_shared_tunnel_if_unused (e.g. crashed, or killed
    outside sidepage) must not permanently block new spawns."""
    transport, provisioned = _provision(monkeypatch)
    from sidepage.config.settings import tunnel_pid_file

    tunnel_pid_file("example.com").parent.mkdir(parents=True, exist_ok=True)
    tunnel_pid_file("example.com").write_text("999999")  # not in pid_tracker.alive -> "dead"

    real_popen = subprocess.Popen
    spawn_calls = []

    def counting_popen(*a, **k):
        proc = real_popen(*a, **k)
        spawn_calls.append(proc)
        pid_tracker.alive.add(proc.pid)
        return proc

    monkeypatch.setattr(subprocess, "Popen", counting_popen)

    _open(transport, provisioned, "app-one", 9001, domain="example.com")
    assert len(spawn_calls) == 1


# --- _domain_lock: real cross-thread mutual exclusion ---


def test_domain_lock_serializes_concurrent_critical_sections(sidepage_home: Path) -> None:
    """Two threads racing on the same domain must never be inside the
    locked section at the same time — this is the guard against
    double-spawning cloudflared or clobbering a GET-modify-PUT ingress
    update (see tunnel_manager._domain_lock's docstring)."""
    events: list[str] = []
    barrier_hit = threading.Event()

    def worker(label: str, hold: float) -> None:
        with tunnel_manager._domain_lock("race.example.com"):
            events.append(f"{label}-start")
            if label == "a":
                barrier_hit.set()
            time.sleep(hold)
            events.append(f"{label}-end")

    t1 = threading.Thread(target=worker, args=("a", 0.15))
    t2 = threading.Thread(target=worker, args=("b", 0.0))
    t1.start()
    barrier_hit.wait(timeout=2)
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # Whichever thread went first must fully finish (start, then end)
    # before the other one starts — no interleaving.
    assert events in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    )
