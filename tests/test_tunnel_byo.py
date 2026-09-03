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

from sidepage.core import _platform as _real_platform
from sidepage.core import registry, secrets_vault, tunnel_manager
from sidepage.core.exceptions import NameCollisionError, TunnelError
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

    def terminate(self) -> None:
        self.returncode = -15  # SIGTERM, mirroring what a real Popen would end up with


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
    `_platform.terminate_process` so shared-process lifecycle tests can
    simulate a process dying on termination without ever touching a real
    OS process."""

    def __init__(self) -> None:
        self.alive: set[int] = set()
        self.signals: list[tuple[int, bool]] = []  # (pid, force)

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    def terminate(self, pid: int, *, force: bool) -> None:
        self.signals.append((pid, force))
        self.alive.discard(pid)


class _FakePlatformModule:
    """Stands in for the `_platform` name inside `tunnel_manager`'s own
    module namespace — swapped in via `monkeypatch.setattr(tunnel_manager,
    "_platform", ...)`, which rebinds the name *tunnel_manager.py itself*
    looks up, not the real `sidepage.core._platform` module object.
    Mutating the real module's `terminate_process` attribute instead would
    be a global change that could leak into other code exercised in the
    same test — this scoping avoids that. Only `terminate_process` is
    faked; everything else (`lock_exclusive`, used by every test that goes
    through `_domain_lock`, which is nearly all of them) passes through to
    the real module unchanged."""

    def __init__(self, tracker: _PidTracker) -> None:
        self._tracker = tracker

    def terminate_process(self, pid: int, *, force: bool) -> None:
        self._tracker.terminate(pid, force=force)

    def __getattr__(self, name: str):
        return getattr(_real_platform, name)


@pytest.fixture
def pid_tracker(monkeypatch: pytest.MonkeyPatch) -> _PidTracker:
    tracker = _PidTracker()
    monkeypatch.setattr(tunnel_manager, "_is_pid_alive", tracker.is_alive)
    monkeypatch.setattr(tunnel_manager, "_platform", _FakePlatformModule(tracker))
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
    transport: _StatefulCloudflareTransport,
    provisioned,
    app_name: str,
    port: int,
    *,
    domain: str,
    suffix: bool = True,
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
        suffix=suffix,
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


def test_open_byo_tunnel_no_suffix_routes_bare_hostname(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`serve --no-suffix`: the routed hostname is `<app>.<domain>` exactly,
    with the CNAME and ingress rule keyed on that same bare name."""
    transport, provisioned = _provision(monkeypatch)

    handle = _open(transport, provisioned, "myapp", 4321, domain="example.com", suffix=False)

    assert handle.hostname == "myapp.example.com"
    assert handle.url == "https://myapp.example.com"
    assert "myapp.example.com" in transport.dns_records

    ingress = transport.tunnels[provisioned.tunnel_id]["ingress"]
    assert {"hostname": "myapp.example.com", "service": "http://127.0.0.1:4321"} in ingress
    assert ingress[-1] == {"service": "http_status:404"}


