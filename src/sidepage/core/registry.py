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
import os
from dataclasses import asdict, dataclass

from sidepage.config.settings import ensure_dirs, registry_file


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


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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
