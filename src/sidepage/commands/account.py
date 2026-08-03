"""`sidepage login` / `sidepage account status` / `sidepage account domain
set` — spec v3 §13, account & login.

Deliberately separate from per-app `--auth` (`sidepage serve --auth`) —
signing in to Sidepage and gating one app's visitors are different
concerns, and sharing a name would collide confusingly.

`account status` absorbs v1's `sidepage whoami` — see
`sidepage.core.account` for why that command was folded in rather than kept
standalone.
"""

from __future__ import annotations

from typing import Annotated

import typer

from sidepage.output import not_implemented

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
    domain: Annotated[str, typer.Argument(help="Domain to use as the persistent default.")],
) -> None:
    """Set the persistent default BYO domain (premium), used by `serve`
    when `--domain` isn't passed explicitly."""
    not_implemented(
        "sidepage account domain set", implemented_by="sidepage.core.account.set_default_domain"
    )