def test_open_byo_tunnel_no_suffix_leaves_name_binding_unassigned(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsuffixed app never consumes a dedupe id — so dropping
    `--no-suffix` later gives it the id it would have had all along,
    rather than one silently burned by an earlier unsuffixed run."""
    from sidepage.config.settings import name_bindings_file

    transport, provisioned = _provision(monkeypatch)
    _open(transport, provisioned, "bareapp", 4321, domain="example.com", suffix=False)
    assert not name_bindings_file().exists()

    suffixed = _open(transport, provisioned, "bareapp", 4321, domain="example.com")
    assert suffixed.hostname != "bareapp.example.com"
    assert suffixed.hostname.startswith("bareapp-")


def test_open_byo_tunnel_suffixed_and_bare_names_coexist(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two apps on one domain, one unsuffixed — neither upsert wipes the
    other's rule, same GET-modify-PUT contract as two suffixed apps."""
    transport, provisioned = _provision(monkeypatch)
    bare = _open(transport, provisioned, "app-one", 5001, domain="example.com", suffix=False)
    suffixed = _open(transport, provisioned, "app-two", 5002, domain="example.com")

    ingress = transport.tunnels[provisioned.tunnel_id]["ingress"]
    hostnames = {r["hostname"] for r in ingress if "hostname" in r}
    assert hostnames == {"app-one.example.com", suffixed.hostname}
    assert bare.hostname == "app-one.example.com"
    assert ingress[-1] == {"service": "http_status:404"}


# --- name collisions: DNS is the authority, and it's checked before claiming ---


def _seed_foreign_record(transport, hostname: str, **overrides) -> dict:
    """A DNS record on `hostname` that sidepage didn't write — the thing a
    collision check has to notice and refuse to overwrite."""
    record = {
        "id": "rec-foreign",
        "type": "CNAME",
        "name": hostname,
        "content": "someone-elses-tunnel.cfargotunnel.com",
        **overrides,
    }
    transport.dns_records[hostname] = record
    return record


def _available(provisioned, app_name: str, *, domain: str, suffix: bool = True) -> str:
    return tunnel_manager.assert_hostname_available(
        app_name,
        domain,
        zone_id=provisioned.zone_id,
        tunnel_id=provisioned.tunnel_id,
        api_token_name=f"api-tok-{domain}",
        suffix=suffix,
    )


def test_open_byo_tunnel_refuses_a_hostname_pointed_somewhere_else(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CNAME on the name that points at a *different* tunnel isn't ours
    to take — claiming it would silently repoint whoever owns it."""
    transport, provisioned = _provision(monkeypatch)
    _seed_foreign_record(transport, "docs.example.com")

    with pytest.raises(NameCollisionError, match="already exists"):
        _open(transport, provisioned, "docs", 4321, domain="example.com", suffix=False)

    # The foreign record is intact and no route was added for it.
    assert transport.dns_records["docs.example.com"]["id"] == "rec-foreign"
    assert transport.dns_records["docs.example.com"]["content"].startswith("someone-elses")
    ingress = transport.tunnels[provisioned.tunnel_id]["ingress"]
    assert not [r for r in ingress if r.get("hostname") == "docs.example.com"]


def test_refused_claim_does_not_start_the_shared_cloudflared_process(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch, pid_tracker: _PidTracker
) -> None:
    """The claim check runs before the shared process is started, so a
    rejected name leaves no `cloudflared` behind that nothing will ever
    tear down."""
    transport, provisioned = _provision(monkeypatch)
    _seed_foreign_record(transport, "docs.example.com")

    spawned = []
    real_popen = subprocess.Popen

    def counting_popen(*a, **k):
        proc = real_popen(*a, **k)
        spawned.append(proc)
        pid_tracker.alive.add(proc.pid)
        return proc

    monkeypatch.setattr(subprocess, "Popen", counting_popen)

    with pytest.raises(NameCollisionError):
        _open(transport, provisioned, "docs", 4321, domain="example.com", suffix=False)

    assert spawned == []


def test_open_byo_tunnel_refuses_a_hostname_held_by_a_non_cname_record(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An A record holds the name just as firmly as a CNAME does — the
    lookup is deliberately not type-filtered."""
    transport, provisioned = _provision(monkeypatch)
    _seed_foreign_record(transport, "docs.example.com", type="A", content="203.0.113.7")

    with pytest.raises(NameCollisionError, match="203.0.113.7"):
        _open(transport, provisioned, "docs", 4321, domain="example.com", suffix=False)


def test_open_byo_tunnel_accepts_its_own_record_on_restart(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ownership test is "CNAME to *this* domain's tunnel" — so an app
    restarting onto the record it wrote last time is not a collision."""
    transport, provisioned = _provision(monkeypatch)
    first = _open(transport, provisioned, "docs", 4321, domain="example.com", suffix=False)
    second = _open(transport, provisioned, "docs", 5555, domain="example.com", suffix=False)

    assert first.hostname == second.hostname == "docs.example.com"
    ingress = transport.tunnels[provisioned.tunnel_id]["ingress"]
    assert {"hostname": "docs.example.com", "service": "http://127.0.0.1:5555"} in ingress


def test_suffixed_hostname_collision_is_caught_too(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a `--no-suffix`-only guard: a suffixed `<app>-<id>` name that
    somehow already exists (a stale record, or an unsuffixed app that
    happens to be named exactly that) is refused on the same test."""
    from sidepage.core.directory_client import check_name

    transport, provisioned = _provision(monkeypatch)
    routed = check_name("myapp")  # assigns and persists the id serve would use
    _seed_foreign_record(transport, f"{routed}.example.com")

    with pytest.raises(NameCollisionError, match=routed):
        _open(transport, provisioned, "myapp", 4321, domain="example.com")


def test_collision_message_only_suggests_dropping_no_suffix_when_it_was_used(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Drop --no-suffix" is the most useful next step for an unsuffixed
    collision and nonsense advice for a suffixed one, so the message says
    it only in the first case."""
    from sidepage.core.directory_client import check_name

    transport, provisioned = _provision(monkeypatch)
    _seed_foreign_record(transport, "docs.example.com")
    _seed_foreign_record(transport, f"{check_name('myapp')}.example.com")

    with pytest.raises(NameCollisionError) as unsuffixed:
        _open(transport, provisioned, "docs", 4321, domain="example.com", suffix=False)
    assert "drop --no-suffix" in str(unsuffixed.value)

    with pytest.raises(NameCollisionError) as suffixed:
        _open(transport, provisioned, "myapp", 4321, domain="example.com")
    assert "--no-suffix" not in str(suffixed.value)
    assert "pick a different --name" in str(suffixed.value)


def test_assert_hostname_available_returns_the_hostname_when_free(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, provisioned = _provision(monkeypatch)
    assert _available(provisioned, "docs", domain="example.com", suffix=False) == "docs.example.com"


def test_assert_hostname_available_raises_before_anything_is_written(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-flight `serve` runs before allocating a port or launching:
    it must report the collision without mutating DNS or ingress."""
    transport, provisioned = _provision(monkeypatch)
    _seed_foreign_record(transport, "docs.example.com")

    with pytest.raises(NameCollisionError):
        _available(provisioned, "docs", domain="example.com", suffix=False)

    assert transport.dns_records["docs.example.com"]["id"] == "rec-foreign"
    assert transport.tunnels[provisioned.tunnel_id]["ingress"] == []


def test_assert_hostname_available_ignores_a_free_name_on_a_busy_domain(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other apps' records on the same domain are none of this name's
    business — the lookup is per-hostname, not per-zone."""
    transport, provisioned = _provision(monkeypatch)
    _open(transport, provisioned, "other-app", 5001, domain="example.com")
    _seed_foreign_record(transport, "unrelated.example.com")

    assert _available(provisioned, "docs", domain="example.com", suffix=False) == "docs.example.com"


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
    # sidepage.core.registry.is_alive) — that's a *real*, unpatched
    # liveness check (os.kill(pid, 0) plus a `ps` zombie check), so these
    # entries need a genuinely-alive pid, not an arbitrary placeholder.
    # The test process's own pid qualifies and is guaranteed alive for
    # the duration of the test.
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


def test_shared_tunnel_output_goes_to_log_file_not_pipe(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression check: stdout/stderr must never be an unread
    `subprocess.PIPE` — this process outlives any single `serve` call, so
    an unread PIPE would eventually fill its OS buffer and make
    `cloudflared` block on its own `write()`, silently. Must go to
    `tunnel_log_file(domain)` instead — a real file, never fills up."""
    from sidepage.config.settings import tunnel_log_file

    transport, provisioned = _provision(monkeypatch)
    captured_kwargs: dict = {}

    def inspecting_popen(*a, **k):
        captured_kwargs.update(k)
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", inspecting_popen)

    _open(transport, provisioned, "app-one", 9001, domain="example.com")

    assert captured_kwargs["stdout"] != subprocess.PIPE
    assert tunnel_log_file("example.com").exists()


def test_shared_tunnel_error_marker_in_log_raises(
    sidepage_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cloudflared doesn't always crash immediately on a bad token/config
    — it can log an error while `proc.poll()` still reports it as alive.
    `_ensure_shared_tunnel_running` must catch that from the log instead
    of optimistically declaring success just because the process hasn't
    exited yet (same precedented "grep the log for an error word"
    pattern `scripts/start_site.sh` and `tests/fixtures/mcp-control-app`
    already use)."""
    transport, provisioned = _provision(monkeypatch)

    def error_logging_popen(*a, **k):
        k["stdout"].write("failed to register connection: bad token\n")
        k["stdout"].flush()
        return _FakeProcess()  # stays "alive" — returncode is None

    monkeypatch.setattr(subprocess, "Popen", error_logging_popen)

    with pytest.raises(TunnelError, match="reported a failure"):
        _open(transport, provisioned, "doomed-app", 8100, domain="example.com")


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
