"""Local configuration paths — XDG-style: `~/.config/sidepage` (persistent
config/credentials), `~/.cache/sidepage` (redownloadable), `~/.local/state/sidepage`
(ephemeral runtime state).

Credentials (secrets vault contents, BYO-domain token *names*) are stored
**locally only** and must never cross into any directory/cloud service —
this module is the only place that reads/writes them.

Real for the pieces `sidepage serve` and `sidepage secrets` actually use:
the vault's encrypted-file store, the per-process runtime/registry files,
the account config (BYO domain + vault secret names, backing real
`sidepage serve --domain`), and the name-bindings file (stable
`<app-name>-<id>` assignment). `login`/`account status` (identity/session)
and the directory-listing cache are still just unimplemented — those need
a cloud backend that doesn't exist.

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


# --- Account config (§13) — real, used by sidepage.core.account for BYO
# domain: the domain itself plus vault secret *names* (never credential
# values — those stay in the vault, see sidepage.core.secrets_vault) ---
def account_config_file() -> Path:
    return config_dir() / "account.json"


# --- Shared BYO-domain tunnel process tracking — real, used by
# sidepage.core.tunnel_manager. One `cloudflared` process is shared across
# every app running under a given BYO domain (ingress routing is done
# remotely via the Cloudflare API, not one process per app) — the lock
# file serializes concurrent `serve` calls racing to start/stop that
# process or mutate its ingress config; the pid file is how a later
# `serve`/`stop` call finds (or confirms the death of) the process a prior
# invocation started, since it isn't a child of the current process. ---
def tunnels_dir() -> Path:
    return state_dir() / "tunnels"


def tunnel_lock_file(domain: str) -> Path:
    return tunnels_dir() / f"{domain}.lock"


def tunnel_pid_file(domain: str) -> Path:
    return tunnels_dir() / f"{domain}.pid"


# --- Name bindings (§3) — real, used by sidepage.core.directory_client.
# Stable <app-name> -> <4-char-id> mapping so a given app name resolves to
# the same `<app-name>-<id>.<domain>` hostname across `serve` restarts,
# rather than a fresh id (and a fresh DNS record) every time. ---
def name_bindings_file() -> Path:
    return config_dir() / "name_bindings.json"


# --- Local app registry (registry spec v2) — real, used by
# sidepage.core.app_registry for `sidepage app register|list|show|unregister`
# and `sidepage serve <app-name>`. Distinct from registry_file() above:
# that one tracks apps *currently running* on this machine (ephemeral,
# pruned on process death); this one is a *saved config* a user chose to
# keep around, with no relationship to whether anything is running right
# now. Deliberately at state_dir()'s root as `registry.json`, matching the
# spec's storage note that it's "the same tier as the runtime file (§8)
# and ports table (§13)" — a small local KV store, not a new subsystem. ---
def app_registry_file() -> Path:
    return state_dir() / "registry.json"


# --- Not yet backed by anything real ---
# TODO: cache_dir() / "bin" / "cloudflared" — resolved/downloaded binary
#       path override; real cloudflared use so far just shells out to PATH.
# TODO: cache_dir() / "directory.json" — last-known cloud directory listing
#       for offline `ls`/`status` — moot until a cloud directory exists.


def ensure_dirs() -> None:
    """Create all XDG roots (0700) if missing. Safe to call repeatedly."""
    for d in (config_dir(), cache_dir(), state_dir(), runtime_dir(), tunnels_dir()):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
