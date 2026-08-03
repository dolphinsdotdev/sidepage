"""Local process + tunnel + proxy lifecycle — backs `sidepage serve` /
`sidepage stop` (spec v3 §2).

pm2-style, port-free from the caller's perspective: `serve` allocates a
real OS-assigned port (see `sidepage.core.target`), injects it into the
wrapped process, starts the local reverse proxy
(`sidepage.core.reverse_proxy`) between the tunnel and that port, brings up
the tunnel (`sidepage.core.tunnel_manager`) per `config.domain`/`config.anon`,
and blocks the terminal until interrupted.

Deliberately foreground/single-process by contract: killing the process
tears down the tunnel **immediately** — no grace period, confirmed default
(graceful draining of in-flight requests/open WS connections is deferred,
see `sidepage.core.reverse_proxy`). Multi-process/background management is
out of scope for this binary and lives in the separate orchestrator product
(see spec's "Out of scope" section, §16) — this module should NOT grow a
background mode implicitly; `--background` is explicitly ruled out in v3,
the orchestrator owns all multi-process concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.target import TargetKind


@dataclass(frozen=True)
class ServeConfig:
    target: Path
    target_kind: TargetKind | None  # None means "auto" — see sidepage.core.target
    name: str | None
    domain: str | None
    auth: AuthTier
    scope: Scope
    anon: bool = False  # Quick Tunnel, no directory entry — orthogonal to `auth`
    token: str | None = None  # explicit --token; None means env var or auto-generate
    guardrail: Path | None = None  # parked, not built — see sidepage.core.guardrail


def serve(config: ServeConfig) -> None:
    """Start the app, register/refresh its directory entry per
    `config.scope` (skipped entirely when `config.anon` is set), resolve
    the token per `config.token`/`SIDEPAGE_TOKEN`
    (`sidepage.core.token_runtime`), start the local reverse proxy in front
    of the allocated port, bring up the tunnel per `config.domain`/
    `config.anon` (`sidepage.core.tunnel_manager`), and block the terminal
    until interrupted. On interrupt, tear down the proxy, tunnel, and
    directory registration immediately (no grace period).

    Not implemented.
    """
    raise NotImplementedError


def stop(app_name: str) -> None:
    """Explicit teardown of a running app by name — distinct from Ctrl+C,
    intended for use once the orchestrator manages background/detached runs
    on top of this binary. Same immediate-teardown semantics as Ctrl+C.

    Not implemented.
    """
    raise NotImplementedError
