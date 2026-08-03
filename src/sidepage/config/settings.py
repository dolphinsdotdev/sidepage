"""Placeholder for local configuration paths and schema.

v3 names two explicit paths (`~/.cache/sidepage/bin/cloudflared`, §6;
`~/.local/state/sidepage/runtime/`, §8), both XDG-style. This module
adopts XDG conventions throughout for consistency, replacing v1's single
ad hoc `~/.sidepage` directory — mixing one XDG-style path with one ad hoc
directory would be more confusing than migrating the rest to match.

Credentials (Cloudflare tokens, BYO-domain tokens per §6) are stored
**locally only** and must never cross into the directory service — the
directory holds public identity metadata, nothing secret. Whatever reads
and writes these paths belongs here, not in `sidepage.core.directory_client`.

Not implemented yet. When it is, this module should own:
  - `CONFIG_DIR` (`~/.config/sidepage`) — account/session state (§13:
    `login`, `account status`), BYO-domain credentials (§6:
    `account domain set`)
  - `CACHE_DIR` (`~/.cache/sidepage`) — the `cloudflared` binary cache
    (§6), and a last-known directory listing for offline `ls`/`status`
  - `STATE_DIR` (`~/.local/state/sidepage`) — per-process runtime token
    files (§8: `runtime/<app-name>-<pid>.json`, mode `0600`)
  - env var overrides for all of the above (e.g. for CI / containers)
  - the (versioned) schema of what's stored at each path, so `account
    domain set`, `serve --token`, and `account status` all agree on shape

Deliberately not doing any of that here yet — just naming the seams so the
command modules have a stable import path to point at.
"""

from __future__ import annotations

from pathlib import Path

# XDG-style roots. Real implementations should make these overridable via
# env vars (e.g. `SIDEPAGE_CONFIG_DIR`) for CI / containers / multiple
# identities on one machine, and should fall back to `$XDG_CONFIG_HOME` /
# `$XDG_CACHE_HOME` / `$XDG_STATE_HOME` when set, per the XDG base dir spec.
CONFIG_DIR = Path.home() / ".config" / "sidepage"
CACHE_DIR = Path.home() / ".cache" / "sidepage"
STATE_DIR = Path.home() / ".local" / "state" / "sidepage"

# TODO: CONFIG_DIR / "account.json" — identity/session (`account status`),
#       local defaults, BYO-domain credentials (`account domain set`).
#       Must be filesystem-permission-restricted (0600) once actually written.
# TODO: CACHE_DIR / "bin" / "cloudflared" — resolved/downloaded binary,
#       see sidepage.core.tunnel_manager.resolve_cloudflared_binary.
# TODO: CACHE_DIR / "directory.json" — last-known directory listing for
#       offline `ls`/`status`.
# TODO: STATE_DIR / "runtime" / "<app-name>-<pid>.json" — per-process
#       token file, see sidepage.core.token_runtime.
