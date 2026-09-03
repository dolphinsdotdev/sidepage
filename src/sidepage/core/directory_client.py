"""Client for the sidepage directory service — backs `sidepage promote`,
`sidepage ls`, and `sidepage status` (spec v3 §3, §5, §10).

Naming model (§3): `<app-name>-<4-char-id>.<domain>.<tld>` — collision-proof
by construction, human prefix kept for legibility. `serve --no-suffix`
opts out of the id for BYO-domain apps only (see `check_name`), trading
that guarantee for a bare `<app-name>.<domain>` on a zone the user
already owns. The directory entry is
the identity root for a named app: owner, creation time, scope, and
teardown/health status all live there, not just DNS.

**Resolved in v3** (both were open in v1):
- Name reclaim after teardown: **no grace period**, confirmed default,
  accepted risk — a caller holding an old resolution could reach a new,
  unrelated owner post-teardown.
- `intranet` scope model: **one directory, scope is a field** — confirmed,
  not split into separate instances per scope tier. `promote` widens scope
  without issuing a new identity.

`--anon` apps (§3, §6) never enter the directory at all — `serve` should
skip calling into this module entirely for those, not call it with some
"anonymous" scope value.

`check_name` is no longer a standalone CLI command (`sidepage name check`
was folded into `sidepage account status` territory, effectively dropped —
name assignment now just happens implicitly during `serve`). It stays here
as the internal primitive `serve`'s directory registration calls — and is
now real, for the one caller that actually needs it: BYO-domain tunneling
(`sidepage.core.tunnel_manager.open_byo_tunnel`), which needs a concrete
`<app-name>-<id>.<domain>` hostname to create a DNS record for. The
`<id>` is generated once per `app_name` and persisted (see
`sidepage.config.settings.name_bindings_file`), not regenerated every
`serve` call — otherwise the DNS record (and the URL you'd bookmark) would
change on every restart. There's still no directory *service* backing
this (no owner/scope/teardown-status tracking, no collision detection
across machines) — just the local stability guarantee this module's
docstring already promises for the id itself.
"""

from __future__ import annotations

import json
import secrets
import string
from dataclasses import dataclass
from enum import StrEnum

from sidepage.config.settings import ensure_dirs, name_bindings_file

_ID_ALPHABET = string.ascii_lowercase + string.digits


class Scope(StrEnum):
    LOCAL = "local"  # not in the directory at all — localhost only
    LAN = "lan"  # mDNS, directory entry marked LAN-scoped
    INTRANET = "intranet"  # ACL-scoped visibility on the same directory
    WEB = "web"  # full public directory entry, public DNS


@dataclass(frozen=True)
class DirectoryEntry:
    name: str  # human prefix, e.g. "myapp"
    fqdn: str  # e.g. "myapp-a1b2.sidepage.dev"
    owner: str
    created_at: str  # ISO 8601
    scope: Scope
    status: str  # health/teardown status


def _load_bindings() -> dict[str, str]:
    path = name_bindings_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_bindings(bindings: dict[str, str]) -> None:
    ensure_dirs()
    name_bindings_file().write_text(json.dumps(bindings))


def check_name(app_name: str, *, suffix: bool = True) -> str:
    """Assign (or look up a previously-assigned) `<app-name>-<4-char-id>`
    for `app_name`. Called internally by `sidepage.core.tunnel_manager.open_byo_tunnel`
    when building the hostname to route — not exposed as its own CLI
    command in v3.

    The id is generated once per `app_name` and persisted locally, so
    repeated `serve` calls for the same app name get the same hostname
    (and don't need a new DNS record each time).

    `suffix=False` (`serve --no-suffix`, BYO-domain only) returns
    `app_name` unchanged: on a domain the user owns, the whole point of
    the dedupe id — collision-proofing a shared namespace — is something
    they may reasonably not want, because they'd rather have
    `app.example.com` than `app-a1b2.example.com` and they're the only
    one assigning names in that zone. Nothing is read or written to the
    bindings file in that case: the mapping is only meaningful for the
    suffixed form, and an app that never uses its id shouldn't burn one
    (or, worse, have a stored id silently reappear if `--no-suffix` is
    later dropped from the invocation — it just uses the same id it
    always would have)."""
    if not suffix:
        return app_name
    bindings = _load_bindings()
    suffix = bindings.get(app_name)
    if suffix is None:
        suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(4))
        bindings[app_name] = suffix
        _save_bindings(bindings)
    return f"{app_name}-{suffix}"


def promote(app_name: str, *, scope: Scope) -> DirectoryEntry:
    """Widen an existing app's scope (`sidepage promote --scope web`)
    *without* issuing a new identity — same app, same directory entry,
    wider visibility.

    Not implemented.
    """
    raise NotImplementedError


def list_entries(
    *, scope: Scope | None = None, mine_only: bool = False
) -> list[DirectoryEntry]:
    """Backs `sidepage ls [--scope <scope>] [--mine]`.

    Not implemented.
    """
    raise NotImplementedError


def get_status(app_name: str) -> DirectoryEntry:
    """Backs `sidepage status <app-name>` — reconciles the directory's
    asserted truth (declared scope/auth tier) against actual reachability,
    including tunnel connectivity (folds in what a standalone `tunnel
    status` command would have reported in v1).

    Not implemented.
    """
    raise NotImplementedError
