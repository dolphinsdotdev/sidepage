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
from dataclasses import asdict, dataclass

from sidepage.config.settings import ensure_dirs, registry_file
from sidepage.core import procutil


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


_is_alive = procutil.pid_alive


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
        if prune_dead and not _is_alive(app.pid):
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
