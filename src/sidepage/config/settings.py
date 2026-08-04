"""Local configuration paths — XDG-style: `~/.config/sidepage` (persistent
config/credentials), `~/.cache/sidepage` (redownloadable), `~/.local/state/sidepage`
(ephemeral runtime state).

Credentials (secrets vault contents, BYO-domain token *names*) are stored
**locally only** and must never cross into any directory/cloud service —
this module is the only place that reads/writes them.

Real for the pieces `sidepage serve` and `sidepage secrets` actually use:
the vault's encrypted-file store and the per-process runtime/registry
files. Account config (`login`, `account status`, `account domain set`)
and the directory-listing cache are still just path constants — those
commands don't have a real backend to talk to yet.

All paths respect `SIDEPAGE_HOME` if set, redirecting every root under one
directory instead of the real XDG locations — this is what tests use to
avoid touching the actual user's `~/.config` etc.

**Functions, not module-level constants, deliberately.** A constant
computed once at import time bakes in whatever `SIDEPAGE_HOME` happened to
be set to (or not) at that moment — `monkeypatch.setenv` in a test that
runs after the module's already been imported elsewhere in the session
would silently have no effect on in-process calls (subprocess-based tests
are fine either way, since each subprocess re-imports fresh). Every path
here is resolved on each call instead, so tests can isolate state reliably
without needing an `importlib.reload` dance.
"""

from __future__ import annotations

import os
from pathlib import Path


def _root() -> Path | None:
    override = os.environ.get("SIDEPAGE_HOME")
    return Path(override) if override else None


def config_dir() -> Path:
    home = _root()
    return (home / "config") if home else Path.home() / ".config" / "sidepage"


def cache_dir() -> Path:
    home = _root()
    return (home / "cache") if home else Path.home() / ".cache" / "sidepage"


def state_dir() -> Path:
    home = _root()
    return (home / "state") if home else Path.home() / ".local" / "state" / "sidepage"


# --- Secrets vault (v4 §9) — real, used by sidepage.core.secrets_vault ---
def vault_key_file() -> Path:
    return config_dir() / "vault.key"  # Fernet key, mode 0600


def vault_data_file() -> Path:
    return config_dir() / "vault.enc"  # encrypted {name: value} blob


# --- Per-process runtime (§8) — real, used by sidepage.core.token_runtime ---
def runtime_dir() -> Path:
    return state_dir() / "runtime"  # <app-name>-<pid>.json, mode 0600


# --- Local running-app registry — real, used by sidepage.core.process /
# directory_client for `ls`/`status`/`stop`. Not a cloud directory (there
# isn't one to talk to) — just what's actually running on this machine.
def registry_file() -> Path:
    return state_dir() / "running_apps.json"


# --- Not yet backed by anything real ---
# TODO: config_dir() / "account.json" — identity/session (`account status`),
#       BYO-domain default domain + vault secret *names* (not values).
# TODO: cache_dir() / "bin" / "cloudflared" — resolved/downloaded binary
#       path override; real cloudflared use so far just shells out to PATH.
# TODO: cache_dir() / "directory.json" — last-known cloud directory listing
#       for offline `ls`/`status` — moot until a cloud directory exists.


def ensure_dirs() -> None:
    """Create all XDG roots (0700) if missing. Safe to call repeatedly."""
    for d in (config_dir(), cache_dir(), state_dir(), runtime_dir()):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
