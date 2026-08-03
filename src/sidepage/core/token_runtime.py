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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeToken:
    app_name: str
    pid: int
    value: str


def resolve_token(*, explicit: str | None, env_value: str | None) -> str:
    """Pick the token value for a `serve` invocation: `explicit` (`--token`)
    if given, else `env_value` (`SIDEPAGE_TOKEN`) if set, else generate a
    new one.

    Not implemented.
    """
    raise NotImplementedError


def write_runtime_file(token: RuntimeToken) -> Path:
    """Write `token` to
    `~/.local/state/sidepage/runtime/<app-name>-<pid>.json` with mode
    `0600`. Returns the written path.

    Not implemented.
    """
    raise NotImplementedError


def read_runtime_file(app_name: str, pid: int) -> RuntimeToken:
    """Read back a previously written runtime token file — used by
    `sidepage status` / `sidepage inspect` to retrieve the value for
    display, and by `sidepage.core.inspector` to auto-source credentials
    when inspecting one's own app.

    Not implemented.
    """
    raise NotImplementedError
