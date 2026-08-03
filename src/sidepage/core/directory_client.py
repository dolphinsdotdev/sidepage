"""Client for the sidepage directory service — backs `sidepage promote`,
`sidepage ls`, and `sidepage status` (spec v3 §3, §5, §10).

Naming model (§3): `<app-name>-<4-char-id>.<domain>.<tld>` — collision-proof
by construction, human prefix kept for legibility. The directory entry is
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
as the internal primitive `serve`'s directory registration calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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


def check_name(app_name: str) -> str:
    """Assign the `<app-name>-<4-char-id>` suffix for `app_name`. Called
    internally by `sidepage.core.process.serve` during directory
    registration — not exposed as its own CLI command in v3.

    Not implemented.
    """
    raise NotImplementedError


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
