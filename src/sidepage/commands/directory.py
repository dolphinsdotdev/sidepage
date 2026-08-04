"""`sidepage ls` / `sidepage status` — directory queries.

Not a numbered section in the v3 spec (v1 had a "Directory queries" §10;
v3 goes straight from §9 local reverse proxy to §10 inspection with no
`ls`/`status` mention). Kept as-is since the directory model itself is
still central to v3 (§3, §5) — treated as not re-stated, not cut.

Real, but against `sidepage.core.registry` (this machine's running apps),
not a cloud directory — there isn't one to talk to (see
`sidepage.core.directory_client`, still unimplemented). `--scope`/`--mine`
have no real meaning against a single-machine registry; `ls` notes that
rather than pretending to filter. `status` does a live reachability check
against the registered local URL — the "reconciliation" the spec describes,
just against this machine's own record instead of a cloud directory's.
"""

from __future__ import annotations

from typing import Annotated

import httpx
import typer

from sidepage.core import registry
from sidepage.core.directory_client import Scope
from sidepage.output import error, info, stdout


def ls(
    scope: Annotated[
        Scope | None, typer.Option("--scope", help="Filter to one scope.")
    ] = None,
    mine: Annotated[
        bool, typer.Option("--mine", help="Limit to the current identity's own apps.")
    ] = False,
) -> None:
    """List apps running on this machine."""
    if scope is not None:
        info("--scope filtering isn't implemented — no cloud directory to filter against yet")
    apps = registry.list_running()
    if not apps:
        stdout.print("[dim]no apps running[/dim]")
        return
    for app in apps:
        line = f"{app.name}  [dim]{app.target_kind}[/dim]  {app.url}"
        if app.tunnel_url:
            line += f"  [cyan]{app.tunnel_url}[/cyan]"
        stdout.print(line)


def status(
    app_name: Annotated[str, typer.Argument(help="App to check.")],
) -> None:
    """Show reachability and connection info for a running app, reconciling
    the local registry's record against a live check."""
    app = registry.get(app_name)
    if app is None:
        error(f"no running app named {app_name!r}")
        raise typer.Exit(1)

    try:
        httpx.get(app.url, timeout=2.0)
        reachable = True
    except httpx.TransportError:
        reachable = False

    stdout.print(f"name:      {app.name}")
    stdout.print(f"target:    {app.target} ({app.target_kind})")
    stdout.print(f"pid:       {app.pid}")
    stdout.print(f"url:       {app.url}")
    if app.tunnel_url:
        stdout.print(f"public:    {app.tunnel_url}")
    stdout.print(f"reachable: {'[green]yes[/green]' if reachable else '[red]no[/red]'}")
