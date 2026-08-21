"""Local running-app registry — real backing for `sidepage ls`/`status`/
`stop`.

Not a cloud directory: the spec's directory service (owner, scope,
teardown/health status shared across machines — `sidepage.core.directory_client`)
doesn't exist to talk to, and building a fake one would be worse than
admitting it isn't there. This is much thinner: a JSON record, on this
machine only, of what `sidepage.core.process.serve()` started, so a second
terminal (or `stop`) can find it. `--scope` beyond `local` and directory
registration (`promote`, `ls --mine`, etc.) still aren't backed by
anything real — this only makes `ls`/`status`/`stop` work for apps
actually running locally.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass

from sidepage.config.settings import ensure_dirs, registry_file
from sidepage.core import _platform
from sidepage.core.exceptions import PeerNotFoundError

# Captured at import time, before any test can monkeypatch `subprocess.Popen`
# — `is_alive`'s `ps` shell-out must stay real even when a test fakes Popen
# process-wide to simulate an unrelated subprocess (e.g. `cloudflared` in
# `tests/test_tunnel_byo.py`). Calling `subprocess.run`/`subprocess.Popen`
# directly would still resolve to the monkeypatched name at call time; this
# doesn't, for the same reason `test_tunnel_byo.py` itself captures
# `_RealHttpxClient = httpx.Client` before patching `httpx.Client`.
_real_popen = subprocess.Popen


@dataclass
class RunningApp:
    name: str
    pid: int
    target: str
    target_kind: str
    listen_port: int
    url: str
    tunnel_url: str | None
    started_at: float
    # BYO-domain zone apex this app is routed through (e.g. "example.com"),
    # not the full per-app hostname — None for brokered/anonymous/no-tunnel
    # apps. This is what `sidepage.core.tunnel_manager` reference-counts
    # against to know whether it's safe to kill the shared `cloudflared`
    # process for a domain: the process should live exactly as long as at
    # least one running app is using it, never longer.
    domain: str | None = None


def _load() -> dict[str, dict]:
    path = registry_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, dict]) -> None:
    ensure_dirs()
    registry_file().write_text(json.dumps(data))


def register(app: RunningApp) -> None:
    data = _load()
    data[app.name] = asdict(app)
    _save(data)


def unregister(name: str) -> None:
    data = _load()
    data.pop(name, None)
    _save(data)


def is_alive(pid: int) -> bool:
    """True if `pid` refers to a live, non-zombie process.

    A bare existence probe (`_platform.is_pid_alive`) alone isn't enough
    on POSIX: it can't distinguish a zombie (a process that has exited but
    whose parent hasn't reaped it yet, still "alive" by that probe) from a
    genuinely running one — which is exactly what let a dead app keep
    showing up in `sidepage ls` and made `sidepage stop` report a false
    "didn't stop within 10s" instead of recognizing it was already gone.
    Shelling out to `ps` is what actually distinguishes a zombie from a
    live process — there's no `/proc` on macOS. Zombies are a POSIX-only
    concept, so on Windows `_platform.is_pid_alive`'s answer is already
    final: `ps` isn't installed there, and the `except OSError` below
    already falls back correctly if it's ever attempted anyway.

    Public — also used by `sidepage.core.process.stop()`, not just
    `list_running` below.
    """
    if not _platform.is_pid_alive(pid):
        return False
    try:
        with _real_popen(
            ["ps", "-o", "stat=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as proc:
            stdout, _ = proc.communicate(timeout=5)
    except OSError:
        return True  # `ps` itself unavailable (e.g. Windows) — trust is_pid_alive above
    state = stdout.strip()
    if not state:
        return False  # ps found nothing — pid was reaped between the two checks
    return not state.startswith("Z")


def get(name: str) -> RunningApp | None:
    raw = _load().get(name)
    return RunningApp(**raw) if raw is not None else None


def list_running(*, prune_dead: bool = True) -> list[RunningApp]:
    """List registered apps. Dead PIDs (process no longer running — most
    likely killed outside `sidepage stop`) are pruned by default, since a
    registry entry for a dead process is just noise."""
    data = _load()
    apps: list[RunningApp] = []
    changed = False
    for name, raw in list(data.items()):
        app = RunningApp(**raw)
        if prune_dead and not is_alive(app.pid):
            del data[name]
            changed = True
            continue
        apps.append(app)
    if changed:
        _save(data)
    return apps


def list_running_for_domain(domain: str) -> list[RunningApp]:
    """Running apps currently routed through BYO `domain` (the zone apex,
    matching `RunningApp.domain`) — the source of truth
    `sidepage.core.tunnel_manager` reference-counts against to decide
    whether the domain's shared `cloudflared` process is still in use."""
    return [app for app in list_running() if app.domain == domain]


def resolve_peer_url(app_name: str) -> str:
    """Resolve `app_name`'s current URL for `--peer` injection (v5) — the
    tunnel URL if it has one, otherwise the loopback `url` every running
    app always has. Always resolved against *live* registry state, never
    cached: this is what both the one-shot boot-time env injection
    (`sidepage.core.process.serve`) and the live `GET
    /.sidepage/peers.json` endpoint (`sidepage.core.reverse_proxy`) call,
    so a peer that restarted mid-session with a fresh anon-tunnel URL is
    reflected on the next live lookup even though the boot-time env var
    baked into some other app's subprocess stays stale.

    Raises `sidepage.core.exceptions.PeerNotFoundError` if `app_name`
    isn't currently running — fails loud rather than injecting an empty
    or stale URL for a peer that was never started or already stopped.
    """
    app = get(app_name)
    if app is None:
        raise PeerNotFoundError(
            f"peer app {app_name!r} isn't currently running — `sidepage serve` it first, "
            "or check `sidepage ls` for the exact registered name."
        )
    return app.tunnel_url or app.url
