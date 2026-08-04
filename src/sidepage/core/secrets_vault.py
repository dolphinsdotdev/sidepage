"""Secrets vault — backs `sidepage secrets set|list|remove` (spec v4 §9),
the major new piece in v4.

v3 had no concept of standing, persistent, user-supplied secrets at all —
only the ephemeral per-process auth-token runtime file (§8,
`sidepage.core.token_runtime`). v4's design calls for the OS keychain as
the primary backend with an encrypted-file fallback; **this build
implements the encrypted-file backend only**. OS keychain access (via the
`keyring` package) triggers an interactive permission prompt on macOS the
first time a process touches it, which isn't safe to depend on for a CLI
tool's automated tests or CI — deferred rather than half-built. The public
API (`set_secret`/`get_secret`/`list_secrets`/`remove_secret`) doesn't
change shape when keychain support is added later; callers won't need to
change.

Storage: a Fernet-encrypted JSON blob (`{name: value}`) at
`sidepage.config.settings.vault_data_file()`, with the symmetric key in a
sibling file (`vault_key_file()`), both mode `0600`. This is meaningfully
weaker than an OS keychain (the key sits next to the data it decrypts, on
the same filesystem, protected only by file permissions) — acceptable for
a local dev tool where the threat model is "don't leave plaintext secrets
lying around in shell history or world-readable files," not "resist an
attacker with read access to this account."

Explicitly separated from `sidepage.core.token_runtime` along two axes,
not one:
  - **Lifecycle** — vault secrets are persistent across `serve` invocations
    and machine restarts. The runtime file is ephemeral: it's wiped when
    the process (and the token it gates) dies.
  - **Direction** — vault secrets are *outbound*: credentials this app
    presents to something else (a third-party API key, a database
    password, the BYO-domain Cloudflare tokens). The runtime file's token
    is *inbound*: it gates access *to* this app.

Consumed by `sidepage serve --env <SECRET_NAME>` (repeatable — see
`sidepage.core.process`), which fails loud (`SecretNotFoundError`) if a
name isn't in the vault.
"""

from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from sidepage.config.settings import ensure_dirs, vault_data_file, vault_key_file
from sidepage.core.exceptions import SecretNotFoundError, VaultError


def _load_key() -> bytes:
    ensure_dirs()
    key_file = vault_key_file()
    if key_file.exists():
        return key_file.read_bytes()
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    key_file.chmod(0o600)
    return key


def _load_store() -> dict[str, str]:
    data_file = vault_data_file()
    if not data_file.exists():
        return {}
    fernet = Fernet(_load_key())
    try:
        plaintext = fernet.decrypt(data_file.read_bytes())
    except InvalidToken as exc:
        raise VaultError(
            f"could not decrypt {data_file} — key file mismatch or corrupted vault"
        ) from exc
    return json.loads(plaintext)


def _save_store(store: dict[str, str]) -> None:
    ensure_dirs()
    fernet = Fernet(_load_key())
    ciphertext = fernet.encrypt(json.dumps(store).encode())
    data_file = vault_data_file()
    data_file.write_bytes(ciphertext)
    data_file.chmod(0o600)


def set_secret(name: str, value: str) -> None:
    """Store `value` under `name` in the encrypted vault file. Overwrites
    any existing value for `name`."""
    store = _load_store()
    store[name] = value
    _save_store(store)


def get_secret(name: str) -> str:
    """Retrieve the value stored under `name`.

    Raises `sidepage.core.exceptions.SecretNotFoundError` if `name` isn't
    in the vault — callers (`serve --env`) are expected to fail loud, never
    silently skip.
    """
    store = _load_store()
    try:
        return store[name]
    except KeyError:
        raise SecretNotFoundError(f"no secret named {name!r} in the vault") from None


def list_secrets() -> list[str]:
    """List stored secret *names* only — values are never returned."""
    return sorted(_load_store())


def remove_secret(name: str) -> None:
    """Delete the stored value for `name`. No error if `name` isn't
    present — removing something already absent is a no-op, not a
    failure."""
    store = _load_store()
    store.pop(name, None)
    _save_store(store)
