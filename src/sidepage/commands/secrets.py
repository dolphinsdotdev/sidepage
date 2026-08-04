"""`sidepage secrets set|list|remove` — spec v4 §9, secrets vault.

The major new piece in v4: v3 had no concept of standing, persistent,
user-supplied secrets — only the ephemeral auth-token runtime file (§8).
See `sidepage.core.secrets_vault` for the lifecycle/direction distinction
that keeps this genuinely separate from that file, not just a rename, and
for why this build's backend is encrypted-file only (OS keychain deferred).

`set` prompts for the value with hidden input rather than taking it as a
CLI argument or `--value` flag — the vault holds standing credentials, so
it gets a stricter shell-history/`ps aux` stance than `serve --token`
already takes for the ephemeral, single-invocation auth token.
"""

from __future__ import annotations

from typing import Annotated

import typer

from sidepage.core import secrets_vault
from sidepage.output import stdout, success

secrets_app = typer.Typer(
    name="secrets",
    help="Manage persistent, named secrets injected into apps via `serve --env`.",
    no_args_is_help=True,
)


@secrets_app.command("set")
def set_(
    name: Annotated[
        str, typer.Argument(help="Secret name, referenced later via `serve --env <name>`.")
    ],
    value: Annotated[
        str,
        typer.Option(
            prompt=True,
            hide_input=True,
            confirmation_prompt=True,
            help="Secret value — always prompted, never a CLI argument.",
        ),
    ],
) -> None:
    """Store a secret in the vault (encrypted local file)."""
    secrets_vault.set_secret(name, value)
    success(f"stored secret {name!r}")


@secrets_app.command("list")
def list_() -> None:
    """List stored secret names — values are never displayed."""
    names = secrets_vault.list_secrets()
    if not names:
        stdout.print("[dim]no secrets stored[/dim]")
        return
    for name in names:
        stdout.print(name)


@secrets_app.command("remove")
def remove(
    name: Annotated[str, typer.Argument(help="Secret name to delete.")],
) -> None:
    """Delete a stored secret."""
    secrets_vault.remove_secret(name)
    success(f"removed secret {name!r}")
