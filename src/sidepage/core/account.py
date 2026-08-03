"""Account & login — backs `sidepage login`, `sidepage account status`,
`sidepage account domain set <domain>` (spec v3 §13).

Deliberately separate from per-app `--auth` — different concern, avoids the
naming collision between "how do I sign in to Sidepage" and "how does this
one app gate its visitors."

Absorbs v1's `sidepage whoami`: `current_account` covers identity/session
the same way `whoami` did, folded here rather than kept as its own command
(v1's `sidepage name check <app-name>` has no replacement — name-collision
handling moved entirely into `sidepage.core.directory_client.check_name`,
called internally during `serve`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountStatus:
    id: str
    label: str | None
    plan: str  # e.g. "free", "premium"
    default_domain: str | None  # premium only, set via set_default_domain


def login() -> None:
    """Interactive login flow for `sidepage login`.

    Not implemented.
    """
    raise NotImplementedError


def current_account() -> AccountStatus:
    """Backs `sidepage account status` — the identity/session/plan
    currently active on this machine.

    Not implemented.
    """
    raise NotImplementedError


def set_default_domain(domain: str) -> None:
    """Backs `sidepage account domain set <domain>` — premium: a persistent
    default BYO domain used by `serve` when `--domain` isn't passed
    explicitly. Credentials for `domain` are stored locally, never in the
    directory (see `sidepage.core.tunnel_manager`, `sidepage.config.settings`).

    Not implemented.
    """
    raise NotImplementedError
