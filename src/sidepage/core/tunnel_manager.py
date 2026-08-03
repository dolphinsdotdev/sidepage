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
    supplies a scoped Zone:DNS:Edit token plus a per-tunnel token (never
    the global account key). Liability shifts to the user's own domain.
    Credentials are set via `sidepage.core.account.set_default_domain` or
    an equivalent per-serve override — never stored in the directory,
    always local (see `sidepage.config.settings`).
  - **Anonymous (`serve --anon`)** — Cloudflare Quick Tunnel. No broker
    call, no directory entry at all, `*.trycloudflare.com`. Orthogonal to
    `--auth`: an anonymous tunnel can still require a token
    (`--anon --auth token` is valid) — `--anon` only controls tunnel/
    directory registration, not the auth gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class TunnelMode(StrEnum):
    BROKERED = "brokered"  # default, free tier, Sidepage's own domain
    BYO_DOMAIN = "byo_domain"  # premium or explicit --domain
    ANONYMOUS = "anonymous"  # --anon, Cloudflare Quick Tunnel


@dataclass(frozen=True)
class TunnelHandle:
    app_name: str | None  # None for anonymous (no directory entry)
    mode: TunnelMode
    url: str
    domain: str | None  # None for brokered/anonymous


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


def open_byo_tunnel(app_name: str, domain: str) -> TunnelHandle:
    """Bring up a tunnel on `domain` using the user's own scoped Cloudflare
    credentials (see `sidepage.core.account` for where those are set).
    Requires `domain` already on Cloudflare-managed DNS (Tunnel binds via
    CNAME).

    Not implemented.
    """
    raise NotImplementedError


def open_anon_tunnel() -> TunnelHandle:
    """Bring up a Cloudflare Quick Tunnel (`*.trycloudflare.com`) — no
    broker call, no directory entry. Backs `serve --anon`.

    Not implemented.
    """
    raise NotImplementedError


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

    Not implemented.
    """
    raise NotImplementedError


def resolve_cloudflared_binary(*, override_path: Path | None = None) -> Path:
    """Locate a usable `cloudflared` binary, in order:

    1. `--cloudflared-path` / `SIDEPAGE_CLOUDFLARED_PATH` (`override_path`).
    2. `PATH`, version-verified; fall through (not hard-fail) on mismatch.
    3. Local cache (`~/.cache/sidepage/bin/cloudflared` — see
       `sidepage.config.settings`).
    4. Download-on-first-run, checksum-verified against Cloudflare's
       published hashes.

    No Python port of `cloudflared` exists or should be assumed — the real
    binary is a hard dependency; this function's job is finding or fetching
    it, not reimplementing it.

    Raises `sidepage.core.exceptions.CloudflaredResolutionError` if none of
    the four steps produce a usable binary.

    Not implemented.
    """
    raise NotImplementedError
