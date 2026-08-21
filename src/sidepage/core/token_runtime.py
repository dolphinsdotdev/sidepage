"""Token handling for `--auth token` — backs `sidepage serve --token
<value>` / `SIDEPAGE_TOKEN` (spec v3 §8). Replaces v1's standing
`sidepage keys create|revoke|list` model entirely: there's no key
management command surface anymore, just a value that exists for the
lifetime of one `serve` process.

- **Source**: `--token <value>` or `SIDEPAGE_TOKEN` env var — prefer the
  env var over the bare CLI arg (shell history / `ps aux` exposure). If
  neither is given, generate one and print it once.
- **Storage**: a per-process runtime file,
  `~/.local/state/sidepage/runtime/<app-name>-<pid>.json` (mode `0600`) —
  see `sidepage.config.settings` for the path constant. Read by `status`/
  `inspect` (`sidepage.core.inspector`) for retrieval.
- **No rotation.** A new value requires a new `serve` invocation — this is
  deliberate, matching Jupyter's own token model, and consistent with the
  no-keychain choice below.
- **No keychain.** Process-scoped plaintext is deliberate, matching the
  ephemeral nature of the token itself — it dies with the process, same as
  the tunnel it gates.

Enforcement itself (gate page, header/query-param check, WS auth riding on
the session cookie) lives in `sidepage.core.reverse_proxy` (§9) — this
module only owns generating and persisting the value, not checking it.

**v4 clarification, not new behavior:** the runtime file holds both the
`--auth token` value *and* any broker-issued tunnel token from
`sidepage.core.tunnel_manager.open_brokered_tunnel` — v3 implied this but
never said so explicitly. Both live here because they share the same
ephemeral lifecycle (die with the process), not because they're the same
kind of thing — the auth token gates inbound access to this app, the
broker tunnel token is Sidepage's own credential for keeping the tunnel
open. This line exists specifically to draw the boundary against
`sidepage.core.secrets_vault` (v4 §9): standing, user-supplied, outbound
credentials belong in the vault; anything that dies with the `serve`
process — no matter what it's for — belongs here instead.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from sidepage.config.settings import ensure_dirs, runtime_dir


@dataclass(frozen=True)
class RuntimeToken:
    app_name: str
    pid: int
    value: str  # the --auth token value
    broker_tunnel_token: str | None = None  # set when the tunnel mode is brokered


def resolve_token(*, explicit: str | None, env_value: str | None) -> str:
    """Pick the token value for a `serve` invocation: `explicit` (`--token`)
    if given, else `env_value` (`SIDEPAGE_TOKEN`) if set, else generate a
    new one."""
    if explicit:
        return explicit
    if env_value:
        return env_value
    return secrets.token_urlsafe(24)


@dataclass(frozen=True)
class ControlToken:
    """Internal-only secret, generated and written for **every** `serve`/
    `proxy` process regardless of `--auth` — a demo app running `--auth
    open` still needs `sidepage stop` to work. Deliberately a separate
    dataclass/file from `RuntimeToken`, not a reuse of the public
    `--auth token` value: that value is user-facing (printed, gates real
    visitors) and may not exist at all for an open app; this one gates a
    single internal route (`POST /.sidepage/stop`,
    `sidepage.core.reverse_proxy`) used only by `sidepage.core.process
    .stop()` on Windows, where a cross-process `SIGTERM` can't reach the
    target's own signal handler the way it does on POSIX (see
    `sidepage.core.process` for why) — so it needs its own trust boundary
    rather than borrowing the public one.
    """

    app_name: str
    pid: int
    value: str


def generate_control_token() -> str:
    return secrets.token_urlsafe(24)


def _runtime_path(app_name: str, pid: int) -> Path:
    return runtime_dir() / f"{app_name}-{pid}.json"


def write_runtime_file(token: RuntimeToken) -> Path:
    """Write `token` to
    `~/.local/state/sidepage/runtime/<app-name>-<pid>.json` with mode
    `0600`. Returns the written path."""
    ensure_dirs()
    path = _runtime_path(token.app_name, token.pid)
    path.write_text(json.dumps(asdict(token)))
    path.chmod(0o600)
    return path


def read_runtime_file(app_name: str, pid: int) -> RuntimeToken:
    """Read back a previously written runtime token file — used by
    `sidepage status` / `sidepage inspect` to retrieve the value for
    display, and by `sidepage.core.inspector` to auto-source credentials
    when inspecting one's own app."""
    path = _runtime_path(app_name, pid)
    data = json.loads(path.read_text())
    return RuntimeToken(**data)


def _control_path(app_name: str, pid: int) -> Path:
    return runtime_dir() / f"{app_name}-{pid}-control.json"


def write_control_token_file(token: ControlToken) -> Path:
    """Write `token` to
    `~/.local/state/sidepage/runtime/<app-name>-<pid>-control.json` with
    mode `0600`. Returns the written path."""
    ensure_dirs()
    path = _control_path(token.app_name, token.pid)
    path.write_text(json.dumps(asdict(token)))
    path.chmod(0o600)
    return path


def read_control_token_file(app_name: str, pid: int) -> ControlToken:
    """Read back a previously written control token file — used by
    `sidepage.core.process.stop()` on Windows to authenticate its
    `POST /.sidepage/stop` request to the target app's own reverse proxy."""
    path = _control_path(app_name, pid)
    data = json.loads(path.read_text())
    return ControlToken(**data)
