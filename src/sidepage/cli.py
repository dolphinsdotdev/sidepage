"""Root Typer app — assembles every `sidepage.commands` module into the
`sidepage` command tree described in the spec (v3). This module owns wiring
only; see each `sidepage.commands.*` module for the spec section it
implements.

Command tree:
  sidepage new <name>                                     §1
  sidepage serve <target>                                  §2
  sidepage stop <app-name>                                 §2
  sidepage promote <app-name>                              §5
  sidepage login                                           §13
  sidepage account status                                  §13
  sidepage account domain set <domain>                     §13 (v4: + --api-token-name)
  sidepage usage <app-name>                                §7
  sidepage secrets set|list|remove                         v4 §9
  sidepage inspect [<app-name-or-url>]                     §10
  sidepage ls                                              (no v3 section — see note below)
  sidepage status <app-name>                               (no v3 section — see note below)
  sidepage app register|list|show|unregister               (registry spec v2, no v3 section)
  sidepage serve <app-name>                                (registry spec v2 — serve's <target>
                                                             also accepts a registered app name)

Note: v3 has no "Directory queries" section (v1's §10) — it doesn't
mention `ls`/`status` at all, jumping from §9 (local reverse proxy) to §10
(inspection) to §11 (static). Kept here on the same reasoning as
guardrails: the directory model itself is still very much alive in v3
(§3, §5), so this reads as "not re-stated" rather than "cut" — but flagged
since it wasn't a locked-in decision the way the identity/keys/tunnel drops
were.

Dropped from v1's tree, not carried forward: `whoami` / `name check`
(folded into `account status`, see sidepage.core.account), `keys
create|revoke|list` (replaced by per-serve `--token`, see
sidepage.core.token_runtime), `tunnel login|token set|status|revoke`
(replaced by `login` / `account domain set`, tunnel mechanics moved into
sidepage.core.tunnel_manager without a dedicated command group).

(§8, token handling, has no standalone command — it's `serve --token` /
`SIDEPAGE_TOKEN`. Guardrails, still parked though absent from v3, remain
`serve --guardrail`; see sidepage.commands.serve and sidepage.core.guardrail.)

v4 adds `sidepage secrets set|list|remove` — the secrets vault, cited by
the user's own summary as "v4 §9." v3 §9 was "local reverse proxy"; the
migration to v4 was done from a 4-point delta summary, not the full v4
document, so whether v4 actually renumbered the reverse proxy section is
unconfirmed — see `docs/OPEN_QUESTIONS.md` for the flagged collision.
`sidepage.core.secrets_vault` is genuinely new, not a replacement for
`sidepage.core.reverse_proxy`, regardless of how the numbering shakes out.
"""

from __future__ import annotations

import typer

from sidepage import __version__
from sidepage.commands import (
    account,
    app_registry,
    directory,
    inspect,
    new,
    scope,
    secrets,
    serve,
    usage,
)

app = typer.Typer(
    name="sidepage",
    help=(
        "Local-first hosting, tunneling, and directory for small apps and MCP servers.\n\n"
        "Scaffold with `sidepage new`, serve it with `sidepage serve`, and share it — "
        "ephemeral by default, torn down when you Ctrl+C."
    ),
    no_args_is_help=True,
    add_completion=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sidepage {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the sidepage CLI version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """sidepage — local-first hosting, tunneling, and directory."""


# §1 — targets / scaffolding
app.command("new")(new.new)

# §2 — serving
app.command("serve")(serve.serve)
app.command("stop")(serve.stop)

# §5 — discovery & scope
app.command("promote")(scope.promote)

# §7 — metering
app.command("usage")(usage.usage)

# v4 §9 — secrets vault
app.add_typer(secrets.secrets_app)

# §10 — inspection & directory queries
app.command("inspect")(inspect.inspect)
app.command("ls")(directory.ls)
app.command("status")(directory.status)

# §13 — account & login
app.command("login")(account.login)
app.add_typer(account.account_app)

# Local app registry (registry spec v2) — sidepage app register|list|show|unregister
app.add_typer(app_registry.app_app)


if __name__ == "__main__":
    app()
