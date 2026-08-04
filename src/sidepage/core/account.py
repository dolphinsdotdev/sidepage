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
    default_zone_token_name: str | None  # vault secret name, v4 §9 — not the token value
    default_tunnel_token_name: str | None  # vault secret name, v4 §9 — not the token value


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


def set_default_domain(domain: str, *, zone_token_name: str, tunnel_token_name: str) -> None:
    """Backs `sidepage account domain set <domain> --zone-token-name
    --tunnel-token-name` — premium: a persistent default BYO domain used by
    `serve` when `--domain` isn't passed explicitly.

    Stores `domain` plus the two vault secret *names* (v4 §9,
    `sidepage.core.secrets_vault`) — not the credential values themselves.
    `zone_token_name` names the Zone:DNS:Edit token, `tunnel_token_name`
    the per-tunnel token; both are resolved from the vault only when
    `sidepage.core.tunnel_manager.open_byo_tunnel` actually opens a tunnel,
    not eagerly here. This is v4's concrete answer to a gap v3 §6 left
    open ("stored locally," no mechanism specified) — never stored in the
    directory (see `sidepage.config.settings`).

    Not implemented.
    """
    raise NotImplementedError
