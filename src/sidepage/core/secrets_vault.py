"""Secrets vault — backs `sidepage secrets set|list|remove` (spec v4 §9),
the major new piece in v4.

v3 had no concept of standing, persistent, user-supplied secrets at all —
only the ephemeral per-process auth-token runtime file (§8,
`sidepage.core.token_runtime`). v4 adds a genuine vault: the OS keychain as
the primary backend, with an encrypted-file fallback when no keychain is
available (e.g. headless CI, containers).

Explicitly separated from `sidepage.core.token_runtime` along two axes,
not one:
  - **Lifecycle** — vault secrets are persistent across `serve` invocations
    and machine restarts. The runtime file is ephemeral: it's wiped when
    the process (and the token it gates) dies.
  - **Direction** — vault secrets are *outbound*: credentials this app
    presents to something else (a third-party API key, a database
    password, the BYO-domain Cloudflare tokens). The runtime file's token
    is *inbound*: it gates access *to* this app. Nothing in the runtime
    file should ever move here, and nothing here should ever be written to
    the runtime file — see `sidepage.core.token_runtime` for the
    corresponding boundary note.

Consumed by:
  - `sidepage serve --env <SECRET_NAME>` (repeatable, see
    `sidepage.core.process`) — injects named secrets into the wrapped
    process's environment. Fails loud (`SecretNotFoundError`) if a name
    isn't in the vault; no blanket passthrough of the whole vault, no
    silent skip.
  - `sidepage account domain set --zone-token-name / --tunnel-token-name`
    (see `sidepage.core.tunnel_manager`, `sidepage.core.account`) — v3 §6
    hand-waved BYO-domain credential storage as "stored locally, no
    mechanism specified." v4 gives it a real answer by routing both
    credentials through this module instead of inventing a second ad hoc
    storage path.

Implementation (once built): Python `keyring` as the primary backend (OS
keychain — Keychain on macOS, Secret Service on Linux, Credential Locker on
Windows), with an encrypted-file fallback (see
`sidepage.config.settings.CONFIG_DIR`) for environments with no keychain
access. Not a dependency yet — named here as intent only, the same way
Starlette/httpx are named elsewhere in this package without being
installed.
"""

from __future__ import annotations


def set_secret(name: str, value: str) -> None:
    """Store `value` under `name` in the vault (OS keychain, or the
    encrypted-file fallback). Overwrites any existing value for `name`.

    Not implemented.
    """
    raise NotImplementedError


def get_secret(name: str) -> str:
    """Retrieve the value stored under `name`.

    Once implemented, raises `sidepage.core.exceptions.SecretNotFoundError`
    if `name` isn't in the vault — callers (`serve --env`, BYO-domain
    credential resolution in `sidepage.core.tunnel_manager`) are expected
    to fail loud, never silently skip or fall back.

    Not implemented.
    """
    raise NotImplementedError


def list_secrets() -> list[str]:
    """List stored secret *names* only — values are never returned or
    displayed by this function.

    Not implemented.
    """
    raise NotImplementedError


def remove_secret(name: str) -> None:
    """Delete the stored value for `name`.

    Not implemented.
    """
    raise NotImplementedError
