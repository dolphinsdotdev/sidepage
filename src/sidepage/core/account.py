"""Account & login — backs `sidepage login`, `sidepage account status`,
`sidepage account domain set <domain>` (spec v3 §13, v4 §9/§13 delta
below).

Deliberately separate from per-app `--auth` — different concern, avoids the
naming collision between "how do I sign in to Sidepage" and "how does this
one app gate its visitors."

Absorbs v1's `sidepage whoami`: `current_account` covers identity/session
the same way `whoami` did, folded here rather than kept as its own command
(v1's `sidepage name check <app-name>` has no replacement — name-collision
handling moved entirely into `sidepage.core.directory_client.check_name`,
called internally during `serve`).

`configure_domain`/`get_default_domain` are real — they're what makes
`sidepage serve --domain` work at all (see `sidepage.core.process`,
`sidepage.core.tunnel_manager.open_byo_tunnel`). `login`/`current_account`
(identity/session/plan) stay unimplemented: there's no Sidepage account
backend to authenticate against, and BYO-domain config doesn't need one —
it's just local state naming which vault entries to use.

**v4 delta — one API token, not two.** The original two-token design
(a user-supplied Zone:DNS:Edit token plus a separate per-tunnel token for
a tunnel created out-of-band) is gone. `configure_domain` now takes a
single scoped Cloudflare API token and does the provisioning itself via
`sidepage.core.tunnel_manager.provision_byo_domain` — see that module's
docstring for why (dynamic ingress on one shared tunnel, not one tunnel
per app). The only thing still typed by the user is the API token's vault
secret name; the tunnel run-token that provisioning returns is stored
automatically under `internal_tunnel_token_name(domain)`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sidepage.config.settings import account_config_file, ensure_dirs
from sidepage.core import secrets_vault, tunnel_manager
from sidepage.core.exceptions import TunnelProvisioningError, VaultError


@dataclass(frozen=True)
class AccountStatus:
    id: str
    label: str | None
    plan: str  # e.g. "free", "premium"
    default_domain: str | None  # premium only, set via configure_domain
    default_api_token_name: str | None  # vault secret name — not the token value
    default_tunnel_token_name: str | None  # vault secret name — not the token value


@dataclass(frozen=True)
class DomainConfig:
    domain: str
    zone_id: str
    account_id: str
    tunnel_id: str
    api_token_name: str  # vault secret name, user-supplied
    tunnel_token_name: str  # vault secret name, auto-assigned — see internal_tunnel_token_name


def internal_tunnel_token_name(domain: str) -> str:
    """Deterministic, reserved vault secret name used to store the
    Cloudflare tunnel run-token `configure_domain` obtains automatically
    at provisioning time. Never typed by the user — but always logged
    explicitly by `sidepage.commands.account.domain_set` on success (or
    named in the error, on failure), so it's discoverable via `sidepage
    secrets list` rather than being an invisible side effect. Reserved-
    prefixed so it reads unambiguously in that listing and doesn't collide
    with a name the user might independently choose."""
    return f"cf-tunnel-token::{domain}"


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


def configure_domain(domain: str, *, api_token_name: str) -> DomainConfig:
    """Backs `sidepage account domain set <domain> --api-token-name <name>`
    — the persistent default BYO domain used by `serve --domain`.

    `api_token_name` must already resolve in the vault (a single
    Cloudflare API token scoped to Account→Tunnel:Edit, Zone→DNS:Edit,
    Zone→Zone:Read — raises `sidepage.core.exceptions.SecretNotFoundError`
    if it doesn't). This function then provisions the domain for real via
    `sidepage.core.tunnel_manager.provision_byo_domain`: resolves the
    zone, creates one Cloudflare Tunnel meant to serve every app that will
    later run under `domain`, and stores the returned run-token in the
    vault under `internal_tunnel_token_name(domain)` — see that function's
    docstring for why that name is reserved/deterministic rather than
    user-chosen.

    Idempotent: re-running for a domain that's already configured returns
    the existing config unchanged rather than provisioning a second
    tunnel (there's no supported way yet to rotate `api_token_name` for an
    already-configured domain short of removing `account.json` by hand).

    Raises `sidepage.core.exceptions.TunnelProvisioningError` if the
    tunnel was created on Cloudflare but persisting its token then failed
    — the run-token is a one-time API response, so that's the one failure
    mode that can leave an orphaned Cloudflare resource behind; the error
    carries `tunnel_id` and the internal secret name so the caller can
    report both.
    """
    existing = get_default_domain()
    if existing is not None and existing.domain == domain:
        return existing

    api_token = secrets_vault.get_secret(api_token_name)  # fails loud if missing
    provisioned = tunnel_manager.provision_byo_domain(domain, api_token)

    internal_name = internal_tunnel_token_name(domain)
    try:
        secrets_vault.set_secret(internal_name, provisioned.tunnel_token)
    except VaultError as exc:
        raise TunnelProvisioningError(
            f"tunnel {provisioned.tunnel_id} was created on Cloudflare but storing its "
            f"token as {internal_name!r} failed: {exc}. The tunnel is now orphaned — its "
            "token can't be fetched again, so delete it via the Cloudflare dashboard (or "
            "API) and retry `sidepage account domain set`.",
            tunnel_id=provisioned.tunnel_id,
            internal_secret_name=internal_name,
        ) from exc

    config = DomainConfig(
        domain=domain,
        zone_id=provisioned.zone_id,
        account_id=provisioned.account_id,
        tunnel_id=provisioned.tunnel_id,
        api_token_name=api_token_name,
        tunnel_token_name=internal_name,
    )
    ensure_dirs()
    account_config_file().write_text(json.dumps(config.__dict__))
    return config


def get_default_domain() -> DomainConfig | None:
    """Read back the persisted BYO domain config, if any. Used internally
    by `sidepage.core.process.serve` to resolve `--domain` into vault
    secret names — not exposed as its own CLI command."""
    path = account_config_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return DomainConfig(**data)
    except (json.JSONDecodeError, OSError, TypeError):
        return None
