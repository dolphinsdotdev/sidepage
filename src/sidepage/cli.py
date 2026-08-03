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
  sidepage account domain set <domain>                     §13
  sidepage usage <app-name>                                §7
  sidepage inspect [<app-name-or-url>]                     §10
  sidepage ls                                              (no v3 section — see note below)
  sidepage status <app-name>                               (no v3 section — see note below)

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
"""

from __future__ import annotations

import typer

from sidepage import __version__
from sidepage.commands import account, directory, inspect, new, scope, serve, usage

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

# §10 — inspection & directory queries
app.command("inspect")(inspect.inspect)
app.command("ls")(directory.ls)
app.command("status")(directory.status)

# §13 — account & login
app.command("login")(account.login)
app.add_typer(account.account_app)


if __name__ == "__main__":
    app()
