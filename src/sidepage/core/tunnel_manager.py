"""Tunnel architecture — backs `sidepage serve --domain` / `--anon` and the
account/login flow (spec v3 §6, v4 §13 delta below). No standalone `tunnel`
command group in v3 (v1 had `tunnel login|token set|status|revoke`); tunnel
setup now rides on `sidepage login` / `sidepage account domain set` (§13,
`sidepage.core.account`) plus whichever mode `serve` selects per-call.
Tunnel *reachability* reporting folds into `sidepage status`
(`sidepage.core.directory_client.get_status`) rather than a dedicated
`tunnel status` command — flagged in the migration plan as a call worth
revisiting if tunnel-specific status ever needs its own surface.

Three modes, selected by what `serve` was given:

  - **Brokered (default, free tier)** — Sidepage's own backend holds real
    Cloudflare credentials server-side and issues a scoped, single-tunnel
    token per `serve` call, rate-limited/quota'd per account key. Runs
    under Sidepage's own domain; Sidepage is operator-of-record for
    anything there, so abuse monitoring is a backend concern, not this
    module's. **Not implemented** — no Sidepage backend exists to broker
    against.
  - **BYO-domain (premium, or explicit `serve --domain`) — real.** v4
    delta: the user supplies a **single** scoped Cloudflare API token
    (Account→Cloudflare Tunnel:Edit, Zone→DNS:Edit, Zone→Zone:Read),
    resolved from `sidepage.core.secrets_vault` by name — never stored in
    the directory, never in the auth-token runtime file (see
    `sidepage.core.token_runtime` for that boundary). `sidepage account
    domain set` uses that token to *create* the tunnel itself
    (`provision_byo_domain`, `POST .../cfd_tunnel` with `config_src:
    "cloudflare"` — remotely-managed ingress) — the earlier two-token
    design (a separate, user-supplied per-tunnel token for an
    out-of-band-created tunnel) is gone; see `docs/OPEN_QUESTIONS.md` #12
    for why that used to be out of scope and what changed.

    The tunnel's run-token (returned once, at creation) is stored back
    into the vault automatically under a reserved internal name
    (`sidepage.core.account.internal_tunnel_token_name`) — not typed by
    the user, but always logged explicitly by the CLI so it's discoverable
    via `sidepage secrets list`.

    **One `cloudflared` process is shared across every app running under a
    given BYO domain**, not one process per app — ingress routing
    (hostname → local port) is mutated remotely via the Cloudflare
    Tunnel configurations API (GET-modify-PUT, never a blind PUT, since
    PUT replaces the *entire* ingress array) rather than the process's own
    `--url` flag, which only ever supports a single hostname. The process
    is started by whichever app is first to need the domain and killed by
    whichever app is last to stop needing it — reference-counted against
    `sidepage.core.registry.list_running_for_domain`, guarded by an
    advisory per-domain file lock (`_domain_lock`) since `serve` calls
    racing on the same domain are separate OS processes, not threads.
  - **Anonymous (`serve --anon`)** — Cloudflare Quick Tunnel. No broker
    call, no directory entry at all, `*.trycloudflare.com`. Orthogonal to
    `--auth`: an anonymous tunnel can still require a token
    (`--anon --auth token` is valid) — `--anon` only controls tunnel/
    directory registration, not the auth gate.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import httpx

from sidepage.config.settings import tunnel_lock_file, tunnel_pid_file, tunnels_dir
from sidepage.core import registry, secrets_vault
from sidepage.core.directory_client import check_name
from sidepage.core.exceptions import CloudflaredResolutionError, TunnelError

_TRYCLOUDFLARE_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
_CF_API_BASE = "https://api.cloudflare.com/client/v4"
_CATCH_ALL_RULE = {"service": "http_status:404"}


class TunnelMode(StrEnum):
    BROKERED = "brokered"  # default, free tier, Sidepage's own domain
    BYO_DOMAIN = "byo_domain"  # premium or explicit --domain
    ANONYMOUS = "anonymous"  # --anon, Cloudflare Quick Tunnel


@dataclass
class TunnelHandle:
    app_name: str | None  # None for anonymous (no directory entry)
    mode: TunnelMode
    url: str
    domain: str | None  # BYO zone apex (e.g. "example.com"); None otherwise
    process: subprocess.Popen | None = field(default=None, repr=False)
    # BYO-domain teardown metadata — always None for brokered/anonymous.
    # `process` above is deliberately *not* used for BYO: the actual
    # `cloudflared` process is shared across apps and outlives any single
    # handle, so these are what `close_tunnel` needs instead to remove
    # just this app's ingress rule and decide whether the shared process
    # should die too.
    hostname: str | None = None  # full routed hostname, e.g. "app-ab12.example.com"
    account_id: str | None = None
    tunnel_id: str | None = None
    api_token_name: str | None = None  # vault secret name, re-resolved at teardown


@dataclass(frozen=True)
class TunnelStatus:
    app_name: str
    mode: TunnelMode
    connected: bool


@dataclass(frozen=True)
class DecodedTunnelToken:
    account_id: str
    tunnel_id: str


@dataclass(frozen=True)
class ProvisionedTunnel:
    """Result of `provision_byo_domain` — everything `sidepage account
    domain set` needs to persist locally (`sidepage.core.account.DomainConfig`)
    plus the one secret it needs to hand off to the vault."""

    account_id: str
    zone_id: str
    tunnel_id: str
    tunnel_token: str


_B64_STANDARD_JUNK_RE = re.compile(r"[^A-Za-z0-9+/]")
_B64_URLSAFE_JUNK_RE = re.compile(r"[^A-Za-z0-9_-]")


def decode_tunnel_token(token: str) -> DecodedTunnelToken:
    """Cloudflare tunnel run-tokens (`cloudflared tunnel token <name>`, the
    dashboard's "install connector" step, or the `token` field returned by
    `provision_byo_domain`'s create-tunnel call) are base64-encoded JSON:
    `{"a": "<account_id>", "t": "<tunnel_id>", "s": "<tunnel_secret>"}`.
    Only `a`/`t` are needed here — the secret itself is opaque to this
    module and passed straight through to `cloudflared`, never parsed or
    logged.

    Tries both standard and URL-safe base64 alphabets rather than assuming
    one, since this module's understanding of the token format is based on
    Cloudflare's public documentation/tooling behavior, not a guarantee —
    if Cloudflare changes it, this fails loud with `TunnelError` rather
    than silently misrouting DNS.

    Strips non-alphabet characters *before* computing padding, not just
    before decoding — a stray trailing newline or shell artifact (e.g.
    zsh's `%` on an unterminated line) picked up during copy/paste would
    otherwise throw off the `-len(token) % 4` padding math even though the
    real token is intact, producing a spurious decode failure.

    Raises `TunnelError` if the token can't be decoded as either.
    """
    data = None
    for decoder, junk_re in (
        (base64.b64decode, _B64_STANDARD_JUNK_RE),
        (base64.urlsafe_b64decode, _B64_URLSAFE_JUNK_RE),
    ):
        cleaned = junk_re.sub("", token)
        padded = cleaned + "=" * (-len(cleaned) % 4)
        try:
            data = json.loads(decoder(padded))
            break
        except (ValueError, json.JSONDecodeError):
            continue
    if data is None:
        raise TunnelError("could not decode tunnel token — unrecognized format")
    try:
        return DecodedTunnelToken(account_id=data["a"], tunnel_id=data["t"])
    except KeyError as exc:
        raise TunnelError(f"tunnel token missing expected field: {exc}") from exc


def _cf_request(method: str, path: str, token: str, **kwargs) -> dict:
    with httpx.Client(timeout=15.0) as client:
        resp = client.request(
            method, f"{_CF_API_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise TunnelError(f"Cloudflare API returned a non-JSON response for {path}") from exc
    if not data.get("success"):
        raise TunnelError(f"Cloudflare API error on {method} {path}: {data.get('errors')}")
    return data


def _resolve_zone(domain: str, api_token: str) -> tuple[str, str]:
    """`domain` is expected to be the zone apex (e.g. "example.com"), not a
    pre-built subdomain. Returns `(zone_id, account_id)` — Cloudflare's
    zone-lookup response includes the owning account, which saves a
    separate account-lookup call/flag: the user never has to supply their
    Cloudflare account ID directly, just the domain."""
    data = _cf_request("GET", "/zones", api_token, params={"name": domain})
    zones = data.get("result") or []
    if not zones:
        raise TunnelError(
            f"no Cloudflare zone found for {domain!r} on this account — is it on "
            "Cloudflare-managed DNS, and does the API token have Zone:Read access to it?"
        )
    zone = zones[0]
    return zone["id"], zone["account"]["id"]


def _upsert_cname_record(zone_id: str, api_token: str, hostname: str, target: str) -> None:
    """Create or update a proxied CNAME record for `hostname` pointing at
    `target` (a `<tunnel-id>.cfargotunnel.com` hostname)."""
    existing = _cf_request(
        "GET",
        f"/zones/{zone_id}/dns_records",
        api_token,
        params={"name": hostname, "type": "CNAME"},
    )
    records = existing.get("result") or []
    body = {"type": "CNAME", "name": hostname, "content": target, "proxied": True, "ttl": 1}
    if records:
        record_path = f"/zones/{zone_id}/dns_records/{records[0]['id']}"
        _cf_request("PUT", record_path, api_token, json=body)
    else:
        _cf_request("POST", f"/zones/{zone_id}/dns_records", api_token, json=body)


def _get_ingress_config(account_id: str, tunnel_id: str, api_token: str) -> list[dict]:
    data = _cf_request(
        "GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", api_token
    )
    result = data.get("result") or {}
    config = result.get("config") or {}
    return list(config.get("ingress") or [])


def _put_ingress_config(
    account_id: str, tunnel_id: str, api_token: str, ingress: list[dict]
) -> None:
    _cf_request(
        "PUT",
        f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        api_token,
        json={"config": {"ingress": ingress}},
    )


def _without_hostname_and_catchall(ingress: list[dict], hostname: str) -> list[dict]:
    """Every rule except `hostname`'s and any catch-all(s) (a rule with no
    `hostname` key at all, e.g. `_CATCH_ALL_RULE`). Shared by upsert and
    remove so both always rebuild from the same base and re-append exactly
    one trailing catch-all — never zero, never duplicated, regardless of
    how many times either is called."""
    return [rule for rule in ingress if rule.get("hostname") not in (None, hostname)]


def _upsert_ingress_rule(
    account_id: str, tunnel_id: str, api_token: str, hostname: str, service: str
) -> None:
    """GET-modify-PUT, never a blind PUT: the Cloudflare configurations
    endpoint replaces the *entire* ingress array on every PUT, so building
    one from scratch would silently drop every other app currently routed
    through this shared tunnel. Idempotent — re-running for a hostname
    already present replaces that one rule rather than duplicating it."""
    ingress = _get_ingress_config(account_id, tunnel_id, api_token)
    rules = _without_hostname_and_catchall(ingress, hostname)
    rules.append({"hostname": hostname, "service": service})
    rules.append(_CATCH_ALL_RULE)
    _put_ingress_config(account_id, tunnel_id, api_token, rules)


def _remove_ingress_rule(account_id: str, tunnel_id: str, api_token: str, hostname: str) -> None:
    """Inverse of `_upsert_ingress_rule` — drops `hostname`'s rule, leaves
    every other app's rule untouched, and always re-appends exactly one
    trailing catch-all."""
    ingress = _get_ingress_config(account_id, tunnel_id, api_token)
    rules = _without_hostname_and_catchall(ingress, hostname)
    rules.append(_CATCH_ALL_RULE)
    _put_ingress_config(account_id, tunnel_id, api_token, rules)


@contextmanager
def _domain_lock(domain: str):
    """Serializes every operation that touches a BYO domain's *shared*
    state — starting/stopping its `cloudflared` process and GET-modify-PUT
    ingress updates — across concurrent `serve`/`stop` invocations. Each
    is a separate OS process, so an in-process `threading.Lock` wouldn't
    cover the real race: two `serve` calls for the same domain starting at
    once could otherwise both see "not running yet" and double-spawn
    `cloudflared`, or both build a PUT from the same stale GET and
    clobber each other's ingress rule. Held for the whole critical
    section, not per sub-step, so no window exists for that to happen.
    """
    tunnels_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = tunnel_lock_file(domain)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _ensure_shared_tunnel_running(domain: str, tunnel_token: str) -> None:
    """Start the shared `cloudflared` process for `domain` if nothing is
    already running for it — a no-op for the second, third, ... app on the
    same domain. Must be called under `_domain_lock(domain)`.

    Spawned with `start_new_session=True` and no `--url` (ingress is
    entirely remotely-managed, see module docstring): this process must
    outlive the particular `serve` invocation that happens to start it,
    since other apps on the same domain will go on running after that one
    exits. Tracked by pid file rather than by any in-memory handle, since
    a later `serve`/`stop` call — a different OS process — is what needs
    to find or kill it.
    """
    pid_file = tunnel_pid_file(domain)
    if pid_file.exists():
        pid_text = pid_file.read_text().strip()
        if pid_text and _is_pid_alive(int(pid_text)):
            return
        pid_file.unlink()  # stale: process died without going through _stop_shared_tunnel_if_unused

    binary = resolve_cloudflared_binary()
    proc = subprocess.Popen(
        [str(binary), "tunnel", "run", "--token", tunnel_token],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    # cloudflared fails fast on a bad token/auth error — give it a moment
    # before declaring success and detaching from it.
    time.sleep(1.5)
    if proc.poll() is not None:
        raise TunnelError(
            f"cloudflared exited early (code {proc.returncode}) starting the shared "
            f"tunnel for {domain}"
        )
    pid_file.write_text(str(proc.pid))


def _stop_shared_tunnel_if_unused(domain: str) -> None:
    """Kill the shared `cloudflared` process for `domain` if no running app
    is using it any more — reference-counted against
    `sidepage.core.registry.list_running_for_domain`. Must be called under
    `_domain_lock(domain)`, and only after the app being torn down has
    already been removed from the registry, or the very last app's
    teardown would see itself still counted and never kill anything,
    leaking the process.
    """
    if registry.list_running_for_domain(domain):
        return
    pid_file = tunnel_pid_file(domain)
    if not pid_file.exists():
        return
    pid_text = pid_file.read_text().strip()
    pid_file.unlink()
    if not pid_text:
        return
    pid = int(pid_text)
    if not _is_pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _is_pid_alive(pid):
        time.sleep(0.1)
    if _is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def provision_byo_domain(domain: str, api_token: str) -> ProvisionedTunnel:
    """One-time setup for `sidepage account domain set <domain>`: resolve
    the Cloudflare zone for `domain`, then create a single tunnel
    (`config_src: "cloudflare"`, i.e. remotely-managed ingress) meant to
    serve every app that will later run under this domain — not one
    tunnel per app.

    `api_token` needs Account→Cloudflare Tunnel:Edit (to create the
    tunnel), Zone→DNS:Edit (for the CNAME upserts `open_byo_tunnel` does
    later), and Zone→Zone:Read (for the zone lookup here) on the account
    owning `domain`.

    The returned `tunnel_token` is Cloudflare's one-time response to the
    create call — there's no way to fetch it again later. Callers
    (`sidepage.core.account.configure_domain`) must persist it immediately
    or the tunnel is effectively orphaned (see
    `sidepage.core.exceptions.TunnelProvisioningError`).
    """
    zone_id, account_id = _resolve_zone(domain, api_token)
    resp = _cf_request(
        "POST",
        f"/accounts/{account_id}/cfd_tunnel",
        api_token,
        json={"name": f"sidepage-{domain}", "config_src": "cloudflare"},
    )
    result = resp["result"]
    return ProvisionedTunnel(
        account_id=account_id,
        zone_id=zone_id,
        tunnel_id=result["id"],
        tunnel_token=result["token"],
    )


def open_brokered_tunnel(app_name: str) -> TunnelHandle:
    """Request a scoped, single-tunnel token from Sidepage's backend and
    bring up a tunnel under Sidepage's own domain. Default mode.

    Not implemented.
    """
    raise NotImplementedError


def open_byo_tunnel(
    app_name: str,
    domain: str,
    listen_port: int,
    *,
    zone_id: str,
    account_id: str,
    tunnel_id: str,
    api_token_name: str,
    tunnel_token_name: str,
) -> TunnelHandle:
    """Route `app_name` through the domain's already-provisioned tunnel
    (`sidepage account domain set` must have run first — see
    `sidepage.core.account.configure_domain`, which is what resolves
    `zone_id`/`account_id`/`tunnel_id` and persists them).

    `zone_token_name`/`tunnel_token_name` from the old two-token design
    are gone: `api_token_name` is the vault name of the single Cloudflare
    API token, `tunnel_token_name` is the vault name of the run-token
    `configure_domain` stored automatically at provisioning time.

    The hostname actually routed is `<app-name>-<id>.<domain>` (see
    `sidepage.core.directory_client.check_name` for the stable `<id>`).

    Real, in order, all under `_domain_lock(domain)` so it can't race a
    concurrent `serve`/`stop` on the same domain:
      1. Resolve both secrets from the vault (raises
         `sidepage.core.exceptions.SecretNotFoundError` if either name
         isn't there).
      2. Ensure the domain's shared `cloudflared` process is running,
         starting it if this is the first app using this domain
         (`_ensure_shared_tunnel_running`).
      3. Create or update a proxied CNAME record for the routed hostname.
      4. Add (or replace) this hostname's ingress rule on the shared
         tunnel via GET-modify-PUT (`_upsert_ingress_rule`) — no restart
         of `cloudflared` needed, it picks up remotely-managed config
         changes on its own.

    Raises `TunnelError` for a Cloudflare API error or `cloudflared`
    exiting immediately on first start.
    """
    api_token = secrets_vault.get_secret(api_token_name)
    tunnel_token = secrets_vault.get_secret(tunnel_token_name)

    hostname = f"{check_name(app_name)}.{domain}"
    cname_target = f"{tunnel_id}.cfargotunnel.com"

    with _domain_lock(domain):
        _ensure_shared_tunnel_running(domain, tunnel_token)
        _upsert_cname_record(zone_id, api_token, hostname, cname_target)
        _upsert_ingress_rule(
            account_id, tunnel_id, api_token, hostname, f"http://127.0.0.1:{listen_port}"
        )

    return TunnelHandle(
        app_name=app_name,
        mode=TunnelMode.BYO_DOMAIN,
        url=f"https://{hostname}",
        domain=domain,
        process=None,  # shared across apps — see TunnelHandle's docstring
        hostname=hostname,
        account_id=account_id,
        tunnel_id=tunnel_id,
        api_token_name=api_token_name,
    )


def open_anon_tunnel(listen_port: int, *, timeout: float = 20.0) -> TunnelHandle:
    """Bring up a Cloudflare Quick Tunnel (`*.trycloudflare.com`) pointed at
    `127.0.0.1:listen_port` — no broker call, no directory entry, no
    credentials needed. Backs `serve --anon`.

    Spawns `cloudflared tunnel --url http://127.0.0.1:<listen_port>` and
    parses the assigned URL from its output. Raises `TunnelError` if
    `cloudflared` exits early or no URL appears within `timeout`.
    """
    binary = resolve_cloudflared_binary()
    proc = subprocess.Popen(
        [str(binary), "tunnel", "--url", f"http://127.0.0.1:{listen_port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # Read output on a background thread so a stall in cloudflared's output
    # can't make `readline()` block past `timeout` — a plain read loop on
    # the main thread would ignore the deadline entirely while blocked.
    lines: queue.Queue[str] = queue.Queue()

    def _pump() -> None:
        if proc.stdout is not None:
            for line in proc.stdout:
                lines.put(line)

    threading.Thread(target=_pump, daemon=True).start()

    deadline = time.monotonic() + timeout
    url: str | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise TunnelError(f"cloudflared exited early (code {proc.returncode})")
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        match = _TRYCLOUDFLARE_RE.search(line)
        if match:
            url = match.group(0)
            break
    if url is None:
        proc.terminate()
        raise TunnelError(f"cloudflared did not report a tunnel URL within {timeout}s")

    return TunnelHandle(
        app_name=None, mode=TunnelMode.ANONYMOUS, url=url, domain=None, process=proc
    )


def status(app_name: str) -> TunnelStatus:
    """Current tunnel connection state for `app_name`. Primarily consumed
    by `sidepage.core.directory_client.get_status`, which folds this into
    `sidepage status`'s reachability reconciliation.

    Not implemented.
    """
    raise NotImplementedError


def close_tunnel(handle: TunnelHandle) -> None:
    """Tear down a tunnel opened by any of the three `open_*` functions
    above. Called on `serve`'s Ctrl+C / `stop` path — immediate, no grace
    period (confirmed default; graceful drain is a deferred open question,
    see `sidepage.core.reverse_proxy`).

    BYO-domain is different from the other two modes: `handle.process` is
    always `None` here (the `cloudflared` process is shared, not owned by
    any one app's handle — see `TunnelHandle`'s docstring), so there's
    nothing to terminate directly. Instead this removes just this app's
    ingress rule, then checks whether the shared process should die too
    (`_stop_shared_tunnel_if_unused`) — both under the same `_domain_lock`
    acquisition so nothing else can mutate this domain's shared state
    in between. **Caller contract**: this must run *after*
    `sidepage.core.registry.unregister` for the app being stopped, or the
    refcount check would still see it as running and could never kill the
    shared process on the last app's teardown (see
    `sidepage.core.process._teardown`). The ingress removal and the
    shared-process check both happen even if the removal step raises —
    a Cloudflare API hiccup during teardown shouldn't be able to leak the
    shared process forever.
    """
    if handle.mode is TunnelMode.BYO_DOMAIN:
        if not (handle.domain and handle.hostname and handle.account_id):
            raise TunnelError("BYO-domain TunnelHandle missing teardown metadata")
        if not (handle.tunnel_id and handle.api_token_name):
            raise TunnelError("BYO-domain TunnelHandle missing teardown metadata")
        with _domain_lock(handle.domain):
            try:
                api_token = secrets_vault.get_secret(handle.api_token_name)
                _remove_ingress_rule(
                    handle.account_id, handle.tunnel_id, api_token, handle.hostname
                )
            finally:
                _stop_shared_tunnel_if_unused(handle.domain)
        return

    if handle.process is not None and handle.process.poll() is None:
        handle.process.terminate()
        try:
            handle.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            handle.process.kill()


def resolve_cloudflared_binary(*, override_path: Path | None = None) -> Path:
    """Locate a usable `cloudflared` binary.

    Implements the first two of the spec's four resolution steps for real:

    1. `override_path` (`--cloudflared-path` / `SIDEPAGE_CLOUDFLARED_PATH`
       — reading the env var itself is `serve`'s job, this function just
       takes the resolved value).
    2. `PATH` lookup (`shutil.which`).

    Steps 3 (local cache download) and 4 (download-on-first-run, checksum
    verified) are **not implemented** — they require fetching and verifying
    a real binary from Cloudflare's release infrastructure, out of scope
    for this round. Not a practical gap today: `cloudflared` installed via
    a system package manager (as this environment has it) satisfies step 2
    every time.

    Raises `sidepage.core.exceptions.CloudflaredResolutionError` if neither
    step finds a usable binary.
    """
    if override_path is not None:
        if override_path.is_file():
            return override_path
        raise CloudflaredResolutionError(f"--cloudflared-path {override_path} does not exist")

    found = shutil.which("cloudflared")
    if found is not None:
        return Path(found)

    raise CloudflaredResolutionError(
        "cloudflared not found on PATH, and no override given. "
        "Local-cache / download-on-first-run resolution isn't implemented "
        "— install cloudflared (e.g. `brew install cloudflared`) for now."
    )
