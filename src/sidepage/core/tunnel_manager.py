"""Tunnel architecture — backs `sidepage serve --domain` / `--anon` and the
account/login flow (spec v3 §6). No standalone `tunnel` command group in
v3 (v1 had `tunnel login|token set|status|revoke`); tunnel setup now rides
on `sidepage login` / `sidepage account domain set` (§13,
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
    module's.
  - **BYO-domain (premium, or explicit `serve --domain`)** — the user
    supplies a scoped Zone:DNS:Edit token plus a separate per-tunnel token
    (never the global account key). Liability shifts to the user's own
    domain. v3 left credential storage unspecified ("stored locally, no
    mechanism given"); v4 answers it: both tokens are stored in
    `sidepage.core.secrets_vault` and referenced *by name*, not by value —
    `sidepage.core.account.set_default_domain` takes `zone_token_name` /
    `tunnel_token_name` (see `sidepage account domain set
    --zone-token-name / --tunnel-token-name`), and this module resolves
    those names to actual values only at tunnel-open time. Never stored in
    the directory, never in the auth-token runtime file (see
    `sidepage.core.token_runtime` for that boundary).
  - **Anonymous (`serve --anon`)** — Cloudflare Quick Tunnel. No broker
    call, no directory entry at all, `*.trycloudflare.com`. Orthogonal to
    `--auth`: an anonymous tunnel can still require a token
    (`--anon --auth token` is valid) — `--anon` only controls tunnel/
    directory registration, not the auth gate.
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sidepage.core.exceptions import CloudflaredResolutionError, TunnelError

_TRYCLOUDFLARE_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


class TunnelMode(StrEnum):
    BROKERED = "brokered"  # default, free tier, Sidepage's own domain
    BYO_DOMAIN = "byo_domain"  # premium or explicit --domain
    ANONYMOUS = "anonymous"  # --anon, Cloudflare Quick Tunnel


@dataclass
class TunnelHandle:
    app_name: str | None  # None for anonymous (no directory entry)
    mode: TunnelMode
    url: str
    domain: str | None  # None for brokered/anonymous
    process: subprocess.Popen | None = field(default=None, repr=False)


@dataclass(frozen=True)
class TunnelStatus:
    app_name: str
    mode: TunnelMode
    connected: bool


def open_brokered_tunnel(app_name: str) -> TunnelHandle:
    """Request a scoped, single-tunnel token from Sidepage's backend and
    bring up a tunnel under Sidepage's own domain. Default mode.

    Not implemented.
    """
    raise NotImplementedError


def open_byo_tunnel(
    app_name: str, domain: str, *, zone_token_name: str, tunnel_token_name: str
) -> TunnelHandle:
    """Bring up a tunnel on `domain` using the user's own scoped Cloudflare
    credentials, resolved from the vault by name (v4 §9):
    `zone_token_name` for the Zone:DNS:Edit token, `tunnel_token_name` for
    the per-tunnel token — see `sidepage.core.secrets_vault.get_secret`.
    Requires `domain` already on Cloudflare-managed DNS (Tunnel binds via
    CNAME).

    Once implemented, raises `sidepage.core.exceptions.SecretNotFoundError`
    if either name isn't in the vault.

    Not implemented.
    """
    raise NotImplementedError


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
    see `sidepage.core.reverse_proxy`)."""
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
