"""`sidepage login` / `sidepage account status` / `sidepage account domain
set` — spec v3 §13, account & login.

Deliberately separate from per-app `--auth` (`sidepage serve --auth`) —
signing in to Sidepage and gating one app's visitors are different
concerns, and sharing a name would collide confusingly.

`account status` absorbs v1's `sidepage whoami` — see
`sidepage.core.account` for why that command was folded in rather than kept
standalone. Still unimplemented: there's no Sidepage account backend to
authenticate against.

`domain set` is real. v4 delta: a **single** required flag,
`--api-token-name` — a vault secret name (v4 §9,
`sidepage.core.secrets_vault`), not a raw credential value. The earlier
two-token design (`--zone-token-name` / `--tunnel-token-name`, requiring a
tunnel already created out-of-band) is gone — this command now creates the
tunnel itself via `sidepage.core.account.configure_domain`, and stores its
run-token in the vault automatically. That storage always happens, but is
never silent: success logs the internal vault name it landed under, and
failure names it too (see `configure_domain`'s
`TunnelProvisioningError` — the run-token is a one-time Cloudflare API
response, so a failure to persist it leaves an orphaned tunnel behind that
the user needs to know the ID of).
"""

from __future__ import annotations

from typing import Annotated

import typer

from sidepage.core import account as account_core
from sidepage.core.exceptions import SecretNotFoundError, TunnelProvisioningError
from sidepage.output import error, info, not_implemented, success

account_app = typer.Typer(
    name="account",
    help="Account status and BYO-domain configuration.",
    no_args_is_help=True,
)

domain_app = typer.Typer(
    name="domain",
    help="Manage the persistent default BYO domain (premium).",
    no_args_is_help=True,
)
account_app.add_typer(domain_app)


def login() -> None:
    """Interactive login flow."""
    not_implemented("sidepage login", implemented_by="sidepage.core.account.login")


@account_app.command("status")
def status() -> None:
    """Show the identity/session/plan currently active on this machine."""
    not_implemented(
        "sidepage account status", implemented_by="sidepage.core.account.current_account"
    )


@domain_app.command("set")
def domain_set(
    domain: Annotated[str, typer.Argument(help="Zone apex to use as the persistent default.")],
    api_token_name: Annotated[
        str,
        typer.Option(
            "--api-token-name",
            help="Vault secret name (see `sidepage secrets set`) holding a Cloudflare API "
            "token scoped to Account -> Cloudflare Tunnel:Edit, Zone -> DNS:Edit, "
            "Zone -> Zone:Read.",
        ),
    ],
) -> None:
    """Provision (or reuse) the persistent default BYO domain (premium):
    creates one Cloudflare Tunnel meant to serve every app later run under
    `domain` via `serve --domain`, and stores that tunnel's run-token in
    the vault automatically — never typed by the user, but always
    reported below by its vault name. `domain` should be the zone apex
    (e.g. `example.com`), not a pre-built subdomain — served apps get
    `<app-name>-<id>.<domain>`. Re-running for an already-configured
    domain is a no-op, not a re-provision."""
    try:
        config = account_core.configure_domain(domain, api_token_name=api_token_name)
    except SecretNotFoundError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    except TunnelProvisioningError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    success(f"default BYO domain set: {domain} (tunnel {config.tunnel_id})")
    info(
        f"tunnel run-token stored in vault as {config.tunnel_token_name!r} "
        "(see `sidepage secrets list`)"
    )
